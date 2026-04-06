"""Tests for the knowledge store module (Phase 5)."""
import pytest
from unittest.mock import AsyncMock, MagicMock
from datetime import datetime
from server.deliberation.schemas import (
    DeliberationResult, KnowledgeUpdate, AnticipatoryScenario,
    MissingDataFlag, NudgeContent, PredictedPatientQuestion
)


def _make_minimal_result() -> DeliberationResult:
    return DeliberationResult(
        deliberation_id="test-abc",
        patient_id="4829341",
        timestamp=datetime.utcnow(),
        trigger="manual",
        models={"claude": "claude-sonnet-4-20250514", "gpt4": "gpt-4o"},
        rounds_completed=1,
        convergence_score=0.80,
        total_tokens=3000,
        total_latency_ms=30000,
        anticipatory_scenarios=[
            AnticipatoryScenario(
                scenario_id="s1", timeframe="next_90_days",
                title="Test scenario", description="Test",
                probability=0.7, confidence=0.8,
                clinical_implications="Test", evidence_basis=["ADA"]
            )
        ],
        predicted_patient_questions=[],
        missing_data_flags=[],
        nudge_content=[],
        knowledge_updates=[
            KnowledgeUpdate(
                update_type="new_inference", scope="patient_specific",
                entry_text="BP trending up", confidence=0.85,
                valid_from=datetime.utcnow(), evidence=["vitals"]
            )
        ],
        unresolved_disagreements=[],
        transcript={"phase1": {}, "phase2_rounds": []}
    )


@pytest.mark.asyncio
async def test_commit_writes_to_deliberations_table():
    """Verify the main deliberation record gets inserted."""
    from server.deliberation.knowledge_store import commit_deliberation

    mock_conn = AsyncMock()

    # transaction() must return an async context manager directly (not a coroutine).
    # AsyncMock makes transaction() a coroutine, so override with MagicMock.
    mock_tx_cm = MagicMock()
    mock_tx_cm.__aenter__ = AsyncMock(return_value=None)
    mock_tx_cm.__aexit__ = AsyncMock(return_value=None)
    mock_conn.transaction = MagicMock(return_value=mock_tx_cm)

    # pool.acquire() must return an async context manager
    mock_acquire_cm = MagicMock()
    mock_acquire_cm.__aenter__ = AsyncMock(return_value=mock_conn)
    mock_acquire_cm.__aexit__ = AsyncMock(return_value=None)

    mock_pool = MagicMock()
    mock_pool.acquire.return_value = mock_acquire_cm

    result = _make_minimal_result()
    dlb_id = await commit_deliberation(
        result=result,
        db_pool=mock_pool,
        convergence_score=0.80,
        rounds_completed=1,
        total_tokens=3000,
        total_latency_ms=30000,
        synthesizer_model="claude-sonnet-4-20250514"
    )
    assert dlb_id == "test-abc"
    # Verify execute was called (deliberation + scenario + knowledge update = 3 calls)
    assert mock_conn.execute.call_count >= 2
