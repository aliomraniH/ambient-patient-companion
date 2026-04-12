"""Tests for the convergence gate (server/deliberation/convergence_gate.py).

Tests cover:
  CG-1: Tier classification by score
  CG-2: Consensus tier — full output proceeds
  CG-3: Partial tier — recommendations preserved with caveats
  CG-4: No consensus tier — recommendations nulled, nudges emptied
  CG-5: Hard constraint: recommendation never present when score < 0.40
  CG-6: Maria Chen scenario — opposing agent recommendations
  CG-7: Two-round retry triggers when convergence < 0.60
"""

import pytest
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock

from server.deliberation.convergence_gate import (
    classify_convergence,
    gate_synthesis_output,
    deliberate_with_retry,
    ConvergenceTier,
    CONSENSUS_THRESHOLD,
    PARTIAL_THRESHOLD,
    RETRY_THRESHOLD,
)
from server.deliberation.schemas import (
    DeliberationResult,
    AnticipatoryScenario,
    NudgeContent,
    KnowledgeUpdate,
    IndependentAnalysis,
)


def _make_analysis(model_id="claude-test"):
    """Build a real IndependentAnalysis instance for retry tests."""
    return IndependentAnalysis(
        model_id=model_id,
        role_emphasis="diagnostic_reasoning",
        key_findings=[],
        risk_flags=[],
        recommended_actions=[],
        anticipated_trajectory="",
        missing_data_identified=[],
        raw_reasoning="",
    )


def _make_scenario(**overrides):
    defaults = {
        "scenario_id": "s1",
        "timeframe": "next_30_days",
        "title": "Test scenario",
        "description": "Test description",
        "probability": 0.7,
        "confidence": 0.85,
        "clinical_implications": "Monitor closely",
        "evidence_basis": ["lab_trend"],
    }
    defaults.update(overrides)
    return AnticipatoryScenario(**defaults)


def _make_nudge(nudge_id="n1"):
    return NudgeContent(
        nudge_id=nudge_id,
        target="patient",
        trigger_condition="HbA1c >= 7",
        behavioral_technique="BCT_1.4_action_planning",
        com_b_target="motivation",
        channels={"sms": "Test message"},
        reading_level="grade_6",
        personalization_factors=[],
    )


def _make_knowledge_update(update_type="new_inference"):
    return KnowledgeUpdate(
        update_type=update_type,
        scope="patient_specific",
        entry_text="Test knowledge entry",
        confidence=0.8,
        valid_from=datetime.utcnow(),
    )


def _make_result(scenarios=None, nudges=None, knowledge_updates=None):
    return DeliberationResult(
        deliberation_id="test-id",
        patient_id="4829341",
        timestamp=datetime.utcnow(),
        trigger="pre_visit",
        anticipatory_scenarios=scenarios or [],
        nudge_content=nudges or [],
        knowledge_updates=knowledge_updates or [],
    )


class TestTierClassification:
    """Test the convergence -> tier mapping."""

    def test_perfect_convergence_is_consensus(self):
        assert classify_convergence(1.0) == ConvergenceTier.CONSENSUS

    def test_threshold_consensus(self):
        assert classify_convergence(CONSENSUS_THRESHOLD) == ConvergenceTier.CONSENSUS

    def test_high_partial(self):
        assert classify_convergence(0.69) == ConvergenceTier.PARTIAL

    def test_partial_threshold(self):
        assert classify_convergence(PARTIAL_THRESHOLD) == ConvergenceTier.PARTIAL

    def test_just_below_partial_is_no_consensus(self):
        assert classify_convergence(0.39) == ConvergenceTier.NO_CONSENSUS

    def test_zero_is_no_consensus(self):
        assert classify_convergence(0.0) == ConvergenceTier.NO_CONSENSUS


class TestConsensusTier:
    """High convergence — output flows through unchanged."""

    def test_consensus_keeps_nudges(self):
        result = _make_result(
            nudges=[_make_nudge("n1"), _make_nudge("n2")],
            knowledge_updates=[_make_knowledge_update()],
            scenarios=[_make_scenario()],
        )
        gated = gate_synthesis_output(result, 0.85)
        assert len(gated.nudge_content) == 2
        assert len(gated.knowledge_updates) == 1
        assert len(gated.anticipatory_scenarios) == 1

    def test_consensus_marker_added(self):
        result = _make_result()
        gated = gate_synthesis_output(result, 0.85)
        assert any(
            d.get("convergence_tier") == "consensus"
            for d in gated.unresolved_disagreements
        )


