"""
SLM-1 to SLM-15: Unit tests for mcp-server/skills/slm_companion.py

All DB and HTTP interactions are replaced with AsyncMock / MagicMock.
No real DB connections, no real HF or Modal HTTP calls.
"""

from __future__ import annotations

import json
import os
import sys
from unittest.mock import AsyncMock, MagicMock, patch, call

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


# ── MockMCP — captures @mcp.tool() decorated functions ───────────────────────

class _MockMCP:
    def __init__(self):
        self._tools: dict = {}

    def tool(self, fn=None, **_kwargs):
        if fn is not None:
            self._tools[fn.__name__] = fn
            return fn

        def decorator(f):
            self._tools[f.__name__] = f
            return f

        return decorator


def _capture_tools() -> tuple[_MockMCP, dict]:
    """Register slm_companion with a MockMCP and return (mcp, tools_dict)."""
    import skills.slm_companion as mod
    mcp = _MockMCP()
    mod.register(mcp)
    return mcp, mcp._tools


def _make_pool(**overrides) -> MagicMock:
    """Return a minimal mock asyncpg pool with AsyncMock methods."""
    pool = MagicMock()
    pool.fetchrow = AsyncMock(return_value=None)
    pool.fetch = AsyncMock(return_value=[])
    pool.fetchval = AsyncMock(return_value=1)
    pool.execute = AsyncMock(return_value=None)
    for k, v in overrides.items():
        setattr(pool, k, v)
    return pool


# ─────────────────────────────────────────────────────────────────────────────
# SLM-1 to SLM-4: _classify_cohort() — pure function, no mocks needed
# ─────────────────────────────────────────────────────────────────────────────

from skills.slm_companion import _classify_cohort


def test_classify_cohort_diabetes_bh():
    """SLM-1: E11 + F32 codes → diabetes_bh."""
    assert _classify_cohort(["E11.9", "F32.1"]) == "diabetes_bh"


def test_classify_cohort_diabetes_only():
    """SLM-2: E11 code without any BH code → diabetes."""
    assert _classify_cohort(["E11.0"]) == "diabetes"


def test_classify_cohort_bh_only_returns_none():
    """SLM-3: BH code alone (no diabetes) → None."""
    assert _classify_cohort(["F41.1"]) is None


def test_classify_cohort_empty_returns_none():
    """SLM-4: Empty code list → None."""
    assert _classify_cohort([]) is None


# ─────────────────────────────────────────────────────────────────────────────
# SLM-5 to SLM-8: _resolve_adapter() — mocked pool
# ─────────────────────────────────────────────────────────────────────────────

from skills.slm_companion import _resolve_adapter


@pytest.mark.asyncio
async def test_resolve_adapter_patient_row_exists():
    """SLM-5: Patient adapter row found → (hf_repo, 'patient')."""
    patient_row = {"hf_repo": "org/patient-abc-adapter"}
    pool = _make_pool(fetchrow=AsyncMock(return_value=patient_row))

    repo, adapter_type = await _resolve_adapter(pool, "patient-uuid-123")

    assert repo == "org/patient-abc-adapter"
    assert adapter_type == "patient"


@pytest.mark.asyncio
async def test_resolve_adapter_cohort_fallback():
    """SLM-6: No patient adapter but matching cohort row → (hf_repo, 'cohort')."""
    cohort_row = {"hf_repo": "org/cohort-diabetes-bh-adapter"}
    conditions = [{"code": "E11.9"}, {"code": "F32.1"}]

    # First fetchrow (patient adapter) → None; second (cohort adapter) → row
    fetchrow_mock = AsyncMock(side_effect=[None, cohort_row])
    fetch_mock = AsyncMock(return_value=conditions)
    pool = _make_pool(fetchrow=fetchrow_mock, fetch=fetch_mock)

    repo, adapter_type = await _resolve_adapter(pool, "patient-uuid-456")

    assert repo == "org/cohort-diabetes-bh-adapter"
    assert adapter_type == "cohort"


@pytest.mark.asyncio
async def test_resolve_adapter_base_model_fallback():
    """SLM-7: No patient adapter, no matching conditions → ('tgi', 'base')."""
    pool = _make_pool(
        fetchrow=AsyncMock(return_value=None),
        fetch=AsyncMock(return_value=[]),  # no conditions → cohort=None
    )

    repo, adapter_type = await _resolve_adapter(pool, "patient-uuid-789")

    assert repo == "tgi"
    assert adapter_type == "base"


