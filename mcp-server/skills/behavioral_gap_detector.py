"""Behavioral screening gap detector — v2 (domain-driven).

A gap now fires per-domain, not per-instrument. For each domain implicated
by the patient's atom signal_types, we check whether ANY instrument in
that domain has been administered within the domain-specific lookback
window (DOMAIN_LOOKBACK_DAYS). Domains with atom pressure but no recent
screening become open gaps; multiple simultaneous domain gaps per patient
are supported.

Returns `list[dict]` (one entry per newly detected domain gap), or []
when no gap conditions are met.

This module reads `behavioral_screenings` (migration 011) — the legacy
`phq9_observations` table is gone.
"""
from __future__ import annotations

import logging
import sys
import uuid
from datetime import date, timedelta
from typing import Optional

from skills.screening_registry import (
    DOMAIN_LOOKBACK_DAYS,
    DOMAINS,
    SCREENING_REGISTRY,
    instruments_for_domain,
    suggest_domains_from_atoms,
    suggest_instruments_from_atoms,
)

logging.basicConfig(level=logging.INFO, stream=sys.stderr)
logger = logging.getLogger(__name__)

# Thresholds (unchanged from v1).
PRESSURE_THRESHOLD = 2.5
MIN_ATOM_COUNT = 3


def compute_temporal_confidence(latest_atom_date: Optional[date]) -> str:
    if not latest_atom_date:
        return "very_low"
    age_days = (date.today() - latest_atom_date).days
    if age_days < 365:
        return "high"
    if age_days < 1095:
        return "moderate"
    if age_days < 2555:
        return "low"
    return "very_low"


async def _upsert_phenotype(
    conn,
    patient_id: str,
    evidence_mode: str,
    screening_gap_id: Optional[uuid.UUID] = None,
    atom_pressure_score: Optional[float] = None,
    temporal_confidence: Optional[str] = None,
    last_formal_screening: Optional[date] = None,
) -> None:
    """Upsert the behavioral_phenotypes row for this patient."""
    await conn.execute(
        """
        INSERT INTO behavioral_phenotypes
            (patient_id, evidence_mode, screening_gap_id,
             atom_pressure_score, temporal_confidence,
             last_formal_screening, updated_at)
        VALUES ($1::uuid, $2, $3, $4, $5, $6, NOW())
        ON CONFLICT (patient_id) DO UPDATE SET
            evidence_mode = EXCLUDED.evidence_mode,
            screening_gap_id = EXCLUDED.screening_gap_id,
            atom_pressure_score = COALESCE(EXCLUDED.atom_pressure_score,
                                           behavioral_phenotypes.atom_pressure_score),
            temporal_confidence = COALESCE(EXCLUDED.temporal_confidence,
                                           behavioral_phenotypes.temporal_confidence),
            last_formal_screening = COALESCE(EXCLUDED.last_formal_screening,
                                             behavioral_phenotypes.last_formal_screening),
            updated_at = NOW()
        """,
        patient_id, evidence_mode, screening_gap_id,
        atom_pressure_score, temporal_confidence, last_formal_screening,
    )


async def _latest_screening_in_domain(
    conn, patient_id: str, domain: str,
) -> Optional[dict]:
    """Return the most recent behavioral_screenings row for this domain,
    or None.
    """
    row = await conn.fetchrow(
        """SELECT id, instrument_key, instrument_name, observation_date,
                  total_score, severity_band, is_positive, triggered_critical
             FROM behavioral_screenings
            WHERE patient_id = $1::uuid
              AND domain = $2
            ORDER BY observation_date DESC
            LIMIT 1""",
        patient_id, domain,
    )
    return dict(row) if row else None