class TestPartialTier:
    """Mid convergence — preserve outputs but mark uncertain."""

    def test_partial_keeps_recommendations(self):
        result = _make_result(
            nudges=[_make_nudge("n1")],
            knowledge_updates=[_make_knowledge_update()],
            scenarios=[_make_scenario()],
        )
        gated = gate_synthesis_output(result, 0.55)
        # Recommendations preserved
        assert len(gated.nudge_content) == 1
        assert len(gated.knowledge_updates) == 1

    def test_partial_marks_dissenting_view(self):
        result = _make_result(scenarios=[_make_scenario()])
        gated = gate_synthesis_output(result, 0.55)
        assert gated.anticipatory_scenarios[0].dissenting_view is not None
        assert "Partial convergence" in gated.anticipatory_scenarios[0].dissenting_view

    def test_partial_caps_confidence(self):
        result = _make_result(scenarios=[_make_scenario(confidence=0.95)])
        gated = gate_synthesis_output(result, 0.55)
        assert gated.anticipatory_scenarios[0].confidence <= 0.65

    def test_partial_marker_added(self):
        result = _make_result()
        gated = gate_synthesis_output(result, 0.55)
        assert any(
            d.get("convergence_tier") == "partial_consensus"
            for d in gated.unresolved_disagreements
        )


class TestNoConsensusTier:
    """HARD CONSTRAINT: convergence < 0.40 must null recommendations."""

    def test_low_convergence_empties_nudges(self):
        result = _make_result(nudges=[_make_nudge("n1"), _make_nudge("n2")])
        gated = gate_synthesis_output(result, 0.0)
        assert gated.nudge_content == []

    def test_low_convergence_removes_new_inference(self):
        result = _make_result(knowledge_updates=[
            _make_knowledge_update(update_type="new_inference"),
            _make_knowledge_update(update_type="reinforcement"),
        ])
        gated = gate_synthesis_output(result, 0.0)
        # new_inference removed; reinforcement preserved
        assert len(gated.knowledge_updates) == 1
        assert gated.knowledge_updates[0].update_type == "reinforcement"

    def test_low_convergence_caps_scenario_confidence(self):
        result = _make_result(scenarios=[
            _make_scenario(confidence=0.95),
            _make_scenario(confidence=0.85),
        ])
        gated = gate_synthesis_output(result, 0.0)
        for scenario in gated.anticipatory_scenarios:
            assert scenario.confidence <= 0.40

    def test_low_convergence_adds_warning_dissenting_view(self):
        result = _make_result(scenarios=[_make_scenario()])
        gated = gate_synthesis_output(result, 0.0)
        assert "WARNING" in gated.anticipatory_scenarios[0].dissenting_view

    def test_low_convergence_recommendation_explicitly_none(self):
        """HARD CONSTRAINT: recommendation must be explicitly None."""
        result = _make_result(nudges=[_make_nudge()])
        gated = gate_synthesis_output(result, 0.0)
        # Find the no_consensus marker
        marker = next(
            (d for d in gated.unresolved_disagreements
             if d.get("convergence_tier") == "no_consensus"),
            None,
        )
        assert marker is not None
        assert marker["recommendation"] is None
        assert "SUPPRESSED" in marker["provider_note"]

    def test_just_below_threshold_treated_as_no_consensus(self):
        """Score 0.39 must be no_consensus (recommendation suppressed)."""
        result = _make_result(nudges=[_make_nudge()])
        gated = gate_synthesis_output(result, 0.39)
        assert gated.nudge_content == []

    def test_maria_chen_opposing_agents_scenario(self):
        """ARIA says defer, MIRA says change immediately — must produce no_consensus."""
        # Simulating: Score 0.0 because agents have nothing in common
        result = _make_result(
            nudges=[_make_nudge("change_metformin"), _make_nudge("defer_action")],
            scenarios=[
                _make_scenario(scenario_id="aria", confidence=0.85,
                               description="Defer all medication changes"),
                _make_scenario(scenario_id="mira", confidence=0.85,
                               description="Immediate medication adjustment required"),
            ],
        )
        gated = gate_synthesis_output(result, 0.0)
        # All recommendations suppressed
        assert gated.nudge_content == []
        # Both scenarios kept but capped
        assert len(gated.anticipatory_scenarios) == 2
        for scenario in gated.anticipatory_scenarios:
            assert scenario.confidence <= 0.40


