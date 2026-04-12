"""
Tests for DeliberationEngine.run_triage() — the lightweight single-LLM mode.

Validates that triage:
  - Runs context compilation + planner + Claude analyst only
  - Skips GPT-4o, critic rounds, synthesis, synthesis_reviewer, behavioral_adapter
  - Produces a DeliberationResult persisted via commit_deliberation
  - Stamps mode='triage' in transcript and models dict
  - Converts analyst.missing_data_identified into MissingDataFlag entries
"""
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from server.deliberation.engine import DeliberationEngine
from server.deliberation.schemas import (
    DeliberationRequest,
    IndependentAnalysis,
    ClaimWithConfidence,
    PatientContextPackage,
)


FIXTURES = Path(__file__).parent / "fixtures"


def _load_context() -> PatientContextPackage:
    data = json.loads((FIXTURES / "maria_chen_context.json").read_text())
    return PatientContextPackage(**data)


def _fake_claude_analysis() -> IndependentAnalysis:
    return IndependentAnalysis(
        model_id="claude-sonnet-4-20250514",
        role_emphasis="diagnostic_reasoning",
        key_findings=[
            ClaimWithConfidence(claim="HbA1c rising trend", confidence=0.85),
            ClaimWithConfidence(claim="BP borderline", confidence=0.7),
        ],
        risk_flags=[
            ClaimWithConfidence(claim="Cardiovascular risk elevated", confidence=0.75),
        ],
        recommended_actions=[
            ClaimWithConfidence(claim="Intensify metformin", confidence=0.8),
        ],
        anticipated_trajectory="Patient may progress to insulin within 6 months.",
        missing_data_identified=[
            "Urine albumin-creatinine ratio",
            "Retinal exam date",
        ],
        raw_reasoning="Chain-of-thought omitted for test.",
    )


def _make_engine_with_mocks(analyst_output):
    """Engine with mocked DB pool; callers still need to patch phase helpers."""
    mock_conn = AsyncMock()
    mock_tx_cm = MagicMock()
    mock_tx_cm.__aenter__ = AsyncMock(return_value=None)
    mock_tx_cm.__aexit__ = AsyncMock(return_value=None)
    mock_conn.transaction = MagicMock(return_value=mock_tx_cm)

    mock_acquire_cm = MagicMock()
    mock_acquire_cm.__aenter__ = AsyncMock(return_value=mock_conn)
    mock_acquire_cm.__aexit__ = AsyncMock(return_value=None)

    mock_pool = MagicMock()
    mock_pool.acquire = MagicMock(return_value=mock_acquire_cm)

    engine = DeliberationEngine(db_pool=mock_pool, vector_store=MagicMock())
    return engine, mock_conn


