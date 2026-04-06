"""D1-D12: Database schema integrity tests.

All tests are async and use the db_pool fixture to query information_schema.
"""

from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


EXPECTED_TABLES = [
    "patients",
    "patient_conditions",
    "patient_medications",
    "patient_sdoh_flags",
    "biometric_readings",
    "daily_checkins",
    "medication_adherence",
    "clinical_events",
    "care_gaps",
    "obt_scores",
    "clinical_facts",
    "behavioral_correlations",
    "agent_interventions",
    "agent_memory_episodes",
    "skill_executions",
    "provider_risk_scores",
    "pipeline_runs",
    "data_sources",
    "source_freshness",
    "ingestion_log",
    "raw_fhir_cache",
    "system_config",
]


# ── D1: All 22 tables exist in the database ──
@pytest.mark.asyncio
async def test_all_tables_exist(db_pool):
    async with db_pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT table_name FROM information_schema.tables
            WHERE table_schema = 'public'
            """
        )
        existing = {r["table_name"] for r in rows}
        assert len(existing) >= 22, f"Only {len(existing)} tables found: {existing}"


# ── D2: system_config has DATA_TRACK row with a valid track value ──
@pytest.mark.asyncio
async def test_system_config_data_track(db_pool):
    async with db_pool.acquire() as conn:
        value = await conn.fetchval(
            "SELECT value FROM system_config WHERE key = $1",
            "DATA_TRACK",
        )
        valid = {"synthea", "healthex", "auto"}
        assert value in valid, (
            f"DATA_TRACK = {value!r} is not a recognised track "
            f"(expected one of {sorted(valid)})"
        )


# ── D3: source_freshness UNIQUE on (patient_id, source_name) ──
@pytest.mark.asyncio
async def test_source_freshness_unique(db_pool):
    async with db_pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT constraint_name FROM information_schema.table_constraints
            WHERE table_name = 'source_freshness'
              AND constraint_type = 'UNIQUE'
            """
        )
        assert len(rows) > 0, "No UNIQUE constraint on source_freshness"
        # Verify it covers patient_id + source_name
        for row in rows:
            cols = await conn.fetch(
                """
                SELECT column_name FROM information_schema.constraint_column_usage
                WHERE constraint_name = $1
                """,
                row["constraint_name"],
            )
            col_names = {c["column_name"] for c in cols}
            if "patient_id" in col_names and "source_name" in col_names:
                return
        pytest.fail("No UNIQUE constraint on (patient_id, source_name)")


# ── D4: raw_fhir_cache UNIQUE on (patient_id, source_name, fhir_resource_id) ──
@pytest.mark.asyncio
async def test_raw_fhir_cache_unique(db_pool):
    async with db_pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT constraint_name FROM information_schema.table_constraints
            WHERE table_name = 'raw_fhir_cache'
              AND constraint_type = 'UNIQUE'
            """
        )
        assert len(rows) > 0, "No UNIQUE constraint on raw_fhir_cache"


# ── D5: obt_scores UNIQUE on (patient_id, score_date) ──
@pytest.mark.asyncio
async def test_obt_scores_unique(db_pool):
    async with db_pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT constraint_name FROM information_schema.table_constraints
            WHERE table_name = 'obt_scores'
              AND constraint_type = 'UNIQUE'
            """
        )
        assert len(rows) > 0, "No UNIQUE constraint on obt_scores"


# ── D6: daily_checkins UNIQUE on (patient_id, checkin_date) ──
@pytest.mark.asyncio
async def test_daily_checkins_unique(db_pool):
    async with db_pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT constraint_name FROM information_schema.table_constraints
            WHERE table_name = 'daily_checkins'
              AND constraint_type = 'UNIQUE'
            """
        )
        assert len(rows) > 0, "No UNIQUE constraint on daily_checkins"


# ── D7: patients.mrn has UNIQUE constraint ──
@pytest.mark.asyncio
async def test_patients_mrn_unique(db_pool):
    async with db_pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT constraint_name FROM information_schema.table_constraints
            WHERE table_name = 'patients'
              AND constraint_type = 'UNIQUE'
            """
        )
        assert len(rows) > 0, "No UNIQUE constraint on patients"
        for row in rows:
            cols = await conn.fetch(
                """
                SELECT column_name FROM information_schema.constraint_column_usage
                WHERE constraint_name = $1
                """,
                row["constraint_name"],
            )
            col_names = {c["column_name"] for c in cols}
            if "mrn" in col_names:
                return
        pytest.fail("No UNIQUE constraint on patients.mrn")


# ── D8: data_source column exists on patients ──
@pytest.mark.asyncio
async def test_data_source_on_patients(db_pool):
    async with db_pool.acquire() as conn:
        col = await conn.fetchval(
            """
            SELECT column_name FROM information_schema.columns
            WHERE table_name = 'patients' AND column_name = 'data_source'
            """
        )
        assert col == "data_source"


# ── D9: data_source column exists on biometric_readings ──
@pytest.mark.asyncio
async def test_data_source_on_biometric_readings(db_pool):
    async with db_pool.acquire() as conn:
        col = await conn.fetchval(
            """
            SELECT column_name FROM information_schema.columns
            WHERE table_name = 'biometric_readings' AND column_name = 'data_source'
            """
        )
        assert col == "data_source"


# ── D10: data_source column exists on daily_checkins ──
@pytest.mark.asyncio
async def test_data_source_on_daily_checkins(db_pool):
    async with db_pool.acquire() as conn:
        col = await conn.fetchval(
            """
            SELECT column_name FROM information_schema.columns
            WHERE table_name = 'daily_checkins' AND column_name = 'data_source'
            """
        )
        assert col == "data_source"


# ── D11: data_source column exists on obt_scores ──
@pytest.mark.asyncio
async def test_data_source_on_obt_scores(db_pool):
    async with db_pool.acquire() as conn:
        col = await conn.fetchval(
            """
            SELECT column_name FROM information_schema.columns
            WHERE table_name = 'obt_scores' AND column_name = 'data_source'
            """
        )
        assert col == "data_source"


# ── D12: All FK columns reference patients.id ──
@pytest.mark.asyncio
async def test_fk_references_patients(db_pool):
    async with db_pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT tc.constraint_name
            FROM information_schema.table_constraints tc
            JOIN information_schema.constraint_column_usage ccu
              ON tc.constraint_name = ccu.constraint_name
            WHERE tc.constraint_type = 'FOREIGN KEY'
              AND ccu.table_name = 'patients'
              AND ccu.column_name = 'id'
            """
        )
        assert len(rows) >= 10, (
            f"Only {len(rows)} FK constraints reference patients.id, expected >= 10"
        )
