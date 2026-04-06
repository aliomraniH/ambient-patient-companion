"""
Comprehensive test suite for the Dual-LLM Deliberation Engine features.

Run with:
    python -m pytest tests/phase2/test_deliberation_features.py -v --tb=short

These tests are designed for Replit Agent to execute. All tests use mocked
API calls — no ANTHROPIC_API_KEY or OPENAI_API_KEY required.

Test categories:
    A. Schema Validation (7 tests)
    B. Context Compilation & Fixtures (5 tests)
    C. Analyst Phase — Prompt Loading (4 tests)
    D. Critic Phase — Convergence & Revision (6 tests)
    E. Synthesizer Phase — Output Schemas (5 tests)
    F. Behavioral Adapter — Nudge Formatting (8 tests)
    G. Knowledge Store — DB Persistence (3 tests)
    H. Engine Orchestration — Full Pipeline (3 tests)
    I. MCP Tool Integration (4 tests)
    J. Security & Compliance (5 tests)
"""

import json
import os
import re
import pytest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

# ── Schema imports ────────────────────────────────────────────────────────────
from server.deliberation.schemas import (
    PatientContextPackage,
    DeliberationRequest,
    IndependentAnalysis,
    ClaimWithConfidence,
    CritiqueItem,
    CrossCritique,
    RevisedAnalysis,
    AnticipatoryScenario,
    PredictedPatientQuestion,
    MissingDataFlag,
    NudgeContent,
    KnowledgeUpdate,
    DeliberationResult,
)

# ── Module imports ────────────────────────────────────────────────────────────
from server.deliberation.analyst import _load_prompt
from server.deliberation.critic import (
    _compute_convergence,
    _analysis_from_revision,
    CONVERGENCE_THRESHOLD,
)
from server.deliberation.behavioral_adapter import (
    validate_sms_length,
    estimate_reading_grade,
    adapt_nudges,
    SMS_MAX_CHARS,
)

FIXTURES = Path(__file__).parents[1].parent / "server" / "deliberation" / "tests" / "fixtures"


def _load_maria_chen() -> PatientContextPackage:
    data = json.loads((FIXTURES / "maria_chen_context.json").read_text())
    return PatientContextPackage(**data)


def _make_claim(claim: str, confidence: float = 0.85) -> ClaimWithConfidence:
    return ClaimWithConfidence(claim=claim, confidence=confidence, evidence_refs=[])


def _make_analysis(model_id: str, findings: list[str]) -> IndependentAnalysis:
    return IndependentAnalysis(
        model_id=model_id,
        role_emphasis="diagnostic_reasoning" if "claude" in model_id else "treatment_optimization",
        key_findings=[_make_claim(f) for f in findings],
        risk_flags=[],
        recommended_actions=[],
        anticipated_trajectory="Stable if treated",
        missing_data_identified=[],
        raw_reasoning="Test reasoning",
    )


def _make_revision(model_id: str, findings: list[str], round_num: int = 1) -> RevisedAnalysis:
    return RevisedAnalysis(
        model_id=model_id,
        round_number=round_num,
        revised_findings=[_make_claim(f) for f in findings],
        revisions_made=[],
        maintained_positions=[],
        raw_revision="Test revision",
    )


def _make_nudge(target: str = "patient", sms: str = "Take your meds",
                portal: str = "Remember your medication.") -> NudgeContent:
    return NudgeContent(
        nudge_id="n1",
        target=target,
        trigger_condition="daily_morning",
        behavioral_technique="BCT_1.4_action_planning",
        com_b_target="capability",
        channels={"sms": sms, "portal": portal},
        reading_level="6th grade",
        personalization_factors=["morning_routine"],
    )


def _make_result(**overrides) -> DeliberationResult:
    defaults = dict(
        deliberation_id="test-dlb-001",
        patient_id="4829341",
        timestamp=datetime.utcnow(),
        trigger="manual",
        models={"claude": "claude-sonnet-4-20250514", "gpt4": "gpt-4o"},
        rounds_completed=2,
        convergence_score=0.85,
        total_tokens=5000,
        total_latency_ms=45000,
        anticipatory_scenarios=[],
        predicted_patient_questions=[],
        missing_data_flags=[],
        nudge_content=[],
        knowledge_updates=[],
        unresolved_disagreements=[],
        transcript={},
    )
    defaults.update(overrides)
    return DeliberationResult(**defaults)