@pytest.mark.asyncio
async def test_run_triage_produces_complete_result_with_single_llm():
    engine, mock_conn = _make_engine_with_mocks(_fake_claude_analysis())

    ctx = _load_context()

    with (
        patch(
            "server.deliberation.engine.compile_patient_context",
            new=AsyncMock(return_value=ctx),
        ),
        patch(
            "server.deliberation.engine.build_deliberation_agenda",
            new=AsyncMock(return_value=None),
        ),
        patch(
            "server.deliberation.engine._analyze_with_claude",
            new=AsyncMock(return_value=_fake_claude_analysis()),
        ) as mock_claude,
        patch(
            "server.deliberation.engine.run_parallel_analysis",
            new=AsyncMock(side_effect=AssertionError("Dual-LLM must not run in triage")),
        ) as mock_dual,
        patch(
            "server.deliberation.engine.run_critique_rounds",
            new=AsyncMock(side_effect=AssertionError("Critic must not run in triage")),
        ) as mock_critic,
        patch(
            "server.deliberation.engine.synthesize",
            new=AsyncMock(side_effect=AssertionError("Synthesizer must not run in triage")),
        ) as mock_synth,
        patch(
            "server.deliberation.engine.review_synthesis",
            new=AsyncMock(side_effect=AssertionError("Synthesis reviewer must not run in triage")),
        ),
        patch(
            "server.deliberation.engine.adapt_nudges",
            new=MagicMock(side_effect=AssertionError("Behavioral adapter must not run in triage")),
        ),
        patch(
            "server.deliberation.engine.commit_deliberation",
            new=AsyncMock(return_value="triage-dlb-id"),
        ) as mock_commit,
    ):
        out = await engine.run_triage(
            DeliberationRequest(patient_id=ctx.patient_id, trigger_type="manual"),
        )

    # The Claude analyst was invoked exactly once.
    assert mock_claude.await_count == 1
    # No dual / critic / synthesizer calls happened.
    assert mock_dual.await_count == 0
    assert mock_critic.await_count == 0
    assert mock_synth.await_count == 0
    # Commit was called.
    assert mock_commit.await_count == 1

    # Response shape.
    assert out["status"] == "complete"
    assert out["mode"] == "triage"
    assert out["patient_id"] == ctx.patient_id
    assert out["convergence_score"] == 1.0
    # Triage-specific summary counts.
    assert out["summary"]["anticipatory_scenarios"] == 0
    assert out["summary"]["predicted_questions"] == 0
    assert out["summary"]["missing_data_flags"] == 2  # two gaps from analyst
    assert out["summary"]["key_findings"] == 2
    assert out["summary"]["risk_flags"] == 1
    assert out["summary"]["recommended_actions"] == 1

    # commit_deliberation was called with synthesizer_model="triage-single-llm"
    commit_kwargs = mock_commit.await_args.kwargs
    assert commit_kwargs["synthesizer_model"] == "triage-single-llm"
    committed_result = commit_kwargs["result"]
    assert committed_result.models.get("mode") == "triage"
    assert committed_result.transcript.get("mode") == "triage"
    assert "analyst_output" in committed_result.transcript
    # Missing-data flags converted from analyst.missing_data_identified.
    flag_descs = {f.description for f in committed_result.missing_data_flags}
    assert "Urine albumin-creatinine ratio" in flag_descs
    assert "Retinal exam date" in flag_descs


@pytest.mark.asyncio
async def test_run_triage_handles_empty_missing_data():
    """Analyst with no missing_data_identified should produce zero flags."""
    ctx = _load_context()
    empty_analysis = _fake_claude_analysis()
    empty_analysis.missing_data_identified = []

    engine, _ = _make_engine_with_mocks(empty_analysis)

    with (
        patch(
            "server.deliberation.engine.compile_patient_context",
            new=AsyncMock(return_value=ctx),
        ),
        patch(
            "server.deliberation.engine.build_deliberation_agenda",
            new=AsyncMock(return_value=None),
        ),
        patch(
            "server.deliberation.engine._analyze_with_claude",
            new=AsyncMock(return_value=empty_analysis),
        ),
        patch(
            "server.deliberation.engine.commit_deliberation",
            new=AsyncMock(return_value="dlb"),
        ),
    ):
        out = await engine.run_triage(
            DeliberationRequest(patient_id=ctx.patient_id, trigger_type="manual"),
        )

    assert out["summary"]["missing_data_flags"] == 0
    assert out["critical_flags"] == []


@pytest.mark.asyncio
async def test_run_triage_agenda_build_failure_non_fatal():
    """A failing planner must not break triage — agenda is optional."""
    ctx = _load_context()
    engine, _ = _make_engine_with_mocks(_fake_claude_analysis())

    with (
        patch(
            "server.deliberation.engine.compile_patient_context",
            new=AsyncMock(return_value=ctx),
        ),
        patch(
            "server.deliberation.engine.build_deliberation_agenda",
            new=AsyncMock(side_effect=RuntimeError("planner exploded")),
        ),
        patch(
            "server.deliberation.engine._analyze_with_claude",
            new=AsyncMock(return_value=_fake_claude_analysis()),
        ),
        patch(
            "server.deliberation.engine.commit_deliberation",
            new=AsyncMock(return_value="dlb"),
        ),
    ):
        out = await engine.run_triage(
            DeliberationRequest(patient_id=ctx.patient_id, trigger_type="manual"),
        )

    assert out["status"] == "complete"
    assert out["mode"] == "triage"
