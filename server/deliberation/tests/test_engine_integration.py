"""
Integration tests for the Dual-LLM Deliberation Engine.
Tests Phase 1 only by default (no real API calls) using mock fixtures.
Set RUN_LIVE_TESTS=true for full end-to-end with real API calls.
"""
import json
import os
import pytest
from pathlib import Path
from unittest.mock import AsyncMock, patch, MagicMock

from server.deliberation.schemas import (
    PatientContextPackage, IndependentAnalysis, DeliberationResult,
    ClaimWithConfidence, RevisedAnalysis
)
from server.deliberation.behavioral_adapter import (
    validate_sms_length, estimate_reading_grade, adapt_nudges
)
from server.deliberation.critic import _compute_convergence, _analysis_from_revision
from server.deliberation.schemas import NudgeContent


FIXTURES = Path(__file__).parent / "fixtures"


def load_maria_chen() -> PatientContextPackage:
    data = json.loads((FIXTURES / "maria_chen_context.json").read_text())
    return PatientContextPackage(**data)


class TestContextCompiler:
    def test_fixture_validates_as_context_package(self):
        """Maria Chen fixture must pass Pydantic validation."""
        ctx = load_maria_chen()
        assert ctx.patient_id == "4829341"
        assert ctx.age == 54
        assert len(ctx.active_conditions) == 3
        assert len(ctx.current_medications) == 3
        assert len(ctx.recent_labs) == 4
        assert len(ctx.care_gaps) == 3

    def test_fixture_has_required_fields(self):
        ctx = load_maria_chen()
        assert ctx.mrn == "4829341"
        assert ctx.primary_provider == "Dr. Rahul Patel"
        assert ctx.days_since_last_encounter == 45
        assert ctx.deliberation_trigger == "scheduled_pre_encounter"


class TestSchemas:
    def test_independent_analysis_schema(self):
        analysis = IndependentAnalysis(
            model_id="claude-sonnet-4-20250514",
            role_emphasis="diagnostic_reasoning",
            key_findings=[],
            risk_flags=[],
            recommended_actions=[],
            anticipated_trajectory="Test trajectory",
            missing_data_identified=[],
            raw_reasoning="Test"
        )
        assert analysis.model_id == "claude-sonnet-4-20250514"

    def test_claim_with_confidence_bounds(self):
        claim = ClaimWithConfidence(claim="test", confidence=0.85)
        assert claim.confidence == 0.85

        with pytest.raises(Exception):
            ClaimWithConfidence(claim="test", confidence=1.5)

        with pytest.raises(Exception):
            ClaimWithConfidence(claim="test", confidence=-0.1)

    def test_deliberation_result_requires_all_fields(self):
        """Deliberation result must fail without required fields."""
        with pytest.raises(Exception):
            DeliberationResult()


class TestConvergence:
    def test_identical_findings_converge(self):
        a = RevisedAnalysis(
            model_id="claude", round_number=1,
            revised_findings=[
                ClaimWithConfidence(claim="HbA1c above target", confidence=0.9),
                ClaimWithConfidence(claim="BP trending up", confidence=0.8),
            ],
            revisions_made=[], maintained_positions=[], raw_revision=""
        )
        b = RevisedAnalysis(
            model_id="gpt4", round_number=1,
            revised_findings=[
                ClaimWithConfidence(claim="HbA1c above target", confidence=0.9),
                ClaimWithConfidence(claim="BP trending up", confidence=0.8),
            ],
            revisions_made=[], maintained_positions=[], raw_revision=""
        )
        assert _compute_convergence(a, b) == 1.0

    def test_no_overlap_zero_convergence(self):
        a = RevisedAnalysis(
            model_id="claude", round_number=1,
            revised_findings=[
                ClaimWithConfidence(claim="Finding A", confidence=0.9),
            ],
            revisions_made=[], maintained_positions=[], raw_revision=""
        )
        b = RevisedAnalysis(
            model_id="gpt4", round_number=1,
            revised_findings=[
                ClaimWithConfidence(claim="Finding B", confidence=0.8),
            ],
            revisions_made=[], maintained_positions=[], raw_revision=""
        )
        assert _compute_convergence(a, b) == 0.0

    def test_empty_findings_zero_convergence(self):
        a = RevisedAnalysis(
            model_id="claude", round_number=1,
            revised_findings=[],
            revisions_made=[], maintained_positions=[], raw_revision=""
        )
        b = RevisedAnalysis(
            model_id="gpt4", round_number=1,
            revised_findings=[],
            revisions_made=[], maintained_positions=[], raw_revision=""
        )
        assert _compute_convergence(a, b) == 0.0


