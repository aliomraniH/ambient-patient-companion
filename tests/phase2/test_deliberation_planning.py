"""
Tests for the deliberation pre-planning and post-synthesis review modules.

DP-1: _detect_interaction_pairs fires for metformin + mood context
DP-2: _detect_interaction_pairs fires for anxiety + glucose context
DP-3: to_prompt_context() produces structured string with known sections
DP-4: Fallback agenda returned when LLM call fails
DP-5: _detect_data_quality_warnings finds 0.0 lab values
DP-6: build_deliberation_agenda includes deterministic pairs even when LLM omits them
DP-7: High-severity objection triggers re_deliberation_needed=True
DP-8: All-concur reviews set re_deliberation_needed=False
DP-9: Reviewer handles asyncio.gather exceptions gracefully

Run with:
    python -m pytest tests/phase2/test_deliberation_planning.py -v
"""

import pytest
from unittest.mock import patch, AsyncMock, MagicMock

from server.deliberation.planner import (
    build_deliberation_agenda,
    _detect_interaction_pairs,
    _detect_data_quality_warnings,
    _fallback_agenda,
    DeliberationAgenda,
    AgendaItem,
)
from server.deliberation.synthesis_reviewer import (
    review_synthesis,
    AgentReview,
    SynthesisReviewResult,
)


# DP-1: Deterministic interaction detection fires for medication + behavioral signals
def test_interaction_detection_medication_behavioral():
    context = "Patient on metformin 1000mg BID. PHQ-9 score declining. Mood: low."
    pairs = _detect_interaction_pairs(context)
    pair_names = [p for p, _ in pairs]
    assert ("medication_change", "behavioral_decline") in pair_names


# DP-2: Deterministic interaction detection fires for anxiety + glucose signals
def test_interaction_detection_anxiety_glucose():
    context = "GAD diagnosis active. HbA1c 7.8%. Fasting glucose elevated."
    pairs = _detect_interaction_pairs(context)
    pair_names = [p for p, _ in pairs]
    assert ("anxiety_elevation", "glucose_dysregulation") in pair_names


# DP-3: to_prompt_context() produces structured string with known sections
def test_agenda_to_prompt_context():
    agenda = DeliberationAgenda(
        items=[
            AgendaItem(
                question="Why is HbA1c rising despite medication adherence?",
                lead_domain="diagnostic_reasoning",
                interaction_flag="Check anxiety->glucose link",
                priority="high",
            )
        ],
        cross_domain_pairs=[("anxiety_elevation", "glucose_dysregulation")],
        data_quality_warnings=["One or more data sources are flagged as stale."],
    )
    text = agenda.to_prompt_context()
    assert "DELIBERATION AGENDA" in text
    assert "HbA1c rising" in text
    assert "diagnostic_reasoning" in text
    assert "anxiety_elevation" in text
    assert "stale" in text


# DP-4: Fallback agenda returned when LLM call fails
@pytest.mark.asyncio
async def test_fallback_agenda_on_llm_failure():
    context = "Patient on metformin. GAD active. Glucose elevated."
    mock_context = MagicMock()
    mock_context.model_dump.return_value = {"text": context}

    with patch(
        "server.deliberation.planner._llm_generate_agenda",
        side_effect=RuntimeError("API down"),
    ):
        agenda = await build_deliberation_agenda(mock_context, "test-delib-id")

    assert isinstance(agenda, DeliberationAgenda)
    assert len(agenda.items) >= 1


# DP-5: Data quality warnings detected for 0.0 lab values
def test_data_quality_warnings_zero_lab_values():
    context = "Lab results: HbA1c = 0.0, LDL = 0.0, eGFR = 0.0. All labs show 0.0."
    warnings = _detect_data_quality_warnings(context)
    assert len(warnings) > 0
    assert any("0.0" in w for w in warnings)


