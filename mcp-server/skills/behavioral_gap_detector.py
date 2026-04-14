"""Behavioral screening gap detector.

Detects patients for whom atom pressure exceeds threshold without a
corresponding formal screening (PHQ-9) on file. Inserts rows into
`behavioral_screening_gaps` and upserts the corresponding
`behavioral_phenotypes` row to surface Mode B (primary_evidence) output.
"""
from __future__ import annotations

import logging
import sys
import uuid
from collections import Counter
from datetime import date, timedelta
from typing import Optional

logging.basicConfig(level=logging.INFO, stream=sys.stderr)
logger = logging.getLogger(__name__)

# Configurable thresholds — tuned in calibration phase.
PRESSURE_THRESHOLD = 2.5
MIN_ATOM_COUNT = 3
SCREENING_LOOKBACK_MONTHS = 12

INSTRUMENT_SUGGESTION_MAP = {
    "psychomotor_restlessness":  ["ASRS-5", "GAD-7"],
    "attention_switching":        ["ASRS-5", "PHQ-9"],
    "device_checking":            ["GAD-7", "ASRS-5"],
    "low_affect":                 ["PHQ-9", "GAD-7"],
    "elevated_affect":            ["MDQ", "PHQ-9"],
    "passive_si":                 ["PHQ-9", "C-SSRS"],
    "social_withdrawal":          ["PHQ-9", "GAD-7"],
    "somatic_preoccupation":      ["PHQ-9", "GAD-7", "ASRS-5"],
    "sleep_disturbance":          ["PHQ-9", "GAD-7"],
    "appetite_change":            ["PHQ-9"],
    "anxiety_markers":            ["GAD-7", "PHQ-9"],
    "concentration_difficulty":   ["ASRS-5", "PHQ-9"],
    "psychomotor_slowing":        ["PHQ-9"],
    "irritability":               ["MDQ", "GAD-7", "PHQ-9"],
    "mood_lability":              ["MDQ", "PHQ-9"],
}


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


def suggest_instruments(signal_types: list[str]) -> list[str]:
    candidates: Counter = Counter()
    for sig in signal_types:
        for instrument in INSTRUMENT_SUGGESTION_MAP.get(sig, []):
            candidates[instrument] += 1
    return [inst for inst, _ in candidates.most_common(3)]


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


