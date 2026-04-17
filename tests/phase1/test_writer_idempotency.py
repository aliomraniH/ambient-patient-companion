"""
test_writer_idempotency.py — P-2 part 2: UNIQUE natural keys collapse duplicates.

Covers (DB-gated):
  - Duplicate condition inserts with the same natural key collapse to 1 row
  - Duplicate medication inserts collapse to 1 row
  - Duplicate encounter inserts collapse to 1 row
  - Duplicate behavioral_screening inserts collapse to 1 row
  - Migration 012 produces natural_key columns and UNIQUE indexes
"""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timezone

import pytest
import pytest_asyncio

_has_db = "DATABASE_URL" in os.environ
skip_no_db = pytest.mark.skipif(not _has_db, reason="DATABASE_URL not set")


@pytest_asyncio.fixture
async def idempotency_patient(db_pool):
    pid = str(uuid.uuid4())
    async with db_pool.acquire() as conn:
        await conn.execute(
            """INSERT INTO patients
                   (id, mrn, first_name, last_name, birth_date, gender,
                    is_synthetic, data_source)
               VALUES ($1::uuid, $2, 'Idempotency', 'Test', '1980-01-01',
                       'female', false, 'healthex')""",
            pid, f"IDP-{pid[:8]}",
        )
    yield pid
    async with db_pool.acquire() as conn:
        await conn.execute("DELETE FROM transfer_log WHERE patient_id=$1::uuid", pid)
        await conn.execute("DELETE FROM behavioral_screenings WHERE patient_id=$1::uuid", pid)
        await conn.execute("DELETE FROM biometric_readings WHERE patient_id=$1::uuid", pid)
        await conn.execute("DELETE FROM patient_conditions WHERE patient_id=$1::uuid", pid)
        await conn.execute("DELETE FROM patient_medications WHERE patient_id=$1::uuid", pid)
        await conn.execute("DELETE FROM clinical_events WHERE patient_id=$1::uuid", pid)
        await conn.execute("DELETE FROM patients WHERE id=$1::uuid", pid)


@skip_no_db
class TestNaturalKeyColumns:
    @pytest.mark.asyncio
    async def test_natural_key_columns_exist(self, db_pool):
        async with db_pool.acquire() as conn:
            for table in (
                "patient_conditions", "patient_medications",
                "clinical_events", "behavioral_screenings",
            ):
                row = await conn.fetchrow(
                    """SELECT column_name FROM information_schema.columns
                       WHERE table_name = $1 AND column_name = 'natural_key'""",
                    table,
                )
                assert row is not None, (
                    f"{table}.natural_key column missing — migration 012 not applied"
                )


@skip_no_db
class TestConditionIdempotency:
    @pytest.mark.asyncio
    async def test_duplicate_condition_inserts_collapse(self, db_pool, idempotency_patient):
        pid = idempotency_patient
        async with db_pool.acquire() as conn:
            # First insert
            await conn.execute(
                """INSERT INTO patient_conditions
                       (id, patient_id, code, display, onset_date,
                        clinical_status, data_source)
                   VALUES ($1::uuid, $2::uuid, 'E66.01', 'Morbid obesity',
                           '2017-04-25', 'active', 'healthex')
                   ON CONFLICT (natural_key) DO UPDATE SET
                       clinical_status = EXCLUDED.clinical_status""",
                str(uuid.uuid4()), pid,
            )
            # Second insert with same natural key (code + onset_date)
            await conn.execute(
                """INSERT INTO patient_conditions
                       (id, patient_id, code, display, onset_date,
                        clinical_status, data_source)
                   VALUES ($1::uuid, $2::uuid, 'E66.01', 'Morbid obesity',
                           '2017-04-25', 'resolved', 'healthex')
                   ON CONFLICT (natural_key) DO UPDATE SET
                       clinical_status = EXCLUDED.clinical_status""",
                str(uuid.uuid4()), pid,
            )
            count = await conn.fetchval(
                """SELECT COUNT(*) FROM patient_conditions
                   WHERE patient_id=$1::uuid AND code='E66.01'""",
                pid,
            )
            # Second call updated status to 'resolved'
            updated = await conn.fetchval(
                """SELECT clinical_status FROM patient_conditions
                   WHERE patient_id=$1::uuid AND code='E66.01'""",
                pid,
            )
        assert count == 1, f"duplicate condition not collapsed, got {count} rows"
        assert updated == "resolved"

    @pytest.mark.asyncio
    async def test_different_onset_dates_remain_distinct(self, db_pool, idempotency_patient):
        """Same code with different onset dates are separate episodes."""
        pid = idempotency_patient
        async with db_pool.acquire() as conn:
            await conn.execute(
                """INSERT INTO patient_conditions
                       (id, patient_id, code, display, onset_date,
                        clinical_status, data_source)
                   VALUES ($1::uuid, $2::uuid, 'J45.909', 'Asthma',
                           '2015-06-10', 'active', 'healthex')
                   ON CONFLICT (natural_key) DO NOTHING""",
                str(uuid.uuid4()), pid,
            )
            await conn.execute(
                """INSERT INTO patient_conditions
                       (id, patient_id, code, display, onset_date,
                        clinical_status, data_source)
                   VALUES ($1::uuid, $2::uuid, 'J45.909', 'Asthma',
                           '2020-03-01', 'active', 'healthex')
                   ON CONFLICT (natural_key) DO NOTHING""",
                str(uuid.uuid4()), pid,
            )
            count = await conn.fetchval(
                """SELECT COUNT(*) FROM patient_conditions
                   WHERE patient_id=$1::uuid AND code='J45.909'""",
                pid,
            )
        assert count == 2


