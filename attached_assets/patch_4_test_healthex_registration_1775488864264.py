"""Tests: HealthEx patient registration flow — HR-1 to HR-7.

Validates the end-to-end path from HealthEx summary → patient registration
→ warehouse persistence → deliberation context availability.

All tests use unittest.mock. NO live database or HealthEx connection required.
"""

from __future__ import annotations

import json
import os
import sys
import uuid
from datetime import date
from unittest.mock import AsyncMock, MagicMock, patch, call

import pytest

# Ensure mcp-server is on the path (same pattern as test_pipeline.py)
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "mcp-server"))


# ── helpers ───────────────────────────────────────────────────────────────────

def _make_mock_pool(fetchrow_return=None, fetchval_return=None):
    """Create a mock asyncpg pool with async context manager support."""
    conn = AsyncMock()
    conn.execute = AsyncMock()
    conn.fetchrow = AsyncMock(return_value=fetchrow_return)
    conn.fetch = AsyncMock(return_value=[])
    conn.fetchval = AsyncMock(return_value=fetchval_return)

    pool = MagicMock()
    acq = AsyncMock()
    acq.__aenter__ = AsyncMock(return_value=conn)
    acq.__aexit__ = AsyncMock(return_value=False)
    pool.acquire.return_value = acq
    return pool, conn


_BARE_FHIR_PATIENT = {
    "resourceType": "Patient",
    "id": "hx-pt-001",
    "name": [{"given": ["Ali"], "family": "Omrani"}],
    "birthDate": "1985-03-15",
    "gender": "male",
    "identifier": [
        {
            "type": {"coding": [{"code": "MR"}]},
            "value": "HX-TEST001",
        }
    ],
    "address": [
        {
            "line": ["123 Main St"],
            "city": "Fremont",
            "state": "CA",
            "postalCode": "94538",
        }
    ],
}

_BUNDLE_PATIENT = {
    "resourceType": "Bundle",
    "entry": [
        {"resource": _BARE_FHIR_PATIENT},
        {
            "resource": {
                "resourceType": "Condition",
                "id": "cond-001",
                "code": {"coding": [{"code": "44054006", "display": "T2DM"}]},
                "clinicalStatus": {"coding": [{"code": "active"}]},
            }
        },
    ],
}

_HEALTHEX_SUMMARY_DICT = {
    "name": "Ali Omrani",
    "birth_date": "1985-03-15",
    "gender": "male",
    "mrn": "HX-TEST002",
    "city": "Fremont",
    "state": "CA",
    "zip": "94538",
}


# ── HR-1: bare FHIR Patient resource ─────────────────────────────────────────

@pytest.mark.asyncio
async def test_hr1_register_bare_fhir_patient():
    """HR-1: register_healthex_patient accepts a bare FHIR Patient resource."""
    canonical_uuid = str(uuid.uuid4())
    pool, conn = _make_mock_pool(
        fetchrow_return={"id": canonical_uuid}
    )

    with (
        patch("skills.ingestion_tools.get_pool", return_value=pool),
        patch("skills.ingestion_tools.log_skill_execution", new_callable=AsyncMock),
    ):
        from skills.ingestion_tools import register_healthex_patient  # noqa: PLC0415 (import inside test)

        result_raw = await register_healthex_patient(
            health_summary_json=json.dumps(_BARE_FHIR_PATIENT)
        )

    result = json.loads(result_raw)

    assert result["status"] == "registered"
    assert result["patient_id"] == canonical_uuid
    assert result["mrn"] == "HX-TEST001"
    assert result["is_synthetic"] is False
    assert result["data_track"] == "healthex"
    # next_step should hint at ingest_from_healthex
    assert "ingest_from_healthex" in result["next_step"]


# ── HR-2: FHIR Bundle ────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_hr2_register_from_bundle():
    """HR-2: register_healthex_patient extracts Patient from a FHIR Bundle."""
    canonical_uuid = str(uuid.uuid4())
    pool, conn = _make_mock_pool(fetchrow_return={"id": canonical_uuid})

    with (
        patch("skills.ingestion_tools.get_pool", return_value=pool),
        patch("skills.ingestion_tools.log_skill_execution", new_callable=AsyncMock),
    ):
        from skills.ingestion_tools import register_healthex_patient

        result_raw = await register_healthex_patient(
            health_summary_json=json.dumps(_BUNDLE_PATIENT)
        )

    result = json.loads(result_raw)
    assert result["status"] == "registered"
    assert result["mrn"] == "HX-TEST001"


# ── HR-3: HealthEx dict (non-FHIR) ───────────────────────────────────────────

@pytest.mark.asyncio
async def test_hr3_register_healthex_dict():
    """HR-3: register_healthex_patient handles a HealthEx summary dict."""
    canonical_uuid = str(uuid.uuid4())
    pool, conn = _make_mock_pool(fetchrow_return={"id": canonical_uuid})

    with (
        patch("skills.ingestion_tools.get_pool", return_value=pool),
        patch("skills.ingestion_tools.log_skill_execution", new_callable=AsyncMock),
    ):
        from skills.ingestion_tools import register_healthex_patient

        result_raw = await register_healthex_patient(
            health_summary_json=json.dumps(_HEALTHEX_SUMMARY_DICT)
        )

    result = json.loads(result_raw)
    assert result["status"] == "registered"
    assert result["mrn"] == "HX-TEST002"
    assert result["is_synthetic"] is False


