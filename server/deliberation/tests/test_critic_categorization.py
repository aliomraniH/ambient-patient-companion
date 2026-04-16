"""Phase 4 — Categorization instruction in critic revision prompt tests."""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent))

from server.deliberation.schemas import (
    PatientContextPackage, IndependentAnalysis, CrossCritique, CritiqueItem,
    ClaimWithConfidence,
)
from server.deliberation.critic import _revise_with_model


def _make_context() -> PatientContextPackage:
    return PatientContextPackage(
        patient_id="p1", patient_name="Test", age=50, sex="M",
        mrn="MRN-001", primary_provider="Dr. A", practice="Clinic",
        active_conditions=[], current_medications=[], recent_labs=[],
        vital_trends=[], care_gaps=[], sdoh_flags=[],
        prior_patient_knowledge=[], applicable_guidelines=[],
        upcoming_appointments=[], days_since_last_encounter=10,
        deliberation_trigger="test",
    )


def _make_analysis() -> IndependentAnalysis:
    return IndependentAnalysis(
        model_id="test-model",
        role_emphasis="diagnostic_reasoning",
        key_findings=[ClaimWithConfidence(claim="HbA1c elevated", confidence=0.9)],
        risk_flags=[],
        recommended_actions=[],
        anticipated_trajectory="stable",
        missing_data_identified=[],
    )


def _make_critique() -> CrossCritique:
    return CrossCritique(
        critique_items=[
            CritiqueItem(
                target_claim="HbA1c elevated",
                critique_type="missed_consideration",
                critique_text="Did not account for recent illness",
                severity="moderate",
            )
        ],
        areas_of_agreement=["Patient has diabetes"],
    )


def test_revision_prompt_contains_confirmed():
    """Revision prompt must contain the CONFIRMED category label."""
    captured_prompts: list[str] = []

    async def mock_call_model(model, prompt, user_msg):
        captured_prompts.append(prompt)
        return """{
            "revised_findings": [{"claim": "HbA1c elevated", "confidence": 0.85, "evidence_refs": []}],
            "revisions_made": ["Considered illness"],
            "maintained_positions": [],
            "raw_revision": "CONFIRMED: HbA1c elevated. CHALLENGED: timing."
        }"""

    def load_prompt(f, subs):
        return ""

    async def run():
        try:
            await _revise_with_model(
                "claude-test", _make_analysis(), _make_critique(),
                _make_context(), 1, load_prompt, mock_call_model
            )
        except Exception:
            pass

    asyncio.run(run())
    assert captured_prompts, "mock_call_model was never called"
    prompt = captured_prompts[0]
    assert "CONFIRMED" in prompt, "CONFIRMED category label missing from revision prompt"


def test_revision_prompt_contains_challenged():
    captured_prompts: list[str] = []

    async def mock_call_model(model, prompt, user_msg):
        captured_prompts.append(prompt)
        return """{
            "revised_findings": [{"claim": "x", "confidence": 0.8, "evidence_refs": []}],
            "revisions_made": [], "maintained_positions": [], "raw_revision": ""
        }"""

    async def run():
        try:
            await _revise_with_model(
                "claude-test", _make_analysis(), _make_critique(),
                _make_context(), 1, lambda f, s: "", mock_call_model
            )
        except Exception:
            pass

    asyncio.run(run())
    assert "CHALLENGED" in captured_prompts[0]


def test_revision_prompt_contains_uncertain():
    captured_prompts: list[str] = []

    async def mock_call_model(model, prompt, user_msg):
        captured_prompts.append(prompt)
        return """{
            "revised_findings": [{"claim": "x", "confidence": 0.8, "evidence_refs": []}],
            "revisions_made": [], "maintained_positions": [], "raw_revision": ""
        }"""

    async def run():
        try:
            await _revise_with_model(
                "gpt4-test", _make_analysis(), _make_critique(),
                _make_context(), 1, lambda f, s: "", mock_call_model
            )
        except Exception:
            pass

    asyncio.run(run())
    assert "UNCERTAIN" in captured_prompts[0]


def test_revision_prompt_contains_new():
    captured_prompts: list[str] = []

    async def mock_call_model(model, prompt, user_msg):
        captured_prompts.append(prompt)
        return """{
            "revised_findings": [{"claim": "x", "confidence": 0.8, "evidence_refs": []}],
            "revisions_made": [], "maintained_positions": [], "raw_revision": ""
        }"""

    async def run():
        try:
            await _revise_with_model(
                "gpt4-test", _make_analysis(), _make_critique(),
                _make_context(), 1, lambda f, s: "", mock_call_model
            )
        except Exception:
            pass

    asyncio.run(run())
    assert "NEW" in captured_prompts[0]


def test_revision_prompt_challenged_items_first_instruction():
    """Prompt must instruct model to address CHALLENGED items first."""
    captured_prompts: list[str] = []

    async def mock_call_model(model, prompt, user_msg):
        captured_prompts.append(prompt)
        return """{
            "revised_findings": [{"claim": "x", "confidence": 0.8, "evidence_refs": []}],
            "revisions_made": [], "maintained_positions": [], "raw_revision": ""
        }"""

    async def run():
        try:
            await _revise_with_model(
                "claude-test", _make_analysis(), _make_critique(),
                _make_context(), 1, lambda f, s: "", mock_call_model
            )
        except Exception:
            pass

    asyncio.run(run())
    assert "CHALLENGED items first" in captured_prompts[0]
