"""
test_transfer_pipeline.py — Tests for the Traceable Transfer Pipeline.

Covers:
  - sanitize_text_field: blob escaping (double-quotes, null bytes, truncation)
  - plan_transfer: strategy selection, chunk structure, record keys
  - transfer_log table: asyncpg round-trip (INSERT → UPDATE → SELECT)
  - execute_transfer_plan_async: per-record write + audit trail (labs round-trip)
  - TracedWriter: conditions written and audit logged

Uses the shared event_loop and db_pool fixtures from conftest.py.
Do NOT redefine event_loop or db_pool here — that causes session fixture conflicts.
"""

import os
import uuid
from datetime import datetime, timezone

import asyncpg
import pytest
import pytest_asyncio

_has_db = "DATABASE_URL" in os.environ
skip_no_db = pytest.mark.skipif(not _has_db, reason="DATABASE_URL not set")


# ---------------------------------------------------------------------------
# transfer_patient — session-scoped test patient for this module
# Uses shared db_pool from conftest.py (no duplicate event_loop)
# ---------------------------------------------------------------------------

@pytest_asyncio.fixture(scope="session")
async def transfer_patient(db_pool):
    """A test patient cleaned up after the whole session."""
    pid = str(uuid.uuid4())
    async with db_pool.acquire() as conn:
        await conn.execute(
            """INSERT INTO patients
                   (id, mrn, first_name, last_name, birth_date, gender,
                    is_synthetic, data_source)
               VALUES ($1::uuid, $2, 'Transfer', 'PipelineTest', '1980-06-15', 'male',
                       true, 'test')
               ON CONFLICT DO NOTHING""",
            pid, f"TXF-{pid[:8]}",
        )
    yield pid
    async with db_pool.acquire() as conn:
        await conn.execute("DELETE FROM transfer_log WHERE patient_id = $1::uuid", pid)
        await conn.execute("DELETE FROM biometric_readings WHERE patient_id = $1::uuid", pid)
        await conn.execute("DELETE FROM patient_conditions WHERE patient_id = $1::uuid", pid)
        await conn.execute("DELETE FROM patients WHERE id = $1::uuid", pid)


# ---------------------------------------------------------------------------
# T1: sanitize_text_field — blob escaping unit tests (no DB needed)
# ---------------------------------------------------------------------------

class TestSanitizeTextField:
    def test_tp1_double_quotes_replaced(self):
        from ingestion.adapters.healthex.traced_writer import sanitize_text_field
        result = sanitize_text_field('Ultrasound: "echogenic" lesion')
        assert '"' not in result
        assert "'" in result
        assert "echogenic" in result

    def test_tp2_null_bytes_stripped(self):
        from ingestion.adapters.healthex.traced_writer import sanitize_text_field
        result = sanitize_text_field("normal\x00text")
        assert "\x00" not in result
        assert "normal" in result

    def test_tp3_truncation_at_max_len(self):
        from ingestion.adapters.healthex.traced_writer import sanitize_text_field
        long_text = "A" * 15_000
        result = sanitize_text_field(long_text, max_len=10_000)
        assert len(result) <= 10_000
        assert result.endswith("...")

    def test_tp4_non_string_passthrough(self):
        from ingestion.adapters.healthex.traced_writer import sanitize_text_field
        assert sanitize_text_field(42) == 42
        assert sanitize_text_field(None) is None

    def test_tp5_sanitize_row_applies_to_all_strings(self):
        from ingestion.adapters.healthex.traced_writer import sanitize_row
        row = {
            "test_name": 'HbA1c "fasting"',
            "value": 7.8,
            "note": "normal\x00value",
        }
        result = sanitize_row(row)
        assert '"' not in result["test_name"]
        assert result["value"] == 7.8
        assert "\x00" not in result["note"]


# ---------------------------------------------------------------------------
# T2: plan_transfer — strategy selection and structure (no DB)
# ---------------------------------------------------------------------------