# ═══════════════════════════════════════════════════════════════════════════════
# A. SCHEMA VALIDATION
# ═══════════════════════════════════════════════════════════════════════════════


class TestSchemaValidation:
    """Verify all Pydantic models enforce their constraints."""

    def test_claim_confidence_lower_bound(self):
        with pytest.raises(Exception):
            ClaimWithConfidence(claim="test", confidence=-0.1)

    def test_claim_confidence_upper_bound(self):
        with pytest.raises(Exception):
            ClaimWithConfidence(claim="test", confidence=1.1)

    def test_claim_confidence_valid_range(self):
        c = ClaimWithConfidence(claim="HbA1c elevated", confidence=0.0)
        assert c.confidence == 0.0
        c2 = ClaimWithConfidence(claim="HbA1c elevated", confidence=1.0)
        assert c2.confidence == 1.0

    def test_deliberation_request_max_rounds_constraint(self):
        with pytest.raises(Exception):
            DeliberationRequest(patient_id="x", trigger_type="manual", max_rounds=10)

    def test_deliberation_request_min_rounds_constraint(self):
        with pytest.raises(Exception):
            DeliberationRequest(patient_id="x", trigger_type="manual", max_rounds=0)

    def test_deliberation_result_missing_required_field(self):
        with pytest.raises(Exception):
            DeliberationResult(deliberation_id="x")

    def test_knowledge_update_temporal_window(self):
        ku = KnowledgeUpdate(
            update_type="new_inference",
            scope="patient_specific",
            entry_text="BP trending up",
            confidence=0.82,
            valid_from=datetime(2026, 1, 1),
            valid_until=datetime(2026, 6, 1),
        )
        assert ku.valid_until > ku.valid_from


# ═══════════════════════════════════════════════════════════════════════════════
# B. CONTEXT COMPILATION & FIXTURES
# ═══════════════════════════════════════════════════════════════════════════════


class TestContextFixtures:
    """Verify the Maria Chen canonical fixture is correct and complete."""

    def test_fixture_file_exists(self):
        assert (FIXTURES / "maria_chen_context.json").exists()

    def test_fixture_pydantic_validation(self):
        ctx = _load_maria_chen()
        assert isinstance(ctx, PatientContextPackage)

    def test_canonical_patient_identity(self):
        ctx = _load_maria_chen()
        assert ctx.patient_name == "Maria Chen"
        assert ctx.mrn == "4829341"
        assert ctx.age == 54
        assert ctx.sex == "F"
        assert ctx.primary_provider == "Dr. Rahul Patel"

    def test_patient_has_three_conditions(self):
        ctx = _load_maria_chen()
        codes = {c["code"] for c in ctx.active_conditions}
        assert "E11.9" in codes   # T2DM
        assert "I10" in codes     # Hypertension
        assert "F41.1" in codes   # Anxiety

    def test_patient_has_upcoming_appointment(self):
        ctx = _load_maria_chen()
        assert len(ctx.upcoming_appointments) >= 1
        assert ctx.upcoming_appointments[0]["provider_name"] == "Dr. Rahul Patel"


# ═══════════════════════════════════════════════════════════════════════════════
# C. ANALYST PHASE — PROMPT LOADING
# ═══════════════════════════════════════════════════════════════════════════════


