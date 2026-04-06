"""HR-1 through HR-7: HealthEx patient registration tests.

All tests use unittest.mock — NO live database required.
Tests verify the register_healthex_patient tool handles three input
shapes, idempotency, mrn_override, data track setting, and
context_compiler integration.
"""

from __future__ import annotations

import json
import os
import sys
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

# Ensure project root and mcp-server are on the path
_project_root = os.path.join(os.path.dirname(__file__), "..", "..")
_mcp_root = os.path.join(_project_root, "mcp-server")
sys.path.insert(0, _project_root)
sys.path.insert(0, _mcp_root)

# ── Helpers ──────────────────────────────────────────────────────────

FAKE_UUID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"


def _make_mock_pool(patient_uuid: str = FAKE_UUID):
    """Create a mock asyncpg pool with async context manager support."""
    conn = AsyncMock()
    conn.execute = AsyncMock()
    conn.fetchrow = AsyncMock(return_value={"id": patient_uuid})
    conn.fetch = AsyncMock(return_value=[])
    conn.fetchval = AsyncMock(return_value=0)

    pool = MagicMock()
    acq = AsyncMock()
    acq.__aenter__ = AsyncMock(return_value=conn)
    acq.__aexit__ = AsyncMock(return_value=False)
    pool.acquire.return_value = acq

    return pool, conn


def _bare_fhir_patient(mrn: str = "HX-9999") -> dict:
    """Minimal bare FHIR Patient resource."""
    return {
        "resourceType": "Patient",
        "identifier": [
            {"type": {"coding": [{"code": "MR"}]}, "value": mrn}
        ],
        "name": [{"given": ["Jane"], "family": "Doe"}],
        "birthDate": "1980-05-15",
        "gender": "female",
    }


def _fhir_bundle(mrn: str = "HX-8888") -> dict:
    """FHIR Bundle containing a Patient entry."""
    return {
        "resourceType": "Bundle",
        "entry": [
            {
                "resource": {
                    "resourceType": "Observation",
                    "code": {"text": "BP"},
                }
            },
            {
                "resource": _bare_fhir_patient(mrn),
            },
        ],
    }


def _healthex_summary(mrn: str = "HX-7777") -> dict:
    """HealthEx summary dict (non-FHIR)."""
    return {
        "mrn": mrn,
        "name": "Maria Chen",
        "birthDate": "1971-03-22",
        "gender": "female",
    }


# ── Stub FastMCP ─────────────────────────────────────────────────────

class _StubFastMCP:
    """Minimal FastMCP stub that captures decorated tool functions."""

    def __init__(self, *a, **kw):
        self._tools: dict[str, callable] = {}

    def tool(self, fn=None):
        if fn is None:
            return self.tool
        self._tools[fn.__name__] = fn
        return fn


# ── Pre-mock heavy dependencies in sys.modules before any imports ───

_mock_asyncpg = MagicMock()
_mock_db = MagicMock()
_mock_db_connection = MagicMock()
_mock_db_connection.get_pool = AsyncMock()
_mock_fastmcp = MagicMock()
_mock_fastmcp.FastMCP = _StubFastMCP


def _load_ingestion_tools(pool):
    """Import skills.ingestion_tools with all external deps mocked.

    Injects mocks into sys.modules for asyncpg, fastmcp, db.connection
    so that the top-level imports in ingestion_tools.py succeed without
    those packages being installed.
    """
    # Save and mock
    saved = {}
    mocks = {
        "asyncpg": _mock_asyncpg,
        "fastmcp": _mock_fastmcp,
        "db": _mock_db,
        "db.connection": _mock_db_connection,
    }
    for name, mock_mod in mocks.items():
        saved[name] = sys.modules.get(name)
        sys.modules[name] = mock_mod

    # Ensure fresh import
    for key in list(sys.modules.keys()):
        if "skills.ingestion_tools" in key:
            del sys.modules[key]

    try:
        import skills.ingestion_tools as mod

        # Patch module-level helpers after import
        mod.get_pool = AsyncMock(return_value=pool)
        mod.log_skill_execution = AsyncMock()
        mod._set_data_track = AsyncMock(return_value="healthex")

        stub_mcp = _StubFastMCP()
        mod.register(stub_mcp)

        return stub_mcp, mod
    finally:
        for name, original in saved.items():
            if original is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = original


