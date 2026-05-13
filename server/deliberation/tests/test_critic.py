"""Tests for the critic module (Phase 2)."""
from server.deliberation.schemas import (
    ClaimWithConfidence, RevisedAnalysis, CrossCritique, CritiqueItem
)
from server.deliberation.critic import _compute_convergence, _analysis_from_revision, CONVERGENCE_THRESHOLD


def test_partial_convergence():
    a = RevisedAnalysis(
        model_id="claude", round_number=1,
        revised_findings=[
            ClaimWithConfidence(claim="hba1c above target", confidence=0.9),
            ClaimWithConfidence(claim="bp trending up", confidence=0.8),
            ClaimWithConfidence(claim="unique to claude", confidence=0.7),
        ],
        revisions_made=[], maintained_positions=[], raw_revision=""
    )
    b = RevisedAnalysis(
        model_id="gpt4", round_number=1,
        revised_findings=[
            ClaimWithConfidence(claim="hba1c above target", confidence=0.9),
            ClaimWithConfidence(claim="bp trending up", confidence=0.8),
            ClaimWithConfidence(claim="unique to gpt4", confidence=0.6),
        ],
        revisions_made=[], maintained_positions=[], raw_revision=""
    )
    score = _compute_convergence(a, b)
    # Trigram Jaccard: identical claims share many trigrams; unique claims share few.
    # Score should be meaningfully above 0 but below the 0.90 convergence threshold.
    assert 0.5 < score < CONVERGENCE_THRESHOLD


def test_cross_critique_schema():
    critique = CrossCritique(
        critic_model="claude",
        target_model="gpt4",
        round_number=1,
        critique_items=[
            CritiqueItem(
                target_claim="BP is fine",
                critique_type="factual_error",
                critique_text="BP is 141/86, above 130/80 target",
                severity="blocking"
            )
        ],
        areas_of_agreement=["HbA1c needs attention"],
        raw_critique="Full critique text"
    )
    assert len(critique.critique_items) == 1
    assert critique.critique_items[0].severity == "blocking"


import pytest


@pytest.mark.asyncio
async def test_critique_round_one_model_failure_continues():
    """When one model raises, the round is degraded but the result is still valid."""
    from unittest.mock import AsyncMock, patch
    from server.deliberation.schemas import (
        PatientContextPackage, IndependentAnalysis, ClaimWithConfidence
    )
    import json
    from datetime import datetime, timezone

    def _make_analysis(model_id: str) -> IndependentAnalysis:
        claim = ClaimWithConfidence(claim="HbA1c elevated", confidence=0.9)
        return IndependentAnalysis(
            model_id=model_id,
            role_emphasis="diagnostic_reasoning",
            key_findings=[claim],
            risk_flags=[claim],
            recommended_actions=[claim],
            anticipated_trajectory="stable",
            missing_data_identified=[],
        )

    ctx = PatientContextPackage(
        patient_id="p1", patient_name="Test", age=50, sex="M",
        mrn="MRN-001", primary_provider="Dr A", practice="Clinic",
        active_conditions=[], current_medications=[], recent_labs=[],
        vital_trends=[], care_gaps=[], sdoh_flags=[],
        prior_patient_knowledge=[], applicable_guidelines=[],
        upcoming_appointments=[], days_since_last_encounter=30,
        deliberation_trigger="manual",
    )

    from server.deliberation.critic import run_critique_rounds

    call_count = {"n": 0}

    async def call_claude_ok(prompt, user_msg=""):
        # Return a minimal critique then a minimal revision
        from server.deliberation.schemas import (
            CrossCritique, CritiqueItem, RevisedAnalysis
        )
        if "critique" in prompt.lower() or call_count["n"] % 2 == 0:
            call_count["n"] += 1
            return json.dumps({
                "critic_model": "claude-sonnet-4-20250514",
                "target_model": "gpt-4o",
                "round_number": 1,
                "critique_items": [],
                "areas_of_agreement": ["agree"],
                "raw_critique": "ok",
            })
        call_count["n"] += 1
        return json.dumps({
            "model_id": "claude-sonnet-4-20250514",
            "round_number": 1,
            "key_findings": [{"claim": "HbA1c elevated", "confidence": 0.9, "evidence_refs": []}],
            "risk_flags": [],
            "recommended_actions": [],
            "anticipated_trajectory": "stable",
            "missing_data_identified": [],
            "changes_made": [],
            "raw_revised_reasoning": "",
        })

    async def call_gpt4_fail(prompt, user_msg=""):
        raise RuntimeError("GPT-4o API error")

    result = await run_critique_rounds(
        claude_analysis=_make_analysis("claude-sonnet-4-20250514"),
        gpt4_analysis=_make_analysis("gpt-4o"),
        context=ctx,
        max_rounds=1,
        load_prompt_fn=lambda name: f"<prompt>{name}</prompt>",
        call_claude_fn=call_claude_ok,
        call_gpt4_fn=call_gpt4_fail,
    )

    assert 1 in result["degraded_rounds"], f"Expected round 1 degraded, got {result['degraded_rounds']}"
    assert result["rounds_completed"] == 1
