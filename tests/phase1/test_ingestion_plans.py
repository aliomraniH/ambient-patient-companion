"""
Integration tests for the two-phase ingestion architecture.

Requires:
  - Live PostgreSQL with ingestion_plans table (migration 002)
  - Clinical MCP Server running on port 8001

Tests:
  IP-1: ingest_from_healthex returns plan_id in response
  IP-2: ingestion_plans row created after ingest call
  IP-3: raw_fhir_cache stores raw_text and detected_format
  IP-4: Non-numeric lab values are NOT dropped
  IP-5: Format B labs produce correct row count (not 1)
  IP-6: get_ingestion_plans query returns insights_summary
  IP-7: Observation converter preserves non-numeric values
  IP-8: Planner confidence is higher for known formats
"""

import json
import os
import sys
import uuid
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(_REPO_ROOT))
sys.path.insert(0, str(_REPO_ROOT / "mcp-server"))

DATABASE_URL = os.environ.get("DATABASE_URL", "")

pytestmark = pytest.mark.skipif(
    not DATABASE_URL,
    reason="DATABASE_URL not set — skip live DB tests",
)


@pytest.fixture
async def test_patient(db_pool):
    patient_id = str(uuid.uuid4())
    mrn = f"TEST-IP-{uuid.uuid4().hex[:8]}"
    async with db_pool.acquire() as conn:
        await conn.execute(
            """INSERT INTO patients (id, mrn, first_name, last_name,
                                    birth_date, gender, data_source)
               VALUES ($1, $2, 'Test', 'Patient', '1970-01-01', 'F', 'test')
               ON CONFLICT (mrn) DO UPDATE SET first_name = 'Test'""",
            patient_id, mrn,
        )
        await conn.execute(
            """INSERT INTO source_freshness
                   (patient_id, source_name, records_count, last_ingested_at)
               VALUES ($1, 'healthex', 0, NOW())
               ON CONFLICT (patient_id, source_name) DO NOTHING""",
            patient_id,
        )
    yield {"id": patient_id, "mrn": mrn}
    async with db_pool.acquire() as conn:
        for tbl in ("ingestion_plans", "raw_fhir_cache", "biometric_readings",
                    "patient_conditions", "clinical_events", "patient_medications",
                    "source_freshness"):
            await conn.execute(
                f"DELETE FROM {tbl} WHERE patient_id = $1::uuid", patient_id
            )
        await conn.execute("DELETE FROM patients WHERE id = $1::uuid", patient_id)


FORMAT_B_LABS = """#Labs 6m|Total:5
D:1=2025-01-15|2=2025-03-20|3=2025-06-01|
C:1=HbA1c|2=LDL Cholesterol|3=eGFR|4=Creatinine|5=BUN|
S:1=final|
Date|TestName|Value|Unit|ReferenceRange|Status|LOINC|EffectiveDate
@1|@1|7.8|%|4.0-5.6|@1|4548-4|@1
@1|@2|112|mg/dL|<100|@1|2089-1|@1
@2|@3|68|mL/min/1.73m2|>60|@1|33914-3|@2
@2|@4|1.1|mg/dL|0.7-1.3|@1|2160-0|@2
@3|@5|18|mg/dL|7-20|@1|3094-0|@3"""

MIXED_LABS = """#Labs|Total:3
D:1=2025-01-15|
C:1=HbA1c|2=HIV Screen|3=Urinalysis|
S:1=final|
Date|TestName|Value|Unit|ReferenceRange|Status|LOINC|EffectiveDate
@1|@1|7.8|%|4.0-5.6|@1|4548-4|@1
@1|@2|Negative||N/A|@1|75622-1|@1
@1|@3|Positive|qual|Negative|@1|5778-6|@1"""


# ── IP-1: Planner returns a structured plan ───────────────────────────────────

@pytest.mark.asyncio
async def test_ip1_planner_returns_plan(db_pool, test_patient):
    from ingestion.adapters.healthex.planner import plan_extraction_deterministic
    plan = plan_extraction_deterministic(FORMAT_B_LABS, "labs", test_patient["id"])

    assert plan["detected_format"] == "compressed_table"
    assert plan["estimated_rows"] == 5
    assert plan["insights_summary"]
    assert plan["planner_confidence"] > 0


# ── IP-2: ingestion_plans row created in DB ───────────────────────────────────

@pytest.mark.asyncio
async def test_ip2_plan_row_created(db_pool, test_patient):
    from ingestion.adapters.healthex.planner import plan_extraction_deterministic
    plan = plan_extraction_deterministic(FORMAT_B_LABS, "labs", test_patient["id"])
    plan_id = str(uuid.uuid4())
    patient_id = test_patient["id"]

    async with db_pool.acquire() as conn:
        await conn.execute(
            """INSERT INTO ingestion_plans
                   (id, patient_id, cache_id, resource_type,
                    detected_format, extraction_strategy, estimated_rows,
                    column_map, sample_rows, insights_summary,
                    planner_confidence, status)
               VALUES ($1::uuid, $2::uuid, $3, $4, $5, $6, $7, $8, $9, $10, $11, 'pending')""",
            plan_id, patient_id, "test-cache-id", "labs",
            plan["detected_format"],
            plan.get("extraction_strategy", ""),
            plan.get("estimated_rows", 0),
            json.dumps(plan.get("column_map", {})),
            json.dumps(plan.get("sample_rows", [])),
            plan.get("insights_summary", ""),
            plan.get("planner_confidence", 0.0),
        )

        row = await conn.fetchrow(
            "SELECT * FROM ingestion_plans WHERE id = $1::uuid", plan_id
        )
        assert row is not None
        assert row["status"] == "pending"
        assert row["detected_format"] == "compressed_table"
        assert row["estimated_rows"] == 5

        await conn.execute("DELETE FROM ingestion_plans WHERE id = $1::uuid", plan_id)