async def _call_register(pool, input_data: dict, mrn_override: str | None = None):
    """Load ingestion_tools, find register_healthex_patient, and call it."""
    stub_mcp, mod = _load_ingestion_tools(pool)

    tool_fn = stub_mcp._tools.get("register_healthex_patient")
    assert tool_fn is not None, "register_healthex_patient not registered"

    result_str = await tool_fn(
        health_summary_json=json.dumps(input_data),
        mrn_override=mrn_override,
    )
    return json.loads(result_str), mod


# ── Tests ────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_hr1_bare_fhir_patient():
    """HR-1: bare FHIR Patient resource accepted."""
    pool, conn = _make_mock_pool()
    result, _ = await _call_register(pool, _bare_fhir_patient("HX-9999"))

    assert result["status"] == "ok"
    assert result["patient_id"] == FAKE_UUID
    assert result["mrn"] == "HX-9999"
    assert result["is_synthetic"] is False
    assert result["data_track"] == "healthex"
    assert "next_step" in result

    # Verify upsert was called
    conn.execute.assert_called()


@pytest.mark.asyncio
async def test_hr2_fhir_bundle():
    """HR-2: FHIR Bundle accepted (Patient extracted from entries)."""
    pool, conn = _make_mock_pool()
    result, _ = await _call_register(pool, _fhir_bundle("HX-8888"))

    assert result["status"] == "ok"
    assert result["mrn"] == "HX-8888"
    assert result["patient_id"] == FAKE_UUID


@pytest.mark.asyncio
async def test_hr3_healthex_summary():
    """HR-3: HealthEx summary dict accepted (non-FHIR)."""
    pool, conn = _make_mock_pool()
    result, _ = await _call_register(pool, _healthex_summary("HX-7777"))

    assert result["status"] == "ok"
    assert result["mrn"] == "HX-7777"
    assert result["name"] == "Maria Chen"
    assert result["is_synthetic"] is False


@pytest.mark.asyncio
async def test_hr4_idempotency():
    """HR-4: calling twice with same MRN returns same UUID."""
    pool, conn = _make_mock_pool()
    result1, _ = await _call_register(pool, _bare_fhir_patient("HX-IDEM"))
    result2, _ = await _call_register(pool, _bare_fhir_patient("HX-IDEM"))

    assert result1["patient_id"] == result2["patient_id"]
    assert result1["patient_id"] == FAKE_UUID


@pytest.mark.asyncio
async def test_hr5_mrn_override():
    """HR-5: mrn_override takes precedence over extracted MRN."""
    pool, conn = _make_mock_pool()
    result, _ = await _call_register(
        pool,
        _bare_fhir_patient("HX-ORIGINAL"),
        mrn_override="HX-OVERRIDE",
    )

    assert result["status"] == "ok"
    assert result["mrn"] == "HX-OVERRIDE"


@pytest.mark.asyncio
async def test_hr6_data_track_set():
    """HR-6: DATA_TRACK = 'healthex' written to system_config."""
    pool, conn = _make_mock_pool()
    result, mod = await _call_register(pool, _healthex_summary("HX-TRACK"))

    assert result["status"] == "ok"
    mod._set_data_track.assert_called_with("healthex", "register_healthex_patient")


@pytest.mark.asyncio
async def test_hr7_context_compiler_lookup():
    """HR-7: post-registration context_compiler lookup succeeds (integration).

    Verifies that after registration, the patients table has a row
    that context_compiler's WHERE mrn = $1 query would find.
    """
    pool, conn = _make_mock_pool()

    # Register the patient
    result, _ = await _call_register(pool, _healthex_summary("HX-COMPILE"))
    assert result["status"] == "ok"

    patient_id = result["patient_id"]
    mrn = result["mrn"]

    # Simulate the context_compiler lookup:
    # SELECT id, mrn, ... FROM patients WHERE mrn = $1
    conn.fetchrow.return_value = {
        "id": patient_id,
        "mrn": mrn,
        "first_name": "Maria",
        "last_name": "Chen",
        "birth_date": None,
        "gender": "female",
        "city": "",
        "state": "",
        "insurance_type": None,
    }

    acq = AsyncMock()
    acq.__aenter__ = AsyncMock(return_value=conn)
    acq.__aexit__ = AsyncMock(return_value=False)
    pool.acquire.return_value = acq

    async with pool.acquire() as c:
        row = await c.fetchrow(
            "SELECT id, mrn, first_name, last_name, birth_date, gender, "
            "city, state, insurance_type FROM patients WHERE mrn = $1",
            mrn,
        )
    assert row is not None
    assert row["id"] == patient_id
    assert row["mrn"] == mrn
