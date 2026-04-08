"""Pytest configuration for Phase 1 integration tests."""

import asyncio
import os
import uuid

import asyncpg
import pytest
import pytest_asyncio


@pytest.fixture(scope="session")
def event_loop():
    """Session-scoped event loop so the asyncpg pool is reused across tests."""
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


_has_db = "DATABASE_URL" in os.environ


@pytest_asyncio.fixture(scope="session")
async def db_pool():
    if not _has_db:
        pytest.skip("DATABASE_URL not set — skipping DB integration tests")
    pool = await asyncpg.create_pool(os.environ["DATABASE_URL"])
    yield pool
    await pool.close()


@pytest_asyncio.fixture
async def healthex_patient(db_pool):
    """Insert a minimal HealthEx test patient row; clean up afterwards."""
    pid = str(uuid.uuid4())
    mrn = f"HX-TEST-{pid[:8]}"
    async with db_pool.acquire() as conn:
        await conn.execute(
            """INSERT INTO patients
                   (id, mrn, first_name, last_name, birth_date, gender,
                    is_synthetic, data_source)
               VALUES ($1, $2, 'Test', 'HealthexGap', '1980-01-15', 'female',
                       false, 'healthex')""",
            pid, mrn,
        )
        await conn.execute(
            """INSERT INTO source_freshness
                   (patient_id, source_name, last_ingested_at, records_count, ttl_hours)
               VALUES ($1, 'healthex', NOW(), 0, 24)
               ON CONFLICT (patient_id, source_name) DO NOTHING""",
            pid,
        )
    yield pid
    async with db_pool.acquire() as conn:
        await conn.execute("DELETE FROM transfer_log WHERE patient_id = $1::uuid", pid)
        await conn.execute("DELETE FROM ingestion_plans WHERE patient_id = $1::uuid", pid)
        await conn.execute("DELETE FROM raw_fhir_cache WHERE patient_id = $1::uuid", pid)
        await conn.execute("DELETE FROM biometric_readings WHERE patient_id = $1::uuid", pid)
        await conn.execute("DELETE FROM patient_conditions WHERE patient_id = $1::uuid", pid)
        await conn.execute("DELETE FROM clinical_events WHERE patient_id = $1::uuid", pid)
        await conn.execute("DELETE FROM patient_medications WHERE patient_id = $1::uuid", pid)
        await conn.execute("DELETE FROM source_freshness WHERE patient_id = $1::uuid", pid)
        await conn.execute("DELETE FROM patients WHERE id = $1::uuid", pid)