class TestAnalystPrompts:
    """Verify prompt templates load and substitute correctly."""

    def test_claude_analyst_prompt_loads(self):
        prompt = _load_prompt("analyst_claude.xml", {
            "PATIENT_CONTEXT_JSON": "{}",
            "GUIDELINES_JSON": "[]",
            "PRIOR_KNOWLEDGE_JSON": "[]",
        })
        assert "Diagnostic Reasoning Analyst" in prompt

    def test_gpt4_analyst_prompt_loads(self):
        prompt = _load_prompt("analyst_gpt4.xml", {
            "PATIENT_CONTEXT_JSON": "{}",
            "GUIDELINES_JSON": "[]",
            "PRIOR_KNOWLEDGE_JSON": "[]",
        })
        assert "Treatment Optimization Analyst" in prompt

    def test_prompt_substitution_complete(self):
        ctx = _load_maria_chen()
        prompt = _load_prompt("analyst_claude.xml", {
            "PATIENT_CONTEXT_JSON": ctx.model_dump_json(indent=2),
            "GUIDELINES_JSON": json.dumps(ctx.applicable_guidelines),
            "PRIOR_KNOWLEDGE_JSON": "[]",
        })
        assert "Maria Chen" in prompt
        assert "{{PATIENT_CONTEXT_JSON}}" not in prompt
        assert "{{GUIDELINES_JSON}}" not in prompt

    def test_all_prompt_files_exist(self):
        prompts_dir = Path(__file__).parents[1].parent / "server" / "deliberation" / "prompts"
        expected = [
            "analyst_claude.xml", "analyst_gpt4.xml",
            "critic_claude.xml", "critic_gpt4.xml",
            "synthesizer.xml",
        ]
        for f in expected:
            assert (prompts_dir / f).exists(), f"Missing prompt file: {f}"


# ═══════════════════════════════════════════════════════════════════════════════
# D. CRITIC PHASE — CONVERGENCE & REVISION
# ═══════════════════════════════════════════════════════════════════════════════


class TestCriticConvergence:
    """Test the convergence detection between revised analyses."""

    def test_perfect_convergence(self):
        findings = ["HbA1c above target", "BP trending upward"]
        a = _make_revision("claude", findings)
        b = _make_revision("gpt4", findings)
        assert _compute_convergence(a, b) == 1.0

    def test_zero_convergence(self):
        a = _make_revision("claude", ["Finding A"])
        b = _make_revision("gpt4", ["Finding B"])
        assert _compute_convergence(a, b) == 0.0

    def test_partial_convergence_is_jaccard(self):
        a = _make_revision("claude", ["shared finding", "only claude"])
        b = _make_revision("gpt4", ["shared finding", "only gpt4"])
        score = _compute_convergence(a, b)
        # Jaccard: 1 shared / 3 unique = 0.333...
        assert abs(score - 1 / 3) < 0.01

    def test_empty_findings_returns_zero(self):
        a = _make_revision("claude", [])
        b = _make_revision("gpt4", [])
        assert _compute_convergence(a, b) == 0.0

    def test_convergence_threshold_is_configured(self):
        assert CONVERGENCE_THRESHOLD == 0.90

    def test_analysis_from_revision_preserves_model_role(self):
        rev_claude = _make_revision("claude-sonnet-4-20250514", ["finding"])
        rev_gpt4 = _make_revision("gpt-4o", ["finding"])
        a_claude = _analysis_from_revision(rev_claude)
        a_gpt4 = _analysis_from_revision(rev_gpt4)
        assert a_claude.role_emphasis == "diagnostic_reasoning"
        assert a_gpt4.role_emphasis == "treatment_optimization"


# ═══════════════════════════════════════════════════════════════════════════════
# E. SYNTHESIZER PHASE — OUTPUT SCHEMAS
# ═══════════════════════════════════════════════════════════════════════════════


