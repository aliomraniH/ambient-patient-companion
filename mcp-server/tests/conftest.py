import pytest
import pytest_asyncio
import asyncio
import asyncpg
import os
import uuid
from datetime import date, timedelta

pytest_plugins = ["pytest_asyncio"]

_has_db = "DATABASE_URL" in os.environ


@pytest.fixture(scope="session")
def event_loop():
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


@pytest_asyncio.fixture(scope="session")
async def db_pool():
    if not _has_db:
        pytest.skip("DATABASE_URL not set — skipping DB tests")
    pool = await asyncpg.create_pool(os.environ["DATABASE_URL"])
    yield pool
    await pool.close()


@pytest_asyncio.fixture
async def test_patient(db_pool):
    pid = str(uuid.uuid4())
    async with db_pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO patients
            (id, mrn, first_name, last_name, birth_date, gender,
             is_synthetic, data_source)
            VALUES ($1, $2, 'Test', 'Patient', '1970-01-01', 'female',
                    true, 'synthea')
            """,
            pid,
            f"MRN-TEST-{pid[:8]}",
        )
    yield pid
    async with db_pool.acquire() as conn:
        await conn.execute("DELETE FROM patients WHERE id=$1", pid)


@pytest_asyncio.fixture
async def caregiver_stress_patient(db_pool):
    """Patient with 7 days of deteriorating signals for crisis tests (S13)."""
    pid = str(uuid.uuid4())
    async with db_pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO patients
            (id, mrn, first_name, last_name, birth_date, gender,
             is_synthetic, data_source)
            VALUES ($1, $2, 'Crisis', 'Patient', '1970-01-01', 'female',
                    true, 'synthea')
            """,
            pid,
            f"MRN-CRISIS-{pid[:8]}",
        )
        for i in range(7):
            day = date.today() - timedelta(days=i)
            await conn.execute(
                """
                INSERT INTO daily_checkins
                (patient_id, checkin_date, mood, energy, stress_level,
                 sleep_hours, data_source)
                VALUES ($1, $2, 'bad', 'very_low', 9, 5.0, 'manual')
                ON CONFLICT DO NOTHING
                """,
                pid,
                day,
            )
            await conn.execute(
                """
                INSERT INTO biometric_readings
                (patient_id, metric_type, value, unit, measured_at, data_source)
                VALUES ($1, 'glucose_fasting', 220, 'mg/dL', NOW(), 'synthea')
                """,
                pid,
            )
    yield pid
    async with db_pool.acquire() as conn:
        await conn.execute("DELETE FROM patients WHERE id=$1", pid)
