"""
behavioral_gap_detector.py — Detect behavioral screening gaps driven by atom pressure.

A "gap" exists when a domain has atom signal pressure above threshold but
no qualifying behavioral_screenings row within the instrument's lookback window.

Key design constraints:
  - run_gap_detector_for_patient() returns list[dict], never Optional[dict]
  - temporal_confidence classification: high/medium/low/very_low
  - Gaps are written to behavioral_screening_gaps table
  - Phenotype labels are upserted to behavioral_phenotypes table

All functions are async (asyncpg).
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime, timezone
from typing import Optional

from shared.datetime_utils import ensure_aware

log = logging.getLogger(__name__)


class _ConnPool:
    """Pool-like adapter wrapping a single asyncpg connection.

    Lets functions that call pool.acquire() work when called with a bare
    connection (e.g. from the executor's already-acquired conn).
    """
    def __init__(self, conn):
        self._conn = conn

    def acquire(self):
        return self

    async def __aenter__(self):
        return self._conn

    async def __aexit__(self, *_args):
        pass


def _to_pool(pool_or_conn):
    """Return a pool-compatible object from either a pool or a bare connection."""
    if hasattr(pool_or_conn, "acquire"):
        return pool_or_conn
    return _ConnPool(pool_or_conn)


# ─── Constants ────────────────────────────────────────────────────────────────

# Atom pressure score that triggers gap detection
_PRESSURE_THRESHOLD = 0.40

# Minimum number of atoms to trigger gap detection (avoid single-word noise)
_MIN_ATOM_COUNT = 2

# Temporal confidence based on atom count and last_atom_at age (days)
_TEMPORAL_CONFIDENCE_RULES = [
    # (min_atoms, max_days_since_last, label)
    (5, 7,   "high"),
    (3, 30,  "medium"),
    (1, 90,  "low"),
    (0, 999, "very_low"),
]


def _classify_temporal_confidence(atom_count: int, last_atom_days: float) -> str:
    for min_atoms, max_days, label in _TEMPORAL_CONFIDENCE_RULES:
        if atom_count >= min_atoms and last_atom_days <= max_days:
            return label
    return "very_low"


def _phenotype_label_for_pressure(
    domain: str,
    pressure_score: float,
    temporal_confidence: str,
) -> str:
    """Derive a simple phenotype label from domain + pressure."""
    if temporal_confidence == "very_low":
        return f"{domain}_signal_faint"
    if pressure_score >= 0.80:
        return f"{domain}_high_burden"
    if pressure_score >= 0.55:
        return f"{domain}_moderate_burden"
    return f"{domain}_emerging"


async def run_gap_detector_for_patient(
    pool,
    patient_id: str,
    pressure_threshold: float = _PRESSURE_THRESHOLD,
    data_source: str = "healthex",
) -> list[dict]:
    """Detect behavioral screening gaps for one patient.

    Algorithm:
      1. Read atom_pressure_scores materialized view for the patient.
      2. Map signal_type → domain(s) using SCREENING_REGISTRY.
      3. For each domain above pressure_threshold:
         a. Check when the most recent behavioral_screenings row was for that domain.
         b. If no row OR row is older than max(instrument.lookback_days), create gap.
      4. Upsert phenotype labels.
      5. Write open gaps to behavioral_screening_gaps.

    Returns:
        list[dict] — one entry per domain gap detected (may be empty).
    """
    from skills.screening_registry import SCREENING_REGISTRY, DOMAIN_LOOKBACK_DAYS
    from skills.screening_registry import suggest_instruments_from_atoms

    pool = _to_pool(pool)
    now = datetime.now(timezone.utc)

    # ── 1. Load atom pressure ─────────────────────────────────────────────────
    async with pool.acquire() as conn:
        pressure_rows = await conn.fetch(
            """
            SELECT signal_type, pressure_score, present_atom_count, last_atom_at
            FROM atom_pressure_scores
            WHERE patient_id = $1::uuid
            """,
            patient_id,
        )

    if not pressure_rows:
        return []

    # ── 2. Map signal_type → domains ─────────────────────────────────────────
    domain_pressure: dict[str, dict] = {}  # domain → {pressure, atoms, last_at}

    for row in pressure_rows:
        sig = row["signal_type"]
        pressure = float(row["pressure_score"] or 0.0)
        count = int(row["present_atom_count"] or 0)
        last_at: Optional[datetime] = row["last_atom_at"]

        if pressure < pressure_threshold or count < _MIN_ATOM_COUNT:
            continue

        # Get domains for this signal type
        instruments_by_domain = suggest_instruments_from_atoms([sig])
        for domain in instruments_by_domain:
            if domain not in domain_pressure:
                domain_pressure[domain] = {
                    "pressure_score": 0.0,
                    "atom_count": 0,
                    "last_atom_at": None,
                }
            dp = domain_pressure[domain]
            # Take max pressure for this domain
            if pressure > dp["pressure_score"]:
                dp["pressure_score"] = pressure
            dp["atom_count"] += count
            if last_at and (dp["last_atom_at"] is None or last_at > dp["last_atom_at"]):
                dp["last_atom_at"] = last_at

    if not domain_pressure:
        return []

    # ── 3. Check most recent screening per domain ────────────────────────────
    async with pool.acquire() as conn:
        recent_screening_rows = await conn.fetch(
            """
            SELECT domain, MAX(administered_at) AS most_recent
            FROM behavioral_screenings
            WHERE patient_id = $1::uuid
            GROUP BY domain
            """,
            patient_id,
        )

    domain_last_screening: dict[str, datetime] = {
        r["domain"]: r["most_recent"] for r in recent_screening_rows
    }

    # ── 4. Detect gaps ───────────────────────────────────────────────────────
    gaps: list[dict] = []

    for domain, dp in domain_pressure.items():
        lookback_days = DOMAIN_LOOKBACK_DAYS.get(domain, 180)
        last_screen = domain_last_screening.get(domain)

        if last_screen is not None:
            days_since = (now - ensure_aware(last_screen)).days
            if days_since <= lookback_days:
                # Not stale — no gap
                continue
            gap_type = "stale_screening"
        else:
            gap_type = "no_screening"

        # Temporal confidence
        last_atom_at = dp["last_atom_at"]
        days_since_atom = (now - ensure_aware(last_atom_at)).days if last_atom_at else 999

        temporal_confidence = _classify_temporal_confidence(dp["atom_count"], days_since_atom)

        # Suggested instruments for this domain
        instruments_by_domain = suggest_instruments_from_atoms(
            [sig for sig in
             [row["signal_type"] for row in pressure_rows]
             if any(domain in suggest_instruments_from_atoms([sig]) for _ in [None])]
        )
        suggested = instruments_by_domain.get(domain, [
            inst_key for inst_key, inst in SCREENING_REGISTRY.items()
            if inst.domain == domain
        ])[:3]

        phenotype = _phenotype_label_for_pressure(domain, dp["pressure_score"], temporal_confidence)

        gap_id = str(uuid.uuid4())

        # ── 5a. Upsert phenotype (always, regardless of gap newness) ─────────
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO behavioral_phenotypes
                    (id, patient_id, domain, phenotype_label, confidence, last_updated)
                VALUES ($1::uuid, $2::uuid, $3, $4, $5, NOW())
                ON CONFLICT (patient_id, domain)
                DO UPDATE SET
                    phenotype_label = EXCLUDED.phenotype_label,
                    confidence = EXCLUDED.confidence,
                    last_updated = NOW()
                """,
                gap_id, patient_id, domain, phenotype, dp["pressure_score"],
            )

        # ── 5b. Insert gap row only if no existing open gap (idempotent) ──────
        async with pool.acquire() as conn:
            existing = await conn.fetchval(
                """
                SELECT id FROM behavioral_screening_gaps
                WHERE patient_id = $1::uuid
                  AND domain = $2
                  AND status = 'open'
                """,
                patient_id, domain,
            )
            is_new_gap = not existing
            if is_new_gap:
                await conn.execute(
                    """
                    INSERT INTO behavioral_screening_gaps
                        (id, patient_id, domain, gap_type, pressure_score,
                         suggested_instruments, phenotype_label, temporal_confidence,
                         status, data_source)
                    VALUES ($1::uuid, $2::uuid, $3, $4, $5, $6, $7, $8, 'open', $9)
                    """,
                    gap_id, patient_id, domain, gap_type,
                    dp["pressure_score"],
                    suggested,
                    phenotype, temporal_confidence, data_source,
                )

        # Only append newly detected gaps (skip existing-open ones).
        if is_new_gap:
            gaps.append({
                "gap_id": gap_id,
                "domain": domain,
                "gap_type": gap_type,
                "pressure_score": round(dp["pressure_score"], 3),
                "atom_count": dp["atom_count"],
                "temporal_confidence": temporal_confidence,
                "suggested_instruments": suggested,
                "phenotype_label": phenotype,
            })

    return gaps


