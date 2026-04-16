"""Test that the critic revision prompt includes the categorization
instruction for anchoring/reasoning-reset mitigation.

We exercise _revise_with_model with a mock call_model_fn that captures the
system prompt, then assert the categorization buckets are present.
"""
import json

import pytest

from server.deliberation.critic import _revise_with_model
from server.deliberation.schemas import (
    ClaimWithConfidence,
    CritiqueItem,
    CrossCritique,
    IndependentAnalysis,
    PatientContextPackage,
    RevisedAnalysis,
)


def _make_ctx() -> PatientContextPackage:
    return PatientContextPackage(
        patient_id="p1", patient_name="P", age=50, sex="F", mrn="M",
        primary_provider="", practice="",
        active_conditions=[{"code": "E11.9", "display": "Type 2 DM"}],
        current_medications=[], recent_labs=[], vital_trends=[],
        care_gaps=[], sdoh_flags=[], prior_patient_knowledge=[],
        applicable_guidelines=[], upcoming_appointments=[],
        days_since_last_encounter=30, deliberation_trigger="t",
    )


def _make_analysis() -> IndependentAnalysis:
    return IndependentAnalysis(
        model_id="claude-sonnet-4-20250514",
        role_emphasis="diagnostic_reasoning",
        key_findings=[ClaimWithConfidence(claim="f1", confidence=0.8, evidence_refs=[])],
        risk_flags=[],
        recommended_actions=[],
        anticipated_trajectory="n/a",
        missing_data_identified=[],
        raw_reasoning="r",
    )


def _make_critique() -> CrossCritique:
    return CrossCritique(
        critic_model="gpt-4o",
        target_model="claude-sonnet-4-20250514",
        round_number=1,
        critique_items=[
            CritiqueItem(
                target_claim="f1",
                critique_type="logical_gap",
                critique_text="c1",
                severity="moderate",
            )
        ],
        areas_of_agreement=[],
        raw_critique="r",
    )


def _fake_revised_json() -> str:
    payload = {
        "revised_findings": [
            {"claim": "f1'", "confidence": 0.8, "evidence_refs": []}
        ],
        "revisions_made": ["addressed c1"],
        "maintained_positions": [],
        "raw_revision": "CONFIRMED on f1 (still supported)",
    }
    return json.dumps(payload)


@pytest.mark.asyncio
async def test_revision_prompt_contains_categorization_instruction():
    captured: dict = {}

    async def fake_call(model, system, user, **kwargs):
        captured["system"] = system
        return _fake_revised_json()

    def fake_load_prompt(filename, subs):
        # Not used by _revise_with_model (it builds the prompt inline)
        return filename

    result = await _revise_with_model(
        model="claude-sonnet-4-20250514",
        current_analysis=_make_analysis(),
        critique=_make_critique(),
        context=_make_ctx(),
        round_number=2,
        load_prompt_fn=fake_load_prompt,
        call_model_fn=fake_call,
    )

    assert isinstance(result, RevisedAnalysis)
    prompt = captured["system"]
    # All four categorization buckets must appear
    for bucket in ("CONFIRMED", "CHALLENGED", "UNCERTAIN", "NEW"):
        assert bucket in prompt, f"missing bucket in revision prompt: {bucket}"
    # And the reasoning-reset rationale text
    assert "reasoning reset" in prompt.lower() or "anchoring" in prompt.lower()