class TestAnalysisFromRevision:
    def test_converts_revision_to_analysis(self):
        revision = RevisedAnalysis(
            model_id="claude-sonnet-4-20250514", round_number=2,
            revised_findings=[
                ClaimWithConfidence(claim="HbA1c elevated", confidence=0.95)
            ],
            revisions_made=["Updated confidence"],
            maintained_positions=["BP assessment"],
            raw_revision="Full text"
        )
        analysis = _analysis_from_revision(revision)
        assert analysis.model_id == "claude-sonnet-4-20250514"
        assert analysis.role_emphasis == "diagnostic_reasoning"
        assert len(analysis.key_findings) == 1

    def test_gpt4_model_gets_treatment_emphasis(self):
        revision = RevisedAnalysis(
            model_id="gpt-4o", round_number=1,
            revised_findings=[], revisions_made=[],
            maintained_positions=[], raw_revision=""
        )
        analysis = _analysis_from_revision(revision)
        assert analysis.role_emphasis == "treatment_optimization"


class TestBehavioralAdapter:
    def test_sms_within_limit_unchanged(self):
        text = "Take your medication today."
        assert validate_sms_length(text) == text

    def test_sms_over_limit_truncated(self):
        text = "A" * 200
        result = validate_sms_length(text)
        assert len(result) <= 160
        assert result.endswith("...")

    def test_sms_truncates_at_word_boundary(self):
        text = "Please remember " + "word " * 40
        result = validate_sms_length(text)
        assert len(result) <= 160
        assert not result.endswith(" ...")

    def test_reading_grade_returns_float(self):
        grade = estimate_reading_grade("Take your medicine every day with food.")
        assert isinstance(grade, float)

    def test_adapt_nudges_adds_provider_reminder(self):
        nudge = NudgeContent(
            nudge_id="test-1",
            target="patient",
            trigger_condition="daily_morning",
            behavioral_technique="BCT_1.4",
            com_b_target="capability",
            channels={"sms": "Take your meds", "portal": "Remember to take your medication."},
            reading_level="6th grade",
            personalization_factors=["morning_routine"]
        )
        result = adapt_nudges([nudge])
        assert len(result) == 1
        assert "healthcare provider" in result[0].channels["portal"].lower()

    def test_adapt_nudges_skips_care_team(self):
        nudge = NudgeContent(
            nudge_id="team-1",
            target="care_team",
            trigger_condition="pre_encounter",
            behavioral_technique="BCT_2.1",
            com_b_target="motivation",
            channels={"portal": "Review patient chart before visit."},
            reading_level="professional",
            personalization_factors=[]
        )
        result = adapt_nudges([nudge])
        assert "healthcare provider" not in result[0].channels["portal"].lower()


class TestKnowledgeStore:
    @pytest.mark.asyncio
    async def test_commit_requires_valid_result(self):
        """Knowledge store commit must reject invalid DeliberationResult."""
        from server.deliberation.knowledge_store import commit_deliberation
        mock_pool = MagicMock()
        mock_conn = AsyncMock()
        mock_pool.acquire.return_value.__aenter__ = AsyncMock(return_value=mock_conn)
        mock_pool.acquire.return_value.__aexit__ = AsyncMock(return_value=None)

        with pytest.raises(Exception):
            await commit_deliberation(
                result=None,
                db_pool=mock_pool,
                convergence_score=0.9,
                rounds_completed=2,
                total_tokens=1000,
                total_latency_ms=5000,
                synthesizer_model="claude-sonnet-4-20250514"
            )


@pytest.mark.skipif(
    os.environ.get("RUN_LIVE_TESTS") != "true",
    reason="Live API tests require RUN_LIVE_TESTS=true and API keys"
)
class TestLiveDeliberation:
    """Full end-to-end tests against real APIs. Slow and costs tokens."""
    @pytest.mark.asyncio
    async def test_full_deliberation_maria_chen(self):
        from server.deliberation.engine import DeliberationEngine
        from server.deliberation.schemas import DeliberationRequest
        # Requires: ANTHROPIC_API_KEY, OPENAI_API_KEY, DATABASE_URL
        engine = DeliberationEngine(
            db_pool=None,  # Would need real pool
            vector_store=None
        )
        result = await engine.run(DeliberationRequest(
            patient_id="4829341",
            trigger_type="manual",
            max_rounds=2
        ))
        assert result.deliberation_id is not None
        assert len(result.anticipatory_scenarios) >= 1
        assert len(result.missing_data_flags) >= 1
        assert len(result.predicted_patient_questions) >= 1
        assert len(result.nudge_content) >= 1
        assert result.convergence_score > 0.0
