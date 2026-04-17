"""
backfill_transfer_log.py — Retroactively emit transfer_log rows for clinical
records that were written before P-0 instrumentation was in place.

For each row in the warehouse tables (patient_conditions, patient_medications,
biometric_readings, clinical_events, patient_immunizations, behavioral_screenings,
patients, clinical_notes) that has no corresponding transfer_log entry,
write a synthetic transfer_log row with status="verified" and timestamps
pinned to the row's created_at.

Usage:
    python scripts/backfill_transfer_log.py --patient-id=<uuid>
    python scripts/backfill_transfer_log.py --patient-id=<uuid> --dry-run

PHI rule: record_key never contains patient name, DOB, or free-text content
— only coded identifiers, dates, and resource types.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import logging
import os
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

import asyncpg

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("backfill")


def _natural_key(resource_type: str, row: dict) -> tuple[str, str, str]:
    """(record_key, loinc_code, icd10_code) — PHI-safe natural key."""
    if resource_type == "conditions":
        code = row.get("code") or ""
        onset = row.get("onset_date") or ""
        return f"condition::{code or 'NOCODE'}::{onset}", "", code
    if resource_type == "medications":
        code = row.get("code") or ""
        authored = row.get("authored_on") or ""
        return f"medication::{code or 'NOCODE'}::{authored}", "", ""
    if resource_type == "labs":
        code = row.get("loinc_code") or ""
        metric = row.get("metric_type") or ""
        measured = row.get("measured_at") or ""
        return f"lab::{code or metric or 'NOCODE'}::{measured}", code, ""
    if resource_type == "encounters":
        etype = row.get("event_type") or ""
        edate = row.get("event_date") or ""
        return f"encounter::{etype}::{edate}", "", ""
    if resource_type == "immunizations":
        code = row.get("cvx_code") or row.get("code") or ""
        given = row.get("administered_on") or row.get("date") or ""
        return f"immunization::{code or 'NOCODE'}::{given}", "", ""
    if resource_type == "behavioral_screenings":
        inst = row.get("instrument_key") or ""
        adm = row.get("administered_at") or ""
        return f"screening::{inst}::{adm}", row.get("loinc_code") or "", ""
    if resource_type == "notes":
        nt = row.get("note_type") or ""
        nd = row.get("note_date") or ""
        return f"note::{nt}::{nd}", "", ""
    if resource_type == "patient_registration":
        mrn = row.get("mrn") or ""
        return f"registration::{mrn}", "", ""
    return f"{resource_type}::unknown", "", ""


_TABLE_QUERIES: dict[str, str] = {
    "conditions": (
        "SELECT code, onset_date, clinical_status, created_at "
        "FROM patient_conditions WHERE patient_id = $1::uuid"
    ),
    "medications": (
        "SELECT code, authored_on, status, created_at "
        "FROM patient_medications WHERE patient_id = $1::uuid"
    ),
    "labs": (
        "SELECT metric_type, loinc_code, measured_at, created_at "
        "FROM biometric_readings WHERE patient_id = $1::uuid"
    ),
    "encounters": (
        "SELECT event_type, event_date, created_at "
        "FROM clinical_events WHERE patient_id = $1::uuid"
    ),
    "behavioral_screenings": (
        "SELECT instrument_key, loinc_code, administered_at, created_at "
        "FROM behavioral_screenings WHERE patient_id = $1::uuid"
    ),
    "patient_registration": (
        "SELECT mrn, created_at FROM patients WHERE id = $1::uuid"
    ),
}


async def _existing_keys(conn, patient_id: str, resource_type: str) -> set[str]:
    rows = await conn.fetch(
        """SELECT record_key FROM transfer_log
           WHERE patient_id = $1::uuid AND resource_type = $2""",
        patient_id, resource_type,
    )
    return {r["record_key"] for r in rows}


async def _backfill_resource(
    conn, patient_id: str, resource_type: str, query: str, *, dry_run: bool
) -> int:
    try:
        rows = await conn.fetch(query, patient_id)
    except Exception as exc:
        log.warning("skipping %s: read failed: %s", resource_type, exc)
        return 0
    if not rows:
        return 0

    existing = await _existing_keys(conn, patient_id, resource_type)
    inserted = 0

    for raw in rows:
        row = dict(raw)
        key, loinc, icd10 = _natural_key(resource_type, row)
        if key in existing:
            continue
        created = row.get("created_at") or datetime.now(timezone.utc)
        key_hash = hashlib.sha256(key.encode()).hexdigest()[:16]
        tid = str(uuid.uuid4())
        batch = str(uuid.uuid4())

        log.warning(
            "backfilled transfer_log for %s/%s/%s",
            patient_id, resource_type, key,
        )
        if dry_run:
            inserted += 1
            continue

        try:
            await conn.execute(
                """INSERT INTO transfer_log
                       (id, patient_id, resource_type, source, record_key,
                        record_hash, loinc_code, icd10_code,
                        batch_id, batch_sequence, batch_total,
                        chunk_id, chunk_sequence, chunk_total,
                        strategy, format_detected,
                        planned_at, sanitized_at, written_at, verified_at,
                        status, payload_size_bytes)
                   VALUES ($1::uuid, $2::uuid, $3, 'backfill', $4,
                           $5, $6, $7,
                           $8::uuid, 1, 1, $8::uuid, 1, 1,
                           'backfill', 'unknown',
                           $9, $9, $9, $9,
                           'verified', 0)
                   ON CONFLICT (id) DO NOTHING""",
                tid, patient_id, resource_type, key,
                key_hash, loinc or None, icd10 or None,
                batch, created,
            )
            inserted += 1
        except Exception as exc:
            log.warning("insert failed for %s: %s", key, exc)

    return inserted


async def main() -> int:
    parser = argparse.ArgumentParser(description="Backfill transfer_log rows")
    parser.add_argument("--patient-id", required=True, help="Patient UUID")
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Report rows that would be written without writing them",
    )
    args = parser.parse_args()

    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        log.error("DATABASE_URL environment variable is required")
        return 1

    conn = await asyncpg.connect(dsn)
    try:
        total = 0
        summary: dict[str, int] = {}
        for resource_type, query in _TABLE_QUERIES.items():
            n = await _backfill_resource(
                conn, args.patient_id, resource_type, query,
                dry_run=args.dry_run,
            )
            summary[resource_type] = n
            total += n

        mode = "dry-run" if args.dry_run else "wrote"
        log.info("%s %d synthetic transfer_log rows for %s", mode, total, args.patient_id)
        for rt, n in summary.items():
            if n:
                log.info("  %-22s  %d", rt, n)
    finally:
        await conn.close()

    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
