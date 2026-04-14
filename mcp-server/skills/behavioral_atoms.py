"""Skill: behavioral_atoms — ATOM-first behavioral detection MCP tools.

Exposes two tools on `ambient-skills-companion`:

- get_behavioral_context(patient_id)
    Multi-domain behavioral context. Returns all open gaps, every
    screening on file, all triggered critical items, and the full
    (bounded) atom history — consumed by MIRA / SYNTHESIS / the cards
    tool to render per-role output.

- run_behavioral_gap_check(patient_id)
    Idempotent domain-driven gap detector — safe to call on every note
    ingest. Returns a list of newly detected domain gaps.
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
from skills.screening_registry import DOMAINS

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
    return str(value)


def _row_to_dict(row) -> dict:
    if row is None:
        return {}
    return {k: _jsonable(v) for k, v in dict(row).items()}


async def get_behavioral_context(patient_id: str) -> str:
    """Return full multi-domain behavioral context for a patient.

    Response shape:
        {
          "mode": "primary_evidence" | "contextual",
          "open_gaps": [ {gap row} ],
          "all_screenings": [ {screening row} ],
          "domain_summary": {
              domain_key: {
                  latest_screening: {...} | null,
                  is_overdue: bool,
                  is_positive: bool,
                  has_open_gap: bool,
                  triggered_critical: [...]
              }, ...
          },
          "critical_flags": [...],
          "atoms": [...],
          "pressure": {...}
        }
    """
    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            phenotype = await conn.fetchrow(
                "SELECT * FROM behavioral_phenotypes WHERE patient_id = $1::uuid",
                patient_id,
            )
            open_gaps = await conn.fetch(
                """SELECT * FROM behavioral_screening_gaps
                    WHERE patient_id = $1::uuid AND status = 'open'
                    ORDER BY detected_at DESC""",
                patient_id,
            )
            screenings = await conn.fetch(
                """SELECT * FROM behavioral_screenings
                    WHERE patient_id = $1::uuid
                    ORDER BY observation_date DESC
                    LIMIT 50""",
                patient_id,
            )
            atoms = await conn.fetch(
                """SELECT clinical_date, note_section, signal_type,
                          signal_value, confidence
                     FROM behavioral_signal_atoms
                    WHERE patient_id = $1::uuid AND assertion = 'present'
                    ORDER BY clinical_date DESC
                    LIMIT 100""",
                patient_id,
            )
            pressure = await conn.fetchrow(
                "SELECT * FROM atom_pressure_scores WHERE patient_id = $1::uuid",
                patient_id,
            )

            # Build domain summary keyed by every domain the registry knows.
            domain_summary: dict = {}
            for d in DOMAINS:
                latest_for_domain = next(
                    (s for s in screenings if s["domain"] == d), None,
                )
                has_open_gap = any(
                    d in (g["triggered_domains"] or []) for g in open_gaps
                )
                triggered: list = []
                if latest_for_domain and latest_for_domain["triggered_critical"]:
                    raw = latest_for_domain["triggered_critical"]
                    if isinstance(raw, str):
                        try:
                            raw = json.loads(raw)
                        except json.JSONDecodeError:
                            raw = []
                    triggered = raw
                domain_summary[d] = {
                    "latest_screening": _row_to_dict(latest_for_domain)
                        if latest_for_domain else None,
                    "has_open_gap": has_open_gap,
                    "is_positive": bool(latest_for_domain and
                                        latest_for_domain["is_positive"]),
                    "triggered_critical": [_jsonable(t) for t in triggered],
                }

            # Collect ALL critical flags across screenings.
            critical_flags: list = []
            for s in screenings:
                raw = s["triggered_critical"]
                if not raw:
                    continue
                if isinstance(raw, str):
                    try:
                        raw = json.loads(raw)
                    except json.JSONDecodeError:
                        continue
                for item in raw:
                    critical_flags.append({
                        **_jsonable(item),
                        "observation_date": _jsonable(s["observation_date"]),
                    })

            mode = (phenotype["evidence_mode"] if phenotype
                    else ("primary_evidence" if open_gaps else "contextual"))

            return json.dumps({
                "status": "ok",
                "patient_id": patient_id,
                "mode": mode,
                "phenotype": _row_to_dict(phenotype) if phenotype else None,
                "open_gaps": [_row_to_dict(g) for g in open_gaps],
                "all_screenings": [_row_to_dict(s) for s in screenings],
                "domain_summary": domain_summary,
                "critical_flags": critical_flags,
                "atoms": [_row_to_dict(a) for a in atoms],
                "pressure": _row_to_dict(pressure) if pressure else None,
            })
    except Exception as e:
        logger.warning("get_behavioral_context failed: %s", type(e).__name__)
        return json.dumps({"status": "error", "detail": type(e).__name__})


async def run_behavioral_gap_check(patient_id: str) -> str:
    """Run the domain-driven behavioral screening gap detector.

    Idempotent — safe to call on every note ingest. Returns one of:
      - gaps_detected: list of newly created gaps (each with domain key)
      - no_gap: no pressure/atom conditions met

    Args:
        patient_id: UUID of the patient.
    """
    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            gaps = await run_gap_detector_for_patient(conn, patient_id)
            if gaps:
                return json.dumps({
                    "status": "gaps_detected",
                    "patient_id": patient_id,
                    "count": len(gaps),
                    "gaps": gaps,
                })
            return json.dumps({"status": "no_gap", "patient_id": patient_id})
    except Exception as e:
        logger.warning("run_behavioral_gap_check failed: %s", type(e).__name__)
        return json.dumps({"status": "error", "detail": type(e).__name__})


def register(mcp: FastMCP) -> None:
    mcp.tool(get_behavioral_context)
    mcp.tool(run_behavioral_gap_check)