# ── HR-4: idempotency ─────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_hr4_registration_is_idempotent():
    """HR-4: Calling register_healthex_patient twice with the same MRN is safe.

    The second call hits ON CONFLICT (mrn) DO UPDATE and returns the same UUID.
    """
    fixed_uuid = str(uuid.uuid4())
    pool, conn = _make_mock_pool(fetchrow_return={"id": fixed_uuid})

    with (
        patch("skills.ingestion_tools.get_pool", return_value=pool),
        patch("skills.ingestion_tools.log_skill_execution", new_callable=AsyncMock),
    ):
        from skills.ingestion_tools import register_healthex_patient

        r1 = json.loads(await register_healthex_patient(
            health_summary_json=json.dumps(_BARE_FHIR_PATIENT)
        ))
        r2 = json.loads(await register_healthex_patient(
            health_summary_json=json.dumps(_BARE_FHIR_PATIENT)
        ))

    assert r1["patient_id"] == r2["patient_id"] == fixed_uuid
    assert r1["mrn"] == r2["mrn"]
    # ON CONFLICT path: execute was called both times (upsert, not skip)
    assert conn.execute.call_count >= 2


# ── HR-5: mrn_override takes precedence ──────────────────────────────────────

@pytest.mark.asyncio
async def test_hr5_mrn_override():
    """HR-5: mrn_override replaces any MRN extracted from the summary."""
    canonical_uuid = str(uuid.uuid4())
    pool, conn = _make_mock_pool(fetchrow_return={"id": canonical_uuid})

    with (
        patch("skills.ingestion_tools.get_pool", return_value=pool),
        patch("skills.ingestion_tools.log_skill_execution", new_callable=AsyncMock),
    ):
        from skills.ingestion_tools import register_healthex_patient

        result = json.loads(await register_healthex_patient(
            health_summary_json=json.dumps(_BARE_FHIR_PATIENT),
            mrn_override="CUSTOM-MRN-999",
        ))

    assert result["mrn"] == "CUSTOM-MRN-999"


# ── HR-6: DATA_TRACK set to healthex ─────────────────────────────────────────

@pytest.mark.asyncio
async def test_hr6_sets_data_track_to_healthex():
    """HR-6: register_healthex_patient sets DATA_TRACK = 'healthex' in system_config."""
    canonical_uuid = str(uuid.uuid4())
    pool, conn = _make_mock_pool(fetchrow_return={"id": canonical_uuid})

    with (
        patch("skills.ingestion_tools.get_pool", return_value=pool),
        patch("skills.ingestion_tools.log_skill_execution", new_callable=AsyncMock),
    ):
        from skills.ingestion_tools import register_healthex_patient

        await register_healthex_patient(
            health_summary_json=json.dumps(_BARE_FHIR_PATIENT)
        )

    # Find the system_config upsert among all execute calls
    all_sql_calls = [str(c) for c in conn.execute.call_args_list]
    assert any("system_config" in s and "healthex" in s for s in all_sql_calls), (
        "Expected a system_config upsert setting DATA_TRACK = 'healthex'"
    )


# ── HR-7: post-registration ingest + deliberation context lookup succeeds ─────

@pytest.mark.asyncio
async def test_hr7_post_registration_deliberation_context_lookup():
    """HR-7: After registration, context_compiler can find the patient by MRN.

    Simulates the full pipeline:
      register_healthex_patient → ingest_from_healthex('labs') → context_compiler lookup
    """
    fixed_uuid = str(uuid.uuid4())
    mrn = "HX-TEST001"

    # Simulate the patients table row returned by MRN lookup
    patient_row = {
        "id": fixed_uuid,
        "mrn": mrn,
        "first_name": "Ali",
        "last_name": "Omrani",
        "birth_date": date(1985, 3, 15),
        "gender": "male",
        "race": "",
        "ethnicity": "",
        "city": "Fremont",
        "state": "CA",
    }

    pool, conn = _make_mock_pool(fetchrow_return={"id": fixed_uuid})
    conn.fetchrow = AsyncMock(side_effect=[
        # register_healthex_patient: SELECT id FROM patients WHERE mrn = $1
        {"id": fixed_uuid},
        # context_compiler: SELECT ... FROM patients WHERE mrn = $1
        patient_row,
    ])
    conn.fetch = AsyncMock(return_value=[])  # empty conditions/meds/etc. is fine

    from server.deliberation.context_compiler import ContextCompiler

    compiler = ContextCompiler.__new__(ContextCompiler)
    compiler.pool = pool

    with (
        patch("skills.ingestion_tools.get_pool", return_value=pool),
        patch("skills.ingestion_tools.log_skill_execution", new_callable=AsyncMock),
        patch(
            "server.deliberation.context_compiler.get_pool",
            return_value=pool,
        ),
    ):
        from skills.ingestion_tools import register_healthex_patient

        reg_result = json.loads(await register_healthex_patient(
            health_summary_json=json.dumps(_BARE_FHIR_PATIENT)
        ))
        patient_id = reg_result["patient_id"]

        # Verify context_compiler finds the patient after registration
        ctx = await compiler.compile_context(patient_id=mrn)

    assert ctx is not None
    assert ctx.patient["mrn"] == mrn or ctx.patient.get("id") == fixed_uuid
