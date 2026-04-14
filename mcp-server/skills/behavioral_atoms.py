"""Skill: behavioral_atoms — ATOM-first behavioral detection MCP tools.

Exposes two tools on `ambient-skills-companion`:

- get_behavioral_context(patient_id)
    Returns mode-aware behavioral context (Mode A = contextual, Mode B =
    primary_evidence) for SYNTHESIS / MIRA to render per-role output.

- run_behavioral_gap_check(patient_id)
    Idempotent gap detector — safe to call on every note ingest.

Reads behavioral_phenotypes.evidence_mode to route the response shape.
"""
from __future__ import annotations

import json
import logging
import sys
from datetime import date, datetime
from typing import Any

from fastmcp import FastMCP

from db.connection import get_pool

from skills.behavioral_gap_detector import run_gap_detector_for_patient

logging.basicConfig(level=logging.INFO, stream=sys.stderr)
logger = logging.getLogger(__name__)


def _jsonable(value: Any) -> Any:
    """Recursively coerce DB values (UUID, date, datetime, Range) to JSON types."""
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    # asyncpg Range, UUID, and anything else → string
    return str(value)


def _row_to_dict(row) -> dict:
    if row is None:
        return {}
    return {k: _jsonable(v) for k, v in dict(row).items()}


async def get_behavioral_context(patient_id: str) -> str:
    """Get mode-aware behavioral context for a patient.

    Reads behavioral_phenotypes.evidence_mode and returns either:
      - Mode B (primary_evidence): atoms + screening-gap as headline
      - Mode A (contextual): PHQ-9 score + atoms as historical context

    Args:
        patient_id: UUID of the patient.

    Returns:
        JSON string with mode, evidence, recommendations, and framing hints.
    """
    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            phenotype = await conn.fetchrow(
                "SELECT * FROM behavioral_phenotypes WHERE patient_id = $1::uuid",
                patient_id,
            )
            if not phenotype:
                return json.dumps({
                    "status": "no_phenotype",
                    "patient_id": patient_id,
                    "mode": "contextual",
                })

            mode = phenotype["evidence_mode"] or "contextual"

            if mode == "primary_evidence":
                gap = await conn.fetchrow(
                    "SELECT * FROM behavioral_screening_gaps "
                    "WHERE patient_id = $1::uuid AND status = 'open' "
                    "ORDER BY detected_at DESC LIMIT 1",
                    patient_id,
                )
                if gap:
                    atom_rows = await conn.fetch(
                        "SELECT clinical_date, note_section, signal_type, "
                        "signal_value, confidence "
                        "FROM behavioral_signal_atoms "
                        "WHERE id = ANY($1::uuid[]) AND assertion = 'present' "
                        "ORDER BY clinical_date ASC",
                        gap["atom_ids"],
                    )
                    drange = gap["atom_date_range"]
                    earliest = drange.lower if drange else None
                    latest = drange.upper if drange else None
                    return json.dumps({
                        "mode": "primary_evidence",
                        "status": "screening_gap_open",
                        "gap_type": gap["gap_type"],
                        "gap_id": str(gap["id"]),
                        "temporal_confidence": gap["temporal_confidence"],
                        "atom_count": gap["atom_count"],
                        "atom_date_range": {
                            "earliest": _jsonable(earliest),
                            "latest": _jsonable(latest),
                        },
                        "atoms": [_row_to_dict(a) for a in atom_rows],
                        "recommended_instruments": list(
                            gap["recommended_instruments"] or []
                        ),
                        "headline": "No formal behavioral health screening on file",
                        "framing": "clinical_gap",
                        "patient_surface_allowed":
                            gap["temporal_confidence"] not in ("low", "very_low"),
                    })
                # Fall through to Mode A if gap was resolved between reads.
                mode = "contextual"

            # Mode A: structured score exists, atoms as context.
            latest_phq = await conn.fetchrow(
                "SELECT * FROM phq9_observations "
                "WHERE patient_id = $1::uuid "
                "ORDER BY observation_date DESC LIMIT 1",
                patient_id,
            )
            historical_atoms = await conn.fetch(
                "SELECT clinical_date, note_section, signal_type, "
                "signal_value, confidence "
                "FROM behavioral_signal_atoms "
                "WHERE patient_id = $1::uuid AND assertion = 'present' "
                "ORDER BY clinical_date ASC",
                patient_id,
            )

            phq_dict = _row_to_dict(latest_phq) if latest_phq else None
            item_9 = latest_phq["item_9_score"] if latest_phq else None
            phenotype_d = _row_to_dict(phenotype)
            return json.dumps({
                "mode": "contextual",
                "status": "screening_exists" if latest_phq else "no_screening",
                "latest_phq9": phq_dict,
                "item_9_score": item_9,
                "item_9_flag": (item_9 or 0) >= 1 if latest_phq else False,
                "trajectory": phenotype_d.get("trajectory_status"),
                "temporal_confidence": phenotype_d.get("temporal_confidence"),
                "historical_atoms": [_row_to_dict(a) for a in historical_atoms],
                "atom_count": len(historical_atoms),
                "framing": "score_with_history",
                "headline": (f"PHQ-9 = {latest_phq['total_score']}"
                             if latest_phq else "No PHQ-9 on file"),
            })
    except Exception as e:
        logger.warning("get_behavioral_context failed: %s", type(e).__name__)
        return json.dumps({"status": "error", "detail": type(e).__name__})


async def run_behavioral_gap_check(patient_id: str) -> str:
    """Run the behavioral screening gap detector for one patient.

    Idempotent — safe to call on every note ingest. Returns one of:
      - gap_detected: newly created gap
      - gap_already_open: a gap is already tracked for this patient
      - no_gap: no pressure/atom conditions met

    Args:
        patient_id: UUID of the patient.
    """
    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            gap = await run_gap_detector_for_patient(conn, patient_id)
            if gap:
                return json.dumps({"status": "gap_detected", **gap})
            existing = await conn.fetchrow(
                "SELECT id, gap_type, detected_at "
                "FROM behavioral_screening_gaps "
                "WHERE patient_id = $1::uuid AND status = 'open'",
                patient_id,
            )
            if existing:
                return json.dumps({
                    "status": "gap_already_open",
                    "gap_id": str(existing["id"]),
                    "gap_type": existing["gap_type"],
                    "detected_at": _jsonable(existing["detected_at"]),
                })
            return json.dumps({"status": "no_gap", "patient_id": patient_id})
    except Exception as e:
        logger.warning("run_behavioral_gap_check failed: %s", type(e).__name__)
        return json.dumps({"status": "error", "detail": type(e).__name__})


def register(mcp: FastMCP) -> None:
    mcp.tool(get_behavioral_context)
    mcp.tool(run_behavioral_gap_check)