class TestSynthesizerOutputs:
    """Verify synthesizer output schemas are correctly structured."""

    def test_anticipatory_scenario_with_dissent(self):
        s = AnticipatoryScenario(
            scenario_id="s1",
            timeframe="next_90_days",
            title="HbA1c progression risk",
            description="HbA1c may reach 8.5% without SGLT2i addition",
            probability=0.72,
            confidence=0.80,
            clinical_implications="Microvascular risk increases",
            evidence_basis=["ADA 2026 9.3a", "vital_trends"],
            dissenting_view="GPT-4 estimated lower probability (0.55)",
        )
        assert s.dissenting_view is not None
        assert s.probability > 0.7

    def test_predicted_question_all_fields(self):
        q = PredictedPatientQuestion(
            question="Do I need to fast before my next blood test?",
            likelihood=0.80,
            category="logistics",
            suggested_response="Yes, you should fast for 8-12 hours before a fasting lipid panel.",
            reading_level="6th grade",
            behavioral_framing="facilitator",
        )
        assert q.reading_level == "6th grade"

    def test_missing_data_flag_consensus(self):
        flag = MissingDataFlag(
            flag_id="f1",
            priority="high",
            data_type="lab_result",
            description="Lipid panel overdue by 6 months",
            clinical_relevance="CVD risk assessment incomplete",
            recommended_action="Order fasting lipid panel",
            confidence=0.95,
            both_models_agreed=True,
        )
        assert flag.both_models_agreed is True
        assert flag.priority == "high"

    def test_result_serialization_roundtrip(self):
        result = _make_result()
        json_str = result.model_dump_json()
        restored = DeliberationResult.model_validate_json(json_str)
        assert restored.deliberation_id == result.deliberation_id
        assert restored.convergence_score == result.convergence_score

    def test_result_with_all_five_categories(self):
        result = _make_result(
            anticipatory_scenarios=[
                AnticipatoryScenario(
                    scenario_id="s1", timeframe="next_30_days",
                    title="T", description="D", probability=0.5,
                    confidence=0.7, clinical_implications="I",
                    evidence_basis=["E"],
                )
            ],
            predicted_patient_questions=[
                PredictedPatientQuestion(
                    question="Q?", likelihood=0.8, category="medication_understanding",
                    suggested_response="A.", reading_level="6", behavioral_framing="spark",
                )
            ],
            missing_data_flags=[
                MissingDataFlag(
                    flag_id="f1", priority="medium", data_type="screening",
                    description="D", clinical_relevance="R",
                    recommended_action="A", confidence=0.9,
                )
            ],
            nudge_content=[_make_nudge()],
            knowledge_updates=[
                KnowledgeUpdate(
                    update_type="new_inference", scope="patient_specific",
                    entry_text="T", confidence=0.8, valid_from=datetime.utcnow(),
                )
            ],
        )
        assert len(result.anticipatory_scenarios) == 1
        assert len(result.predicted_patient_questions) == 1
        assert len(result.missing_data_flags) == 1
        assert len(result.nudge_content) == 1
        assert len(result.knowledge_updates) == 1


# ═══════════════════════════════════════════════════════════════════════════════
# F. BEHAVIORAL ADAPTER — NUDGE FORMATTING
# ═══════════════════════════════════════════════════════════════════════════════


class TestBehavioralAdapter:
    """Test SMS truncation, reading level, and nudge adaptation."""

    def test_sms_under_limit_unchanged(self):
        text = "Take your medication today."
        assert validate_sms_length(text) == text

    def test_sms_exactly_160_chars(self):
        text = "A" * 160
        assert validate_sms_length(text) == text
        assert len(validate_sms_length(text)) == 160

    def test_sms_over_limit_truncated_with_ellipsis(self):
        text = "word " * 50  # 250 chars
        result = validate_sms_length(text)
        assert len(result) <= SMS_MAX_CHARS
        assert result.endswith("...")

    def test_sms_truncation_preserves_word_boundaries(self):
        text = "Please remember to take your daily medication as prescribed " * 5
        result = validate_sms_length(text)
        assert not result.endswith(" ...")  # Space before ... means mid-word

    def test_reading_grade_simple_text(self):
        grade = estimate_reading_grade("The cat sat on the mat.")
        assert isinstance(grade, float)
        assert grade < 10  # Simple text should be low grade

    def test_patient_nudge_gets_provider_reminder(self):
        nudge = _make_nudge(target="patient", portal="Remember to check your blood sugar.")
        result = adapt_nudges([nudge])
        assert "healthcare provider" in result[0].channels["portal"].lower()

    def test_care_team_nudge_no_provider_reminder(self):
        nudge = _make_nudge(target="care_team", portal="Review chart before visit.")
        result = adapt_nudges([nudge])
        assert "healthcare provider" not in result[0].channels["portal"].lower()

    def test_patient_nudge_sms_enforced(self):
        long_sms = "A" * 200
        nudge = _make_nudge(target="patient", sms=long_sms)
        result = adapt_nudges([nudge])
        assert len(result[0].channels["sms"]) <= SMS_MAX_CHARS


