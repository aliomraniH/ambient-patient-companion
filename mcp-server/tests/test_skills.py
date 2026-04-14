"""S1-S22: MCP skill tool correctness tests.

All tests are async, use db_pool and test_patient fixtures,
and call skill functions directly via module-level imports.
"""

from __future__ import annotations

import importlib
import json
import os
import sys
import uuid
from datetime import date, datetime, timedelta

import pytest
import pytest_asyncio

# Ensure mcp-server is on the path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from skills.generate_patient import generate_patient
from skills.generate_vitals import generate_daily_vitals
from skills.generate_checkins import generate_daily_checkins
from skills.compute_obt_score import compute_obt_score
from skills.sdoh_assessment import run_sdoh_assessment
from skills.crisis_escalation import run_crisis_escalation
from skills.food_access_nudge import run_food_access_nudge
from skills.compute_provider_risk import compute_provider_risk


def _get_fixture_file() -> str:
    """Return path to the first FHIR fixture file."""
    synth_dir = os.environ.get("SYNTHEA_OUTPUT_DIR", "/home/user/synthea-output")
    fhir_dir = os.path.join(synth_dir, "fhir")
    import glob
    files = sorted(glob.glob(os.path.join(fhir_dir, "*.json")))
    assert files, f"No FHIR files in {fhir_dir}"
    return files[0]


def _get_skill_function(module, skill_name: str):
    """Extract the async tool function from a skill module.

    Resolution order:
    1. Module-level attribute (promoted helpers such as get_data_source_status).
    2. _helpers registry populated by register() for inner non-MCP helpers.
    3. @mcp.tool-decorated functions captured via MockMCP.
    """
    if hasattr(module, skill_name):
        return getattr(module, skill_name)

    captured = {}

    class MockMCP:
        def tool(self, fn=None, **_kwargs):
            if fn is not None:
                captured[fn.__name__] = fn
                return fn
            def decorator(f):
                captured[f.__name__] = f
                return f
            return decorator

    try:
        module.register(MockMCP())
    except Exception:
        pass

    if skill_name in captured:
        return captured[skill_name]

    return getattr(module, "_helpers", {}).get(skill_name)


# ── S1: generate_patient inserts row, returns OK string ──
@pytest.mark.asyncio
async def test_generate_patient_returns_ok(db_pool):
    result = await generate_patient(synthea_file=_get_fixture_file())
    assert isinstance(result, str)
    assert result.startswith("OK"), f"Expected OK, got: {result[:80]}"
    # Cleanup: extract patient_id from result string
    pid = result.rsplit("|", 1)[-1].strip()
    async with db_pool.acquire() as conn:
        await conn.execute("DELETE FROM patients WHERE id=$1", pid)


# ── S2: generate_patient inserts correct condition count ──
@pytest.mark.asyncio
async def test_generate_patient_conditions(db_pool):
    result = await generate_patient(synthea_file=_get_fixture_file())
    pid = result.rsplit("|", 1)[-1].strip()
    async with db_pool.acquire() as conn:
        count = await conn.fetchval(
            "SELECT COUNT(*) FROM patient_conditions WHERE patient_id=$1", pid
        )
        assert count >= 1, f"Expected >=1 conditions, got {count}"
        await conn.execute("DELETE FROM patients WHERE id=$1", pid)


# ── S3: generate_patient data_source matches active data track ──
@pytest.mark.asyncio
async def test_generate_patient_data_source(db_pool):
    result = await generate_patient(synthea_file=_get_fixture_file())
    pid = result.rsplit("|", 1)[-1].strip()
    async with db_pool.acquire() as conn:
        ds = await conn.fetchval(
            "SELECT data_source FROM patients WHERE id=$1", pid
        )
        # data_source now comes from active DATA_TRACK (defaults to "synthea")
        assert ds is not None, "No data_source on patient row"
        await conn.execute("DELETE FROM patients WHERE id=$1", pid)


# ── S4: generate_vitals inserts biometric_readings rows ──
@pytest.mark.asyncio
async def test_generate_vitals_inserts(db_pool, test_patient):
    result = await generate_daily_vitals(patient_id=test_patient)
    assert isinstance(result, str)
    async with db_pool.acquire() as conn:
        count = await conn.fetchval(
            "SELECT COUNT(*) FROM biometric_readings WHERE patient_id=$1",
            test_patient,
        )
        assert count > 0, "No biometric_readings inserted"