class TestTransferPlanner:
    def test_tp6_single_strategy_small_batch(self):
        from ingestion.adapters.healthex.transfer_planner import plan_transfer
        records = [{"test_name": "HbA1c", "value": "7.8", "date": "2025-01-15"}]
        plan = plan_transfer("pid", "labs", records, 200, "compressed_table")
        assert plan.strategy == "single"
        assert plan.total_records == 1
        assert len(plan.chunks) == 1
        assert len(plan.chunks[0]) == 1

    def test_tp7_chunked_small_strategy_for_medium_batch(self):
        from ingestion.adapters.healthex.transfer_planner import plan_transfer
        records = [{"test_name": f"Test{i}", "value": str(i)} for i in range(20)]
        plan = plan_transfer("pid", "labs", records, 5_000, "plain_text")
        assert plan.strategy in ("chunked_small", "chunked_medium")
        assert plan.total_records == 20
        assert sum(len(c) for c in plan.chunks) == 20

    def test_tp8_record_key_for_labs(self):
        from ingestion.adapters.healthex.transfer_planner import plan_transfer
        records = [{"test_name": "Glucose", "value": "110", "date": "2025-06-01"}]
        plan = plan_transfer("pid", "labs", records, 100, "plain_text")
        tr = plan.chunks[0][0]
        assert tr.record_key == "Glucose::2025-06-01"

    def test_tp9_record_key_for_conditions(self):
        from ingestion.adapters.healthex.transfer_planner import plan_transfer
        records = [{"name": "Prediabetes", "icd10": "R73.03", "onset_date": "2017-04-25"}]
        plan = plan_transfer("pid", "conditions", records, 100, "plain_text")
        tr = plan.chunks[0][0]
        assert "Prediabetes" in tr.record_key
        assert tr.icd10_code == "R73.03"

    def test_tp10_batch_id_shared_within_plan(self):
        from ingestion.adapters.healthex.transfer_planner import plan_transfer
        records = [{"test_name": f"T{i}", "value": str(i)} for i in range(5)]
        plan = plan_transfer("pid", "labs", records, 300, "compressed_table")
        batch_ids = {tr.batch_id for chunk in plan.chunks for tr in chunk}
        assert len(batch_ids) == 1
        assert batch_ids.pop() == plan.batch_id

    def test_tp11_record_hash_is_16_chars(self):
        from ingestion.adapters.healthex.transfer_planner import plan_transfer
        records = [{"test_name": "HbA1c", "value": "7.8"}]
        plan = plan_transfer("pid", "labs", records, 100, "plain_text")
        tr = plan.chunks[0][0]
        assert len(tr.record_hash) == 16
        assert tr.record_hash.isalnum()

    def test_tp12_empty_records_returns_single_strategy(self):
        from ingestion.adapters.healthex.transfer_planner import plan_transfer
        plan = plan_transfer("pid", "labs", [], 0, "plain_text")
        assert plan.strategy == "single"
        assert plan.total_records == 0
        assert sum(len(c) for c in plan.chunks) == 0

    def test_tp12b_format_detected_stored_in_plan(self):
        from ingestion.adapters.healthex.transfer_planner import plan_transfer
        records = [{"test_name": "TSH", "value": "2.5"}]
        plan = plan_transfer("pid", "labs", records, 100, "compressed_table")
        assert plan.format_detected == "compressed_table"


# ---------------------------------------------------------------------------
# T3: transfer_log round-trip (asyncpg INSERT → UPDATE → SELECT)
# ---------------------------------------------------------------------------

