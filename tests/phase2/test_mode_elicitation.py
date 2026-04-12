"""
Tests for run_deliberation mode elicitation (two-call protocol).

Covers:
  - mode=None / mode="ask" on a new patient → mode_selection_required,
    recommended_mode="triage", is_initial_run=True
  - mode="ask" on a patient with prior high-convergence run →
    recommended_mode="progressive"
  - mode="ask" on a patient with prior low-convergence run →
    recommended_mode="full"
  - Re-invocation with a valid selection_token consumes the token and
    dispatches to the chosen engine method (triage / progressive / full)
  - Invalid mode string → status="invalid_mode"
  - Expired / missing / patient-mismatched selection_token →
    status="invalid_selection_token"
  - Explicit mode="progressive" / mode="full" works unchanged (regression)
"""
from __future__ import annotations

import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from server import mcp_server
from server.mcp_server import (
    run_deliberation,
    _MODE_SELECTION_CACHE,
    _MODE_SELECTION_TTL_SEC,
    _purge_expired_selection_tokens,
)


# ── Helpers ──────────────────────────────────────────────────────────────────

def _make_mock_pool(prior_rows=None, count=0):
    """Build a mock asyncpg pool whose connection returns canned history rows."""
    mock_conn = AsyncMock()
    mock_conn.fetch = AsyncMock(return_value=prior_rows or [])
    mock_conn.fetchrow = AsyncMock(return_value={"n": count})

    mock_acquire_cm = MagicMock()
    mock_acquire_cm.__aenter__ = AsyncMock(return_value=mock_conn)
    mock_acquire_cm.__aexit__ = AsyncMock(return_value=None)

    mock_pool = MagicMock()
    mock_pool.acquire = MagicMock(return_value=mock_acquire_cm)
    return mock_pool


@pytest.fixture(autouse=True)
def _clean_cache():
    """Each test starts with an empty selection-token cache."""
    _MODE_SELECTION_CACHE.clear()
    yield
    _MODE_SELECTION_CACHE.clear()


# ── Elicitation — new patient ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_mode_none_on_new_patient_recommends_triage():
    pool = _make_mock_pool(prior_rows=[], count=0)
    with patch.object(mcp_server, "_get_db_pool", AsyncMock(return_value=pool)):
        out = await run_deliberation(patient_id="new-patient-uuid")

    assert out["status"] == "mode_selection_required"
    assert out["is_initial_run"] is True
    assert out["prior_deliberations"] == 0
    assert out["latest_convergence"] is None
    assert out["recommended_mode"] == "triage"
    assert "selection_token" in out
    assert out["expires_in_sec"] == _MODE_SELECTION_TTL_SEC
    # Options include all three executable modes.
    modes_offered = {opt["mode"] for opt in out["options"]}
    assert modes_offered == {"triage", "progressive", "full"}
    # Token stored in cache.
    assert out["selection_token"] in _MODE_SELECTION_CACHE


@pytest.mark.asyncio
async def test_mode_ask_is_equivalent_to_none():
    pool = _make_mock_pool(prior_rows=[], count=0)
    with patch.object(mcp_server, "_get_db_pool", AsyncMock(return_value=pool)):
        out = await run_deliberation(patient_id="p1", mode="ask")
    assert out["status"] == "mode_selection_required"


# ── Elicitation — subsequent runs ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_high_prior_convergence_recommends_progressive():
    pool = _make_mock_pool(
        prior_rows=[{
            "id": "abc", "triggered_at": None,
            "convergence_score": 0.82, "rounds_completed": 2,
            "status": "complete",
        }],
        count=3,
    )
    with patch.object(mcp_server, "_get_db_pool", AsyncMock(return_value=pool)):
        out = await run_deliberation(patient_id="p-returning", mode="ask")

    assert out["status"] == "mode_selection_required"
    assert out["is_initial_run"] is False
    assert out["prior_deliberations"] == 3
    assert out["latest_convergence"] == pytest.approx(0.82)
    assert out["recommended_mode"] == "progressive"


@pytest.mark.asyncio
async def test_low_prior_convergence_recommends_full():
    pool = _make_mock_pool(
        prior_rows=[{
            "id": "abc", "triggered_at": None,
            "convergence_score": 0.41, "rounds_completed": 3,
            "status": "complete",
        }],
        count=1,
    )
    with patch.object(mcp_server, "_get_db_pool", AsyncMock(return_value=pool)):
        out = await run_deliberation(patient_id="p-divergent", mode="ask")

    assert out["recommended_mode"] == "full"
    assert out["is_initial_run"] is False