# ═══════════════════════════════════════════════════════════════════════════════
# G. KNOWLEDGE STORE — DB PERSISTENCE
# ═══════════════════════════════════════════════════════════════════════════════


class TestKnowledgeStore:
    """Test database persistence with mocked asyncpg pool."""

    @staticmethod
    def _mock_pool():
        mock_conn = AsyncMock()
        mock_tx_cm = MagicMock()
        mock_tx_cm.__aenter__ = AsyncMock(return_value=None)
        mock_tx_cm.__aexit__ = AsyncMock(return_value=None)
        mock_conn.transaction = MagicMock(return_value=mock_tx_cm)

        mock_acquire_cm = MagicMock()
        mock_acquire_cm.__aenter__ = AsyncMock(return_value=mock_conn)
        mock_acquire_cm.__aexit__ = AsyncMock(return_value=None)

        mock_pool = MagicMock()
        mock_pool.acquire.return_value = mock_acquire_cm
        return mock_pool, mock_conn

    @pytest.mark.asyncio
    async def test_commit_returns_deliberation_id(self):
        from server.deliberation.knowledge_store import commit_deliberation
        pool, conn = self._mock_pool()
        result = _make_result(
            anticipatory_scenarios=[
                AnticipatoryScenario(
                    scenario_id="s1", timeframe="next_30_days",
                    title="T", description="D", probability=0.5,
                    confidence=0.7, clinical_implications="I",
                    evidence_basis=["E"],
                )
            ],
        )
        dlb_id = await commit_deliberation(
            result=result, db_pool=pool,
            convergence_score=0.85, rounds_completed=2,
            total_tokens=5000, total_latency_ms=45000,
            synthesizer_model="claude-sonnet-4-20250514",
        )
        assert dlb_id == "test-dlb-001"

    @pytest.mark.asyncio
    async def test_commit_calls_execute_for_each_output(self):
        from server.deliberation.knowledge_store import commit_deliberation
        pool, conn = self._mock_pool()
        result = _make_result(
            anticipatory_scenarios=[
                AnticipatoryScenario(
                    scenario_id="s1", timeframe="next_30_days",
                    title="T", description="D", probability=0.8,
                    confidence=0.9, clinical_implications="I",
                    evidence_basis=["E"],
                )
            ],
            missing_data_flags=[
                MissingDataFlag(
                    flag_id="f1", priority="high", data_type="lab_result",
                    description="D", clinical_relevance="R",
                    recommended_action="A", confidence=0.9,
                )
            ],
        )
        await commit_deliberation(
            result=result, db_pool=pool,
            convergence_score=0.85, rounds_completed=2,
            total_tokens=5000, total_latency_ms=45000,
            synthesizer_model="claude-sonnet-4-20250514",
        )
        # 1 deliberation + 1 scenario + 1 flag = at least 3 execute calls
        assert conn.execute.call_count >= 3

    @pytest.mark.asyncio
    async def test_commit_rejects_none_result(self):
        from server.deliberation.knowledge_store import commit_deliberation
        pool, _ = self._mock_pool()
        with pytest.raises(Exception):
            await commit_deliberation(
                result=None, db_pool=pool,
                convergence_score=0.9, rounds_completed=1,
                total_tokens=1000, total_latency_ms=5000,
                synthesizer_model="claude-sonnet-4-20250514",
            )


# ═══════════════════════════════════════════════════════════════════════════════
# H. ENGINE ORCHESTRATION — FULL PIPELINE (MOCKED)
# ═══════════════════════════════════════════════════════════════════════════════


