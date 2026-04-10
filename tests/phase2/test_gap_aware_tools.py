"""Tests for gap-aware MCP tools — mocked DB and LLM calls."""
import json
import pytest
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch
from datetime import datetime, timedelta, timezone


# ── Helper to build a properly-mockable asyncpg pool ─────────────────────────

def _make_mock_pool():
    """Create a mock asyncpg pool where pool.acquire() works as async ctx mgr."""
    mock_conn = AsyncMock()
    mock_conn.execute = AsyncMock()
    mock_conn.fetch = AsyncMock(return_value=[])
    mock_conn.fetchrow = AsyncMock(return_value=None)

    mock_pool = MagicMock()

    @asynccontextmanager
    async def _acquire():
        yield mock_conn

    mock_pool.acquire = _acquire
    return mock_pool, mock_conn


# ── detect_context_staleness ──────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_staleness_detects_stale_hba1c():
    """HbA1c older than 90 days should be flagged as stale in pre_encounter."""
    from ingestion.server import detect_context_staleness

    stale_date = (datetime.now(timezone.utc) - timedelta(days=100)).isoformat()
    fresh_date = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()

    result_str = await detect_context_staleness(
        patient_mrn="4829341",
        context_elements=[
            {"element_type": "lab_result", "loinc_code": "4548-4",
             "last_updated": stale_date, "source_system": "ehr"},
            {"element_type": "vital_sign", "loinc_code": None,
             "last_updated": fresh_date, "source_system": "ehr"},
        ],
        clinical_scenario="pre_encounter",
    )
    result = json.loads(result_str)

    assert len(result["stale_elements"]) == 1
    assert result["stale_elements"][0]["loinc_code"] == "4548-4"
    assert result["freshness_score"] == 0.5


@pytest.mark.asyncio
async def test_staleness_all_fresh():
    """All fresh elements should yield freshness_score = 1.0."""
    from ingestion.server import detect_context_staleness

    fresh_date = (datetime.now(timezone.utc) - timedelta(hours=12)).isoformat()

    result_str = await detect_context_staleness(
        patient_mrn="4829341",
        context_elements=[
            {"element_type": "vital_sign", "loinc_code": None,
             "last_updated": fresh_date, "source_system": "ehr"},
        ],
        clinical_scenario="pre_encounter",
    )
    result = json.loads(result_str)

    assert len(result["stale_elements"]) == 0
    assert result["freshness_score"] == 1.0


@pytest.mark.asyncio
async def test_staleness_acute_event_stricter():
    """In acute events, even recent vitals (>4h) should be stale."""
    from ingestion.server import detect_context_staleness

    six_hours_ago = (datetime.now(timezone.utc) - timedelta(hours=6)).isoformat()

    result_str = await detect_context_staleness(
        patient_mrn="4829341",
        context_elements=[
            {"element_type": "vital_sign", "loinc_code": None,
             "last_updated": six_hours_ago, "source_system": "ehr"},
        ],
        clinical_scenario="acute_event",
    )
    result = json.loads(result_str)

    assert len(result["stale_elements"]) == 1
    assert result["freshness_score"] == 0.0


@pytest.mark.asyncio
async def test_staleness_empty_elements():
    """Empty context should return perfect freshness."""
    from ingestion.server import detect_context_staleness

    result_str = await detect_context_staleness(
        patient_mrn="4829341",
        context_elements=[],
        clinical_scenario="pre_encounter",
    )
    result = json.loads(result_str)

    assert result["freshness_score"] == 1.0
    assert result["stale_elements"] == []


# ── emit_reasoning_gap_artifact ───────────────────────────────────────────────

@pytest.mark.asyncio
async def test_emit_gap_artifact_returns_stored():
    """emit_reasoning_gap_artifact should return stored=True with an artifact_id."""
    mock_pool, _ = _make_mock_pool()

    async def _get_pool():
        return mock_pool

    with patch("gap_aware.db.get_pool", side_effect=_get_pool):
        from server.mcp_server import emit_reasoning_gap_artifact

        result = await emit_reasoning_gap_artifact(
            deliberation_id="test_delib_001",
            emitting_agent="MIRA",
            gap_id="gap_test123",
            gap_type="stale_data",
            severity="high",
            description="HbA1c stale",
            impact_statement="Cannot assess glycemic control",
            confidence_without_resolution=0.45,
            confidence_with_resolution=0.85,
            recommended_action_for_synthesis="include_caveat_in_output",
            patient_mrn="4829341",
        )

        assert result["stored"] is True
        assert result["synthesis_notified"] is True
        assert "artifact_id" in result