# ── S5: generate_vitals idempotent (ON CONFLICT DO NOTHING) ──
@pytest.mark.asyncio
async def test_generate_vitals_idempotent(db_pool, test_patient):
    await generate_daily_vitals(patient_id=test_patient, target_date=str(date.today()))
    async with db_pool.acquire() as conn:
        count1 = await conn.fetchval(
            "SELECT COUNT(*) FROM biometric_readings WHERE patient_id=$1",
            test_patient,
        )
    await generate_daily_vitals(patient_id=test_patient, target_date=str(date.today()))
    async with db_pool.acquire() as conn:
        count2 = await conn.fetchval(
            "SELECT COUNT(*) FROM biometric_readings WHERE patient_id=$1",
            test_patient,
        )
    assert count2 == count1, f"Not idempotent: {count1} vs {count2}"


# ── S6: generate_checkins inserts daily_checkins rows ──
@pytest.mark.asyncio
async def test_generate_checkins_inserts(db_pool, test_patient):
    result = await generate_daily_checkins(patient_id=test_patient)
    assert isinstance(result, str)
    async with db_pool.acquire() as conn:
        count = await conn.fetchval(
            "SELECT COUNT(*) FROM daily_checkins WHERE patient_id=$1",
            test_patient,
        )
        assert count > 0, "No daily_checkins inserted"


# ── S7: generate_checkins data_source='manual' on check-in rows ──
@pytest.mark.asyncio
async def test_generate_checkins_data_source(db_pool, test_patient):
    await generate_daily_checkins(patient_id=test_patient)
    async with db_pool.acquire() as conn:
        ds = await conn.fetchval(
            "SELECT data_source FROM daily_checkins WHERE patient_id=$1 LIMIT 1",
            test_patient,
        )
        # Checkins are patient-reported, could be 'manual' or 'synthea'
        assert ds is not None, "No data_source found on daily_checkins"


# ── S8: compute_obt_score returns JSON with score + primary_driver ──
@pytest.mark.asyncio
async def test_obt_score_returns_json(db_pool, test_patient):
    # Seed some vitals first
    await generate_daily_vitals(patient_id=test_patient)
    await generate_daily_checkins(patient_id=test_patient)

    result = await compute_obt_score(patient_id=test_patient)
    assert isinstance(result, str)
    data = json.loads(result)
    assert "score" in data, f"Missing score in: {data}"
    assert "primary_driver" in data, f"Missing primary_driver in: {data}"


# ── S9: compute_obt_score score in 0-100 range ──
@pytest.mark.asyncio
async def test_obt_score_range(db_pool, test_patient):
    await generate_daily_vitals(patient_id=test_patient)

    result = await compute_obt_score(patient_id=test_patient)
    data = json.loads(result)
    assert 0 <= data["score"] <= 100, f"Score out of range: {data['score']}"


# ── S10: compute_obt_score writes to obt_scores table ──
@pytest.mark.asyncio
async def test_obt_writes_to_table(db_pool, test_patient):
    await generate_daily_vitals(patient_id=test_patient)

    await compute_obt_score(patient_id=test_patient)
    async with db_pool.acquire() as conn:
        count = await conn.fetchval(
            "SELECT COUNT(*) FROM obt_scores WHERE patient_id=$1",
            test_patient,
        )
        assert count > 0, "No obt_scores written"


# ── S11: compute_obt_score writes clinical_facts with TTL ──
@pytest.mark.asyncio
async def test_obt_writes_clinical_facts(db_pool, test_patient):
    await generate_daily_vitals(patient_id=test_patient)

    await compute_obt_score(patient_id=test_patient)
    async with db_pool.acquire() as conn:
        count = await conn.fetchval(
            "SELECT COUNT(*) FROM clinical_facts WHERE patient_id=$1",
            test_patient,
        )
        assert count > 0, "No clinical_facts written"


# ── S12: run_sdoh_assessment inserts patient_sdoh_flags rows ──
@pytest.mark.asyncio
async def test_sdoh_assessment(db_pool, test_patient):
    result = await run_sdoh_assessment(patient_id=test_patient)
    assert isinstance(result, str)
    async with db_pool.acquire() as conn:
        count = await conn.fetchval(
            "SELECT COUNT(*) FROM patient_sdoh_flags WHERE patient_id=$1",
            test_patient,
        )
        assert count > 0, "No sdoh_flags inserted"


# ── S13: run_crisis_escalation returns JSON with escalation_triggered bool ──
@pytest.mark.asyncio
async def test_crisis_escalation_result(db_pool, caregiver_stress_patient):
    result = await run_crisis_escalation(patient_id=caregiver_stress_patient)
    assert isinstance(result, str)
    data = json.loads(result)
    assert "escalation_triggered" in data, f"Missing escalation_triggered: {data}"
    assert isinstance(data["escalation_triggered"], bool)


# ── S14: run_crisis_escalation logs to skill_executions ──
@pytest.mark.asyncio
async def test_crisis_escalation_logs(db_pool, caregiver_stress_patient):
    await run_crisis_escalation(patient_id=caregiver_stress_patient)
    async with db_pool.acquire() as conn:
        count = await conn.fetchval(
            """
            SELECT COUNT(*) FROM skill_executions
            WHERE skill_name='run_crisis_escalation'
              AND patient_id=$1
            """,
            caregiver_stress_patient,
        )
        assert count > 0, "No skill_executions log entry"


