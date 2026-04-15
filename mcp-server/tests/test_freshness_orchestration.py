"""Tests for freshness orchestration: check_data_freshness enhancements,
_is_stale helper, _get_skill_freshness, _get_deliberation_freshness,
and the orchestrate_refresh pipeline.
"""

from __future__ import annotations

import json
import os
import sys
import uuid
from datetime import date, datetime, timedelta
from unittest.mock import patch, MagicMock

import pytest
import pytest_asyncio

# Ensure mcp-server is on the path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from skills.ingestion_tools import (
    FRESHNESS_TTL,
    _get_deliberation_freshness,
    _get_skill_freshness,
    _is_stale,
)


# ---------------------------------------------------------------------------
# Unit tests for _is_stale helper
# ---------------------------------------------------------------------------


class TestIsStale:
    def test_none_is_stale(self):
        assert _is_stale(None, 24) is True

    def test_recent_is_fresh(self):
        recent = datetime.utcnow() - timedelta(hours=1)
        assert _is_stale(recent, 24) is False

    def test_old_is_stale(self):
        old = datetime.utcnow() - timedelta(hours=25)
        assert _is_stale(old, 24) is True

    def test_exact_boundary_is_stale(self):
        # At exactly the TTL boundary, should be stale (>=)
        boundary = datetime.utcnow() - timedelta(hours=24)
        assert _is_stale(boundary, 24) is True

    def test_just_under_boundary_is_fresh(self):
        just_under = datetime.utcnow() - timedelta(hours=23, minutes=59)
        assert _is_stale(just_under, 24) is False

    def test_timezone_aware_datetime(self):
        """_is_stale should handle tz-aware datetimes by stripping tzinfo."""
        from datetime import timezone

        aware = datetime.now(timezone.utc) - timedelta(hours=1)
        assert _is_stale(aware, 24) is False

    def test_zero_ttl_always_stale(self):
        now = datetime.utcnow()
        assert _is_stale(now, 0) is True


# ---------------------------------------------------------------------------
# Regression guard: register_healthex_patient must initialise
# source_freshness.last_ingested_at as NULL, not NOW(). Writing NOW() on
# registration makes a brand-new patient look "just ingested" so the very
# first orchestrate_refresh silently skips the ingest phase (duration_ms: 0).
# ---------------------------------------------------------------------------


class TestRegistrationFreshnessInit:
    @staticmethod
    def _read(relpath: str) -> str:
        import pathlib
        return pathlib.Path(
            os.path.join(os.path.dirname(__file__), "..", "..", relpath)
        ).resolve().read_text()

    def test_ingestion_tools_register_uses_null(self):
        """The ingestion_tools.register_healthex_patient INSERT into
        source_freshness must bind last_ingested_at to NULL."""
        src = self._read("mcp-server/skills/ingestion_tools.py")
        idx = src.find("INSERT INTO source_freshness")
        assert idx != -1, "INSERT INTO source_freshness missing"
        # Snip the first 400 chars of the insert block.
        block = src[idx:idx + 400]
        assert "VALUES ($1, $2, NULL" in block or "VALUES ($1, $2, NULL," in block, (
            "register_healthex_patient must init last_ingested_at = NULL, "
            "not NOW(). Found:\n" + block
        )

    def test_mcp_server_register_uses_null(self):
        """The S1 server register_healthex_patient INSERT into
        source_freshness must also bind last_ingested_at to NULL."""
        src = self._read("server/mcp_server.py")
        idx = src.find("INSERT INTO source_freshness")
        assert idx != -1, "INSERT INTO source_freshness missing"
        block = src[idx:idx + 400]
        assert "VALUES ($1,$2,NULL" in block or "VALUES ($1, $2, NULL" in block, (
            "server/mcp_server.py register path must init "
            "last_ingested_at = NULL, not NOW(). Found:\n" + block
        )


# ---------------------------------------------------------------------------
# Unit tests for freshness TTL constants
# ---------------------------------------------------------------------------


class TestFreshnessTTL:
    def test_obt_ttl_exists(self):
        assert "compute_obt_score" in FRESHNESS_TTL
        assert FRESHNESS_TTL["compute_obt_score"] == 24

    def test_provider_risk_ttl_exists(self):
        assert "compute_provider_risk" in FRESHNESS_TTL
        assert FRESHNESS_TTL["compute_provider_risk"] == 24

    def test_deliberation_ttl_exists(self):
        assert "deliberation" in FRESHNESS_TTL
        assert FRESHNESS_TTL["deliberation"] == 12

    def test_previsit_brief_ttl_exists(self):
        assert "generate_previsit_brief" in FRESHNESS_TTL
        assert FRESHNESS_TTL["generate_previsit_brief"] == 24


# ---------------------------------------------------------------------------
# DB-backed tests for freshness query helpers
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_skill_freshness_no_records(db_pool, test_patient):
    """When no skill_executions exist, returns None."""
    async with db_pool.acquire() as conn:
        result = await _get_skill_freshness(
            conn, test_patient, "compute_obt_score",
        )
    assert result is None


