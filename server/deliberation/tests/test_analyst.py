"""Tests for the analyst module (Phase 1)."""
import json
from pathlib import Path
from server.deliberation.schemas import IndependentAnalysis, PatientContextPackage
from server.deliberation.analyst import _load_prompt

FIXTURES = Path(__file__).parent / "fixtures"


def _get_context() -> PatientContextPackage:
    data = json.loads((FIXTURES / "maria_chen_context.json").read_text())
    return PatientContextPackage(**data)


def test_load_prompt_substitutes_placeholders():
    ctx = _get_context()
    prompt = _load_prompt("analyst_claude.xml", {
        "PATIENT_CONTEXT_JSON": ctx.model_dump_json(indent=2),
        "GUIDELINES_JSON": "[]",
        "PRIOR_KNOWLEDGE_JSON": "[]"
    })
    assert "Maria Chen" in prompt
    assert "{{PATIENT_CONTEXT_JSON}}" not in prompt


def test_load_prompt_gpt4_exists():
    prompt = _load_prompt("analyst_gpt4.xml", {
        "PATIENT_CONTEXT_JSON": "{}",
        "GUIDELINES_JSON": "[]",
        "PRIOR_KNOWLEDGE_JSON": "[]"
    })
    assert "Treatment Optimization Analyst" in prompt


def test_independent_analysis_round_trips():
    """IndependentAnalysis can serialize and deserialize."""
    analysis = IndependentAnalysis(
        model_id="claude-sonnet-4-20250514",
        role_emphasis="diagnostic_reasoning",
        key_findings=[],
        risk_flags=[],
        recommended_actions=[],
        anticipated_trajectory="Stable",
        missing_data_identified=["lipid panel"],
        raw_reasoning="Test reasoning"
    )
    json_str = analysis.model_dump_json()
    restored = IndependentAnalysis.model_validate_json(json_str)
    assert restored.model_id == analysis.model_id
    assert restored.missing_data_identified == ["lipid panel"]
