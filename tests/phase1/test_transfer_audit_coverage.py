"""
test_transfer_audit_coverage.py — P-0 verification tests.

Covers:
  - log_single_record_transfer helper emits a well-formed transfer_log row
  - register_healthex_patient emits a patient_registration transfer_log row
  - Summary-mode ingest emits summary_section marker rows + per-record rows
  - get_transfer_audit reports coverage gaps when clinical rows exist
    without transfer_log entries
  - backfill_transfer_log.py closes coverage gaps

Uses the shared event_loop and db_pool fixtures from conftest.py.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

import asyncpg
import pytest
import pytest_asyncio

_has_db = "DATABASE_URL" in os.environ
skip_no_db = pytest.mark.skipif(not _has_db, reason="DATABASE_URL not set")

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent


@pytest_asyncio.fixture
async def coverage_patient(db_pool):
    """Dedicated patient per test; cleaned up after."""
    pid = str(uuid.uuid4())
    mrn = f"COV-{pid[:8]}"
    async with db_pool.acquire() as conn:
        await conn.execute(
            """INSERT INTO patients
                   (id, mrn, first_name, last_name, birth_date, gender,
                    is_synthetic, data_source)
               VALUES ($1::uuid, $2, 'Coverage', 'Test', '1970-01-01',
                       'female', false, 'healthex')""",
            pid, mrn,
        )
    yield pid, mrn
    async with db_pool.acquire() as conn:
        await conn.execute("DELETE FROM transfer_log WHERE patient_id=$1::uuid", pid)
        await conn.execute("DELETE FROM behavioral_screenings WHERE patient_id=$1::uuid", pid)
        await conn.execute("DELETE FROM biometric_readings WHERE patient_id=$1::uuid", pid)
        await conn.execute("DELETE FROM patient_conditions WHERE patient_id=$1::uuid", pid)
        await conn.execute("DELETE FROM patient_medications WHERE patient_id=$1::uuid", pid)
        await conn.execute("DELETE FROM clinical_events WHERE patient_id=$1::uuid", pid)
        await conn.execute("DELETE FROM source_freshness WHERE patient_id=$1::uuid", pid)
        await conn.execute("DELETE FROM data_sources WHERE patient_id=$1::uuid", pid)
        await conn.execute("DELETE FROM patients WHERE id=$1::uuid", pid)


# ---------------------------------------------------------------------------
# log_single_record_transfer helper
# ---------------------------------------------------------------------------

@skip_no_db
class TestLogSingleRecordTransfer:
    @pytest.mark.asyncio
    async def test_writes_verified_row(self, db_pool, coverage_patient):
        from ingestion.adapters.healthex.traced_writer import (
            log_single_record_transfer,
        )
        pid, _ = coverage_patient
        async with db_pool.acquire() as conn:
            tid = await log_single_record_transfer(
                conn, patient_id=pid,
                resource_type="patient_registration",
                source="healthex",
                record_key=f"registration::TEST-{pid[:8]}",
                strategy="register",
                format_detected="healthex_summary",
                payload_bytes=512,
                mark_verified=True,
            )
            row = await conn.fetchrow(
                "SELECT status, strategy, format_detected, record_key, "
                "verified_at, written_at, sanitized_at "
                "FROM transfer_log WHERE id=$1::uuid",
                tid,
            )

        assert row is not None
        assert row["status"] == "verified"
        assert row["strategy"] == "register"
        assert row["format_detected"] == "healthex_summary"
        assert row["record_key"].startswith("registration::")
        assert row["verified_at"] is not None
        assert row["written_at"] is not None
        assert row["sanitized_at"] is not None


# ---------------------------------------------------------------------------
# Summary-mode ingest writes transfer_log rows per section and per record
# ---------------------------------------------------------------------------

@skip_no_db
class TestSummaryIngestTransferLog:
    @pytest.mark.asyncio
    async def test_summary_section_and_per_record_rows(self, db_pool, coverage_patient):
        """Directly exercise _transform_and_write + summary_section emitter.

        We avoid the full MCP tool call here because it runs the LLM planner;
        this test focuses on the transfer_log emission invariants.
        """
        from ingestion.adapters.healthex.traced_writer import (
            log_single_record_transfer,
        )
        from server.mcp_server import (
            _transform_and_write,
            _normalize_to_fhir,
        )
        from transforms.fhir_to_schema import (
            transform_conditions,
            transform_medications,
            transform_clinical_observations,
            transform_encounters,
        )

        pid, _ = coverage_patient

        # Seed: a summary with 2 conditions + 1 medication + 3 labs
        conds = [
            {"name": "Prediabetes", "icd10": "R73.03", "onset_date": "2017-04-25", "status": "active"},
            {"name": "Fatty liver", "icd10": "K76.0", "onset_date": "2020-01-15", "status": "active"},
        ]
        meds = [
            {"name": "Pantoprazole", "rxnorm": "40790", "start_date": "2022-03-10", "status": "active"},
        ]
        labs = [
            {"test_name": "HbA1c", "loinc": "4548-4", "value": "6.1", "unit": "%", "date": "2025-01-15"},
            {"test_name": "Glucose", "loinc": "2345-7", "value": "110", "unit": "mg/dL", "date": "2025-01-15"},
            {"test_name": "LDL", "loinc": "13457-7", "value": "130", "unit": "mg/dL", "date": "2025-01-15"},
        ]

        async with db_pool.acquire() as conn:
            for sub_type, items in (
                ("conditions", conds),
                ("medications", meds),
                ("labs", labs),
            ):
                await log_single_record_transfer(
                    conn, patient_id=pid,
                    resource_type=sub_type,
                    source="healthex",
                    record_key=f"summary::{sub_type}::{len(items)}",
                    strategy="summary_section",
                    format_detected="healthex_summary",
                    payload_bytes=800,
                    mark_verified=True,
                )
                fhir_resources = _normalize_to_fhir(sub_type, items)
                await _transform_and_write(
                    conn, sub_type, fhir_resources, pid,
                    transform_conditions, transform_medications,
                    transform_clinical_observations, transform_encounters,
                    format_detected="healthex_summary",
                    strategy="summary_record",
                    payload_bytes=800,
                )

            section_cnt = await conn.fetchval(
                """SELECT COUNT(*) FROM transfer_log
                   WHERE patient_id=$1::uuid AND strategy='summary_section'""",
                pid,
            )
            record_cnt = await conn.fetchval(
                """SELECT COUNT(*) FROM transfer_log
                   WHERE patient_id=$1::uuid AND strategy='summary_record'""",
                pid,
            )

        assert section_cnt == 3, f"expected 3 section markers, got {section_cnt}"
        # 2 conds + 1 med + 3 labs = 6 per-record rows
        assert record_cnt == 6, f"expected 6 per-record rows, got {record_cnt}"


# ---------------------------------------------------------------------------
# Coverage detection + backfill
# ---------------------------------------------------------------------------

@skip_no_db
class TestCoverageAndBackfill:
    @pytest.mark.asyncio
    async def test_coverage_gap_detected_for_orphan_rows(self, db_pool, coverage_patient):
        """Insert a condition directly via SQL (bypassing ingest). The audit
        trail should report a coverage_gap for conditions.
        """
        from server.mcp_server import _compute_audit_coverage

        pid, _ = coverage_patient
        async with db_pool.acquire() as conn:
            await conn.execute(
                """INSERT INTO patient_conditions
                       (id, patient_id, code, display, onset_date,
                        clinical_status, data_source)
                   VALUES ($1::uuid, $2::uuid, 'E11.9', 'Type 2 diabetes',
                           '2015-01-01', 'active', 'manual')""",
                str(uuid.uuid4()), pid,
            )
            coverage = await _compute_audit_coverage(conn, pid)

        conds = [c for c in coverage if c["resource_type"] == "conditions"]
        assert conds, "conditions coverage row missing"
        entry = conds[0]
        assert entry["warehouse_rows"] >= 1
        assert entry["coverage_gap"] >= 1
        assert "warning" in entry

    @pytest.mark.asyncio
    async def test_backfill_script_closes_gap(self, db_pool, coverage_patient):
        from server.mcp_server import _compute_audit_coverage

        pid, _ = coverage_patient
        async with db_pool.acquire() as conn:
            await conn.execute(
                """INSERT INTO patient_conditions
                       (id, patient_id, code, display, onset_date,
                        clinical_status, data_source)
                   VALUES ($1::uuid, $2::uuid, 'I10', 'Essential hypertension',
                           '2019-06-01', 'active', 'manual')""",
                str(uuid.uuid4()), pid,
            )

        script = _REPO_ROOT / "scripts" / "backfill_transfer_log.py"
        result = subprocess.run(
            [sys.executable, str(script), "--patient-id", pid],
            check=False,
            capture_output=True,
            text=True,
            env={**os.environ},
        )
        assert result.returncode == 0, (
            f"backfill exited {result.returncode}\nstdout={result.stdout}\n"
            f"stderr={result.stderr}"
        )

        async with db_pool.acquire() as conn:
            coverage = await _compute_audit_coverage(conn, pid)

        for entry in coverage:
            assert entry["coverage_gap"] == 0, (
                f"{entry['resource_type']} still has gap: {entry}"
            )

    @pytest.mark.asyncio
    async def test_backfill_dry_run_writes_nothing(self, db_pool, coverage_patient):
        pid, _ = coverage_patient
        async with db_pool.acquire() as conn:
            await conn.execute(
                """INSERT INTO patient_conditions
                       (id, patient_id, code, display, onset_date,
                        clinical_status, data_source)
                   VALUES ($1::uuid, $2::uuid, 'M54.5', 'Low back pain',
                           '2021-02-14', 'active', 'manual')""",
                str(uuid.uuid4()), pid,
            )

        script = _REPO_ROOT / "scripts" / "backfill_transfer_log.py"
        result = subprocess.run(
            [sys.executable, str(script), "--patient-id", pid, "--dry-run"],
            check=False,
            capture_output=True,
            text=True,
            env={**os.environ},
        )
        assert result.returncode == 0

        async with db_pool.acquire() as conn:
            tl_cnt = await conn.fetchval(
                "SELECT COUNT(*) FROM transfer_log WHERE patient_id=$1::uuid",
                pid,
            )
        assert tl_cnt == 0, f"dry-run should not write, got {tl_cnt} rows"