@pytest.mark.asyncio
async def test_resolve_adapter_priority_patient_over_cohort():
    """SLM-8: Patient adapter found → conditions query never reached."""
    patient_row = {"hf_repo": "org/patient-high-prio-adapter"}
    fetch_mock = AsyncMock(return_value=[])
    pool = _make_pool(
        fetchrow=AsyncMock(return_value=patient_row),
        fetch=fetch_mock,
    )

    repo, adapter_type = await _resolve_adapter(pool, "patient-uuid-prio")

    assert adapter_type == "patient"
    fetch_mock.assert_not_called()  # conditions lookup must be skipped


# ─────────────────────────────────────────────────────────────────────────────
# SLM-9 to SLM-11: flag_adapter_for_update tool
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_flag_adapter_for_update_urgent_cohort():
    """SLM-9: urgent cohort job → queued with status=pending, created_by=claude_mcp."""
    pool = _make_pool(
        fetchrow=AsyncMock(return_value={"hf_repo": "org/cohort-diabetes-bh-adapter"}),
        fetchval=AsyncMock(return_value=42),
        execute=AsyncMock(return_value=None),
    )
    _, tools = _capture_tools()

    with patch("skills.slm_companion._get_pool", AsyncMock(return_value=pool)):
        result = json.loads(await tools["flag_adapter_for_update"](
            reason="test urgent run",
            cohort_name="diabetes_bh",
            priority="urgent",
        ))

    assert result["status"] == "queued"
    assert result["priority"] == "urgent"
    assert result["queue_id"] == 42

    # Confirm the INSERT SQL contains status='pending' and created_by='claude_mcp'
    insert_sql = pool.fetchval.call_args.args[0]
    assert "'pending'" in insert_sql
    assert "'claude_mcp'" in insert_sql


@pytest.mark.asyncio
async def test_flag_adapter_for_update_requires_patient_or_cohort():
    """SLM-10: Neither patient_id nor cohort_name → returns error dict."""
    pool = _make_pool()
    _, tools = _capture_tools()

    with patch("skills.slm_companion._get_pool", AsyncMock(return_value=pool)):
        result = json.loads(await tools["flag_adapter_for_update"](reason="oops"))

    assert "error" in result


@pytest.mark.asyncio
async def test_flag_adapter_for_update_normal_priority_scheduled_nightly():
    """SLM-11: normal priority → scheduled note references 02:00 UTC."""
    pool = _make_pool(
        fetchrow=AsyncMock(return_value={"hf_repo": "org/cohort-diabetes-bh-adapter"}),
        fetchval=AsyncMock(return_value=7),
        execute=AsyncMock(return_value=None),
    )
    _, tools = _capture_tools()

    with patch("skills.slm_companion._get_pool", AsyncMock(return_value=pool)):
        result = json.loads(await tools["flag_adapter_for_update"](
            reason="scheduled retrain",
            cohort_name="diabetes_bh",
            priority="normal",
        ))

    assert result["priority"] == "normal"
    assert "UTC" in result["scheduled"]


# ─────────────────────────────────────────────────────────────────────────────
# SLM-12: get_cohort_corpus_stats — 0 cohort patients
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_get_cohort_corpus_stats_no_patients():
    """SLM-12: 0 cohort patients → cohort_patients=0, ready_for_training=False."""
    pool = _make_pool(fetch=AsyncMock(return_value=[]))
    _, tools = _capture_tools()

    with patch("skills.slm_companion._get_pool", AsyncMock(return_value=pool)):
        result = json.loads(await tools["get_cohort_corpus_stats"](
            cohort_name="diabetes_bh",
        ))

    assert result["cohort_patients"] == 0
    assert result["ready_for_training"] is False


# ─────────────────────────────────────────────────────────────────────────────
# SLM-13 to SLM-15: Module-level constants and callables
# ─────────────────────────────────────────────────────────────────────────────

def test_watcher_interval_is_900():
    """SLM-13: WATCHER_INTERVAL must be 900 seconds (15 min)."""
    import skills.slm_companion as mod
    assert mod.WATCHER_INTERVAL == 900


def test_register_watchers_is_callable():
    """SLM-14: register_watchers must be exported and callable."""
    import skills.slm_companion as mod
    assert hasattr(mod, "register_watchers")
    assert callable(mod.register_watchers)


def test_register_is_callable():
    """SLM-15: register must be exported and callable."""
    import skills.slm_companion as mod
    assert hasattr(mod, "register")
    assert callable(mod.register)