# DP-6: build_deliberation_agenda includes deterministic pairs even when LLM omits them
@pytest.mark.asyncio
async def test_build_agenda_includes_deterministic_pairs():
    context_text = (
        "Medications: Metformin 1000mg BID, Lisinopril 10mg QD. "
        "PHQ-9: 14 (moderate depression). Sleep: 5.2h avg. Mood: low."
    )
    mock_context = MagicMock()
    mock_context.model_dump.return_value = {"data": context_text}

    # LLM returns agenda with NO cross_domain_pairs
    mock_llm_result = DeliberationAgenda(
        items=[AgendaItem("Test question", "diagnostic_reasoning", priority="medium")],
        cross_domain_pairs=[],
    )
    with patch(
        "server.deliberation.planner._llm_generate_agenda",
        new_callable=AsyncMock,
        return_value=mock_llm_result,
    ):
        with patch(
            "server.deliberation.planner._context_to_text",
            return_value=context_text,
        ):
            agenda = await build_deliberation_agenda(mock_context, "test-id")

    # Deterministic detection should have added the medication + behavioral pair
    assert ("medication_change", "behavioral_decline") in agenda.cross_domain_pairs


# DP-7: High-severity objection triggers re_deliberation_needed=True
@pytest.mark.asyncio
async def test_high_severity_objection_triggers_redeliberation():
    diag_review = AgentReview(
        domain="diagnostic_reasoning",
        concurs=False,
        objection="Synthesis missed that Metformin dose should be reduced given eGFR 42",
        severity="high",
        missed_interaction="renal_function <-> medication_safety",
    )
    treat_review = AgentReview(domain="treatment_optimization", concurs=True)

    mock_agenda = MagicMock()
    mock_agenda.to_prompt_context.return_value = "mock agenda"

    with patch(
        "server.deliberation.synthesis_reviewer._single_domain_review",
        new_callable=AsyncMock,
        side_effect=[diag_review, treat_review],
    ):
        result = await review_synthesis(
            synthesis_text="Synthesis: continue current medications.",
            patient_context_text="eGFR 42. On Metformin 1000mg BID.",
            agenda=mock_agenda,
            deliberation_id="test-delib-id",
        )

    assert result.re_deliberation_needed is True
    assert "Metformin" in result.re_deliberation_focus or "eGFR" in result.re_deliberation_focus


# DP-8: All-concur reviews set re_deliberation_needed=False
@pytest.mark.asyncio
async def test_all_concur_no_redeliberation():
    reviews = [
        AgentReview(domain="diagnostic_reasoning", concurs=True),
        AgentReview(domain="treatment_optimization", concurs=True),
    ]
    mock_agenda = MagicMock()
    mock_agenda.to_prompt_context.return_value = ""

    with patch(
        "server.deliberation.synthesis_reviewer._single_domain_review",
        new_callable=AsyncMock,
        side_effect=reviews,
    ):
        result = await review_synthesis(
            synthesis_text="Synthesis: good overall management.",
            patient_context_text="Stable patient, all values within range.",
            agenda=mock_agenda,
            deliberation_id="test-delib-id",
        )

    assert result.re_deliberation_needed is False
    assert result.consensus_reached is True


# DP-9: Reviewer handles asyncio.gather exceptions gracefully
@pytest.mark.asyncio
async def test_reviewer_handles_exceptions_gracefully():
    mock_agenda = MagicMock()
    mock_agenda.to_prompt_context.return_value = ""

    with patch(
        "server.deliberation.synthesis_reviewer._single_domain_review",
        new_callable=AsyncMock,
        side_effect=[
            RuntimeError("API timeout"),
            AgentReview(domain="treatment_optimization", concurs=True),
        ],
    ):
        result = await review_synthesis(
            synthesis_text="Synthesis output.",
            patient_context_text="Patient context.",
            agenda=mock_agenda,
            deliberation_id="test-delib-id",
        )

    # Should not crash — one review failed, one succeeded
    assert result.consensus_reached is True
    assert len(result.reviews) == 1  # Only the successful review