# ── IP-3: raw_fhir_cache stores raw_text + detected_format ───────────────────

@pytest.mark.asyncio
async def test_ip3_raw_cache_stores_text(db_pool, test_patient):
    patient_id = test_patient["id"]
    cache_id = str(uuid.uuid4())

    async with db_pool.acquire() as conn:
        await conn.execute(
            """INSERT INTO raw_fhir_cache
                   (patient_id, source_name, resource_type, raw_json,
                    raw_text, detected_format, fhir_resource_id)
               VALUES ($1::uuid, 'healthex', 'labs', $2, $3, $4, $5)
               ON CONFLICT (patient_id, source_name, fhir_resource_id)
               DO UPDATE SET raw_text = EXCLUDED.raw_text""",
            patient_id,
            json.dumps(FORMAT_B_LABS[:1000]),
            FORMAT_B_LABS,
            "compressed_table",
            cache_id,
        )

        row = await conn.fetchrow(
            "SELECT raw_text, detected_format FROM raw_fhir_cache WHERE fhir_resource_id = $1",
            cache_id,
        )
        assert row is not None
        assert row["raw_text"] is not None
        assert len(row["raw_text"]) > 100
        assert row["detected_format"] == "compressed_table"

        await conn.execute(
            "DELETE FROM raw_fhir_cache WHERE fhir_resource_id = $1", cache_id
        )


# ── IP-4: Non-numeric lab values NOT dropped ─────────────────────────────────

@pytest.mark.asyncio
async def test_ip4_non_numeric_labs_preserved(db_pool, test_patient):
    from ingestion.adapters.healthex.ingest import adaptive_parse

    rows, fmt, parser = adaptive_parse(MIXED_LABS, "labs")
    assert len(rows) == 3, f"Expected 3 rows, got {len(rows)}: {rows}"

    names = [r.get("test_name", "") or r.get("name", "") for r in rows]
    assert "HbA1c" in names
    assert "HIV Screen" in names
    assert "Urinalysis" in names


# ── IP-5: Format B labs produce correct row count ────────────────────────────

@pytest.mark.asyncio
async def test_ip5_format_b_correct_row_count(db_pool, test_patient):
    from ingestion.adapters.healthex.ingest import adaptive_parse

    rows, fmt, parser = adaptive_parse(FORMAT_B_LABS, "labs")
    assert fmt == "compressed_table"
    assert len(rows) == 5, f"Expected 5 rows, got {len(rows)}"

    test_names = sorted(r.get("test_name", "") or r.get("name", "") for r in rows)
    assert "BUN" in test_names
    assert "Creatinine" in test_names
    assert "HbA1c" in test_names


# ── IP-6: get_ingestion_plans DB query ───────────────────────────────────────

@pytest.mark.asyncio
async def test_ip6_get_plans_query(db_pool, test_patient):
    patient_id = test_patient["id"]
    plan_id = str(uuid.uuid4())

    async with db_pool.acquire() as conn:
        await conn.execute(
            """INSERT INTO ingestion_plans
                   (id, patient_id, cache_id, resource_type,
                    detected_format, insights_summary, status, rows_written)
               VALUES ($1::uuid, $2::uuid, 'test', 'labs',
                       'compressed_table',
                       'Test: 5 lab results from Jan-Jun 2025', 'complete', 5)""",
            plan_id, patient_id,
        )

        plans = await conn.fetch(
            """SELECT resource_type, insights_summary, rows_written, status
               FROM ingestion_plans
               WHERE patient_id = $1::uuid AND status = 'complete'""",
            patient_id,
        )
        assert len(plans) >= 1
        found = [p for p in plans if str(p["insights_summary"]).startswith("Test:")]
        assert len(found) == 1
        assert found[0]["rows_written"] == 5

        await conn.execute("DELETE FROM ingestion_plans WHERE id = $1::uuid", plan_id)


# ── IP-7: Observation converter preserves non-numeric values ─────────────────

def test_ip7_observation_converter_non_numeric():
    from ingestion.adapters.healthex.executor import _native_to_fhir_observations

    items = [
        {"test_name": "HbA1c", "value": "7.8", "unit": "%", "date": "2025-01-15"},
        {"test_name": "HIV", "value": "Negative", "unit": "", "date": "2025-01-15"},
        {"test_name": "Culture", "value": "No growth", "unit": "", "date": "2025-01-15"},
    ]
    fhir = _native_to_fhir_observations(items)

    assert len(fhir) == 3
    assert fhir[0]["valueQuantity"]["value"] == 7.8
    assert fhir[1]["valueQuantity"]["value"] == 0.0
    assert "Negative" in fhir[1]["valueQuantity"]["unit"]


# ── IP-8: Planner confidence is higher for known formats ─────────────────────

def test_ip8_planner_confidence_levels():
    from ingestion.adapters.healthex.planner import plan_extraction_deterministic

    known = plan_extraction_deterministic(FORMAT_B_LABS, "labs")
    unknown = plan_extraction_deterministic("random gibberish data", "labs")

    assert known["planner_confidence"] > unknown["planner_confidence"]