async def run_gap_detector_for_patient(
    conn, patient_id: str,
) -> list[dict]:
    """Domain-aware gap detector. Returns a list of newly opened gaps.

    For each domain implicated by the patient's atoms:
      - If domain already has an open gap → skip.
      - If domain has a screening within its lookback window → no gap.
      - Otherwise → open a gap for that domain.

    Pressure threshold + atom-count threshold still gate ALL gap creation
    (we only open gaps for patients with meaningful signal density).
    """
    pressure_row = await conn.fetchrow(
        "SELECT * FROM atom_pressure_scores WHERE patient_id = $1::uuid",
        patient_id,
    )
    if not pressure_row:
        return []

    pressure_score = float(pressure_row["pressure_score"] or 0)
    atom_count = int(pressure_row["present_atom_count"] or 0)

    if pressure_score < PRESSURE_THRESHOLD or atom_count < MIN_ATOM_COUNT:
        return []

    # Fetch all present/historical atoms for this patient to derive
    # implicated domains + evidence.
    atom_rows = await conn.fetch(
        """SELECT id, clinical_date, signal_type
             FROM behavioral_signal_atoms
            WHERE patient_id = $1::uuid AND assertion = 'present'
            ORDER BY clinical_date ASC""",
        patient_id,
    )
    if not atom_rows:
        return []

    atom_ids = [r["id"] for r in atom_rows]
    signal_types = [r["signal_type"] for r in atom_rows]
    dates = [r["clinical_date"] for r in atom_rows]
    latest_atom = max(dates) if dates else None
    earliest_atom = min(dates) if dates else None
    temporal_confidence = compute_temporal_confidence(latest_atom)

    implicated = suggest_domains_from_atoms(signal_types)
    if not implicated:
        return []

    # Existing open gaps — skip domains already tracked.
    open_domain_rows = await conn.fetch(
        """SELECT triggered_domains
             FROM behavioral_screening_gaps
            WHERE patient_id = $1::uuid AND status = 'open'""",
        patient_id,
    )
    already_open_domains: set[str] = set()
    for row in open_domain_rows:
        for d in (row["triggered_domains"] or []):
            already_open_domains.add(d)

    created: list[dict] = []
    today = date.today()

    for domain in implicated:
        if domain in already_open_domains:
            continue
        lookback = DOMAIN_LOOKBACK_DAYS.get(domain, 365)
        cutoff = today - timedelta(days=lookback)

        latest_screen = await _latest_screening_in_domain(conn, patient_id, domain)
        if latest_screen and latest_screen["observation_date"] >= cutoff:
            # Recent screening covers this domain; no gap.
            continue

        gap_type = "no_screening" if latest_screen is None else "stale_screening"
        recommended = [
            inst.display_name for inst in instruments_for_domain(domain)
        ][:3]
        # Refine ordering by atom-implication ranking where possible.
        ranked = suggest_instruments_from_atoms(signal_types)
        recommended = [r for r in ranked if r in recommended] or recommended

        gap_id = uuid.uuid4()
        await conn.execute(
            """
            INSERT INTO behavioral_screening_gaps (
                id, patient_id, gap_type, atom_count,
                atom_date_range, atom_ids, pressure_score,
                last_screening_date, last_screening_score,
                output_mode, temporal_confidence,
                recommended_instruments, triggered_domains
            ) VALUES (
                $1::uuid, $2::uuid, $3, $4,
                daterange($5, $6, '[]'), $7, $8,
                $9, $10,
                'primary_evidence', $11,
                $12, $13
            )
            """,
            gap_id, patient_id, gap_type, atom_count,
            earliest_atom, latest_atom, atom_ids, pressure_score,
            latest_screen["observation_date"] if latest_screen else None,
            latest_screen["total_score"] if latest_screen else None,
            temporal_confidence, recommended, [domain],
        )
        already_open_domains.add(domain)

        created.append({
            "gap_id": str(gap_id),
            "patient_id": patient_id,
            "domain": domain,
            "gap_type": gap_type,
            "atom_count": atom_count,
            "pressure_score": pressure_score,
            "temporal_confidence": temporal_confidence,
            "recommended_instruments": recommended,
        })

    if created:
        # Associate atoms with the FIRST new gap (primary anchor) — mirrors
        # v1 behavior. Multi-gap linkage uses triggered_domains for fanout.
        first_gap_id = uuid.UUID(created[0]["gap_id"])
        await conn.execute(
            "UPDATE behavioral_signal_atoms "
            "SET contributed_to_gap_id = $1 "
            "WHERE patient_id = $2::uuid AND assertion = 'present' "
            "AND contributed_to_gap_id IS NULL",
            first_gap_id, patient_id,
        )

        await _upsert_phenotype(
            conn,
            patient_id=patient_id,
            evidence_mode="primary_evidence",
            screening_gap_id=first_gap_id,
            atom_pressure_score=pressure_score,
            temporal_confidence=temporal_confidence,
        )

    return created


async def resolve_gap_on_new_screening(
    conn,
    patient_id: str,
    new_screening_id: str,
    instrument_key: str,
    domain: str,
    screening_date: date,
) -> int:
    """Close gaps in `domain` when a new screening is ingested.

    Returns the number of gaps resolved. If no domain gaps remain open
    after resolution, the phenotype flips back to 'contextual' (Mode A).
    """
    try:
        screening_uuid = uuid.UUID(str(new_screening_id))
    except (TypeError, ValueError):
        screening_uuid = None

    # Resolve any open gap that contains this domain in triggered_domains.
    result = await conn.execute(
        """
        UPDATE behavioral_screening_gaps
        SET status = 'resolved',
            resolved_by_screening_id = $1,
            resolved_at = NOW(),
            output_mode = 'contextual',
            updated_at = NOW()
        WHERE patient_id = $2::uuid
          AND status = 'open'
          AND $3 = ANY(triggered_domains)
        """,
        screening_uuid, patient_id, domain,
    )
    # asyncpg execute returns "UPDATE N"
    try:
        n_resolved = int(str(result).split()[-1])
    except Exception:
        n_resolved = 0

    # Does any open gap remain?
    still_open = await conn.fetchval(
        "SELECT EXISTS(SELECT 1 FROM behavioral_screening_gaps "
        "WHERE patient_id = $1::uuid AND status = 'open')",
        patient_id,
    )

    await _upsert_phenotype(
        conn,
        patient_id=patient_id,
        evidence_mode="primary_evidence" if still_open else "contextual",
        screening_gap_id=None if not still_open else None,
        last_formal_screening=screening_date,
    )
    return n_resolved


async def run_batch_gap_detector(conn) -> list[dict]:
    """Nightly batch: scan all patients above pressure threshold."""
    candidates = await conn.fetch(
        """
        SELECT aps.patient_id
        FROM atom_pressure_scores aps
        WHERE aps.pressure_score >= $1
          AND aps.present_atom_count >= $2
        """,
        PRESSURE_THRESHOLD, MIN_ATOM_COUNT,
    )
    detected: list[dict] = []
    for row in candidates:
        gaps = await run_gap_detector_for_patient(conn, str(row["patient_id"]))
        detected.extend(gaps)
    return detected


def register(mcp):  # pragma: no cover - no-op to silence skill loader warning
    return