class TestEngineOrchestration:
    """Test the engine orchestrator with fully mocked API calls."""

    def test_deliberation_request_defaults(self):
        req = DeliberationRequest(patient_id="4829341", trigger_type="manual")
        assert req.max_rounds == 3
        assert req.force_round_count is None

    def test_deliberation_request_valid_trigger_types(self):
        for trigger in [
            "scheduled_pre_encounter", "lab_result_received",
            "medication_change", "missed_appointment",
            "temporal_threshold", "manual",
        ]:
            req = DeliberationRequest(patient_id="x", trigger_type=trigger)
            assert req.trigger_type == trigger

    def test_engine_class_importable(self):
        from server.deliberation.engine import DeliberationEngine
        engine = DeliberationEngine(db_pool=MagicMock(), vector_store=MagicMock())
        assert engine.db_pool is not None
        assert engine.vector_store is not None


# ═══════════════════════════════════════════════════════════════════════════════
# I. MCP TOOL INTEGRATION
# ═══════════════════════════════════════════════════════════════════════════════


class TestMCPToolIntegration:
    """Verify the 4 new MCP tools are importable and correctly defined."""

    def test_run_deliberation_importable(self):
        from server.mcp_server import run_deliberation
        assert callable(run_deliberation)

    def test_get_deliberation_results_importable(self):
        from server.mcp_server import get_deliberation_results
        assert callable(get_deliberation_results)

    def test_get_patient_knowledge_importable(self):
        from server.mcp_server import get_patient_knowledge
        assert callable(get_patient_knowledge)

    def test_get_pending_nudges_importable(self):
        from server.mcp_server import get_pending_nudges
        assert callable(get_pending_nudges)


# ═══════════════════════════════════════════════════════════════════════════════
# J. SECURITY & COMPLIANCE
# ═══════════════════════════════════════════════════════════════════════════════


class TestSecurityCompliance:
    """Verify no PHI leakage, no hardcoded keys, and compliance rules."""

    def test_no_hardcoded_api_keys_in_deliberation_modules(self):
        """Scan all deliberation Python files for hardcoded API keys."""
        delib_dir = Path(__file__).parents[1].parent / "server" / "deliberation"
        key_patterns = [r"sk-ant-[a-zA-Z0-9]", r"sk-proj-[a-zA-Z0-9]", r"sk-[a-zA-Z0-9]{20,}"]
        for py_file in delib_dir.rglob("*.py"):
            content = py_file.read_text()
            for pattern in key_patterns:
                matches = re.findall(pattern, content)
                assert not matches, f"Hardcoded key in {py_file.name}: {matches}"

    def test_no_patient_name_in_log_statements(self):
        """No print/logging statements should reference patient_name or mrn."""
        delib_dir = Path(__file__).parents[1].parent / "server" / "deliberation"
        forbidden = [r"print\(.*patient_name", r"logging\..*mrn", r"print\(.*mrn"]
        for py_file in delib_dir.rglob("*.py"):
            content = py_file.read_text()
            for pattern in forbidden:
                assert not re.search(pattern, content), (
                    f"PHI leak risk in {py_file.name}: pattern '{pattern}'"
                )

    def test_patient_nudge_always_has_disclaimer(self):
        """Patient-facing nudges must include provider sign-off after adaptation."""
        nudge = _make_nudge(target="patient", portal="Check your blood sugar daily.")
        adapted = adapt_nudges([nudge])
        portal_text = adapted[0].channels["portal"]
        assert "healthcare provider" in portal_text.lower()

    def test_sms_never_exceeds_160_chars(self):
        """All SMS content must be <= 160 characters after adaptation."""
        for length in [50, 160, 200, 500]:
            nudge = _make_nudge(target="patient", sms="x" * length)
            adapted = adapt_nudges([nudge])
            assert len(adapted[0].channels["sms"]) <= 160

    def test_db_migration_file_exists(self):
        """The SQL migration must exist for reproducible deployment."""
        migration = (
            Path(__file__).parents[1].parent
            / "server" / "deliberation" / "migrations"
            / "001_deliberation_tables.sql"
        )
        assert migration.exists()
        content = migration.read_text()
        assert "CREATE TABLE IF NOT EXISTS deliberations" in content
        assert "CREATE TABLE IF NOT EXISTS deliberation_outputs" in content
        assert "CREATE TABLE IF NOT EXISTS patient_knowledge" in content
        assert "CREATE TABLE IF NOT EXISTS core_knowledge_updates" in content