@skip_no_db
class TestMedicationIdempotency:
    @pytest.mark.asyncio
    async def test_duplicate_medication_inserts_collapse(self, db_pool, idempotency_patient):
        pid = idempotency_patient
        async with db_pool.acquire() as conn:
            for _ in range(3):
                await conn.execute(
                    """INSERT INTO patient_medications
                           (id, patient_id, code, display, status,
                            authored_on, data_source)
                       VALUES ($1::uuid, $2::uuid, '40790', 'Pantoprazole',
                               'active', '2022-03-10', 'healthex')
                       ON CONFLICT (natural_key) DO UPDATE SET
                           status = EXCLUDED.status""",
                    str(uuid.uuid4()), pid,
                )
            count = await conn.fetchval(
                """SELECT COUNT(*) FROM patient_medications
                   WHERE patient_id=$1::uuid AND code='40790'""",
                pid,
            )
        assert count == 1


@skip_no_db
class TestEncounterIdempotency:
    @pytest.mark.asyncio
    async def test_duplicate_encounter_inserts_collapse(self, db_pool, idempotency_patient):
        pid = idempotency_patient
        async with db_pool.acquire() as conn:
            for _ in range(2):
                await conn.execute(
                    """INSERT INTO clinical_events
                           (id, patient_id, event_type, event_date,
                            description, data_source)
                       VALUES ($1::uuid, $2::uuid, 'office_visit',
                               '2025-07-01 10:00:00+00',
                               'Annual physical', 'healthex')
                       ON CONFLICT (natural_key) DO UPDATE SET
                           data_source = EXCLUDED.data_source""",
                    str(uuid.uuid4()), pid,
                )
            count = await conn.fetchval(
                """SELECT COUNT(*) FROM clinical_events
                   WHERE patient_id=$1::uuid""",
                pid,
            )
        assert count == 1


@skip_no_db
class TestBehavioralScreeningIdempotency:
    @pytest.mark.asyncio
    async def test_duplicate_screening_inserts_collapse(self, db_pool, idempotency_patient):
        pid = idempotency_patient
        import json as _json
        async with db_pool.acquire() as conn:
            adm = datetime(2023, 12, 13, tzinfo=timezone.utc)
            for _ in range(2):
                await conn.execute(
                    """INSERT INTO behavioral_screenings
                           (id, patient_id, instrument_key, domain, loinc_code,
                            score, band, item_answers, triggered_critical,
                            source_type, administered_at, data_source)
                       VALUES ($1::uuid, $2::uuid, 'phq9', 'depression',
                               '44249-1', 6, 'mild',
                               $3::jsonb, $4::jsonb, 'fhir',
                               $5, 'healthex')
                       ON CONFLICT (natural_key) DO NOTHING""",
                    str(uuid.uuid4()), pid,
                    _json.dumps({"9": 1}), _json.dumps([]),
                    adm,
                )
            count = await conn.fetchval(
                """SELECT COUNT(*) FROM behavioral_screenings
                   WHERE patient_id=$1::uuid AND instrument_key='phq9'""",
                pid,
            )
        assert count == 1