# ── Second-call dispatch ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_second_call_with_valid_token_dispatches_to_triage():
    pool = _make_mock_pool(prior_rows=[], count=0)

    mock_engine = MagicMock()
    mock_engine.run_triage = AsyncMock(return_value={
        "status": "complete", "mode": "triage", "deliberation_id": "t1",
        "patient_id": "p1", "convergence_score": 1.0, "summary": {},
    })

    with (
        patch.object(mcp_server, "_get_db_pool", AsyncMock(return_value=pool)),
        patch.object(
            mcp_server, "get_deliberation_engine",
            AsyncMock(return_value=mock_engine),
        ),
    ):
        offer = await run_deliberation(patient_id="p1")
        token = offer["selection_token"]
        result = await run_deliberation(
            patient_id="p1", mode="triage", selection_token=token,
        )

    assert result["status"] == "complete"
    assert result["mode"] == "triage"
    assert mock_engine.run_triage.await_count == 1
    # Token is consumed — replay fails.
    replay = None
    with (
        patch.object(mcp_server, "_get_db_pool", AsyncMock(return_value=pool)),
        patch.object(
            mcp_server, "get_deliberation_engine",
            AsyncMock(return_value=mock_engine),
        ),
    ):
        replay = await run_deliberation(
            patient_id="p1", mode="triage", selection_token=token,
        )
    assert replay["status"] == "invalid_selection_token"


@pytest.mark.asyncio
async def test_second_call_dispatches_to_progressive():
    pool = _make_mock_pool()
    mock_engine = MagicMock()
    mock_engine.run_progressive = AsyncMock(return_value={
        "status": "complete", "mode": "progressive",
    })

    with (
        patch.object(mcp_server, "_get_db_pool", AsyncMock(return_value=pool)),
        patch.object(
            mcp_server, "get_deliberation_engine",
            AsyncMock(return_value=mock_engine),
        ),
    ):
        offer = await run_deliberation(patient_id="p2")
        result = await run_deliberation(
            patient_id="p2", mode="progressive",
            selection_token=offer["selection_token"],
        )

    assert mock_engine.run_progressive.await_count == 1
    assert result["status"] == "complete"


# ── Token validation ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_bad_token_returns_invalid_selection_token():
    result = await run_deliberation(
        patient_id="px", mode="triage", selection_token="does-not-exist",
    )
    assert result["status"] == "invalid_selection_token"


@pytest.mark.asyncio
async def test_patient_mismatched_token_is_rejected():
    pool = _make_mock_pool()
    with patch.object(mcp_server, "_get_db_pool", AsyncMock(return_value=pool)):
        offer = await run_deliberation(patient_id="patientA")

    # Use the token with a different patient_id.
    result = await run_deliberation(
        patient_id="patientB",
        mode="triage",
        selection_token=offer["selection_token"],
    )
    assert result["status"] == "invalid_selection_token"


@pytest.mark.asyncio
async def test_expired_token_is_purged():
    pool = _make_mock_pool()
    with patch.object(mcp_server, "_get_db_pool", AsyncMock(return_value=pool)):
        offer = await run_deliberation(patient_id="pz")
    token = offer["selection_token"]

    # Force expiration.
    _MODE_SELECTION_CACHE[token]["expires_at"] = time.time() - 1
    _purge_expired_selection_tokens()
    assert token not in _MODE_SELECTION_CACHE

    result = await run_deliberation(
        patient_id="pz", mode="triage", selection_token=token,
    )
    assert result["status"] == "invalid_selection_token"


# ── Invalid mode ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_invalid_mode_returns_error_not_full_dispatch():
    """Regression: invalid mode strings must NOT silently fall through to full."""
    result = await run_deliberation(patient_id="p", mode="bogus")
    assert result["status"] == "invalid_mode"
    assert "triage" in result["accepted"]
    assert "progressive" in result["accepted"]
    assert "full" in result["accepted"]
    assert result["received"] == "bogus"


# ── Regression: explicit modes still work without elicitation ───────────────


@pytest.mark.asyncio
async def test_explicit_progressive_skips_elicitation():
    mock_engine = MagicMock()
    mock_engine.run_progressive = AsyncMock(return_value={"status": "complete"})

    with patch.object(
        mcp_server, "get_deliberation_engine",
        AsyncMock(return_value=mock_engine),
    ):
        out = await run_deliberation(patient_id="p", mode="progressive")

    assert out["status"] == "complete"
    assert mock_engine.run_progressive.await_count == 1


@pytest.mark.asyncio
async def test_explicit_full_skips_elicitation():
    from server.deliberation.schemas import DeliberationResult
    from datetime import datetime

    fake_result = DeliberationResult(
        deliberation_id="dlb-x",
        patient_id="p",
        timestamp=datetime.utcnow(),
        trigger="manual",
        models={"claude": "claude-sonnet-4-20250514", "gpt4": "gpt-4o"},
        rounds_completed=2,
        convergence_score=0.82,
        total_tokens=1000,
        total_latency_ms=1000,
    )

    mock_engine = MagicMock()
    mock_engine.run = AsyncMock(return_value=fake_result)

    with patch.object(
        mcp_server, "get_deliberation_engine",
        AsyncMock(return_value=mock_engine),
    ):
        out = await run_deliberation(patient_id="p", mode="full")

    assert out["status"] == "complete"
    assert out["mode"] == "full"
    assert out["convergence_score"] == pytest.approx(0.82)
    assert mock_engine.run.await_count == 1