@pytest.mark.asyncio
async def test_get_skill_freshness_returns_latest(db_pool, test_patient):
    """Returns the most recent successful execution timestamp."""
    now = datetime.utcnow()
    older = now - timedelta(hours=5)
    async with db_pool.acquire() as conn:
        # Insert an older execution
        await conn.execute(
            """
            INSERT INTO skill_executions
                (skill_name, patient_id, status, execution_date, data_source)
            VALUES ($1, $2, 'completed', $3, 'synthea')
            """,
            "compute_obt_score", test_patient, older,
        )
        # Insert a newer execution
        await conn.execute(
            """
            INSERT INTO skill_executions
                (skill_name, patient_id, status, execution_date, data_source)
            VALUES ($1, $2, 'completed', $3, 'synthea')
            """,
            "compute_obt_score", test_patient, now,
        )
        result = await _get_skill_freshness(
            conn, test_patient, "compute_obt_score",
        )
    assert result is not None
    # Should be within a few seconds of 'now'
    # Strip tzinfo before comparison — asyncpg returns timezone-aware datetimes
    result_naive = result.replace(tzinfo=None) if result.tzinfo else result
    assert abs((result_naive - now).total_seconds()) < 5


@pytest.mark.asyncio
async def test_get_skill_freshness_ignores_failed(db_pool, test_patient):
    """Failed executions should not count as fresh."""
    now = datetime.utcnow()
    async with db_pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO skill_executions
                (skill_name, patient_id, status, execution_date, data_source)
            VALUES ($1, $2, 'failed', $3, 'synthea')
            """,
            "compute_obt_score", test_patient, now,
        )
        result = await _get_skill_freshness(
            conn, test_patient, "compute_obt_score",
        )
    assert result is None


@pytest.mark.asyncio
async def test_get_deliberation_freshness_no_records(db_pool, test_patient):
    """When no deliberations exist, returns None."""
    async with db_pool.acquire() as conn:
        result = await _get_deliberation_freshness(conn, test_patient)
    assert result is None


@pytest.mark.asyncio
async def test_get_deliberation_freshness_returns_latest(db_pool, test_patient):
    """Returns the most recent completed deliberation timestamp."""
    now = datetime.utcnow()
    async with db_pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO deliberations
                (patient_id, trigger_type, triggered_at, status,
                 synthesizer_model)
            VALUES ($1, 'manual', $2, 'complete', 'test')
            """,
            test_patient, now,
        )
        result = await _get_deliberation_freshness(conn, test_patient)
    assert result is not None
    # Strip tzinfo before comparison — asyncpg returns timezone-aware datetimes
    result_naive = result.replace(tzinfo=None) if result.tzinfo else result
    assert abs((result_naive - now).total_seconds()) < 5


@pytest.mark.asyncio
async def test_get_deliberation_freshness_ignores_pending(db_pool, test_patient):
    """Pending/running deliberations should not count as fresh."""
    now = datetime.utcnow()
    async with db_pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO deliberations
                (patient_id, trigger_type, triggered_at, status,
                 synthesizer_model)
            VALUES ($1, 'manual', $2, 'pending', 'test')
            """,
            test_patient, now,
        )
        result = await _get_deliberation_freshness(conn, test_patient)
    assert result is None


# ---------------------------------------------------------------------------
# check_data_freshness enhanced response tests
# ---------------------------------------------------------------------------


def _get_tool_fn(tool_name: str):
    """Extract a registered MCP tool function by name."""
    import skills.ingestion_tools as mod

    captured = {}

    class FakeMCP:
        def tool(self, fn):
            captured[fn.__name__] = fn
            return fn

    mod.register(FakeMCP())
    return captured[tool_name]


@pytest.mark.asyncio
async def test_check_data_freshness_includes_skills(db_pool, test_patient):
    """Enhanced check_data_freshness returns skills, deliberation, and artifacts."""
    check = _get_tool_fn("check_data_freshness")
    result_json = await check(test_patient)
    result = json.loads(result_json)

    assert "patient_id" in result
    assert "sources" in result

    # New fields
    assert "skills" in result
    assert "compute_obt_score" in result["skills"]
    assert "compute_provider_risk" in result["skills"]

    assert "deliberation" in result
    assert "is_stale" in result["deliberation"]

    assert "artifacts" in result
    assert "generate_previsit_brief" in result["artifacts"]

    assert "recommended_actions" in result
    assert isinstance(result["recommended_actions"], list)


@pytest.mark.asyncio
async def test_check_data_freshness_stale_recommendations(db_pool, test_patient):
    """When nothing has run, all actions should be recommended."""
    check = _get_tool_fn("check_data_freshness")
    result_json = await check(test_patient)
    result = json.loads(result_json)

    # Patient has no executions, so skills/deliberation/artifacts are stale
    assert result["skills"]["compute_obt_score"]["is_stale"] is True
    assert result["deliberation"]["is_stale"] is True
    assert result["artifacts"]["generate_previsit_brief"]["is_stale"] is True

    assert "deliberation" in result["recommended_actions"]
    assert "recompute_skills" in result["recommended_actions"]
    assert "generate_artifacts" in result["recommended_actions"]


@pytest.mark.asyncio
async def test_check_data_freshness_fresh_skill(db_pool, test_patient):
    """When a skill ran recently, it should not be stale."""
    # Insert a recent skill execution
    now = datetime.utcnow()
    async with db_pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO skill_executions
                (skill_name, patient_id, status, execution_date, data_source)
            VALUES ($1, $2, 'completed', $3, 'synthea')
            """,
            "compute_obt_score", test_patient, now,
        )

    check = _get_tool_fn("check_data_freshness")
    result_json = await check(test_patient)
    result = json.loads(result_json)

    assert result["skills"]["compute_obt_score"]["is_stale"] is False
    assert result["skills"]["compute_obt_score"]["last_run_at"] is not None
