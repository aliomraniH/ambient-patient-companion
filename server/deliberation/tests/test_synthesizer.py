"""Tests for the synthesizer module (Phase 3)."""
from server.deliberation.schemas import (
    AnticipatoryScenario, PredictedPatientQuestion, MissingDataFlag,
    NudgeContent, KnowledgeUpdate, DeliberationResult
)
from datetime import datetime
import pytest


def test_anticipatory_scenario_schema():
    scenario = AnticipatoryScenario(
        scenario_id="s1",
        timeframe="next_90_days",
        title="HbA1c progression",
        description="Without intervention, HbA1c may reach 8.2%",
        probability=0.72,
        confidence=0.80,
        clinical_implications="Risk of microvascular complications increases",
        evidence_basis=["ADA 2026 9.3a"],
        dissenting_view=None
    )
    assert scenario.probability == 0.72


def test_predicted_question_schema():
    q = PredictedPatientQuestion(
        question="Should I take my metformin with food?",
        likelihood=0.85,
        category="medication_understanding",
        suggested_response="Yes, taking metformin with food helps reduce stomach upset.",
        reading_level="6th grade",
        behavioral_framing="facilitator"
    )
    assert q.category == "medication_understanding"


def test_missing_data_flag_both_models_agreed():
    flag = MissingDataFlag(
        flag_id="f1",
        priority="high",
        data_type="lab_result",
        description="Lipid panel overdue by 6 months",
        clinical_relevance="Cannot assess CVD risk accurately",
        recommended_action="Order fasting lipid panel",
        confidence=0.95,
        both_models_agreed=True
    )
    assert flag.both_models_agreed is True


def test_knowledge_update_with_temporal_window():
    ku = KnowledgeUpdate(
        update_type="new_inference",
        scope="patient_specific",
        entry_text="Patient's systolic BP is on an upward trend",
        confidence=0.82,
        valid_from=datetime(2026, 4, 1),
        valid_until=datetime(2026, 7, 1),
        evidence=["vital_trends.systolic_bp"]
    )
    assert ku.valid_until is not None


def test_deliberation_result_requires_five_output_categories():
    """DeliberationResult must include all five output lists."""
    result = DeliberationResult(
        deliberation_id="test-123",
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
        transcript={}
    )
    assert result.deliberation_id == "test-123"
    assert isinstance(result.anticipatory_scenarios, list)