async def run_gap_detector_for_patient(conn, patient_id: str) -> Optional[dict]:
    """Check one patient for a behavioral screening gap.

    Returns a dict describing the newly inserted gap, or None if no gap
    condition is met (below threshold, already tracked, recent screening).
    """
    pressure_row = await conn.fetchrow(
        "SELECT * FROM atom_pressure_scores WHERE patient_id = $1::uuid",
        patient_id,
    )
    if not pressure_row:
        return None

    pressure_score = float(pressure_row["pressure_score"] or 0)
    atom_count = int(pressure_row["present_atom_count"] or 0)

    if pressure_score < PRESSURE_THRESHOLD or atom_count < MIN_ATOM_COUNT:
        return None

    existing_gap = await conn.fetchrow(
        "SELECT id FROM behavioral_screening_gaps "
        "WHERE patient_id = $1::uuid AND status = 'open'",
        patient_id,
    )
    if existing_gap:
        return None

    last_phq = await conn.fetchrow(
        "SELECT observation_date, total_score, item_9_score "
        "FROM phq9_observations "
        "WHERE patient_id = $1::uuid "
        "ORDER BY observation_date DESC LIMIT 1",
        patient_id,
    )

    lookback_cutoff = date.today() - timedelta(days=SCREENING_LOOKBACK_MONTHS * 30)
    if not last_phq:
        gap_type = "no_screening"
    elif last_phq["observation_date"] < lookback_cutoff:
        gap_type = "stale_screening"
    elif (last_phq["item_9_score"] or 0) >= 1:
        has_followup = await conn.fetchval(
            "SELECT EXISTS(SELECT 1 FROM care_gaps "
            "WHERE patient_id = $1::uuid "
            "AND gap_type = 'behavioral_health_followup' "
            "AND status = 'closed' AND updated_at > $2)",
            patient_id, last_phq["observation_date"],
        )
        if not has_followup:
            gap_type = "item9_no_followup"
        else:
            return None
    else:
        return None

    atom_rows = await conn.fetch(
        "SELECT id, clinical_date, signal_type "
        "FROM behavioral_signal_atoms "
        "WHERE patient_id = $1::uuid AND assertion = 'present' "
        "ORDER BY clinical_date ASC",
        patient_id,
    )
    atom_ids = [r["id"] for r in atom_rows]
    signal_types = [r["signal_type"] for r in atom_rows]
    dates = [r["clinical_date"] for r in atom_rows]

    latest_atom = max(dates) if dates else None
    earliest_atom = min(dates) if dates else None
    temporal_confidence = compute_temporal_confidence(latest_atom)
    instruments = suggest_instruments(signal_types)

    gap_id = uuid.uuid4()
    await conn.execute(
        """
        INSERT INTO behavioral_screening_gaps (
            id, patient_id, gap_type, atom_count,
            atom_date_range, atom_ids, pressure_score,
            last_screening_date, last_screening_score, last_screening_item9,
            output_mode, temporal_confidence, recommended_instruments
        ) VALUES (
            $1::uuid, $2::uuid, $3, $4,
            daterange($5, $6, '[]'), $7, $8,
            $9, $10, $11,
            'primary_evidence', $12, $13
        )
        """,
        gap_id, patient_id, gap_type, atom_count,
        earliest_atom, latest_atom, atom_ids, pressure_score,
        last_phq["observation_date"] if last_phq else None,
        last_phq["total_score"] if last_phq else None,
        last_phq["item_9_score"] if last_phq else None,
        temporal_confidence, instruments,
    )

    await conn.execute(
        "UPDATE behavioral_signal_atoms "
        "SET contributed_to_gap_id = $1 "
        "WHERE patient_id = $2::uuid AND assertion = 'present' "
        "AND contributed_to_gap_id IS NULL",
        gap_id, patient_id,
    )

    await _upsert_phenotype(
        conn,
        patient_id=patient_id,
        evidence_mode="primary_evidence",
        screening_gap_id=gap_id,
        atom_pressure_score=pressure_score,
        temporal_confidence=temporal_confidence,
    )

    return {
        "gap_id": str(gap_id),
        "patient_id": patient_id,
        "gap_type": gap_type,
        "atom_count": atom_count,
        "pressure_score": pressure_score,
        "temporal_confidence": temporal_confidence,
        "recommended_instruments": instruments,
    }


# TODO(phq9-ingest): once a PHQ-9 writer is added to
# ingestion/adapters/healthex/parsers/, call resolve_gap_on_new_screening()
# immediately after the INSERT into phq9_observations so Mode B → Mode A
# transition happens synchronously. Until then, `run_behavioral_gap_check`
# idempotency + the nightly `run_batch_gap_detector` keep state consistent.
async def resolve_gap_on_new_screening(
    conn,
    patient_id: str,
    new_screening_id: str,
    screening_date: date,
    total_score: int,
    item_9_score: Optional[int],
) -> None:
    """Transition Mode B → Mode A when a new PHQ-9 arrives.

    Closes any open gap and updates the phenotype. Safe no-op if no gap
    is open.
    """
    try:
        screening_uuid = uuid.UUID(str(new_screening_id))
    except (TypeError, ValueError):
        screening_uuid = None

    await conn.execute(
        """
        UPDATE behavioral_screening_gaps
        SET status = 'resolved',
            resolved_by_screening_id = $1,
            resolved_at = NOW(),
            output_mode = 'contextual',
            updated_at = NOW()
        WHERE patient_id = $2::uuid AND status = 'open'
        """,
        screening_uuid, patient_id,
    )

    await _upsert_phenotype(
        conn,
        patient_id=patient_id,
        evidence_mode="contextual",
        screening_gap_id=None,
        last_formal_screening=screening_date,
    )


async def run_batch_gap_detector(conn) -> list[dict]:
    """Nightly batch: scan all patients above pressure threshold."""
    candidates = await conn.fetch(
        """
        SELECT aps.patient_id
        FROM atom_pressure_scores aps
        WHERE aps.pressure_score >= $1
          AND aps.present_atom_count >= $2
          AND NOT EXISTS (
            SELECT 1 FROM behavioral_screening_gaps bsg
            WHERE bsg.patient_id = aps.patient_id AND bsg.status = 'open'
          )
        """,
        PRESSURE_THRESHOLD, MIN_ATOM_COUNT,
    )
    detected: list[dict] = []
    for row in candidates:
        gap = await run_gap_detector_for_patient(conn, str(row["patient_id"]))
        if gap:
            detected.append(gap)
    return detected


def register(mcp):  # pragma: no cover - no-op to silence skill loader warning
    return