class TestRetryLogic:
    """Two-round retry when convergence is too low."""

    @pytest.mark.asyncio
    async def test_no_retry_when_above_threshold(self):
        """Convergence >= RETRY_THRESHOLD: no retry."""
        initial = {"convergence_score": 0.75, "transcript": {}}
        retry_fn = AsyncMock()
        result = await deliberate_with_retry(
            initial_critique_result=initial,
            claude_analysis=MagicMock(),
            gpt4_analysis=MagicMock(),
            context=MagicMock(),
            max_additional_rounds=2,
            run_critique_rounds_fn=retry_fn,
            load_prompt_fn=MagicMock(),
            call_claude_fn=MagicMock(),
            call_gpt4_fn=MagicMock(),
        )
        # Retry function should NOT have been called
        retry_fn.assert_not_called()
        assert result == initial

    @pytest.mark.asyncio
    async def test_retry_when_below_threshold(self):
        """Convergence < RETRY_THRESHOLD: retry runs."""
        initial = {
            "convergence_score": 0.30,
            "transcript": {"r1": "data"},
            "final_claude_revision": _make_analysis("claude-r1"),
            "final_gpt4_revision": _make_analysis("gpt4-r1"),
            "rounds_completed": 1,
        }
        retry_outcome = {
            "convergence_score": 0.75,
            "transcript": {"r2": "data"},
            "rounds_completed": 2,
        }
        retry_fn = AsyncMock(return_value=retry_outcome)
        result = await deliberate_with_retry(
            initial_critique_result=initial,
            claude_analysis=MagicMock(),
            gpt4_analysis=MagicMock(),
            context=MagicMock(),
            max_additional_rounds=2,
            run_critique_rounds_fn=retry_fn,
            load_prompt_fn=MagicMock(),
            call_claude_fn=MagicMock(),
            call_gpt4_fn=MagicMock(),
        )
        # Retry was called
        retry_fn.assert_called_once()
        # Better score wins
        assert result["convergence_score"] == 0.75
        # Transcripts merged
        assert "round1" in result["transcript"]
        assert "round2_retry" in result["transcript"]

    @pytest.mark.asyncio
    async def test_retry_worse_returns_initial(self):
        """If retry score is worse, return the initial result."""
        initial = {
            "convergence_score": 0.50,
            "transcript": {"r1": "data"},
            "final_claude_revision": _make_analysis("claude-r1"),
            "final_gpt4_revision": _make_analysis("gpt4-r1"),
            "rounds_completed": 1,
        }
        retry_outcome = {"convergence_score": 0.20, "transcript": {}, "rounds_completed": 2}
        retry_fn = AsyncMock(return_value=retry_outcome)
        result = await deliberate_with_retry(
            initial_critique_result=initial,
            claude_analysis=MagicMock(),
            gpt4_analysis=MagicMock(),
            context=MagicMock(),
            max_additional_rounds=2,
            run_critique_rounds_fn=retry_fn,
            load_prompt_fn=MagicMock(),
            call_claude_fn=MagicMock(),
            call_gpt4_fn=MagicMock(),
        )
        assert result == initial

    @pytest.mark.asyncio
    async def test_retry_failure_returns_initial(self):
        """If retry crashes, return the initial result gracefully."""
        initial = {"convergence_score": 0.20, "transcript": {}, "rounds_completed": 1,
                   "final_claude_revision": MagicMock(),
                   "final_gpt4_revision": MagicMock()}
        retry_fn = AsyncMock(side_effect=Exception("retry boom"))
        result = await deliberate_with_retry(
            initial_critique_result=initial,
            claude_analysis=MagicMock(),
            gpt4_analysis=MagicMock(),
            context=MagicMock(),
            max_additional_rounds=2,
            run_critique_rounds_fn=retry_fn,
            load_prompt_fn=MagicMock(),
            call_claude_fn=MagicMock(),
            call_gpt4_fn=MagicMock(),
        )
        assert result == initial