@pytest.mark.asyncio
async def test_emit_gap_artifact_critical_escalates():
    """Critical severity should trigger synthesis_priority_escalated."""
    mock_pool, _ = _make_mock_pool()

    async def _get_pool():
        return mock_pool

    with patch("gap_aware.db.get_pool", side_effect=_get_pool):
        from server.mcp_server import emit_reasoning_gap_artifact

        result = await emit_reasoning_gap_artifact(
            deliberation_id="test_delib_002",
            emitting_agent="ARIA",
            gap_id="gap_crit001",
            gap_type="drug_interaction_unknown",
            severity="critical",
            description="Unknown interaction",
            impact_statement="Safety concern",
            confidence_without_resolution=0.3,
            confidence_with_resolution=0.9,
            recommended_action_for_synthesis="defer_to_provider",
            patient_mrn="4829341",
        )

        assert "synthesis_priority_escalated" in result["downstream_actions_triggered"]


# ── request_clarification ─────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_request_clarification_returns_pending():
    """request_clarification should return status=pending with a clarification_id."""
    mock_pool, _ = _make_mock_pool()

    async def _get_pool():
        return mock_pool

    with patch("gap_aware.db.get_pool", side_effect=_get_pool):
        from server.mcp_server import request_clarification

        result = await request_clarification(
            deliberation_id="test_delib_003",
            requesting_agent="MIRA",
            recipient="provider",
            urgency="preferred",
            question_text="What is the patient's current buspirone dose?",
            clinical_rationale="Need dose to check interaction severity",
            gap_id="gap_dose001",
        )

        assert result["status"] == "pending"
        assert result["clarification_id"].startswith("clar_")
        assert result["resolution_action"] == "fallback_applied"


@pytest.mark.asyncio
async def test_request_clarification_blocking_escalates():
    """Blocking urgency should set resolution_action=escalated."""
    mock_pool, _ = _make_mock_pool()

    async def _get_pool():
        return mock_pool

    with patch("gap_aware.db.get_pool", side_effect=_get_pool):
        from server.mcp_server import request_clarification

        result = await request_clarification(
            deliberation_id="test_delib_004",
            requesting_agent="ARIA",
            recipient="provider",
            urgency="blocking",
            question_text="Is the patient pregnant?",
            clinical_rationale="Contraindicated medication in pregnancy",
            gap_id="gap_preg001",
        )

        assert result["resolution_action"] == "escalated"


# ── register_gap_trigger ─────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_register_gap_trigger_returns_registered():
    """register_gap_trigger should return registered=True with a trigger_id."""
    mock_pool, _ = _make_mock_pool()

    async def _get_pool():
        return mock_pool

    with patch("gap_aware.db.get_pool", side_effect=_get_pool):
        from server.mcp_server import register_gap_trigger

        result = await register_gap_trigger(
            patient_mrn="4829341",
            gap_id="gap_hba1c001",
            watch_for="lab_result",
            expires_at="2026-05-01T00:00:00Z",
            on_fire_action="re_run_deliberation",
            loinc_code="4548-4",
        )

        assert result["registered"] is True
        assert result["trigger_id"].startswith("trig_")
        assert result["estimated_resolution_probability"] == 0.75


@pytest.mark.asyncio
async def test_register_gap_trigger_vital_sign_probability():
    """vital_sign watch_for should have 0.85 estimated probability."""
    mock_pool, _ = _make_mock_pool()

    async def _get_pool():
        return mock_pool

    with patch("gap_aware.db.get_pool", side_effect=_get_pool):
        from server.mcp_server import register_gap_trigger

        result = await register_gap_trigger(
            patient_mrn="4829341",
            gap_id="gap_bp001",
            watch_for="vital_sign",
            expires_at="2026-05-01T00:00:00Z",
            on_fire_action="notify_synthesis",
        )

        assert result["estimated_resolution_probability"] == 0.85