async def resolve_gap(
    pool,
    patient_id: str,
    domain: str,
    resolved_by_screening_id: Optional[str] = None,
) -> bool:
    """Mark open gap(s) for a domain as resolved."""
    async with pool.acquire() as conn:
        result = await conn.execute(
            """
            UPDATE behavioral_screening_gaps
            SET status = 'resolved',
                resolved_at = NOW(),
                resolved_by = $1::uuid
            WHERE patient_id = $2::uuid
              AND domain = $3
              AND status = 'open'
            """,
            resolved_by_screening_id, patient_id, domain,
        )
    return result != "UPDATE 0"


async def get_open_gaps_for_patient(pool, patient_id: str) -> list[dict]:
    """Return all open behavioral screening gaps for a patient."""
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT id::text, domain, gap_type, pressure_score,
                   suggested_instruments, phenotype_label, temporal_confidence,
                   triggered_at, status
            FROM behavioral_screening_gaps
            WHERE patient_id = $1::uuid
              AND status = 'open'
            ORDER BY triggered_at DESC
            """,
            patient_id,
        )
    return [dict(r) for r in rows]


async def resolve_gap_on_new_screening(
    conn,
    patient_id: str,
    new_screening_id: Optional[str] = None,
    instrument_key: Optional[str] = None,
    domain: Optional[str] = None,
    screening_date=None,
) -> bool:
    """Resolve open gap(s) for a domain given a newly ingested screening.

    Accepts a raw asyncpg connection (executor uses a single conn, not a pool).
    Either `domain` or `instrument_key` is required; when only `instrument_key`
    is supplied the domain is derived from SCREENING_REGISTRY.
    Returns True if any rows were updated.
    """
    if not domain and instrument_key:
        inst = SCREENING_REGISTRY.get(instrument_key)
        if inst:
            domain = inst.domain
    if not domain:
        return False
    result = await conn.execute(
        """
        UPDATE behavioral_screening_gaps
        SET status = 'resolved',
            resolved_at = NOW(),
            resolved_by = $1::uuid
        WHERE patient_id = $2::uuid
          AND domain = $3
          AND status = 'open'
        """,
        new_screening_id, patient_id, domain,
    )
    return result != "UPDATE 0"