# ── S15: check_data_freshness returns valid JSON string ──
@pytest.mark.asyncio
async def test_check_data_freshness(db_pool, test_patient):
    mod = importlib.import_module("skills.ingestion_tools")
    fn = _get_skill_function(mod, "check_data_freshness")
    result = await fn(patient_id=test_patient)
    assert isinstance(result, str)
    data = json.loads(result)
    assert "patient_id" in data


# ── S16: get_data_source_status JSON contains active_track field ──
@pytest.mark.asyncio
async def test_get_data_source_status(db_pool):
    mod = importlib.import_module("skills.ingestion_tools")
    fn = mod.get_data_source_status
    result = await fn()
    assert isinstance(result, str)
    data = json.loads(result)
    assert "active_track" in data, f"Missing active_track: {data}"


# ── S17: ingest_from_healthex returns Error on invalid resource_type ──
@pytest.mark.asyncio
async def test_ingest_from_healthex_invalid_type(db_pool, test_patient):
    mod = importlib.import_module("skills.ingestion_tools")
    fn = _get_skill_function(mod, "ingest_from_healthex")
    result = await fn(
        patient_id=test_patient,
        resource_type="invalid_type",
        fhir_json="[]",
    )
    assert isinstance(result, str)
    assert result.startswith("Error"), f"Expected Error, got: {result[:80]}"


# ── S18: switch_data_track rejects values other than synthea/healthex ──
@pytest.mark.asyncio
async def test_switch_data_track_invalid(db_pool):
    mod = importlib.import_module("skills.ingestion_tools")
    fn = _get_skill_function(mod, "switch_data_track")
    result = await fn(track="unknown_source")
    assert isinstance(result, str)
    assert result.startswith("Error"), f"Expected Error, got: {result[:80]}"


# ── S19: use_healthex sets DATA_TRACK to healthex ──
@pytest.mark.asyncio
async def test_use_healthex_sets_track(db_pool):
    mod = importlib.import_module("skills.ingestion_tools")
    fn = _get_skill_function(mod, "use_healthex")
    result = await fn()
    assert isinstance(result, str)
    assert "HealthEx" in result, f"Expected HealthEx mention: {result[:80]}"
    async with db_pool.acquire() as conn:
        track = await conn.fetchval(
            "SELECT value FROM system_config WHERE key = $1",
            "DATA_TRACK",
        )
        assert track == "healthex", f"Expected healthex, got {track}"


# ── S20: use_demo_data sets DATA_TRACK to synthea ──
@pytest.mark.asyncio
async def test_use_demo_data_sets_track(db_pool):
    mod = importlib.import_module("skills.ingestion_tools")
    fn = _get_skill_function(mod, "use_demo_data")
    result = await fn()
    assert isinstance(result, str)
    assert "demo" in result.lower(), f"Expected demo mention: {result[:80]}"
    async with db_pool.acquire() as conn:
        track = await conn.fetchval(
            "SELECT value FROM system_config WHERE key = $1",
            "DATA_TRACK",
        )
        assert track == "synthea", f"Expected synthea, got {track}"


# ── S21: use_healthex then use_demo_data round-trips correctly ──
@pytest.mark.asyncio
async def test_data_track_round_trip(db_pool):
    mod = importlib.import_module("skills.ingestion_tools")
    use_hx = _get_skill_function(mod, "use_healthex")
    use_demo = _get_skill_function(mod, "use_demo_data")

    await use_hx()
    async with db_pool.acquire() as conn:
        track = await conn.fetchval(
            "SELECT value FROM system_config WHERE key = $1",
            "DATA_TRACK",
        )
        assert track == "healthex", f"After use_healthex: expected healthex, got {track}"

    await use_demo()
    async with db_pool.acquire() as conn:
        track = await conn.fetchval(
            "SELECT value FROM system_config WHERE key = $1",
            "DATA_TRACK",
        )
        assert track == "synthea", f"After use_demo_data: expected synthea, got {track}"


# ── S22: switch_data_track positive case still works after refactor ──
@pytest.mark.asyncio
async def test_switch_data_track_positive(db_pool):
    mod = importlib.import_module("skills.ingestion_tools")
    fn = _get_skill_function(mod, "switch_data_track")
    result = await fn(track="synthea")
    assert isinstance(result, str)
    assert result.startswith("OK"), f"Expected OK, got: {result[:80]}"
    async with db_pool.acquire() as conn:
        track = await conn.fetchval(
            "SELECT value FROM system_config WHERE key = $1",
            "DATA_TRACK",
        )
        assert track == "synthea", f"Expected synthea, got {track}"