@skip_no_db
class TestTransferLogTable:
    @pytest.mark.asyncio
    async def test_tp13_insert_and_query_planned_row(self, db_pool, transfer_patient):
        pid = transfer_patient
        tid = str(uuid.uuid4())
        bid = str(uuid.uuid4())
        cid = str(uuid.uuid4())
        now = datetime.now(timezone.utc)

        async with db_pool.acquire() as conn:
            await conn.execute(
                """INSERT INTO transfer_log
                       (id, patient_id, resource_type, source, record_key,
                        batch_id, batch_sequence, batch_total,
                        chunk_id, chunk_sequence, chunk_total,
                        strategy, planned_at, status)
                   VALUES ($1::uuid,$2::uuid,'labs','healthex','HbA1c::2025-01-15',
                           $3::uuid,1,1,$4::uuid,1,1,'single',$5,'planned')""",
                tid, pid, bid, cid, now,
            )
            row = await conn.fetchrow(
                "SELECT * FROM transfer_log WHERE id = $1::uuid", tid
            )

        assert row is not None
        assert row["status"] == "planned"
        assert row["resource_type"] == "labs"
        assert row["record_key"] == "HbA1c::2025-01-15"
        assert str(row["patient_id"]) == pid

    @pytest.mark.asyncio
    async def test_tp14_status_transitions_planned_to_verified(self, db_pool,
                                                                transfer_patient):
        pid = transfer_patient
        tid = str(uuid.uuid4())
        bid = str(uuid.uuid4())
        cid = str(uuid.uuid4())
        now = datetime.now(timezone.utc)

        async with db_pool.acquire() as conn:
            await conn.execute(
                """INSERT INTO transfer_log
                       (id, patient_id, resource_type, source, record_key,
                        batch_id, batch_sequence, batch_total,
                        chunk_id, chunk_sequence, chunk_total,
                        strategy, planned_at, status)
                   VALUES ($1::uuid,$2::uuid,'conditions','healthex','Prediabetes::2017',
                           $3::uuid,1,1,$4::uuid,1,1,'single',$5,'planned')""",
                tid, pid, bid, cid, now,
            )
            for new_status, col in [
                ("sanitized",  "sanitized_at"),
                ("written",    "written_at"),
                ("verified",   "verified_at"),
            ]:
                await conn.execute(
                    f"UPDATE transfer_log SET status=$1, {col}=$2 WHERE id=$3::uuid",
                    new_status, now, tid,
                )
            row = await conn.fetchrow(
                "SELECT status, sanitized_at, written_at, verified_at "
                "FROM transfer_log WHERE id=$1::uuid",
                tid,
            )

        assert row["status"] == "verified"
        assert row["sanitized_at"] is not None
        assert row["written_at"] is not None
        assert row["verified_at"] is not None

    @pytest.mark.asyncio
    async def test_tp15_failed_status_stores_error_stage(self, db_pool, transfer_patient):
        pid = transfer_patient
        tid = str(uuid.uuid4())
        bid = str(uuid.uuid4())
        cid = str(uuid.uuid4())
        now = datetime.now(timezone.utc)

        async with db_pool.acquire() as conn:
            await conn.execute(
                """INSERT INTO transfer_log
                       (id, patient_id, resource_type, source, record_key,
                        batch_id, batch_sequence, batch_total,
                        chunk_id, chunk_sequence, chunk_total,
                        strategy, planned_at, status)
                   VALUES ($1::uuid,$2::uuid,'medications','healthex','Metformin::2020',
                           $3::uuid,1,1,$4::uuid,1,1,'single',$5,'planned')""",
                tid, pid, bid, cid, now,
            )
            await conn.execute(
                """UPDATE transfer_log SET
                       status='failed', failed_at=$1,
                       error_stage='transform', error_message='No FHIR resource produced'
                   WHERE id=$2::uuid""",
                now, tid,
            )
            row = await conn.fetchrow(
                "SELECT status, error_stage, error_message "
                "FROM transfer_log WHERE id=$1::uuid",
                tid,
            )

        assert row["status"] == "failed"
        assert row["error_stage"] == "transform"
        assert "FHIR" in row["error_message"]

    @pytest.mark.asyncio
    async def test_tp16_invalid_status_rejected_by_constraint(self, db_pool,
                                                               transfer_patient):
        pid = transfer_patient
        tid = str(uuid.uuid4())
        bid = str(uuid.uuid4())
        cid = str(uuid.uuid4())
        now = datetime.now(timezone.utc)

        with pytest.raises(asyncpg.CheckViolationError):
            async with db_pool.acquire() as conn:
                await conn.execute(
                    """INSERT INTO transfer_log
                           (id, patient_id, resource_type, source, record_key,
                            batch_id, batch_sequence, batch_total,
                            chunk_id, chunk_sequence, chunk_total,
                            strategy, planned_at, status)
                       VALUES ($1::uuid,$2::uuid,'labs','healthex','bad',
                               $3::uuid,1,1,$4::uuid,1,1,'single',$5,'bogus_status')""",
                    tid, pid, bid, cid, now,
                )


# ---------------------------------------------------------------------------
# T4: execute_transfer_plan_async — full end-to-end round-trip
# ---------------------------------------------------------------------------

@skip_no_db
class TestExecuteTransferPlanAsync:
    @pytest.mark.asyncio
    async def test_tp17_labs_written_and_verified(self, db_pool, transfer_patient):
        from ingestion.adapters.healthex.transfer_planner import plan_transfer
        from ingestion.adapters.healthex.traced_writer import execute_transfer_plan_async

        pid = transfer_patient
        records = [
            {"test_name": "HbA1c", "loinc": "4548-4", "value": "7.8",
             "unit": "%", "date": "2025-01-15"},
            {"test_name": "Glucose", "loinc": "2345-7", "value": "110",
             "unit": "mg/dL", "date": "2025-01-15"},
        ]
        plan = plan_transfer(pid, "labs", records, 500, "compressed_table")
        result = await execute_transfer_plan_async(db_pool, plan, pid)

        assert result["records_planned"] == 2
        assert result["records_written"] >= 1, f"Expected ≥1 written: {result}"
        assert result["records_verified"] >= 1, f"Expected ≥1 verified: {result}"
        assert result["records_failed"] == 0, f"Expected 0 failed: {result}"
        assert result["batch_id"] == plan.batch_id
        assert result["strategy"] == "single"

    @pytest.mark.asyncio
    async def test_tp18_transfer_log_rows_created_for_each_record(self, db_pool,
                                                                    transfer_patient):
        from ingestion.adapters.healthex.transfer_planner import plan_transfer
        from ingestion.adapters.healthex.traced_writer import execute_transfer_plan_async

        pid = transfer_patient
        records = [
            {"test_name": "Cholesterol", "value": "180", "unit": "mg/dL",
             "date": "2025-03-01"},
        ]
        plan = plan_transfer(pid, "labs", records, 200, "plain_text")
        await execute_transfer_plan_async(db_pool, plan, pid)

        async with db_pool.acquire() as conn:
            cnt = await conn.fetchval(
                "SELECT COUNT(*) FROM transfer_log WHERE batch_id=$1::uuid",
                plan.batch_id,
            )
            row = await conn.fetchrow(
                "SELECT status, record_key FROM transfer_log "
                "WHERE batch_id=$1::uuid LIMIT 1",
                plan.batch_id,
            )

        assert cnt == 1
        assert row["status"] in ("verified", "written", "written_unverified")
        assert "Cholesterol" in row["record_key"]

    @pytest.mark.asyncio
    async def test_tp19_blob_escaped_before_write(self, db_pool, transfer_patient):
        from ingestion.adapters.healthex.traced_writer import sanitize_text_field

        narrative = 'Ultrasound: "echogenic" mass at liver margin\x00'
        sanitized = sanitize_text_field(narrative)
        assert '"' not in sanitized, "Double-quotes must be escaped before DB write"
        assert "\x00" not in sanitized, "Null bytes must be stripped"
        assert "echogenic" in sanitized

    @pytest.mark.asyncio
    async def test_tp20_conditions_written_and_audit_logged(self, db_pool,
                                                             transfer_patient):
        from ingestion.adapters.healthex.transfer_planner import plan_transfer
        from ingestion.adapters.healthex.traced_writer import execute_transfer_plan_async

        pid = transfer_patient
        records = [
            {"name": "Prediabetes", "icd10": "R73.03",
             "onset_date": "2017-04-25", "status": "active"},
        ]
        plan = plan_transfer(pid, "conditions", records, 200, "plain_text")
        result = await execute_transfer_plan_async(db_pool, plan, pid)

        assert result["records_written"] >= 1, f"Expected ≥1 written: {result}"

        async with db_pool.acquire() as conn:
            tl = await conn.fetchrow(
                "SELECT status, icd10_code, record_key FROM transfer_log "
                "WHERE batch_id=$1::uuid LIMIT 1",
                plan.batch_id,
            )
        assert tl is not None
        assert tl["status"] in ("verified", "written", "written_unverified")
        assert tl["record_key"].startswith("Prediabetes")
