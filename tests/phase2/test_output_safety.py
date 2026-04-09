"""
Tests for the deliberation output safety wrapper.

OS-1: Patient-facing content with PHI_LEAKAGE flag is blocked
OS-2: Clean patient-facing content passes
OS-3: Provider-facing content with DIAGNOSTIC_LANGUAGE is sanitized, not blocked
OS-4: validate_nudge_batch filters out blocked nudges
OS-5: Empty content passes without calling validator
OS-6: Validator exception blocks patient-facing content
OS-7: Non-blocking flags (MISSING_CITATION) still pass content through

Run with:
    python -m pytest tests/phase2/test_output_safety.py -v
"""

import pytest
from unittest.mock import patch, MagicMock
from dataclasses import dataclass, field

from server.guardrails.output_validator import OutputValidation
from server.deliberation.output_safety import (
    validate_deliberation_output,
    validate_nudge_batch,
    validate_nudge_dicts,
)


def _make_safe(response_text, flags=None):
    """Return an OutputValidation that passed all checks."""
    return OutputValidation(safe=True, flags=flags or [], safe_response=response_text)


def _make_unsafe_phi(response_text):
    """Return an OutputValidation blocked by PHI leakage."""
    fallback = (
        "The generated response did not meet safety validation criteria. "
        "Insufficient guideline evidence. Clinician judgment required."
    )
    return OutputValidation(
        safe=False,
        flags=["PHI_LEAKAGE: Potential SSN detected in generated output."],
        safe_response=fallback,
    )


def _make_unsafe_diagnostic(response_text):
    """Return an OutputValidation blocked by diagnostic language."""
    fallback = (
        "The generated response did not meet safety validation criteria. "
        "Insufficient guideline evidence. Clinician judgment required."
    )
    return OutputValidation(
        safe=False,
        flags=[
            "DIAGNOSTIC_LANGUAGE (definitive_diagnosis): Response contains "
            "definitive diagnostic language that must be rewritten as "
            "differential considerations."
        ],
        safe_response=fallback,
    )


def _make_safe_with_citation_warning(response_text):
    """Return an OutputValidation that is safe but has a MISSING_CITATION warning."""
    return OutputValidation(
        safe=True,
        flags=[
            "MISSING_CITATION: Response does not reference a guideline source "
            "and version. All recommendations must cite their evidence base."
        ],
        safe_response=response_text,
    )


# OS-1: Patient-facing content with PHI_LEAKAGE flag is blocked
def test_patient_facing_phi_leakage_is_blocked():
    with patch(
        "server.deliberation.output_safety.validate_output",
        side_effect=lambda response: _make_unsafe_phi(response),
    ):
        result = validate_deliberation_output(
            content="Patient SSN: 123-45-6789. Please take your medication.",
            output_type="patient_nudges",
            patient_id="test-pid",
            deliberation_id="test-did",
        )
    assert result["passed"] is False
    assert result["action"] == "block"
    assert result["content"] == ""
    assert any("PHI_LEAKAGE" in v for v in result["violations"])


# OS-2: Clean patient-facing content passes
def test_patient_facing_clean_content_passes():
    with patch(
        "server.deliberation.output_safety.validate_output",
        side_effect=lambda response: _make_safe(response),
    ):
        result = validate_deliberation_output(
            content="Your blood sugar has been trending well this week.",
            output_type="patient_nudges",
            patient_id="test-pid",
            deliberation_id="test-did",
        )
    assert result["passed"] is True
    assert result["content"] == "Your blood sugar has been trending well this week."
    assert result["action"] == "pass"


# OS-3: Provider-facing with DIAGNOSTIC_LANGUAGE is sanitized, not blocked
def test_provider_facing_diagnostic_language_passes_through():
    with patch(
        "server.deliberation.output_safety.validate_output",
        side_effect=lambda response: _make_unsafe_diagnostic(response),
    ):
        result = validate_deliberation_output(
            content="I diagnose this patient with uncontrolled diabetes.",
            output_type="anticipatory_scenarios",
            patient_id="test-pid",
            deliberation_id="test-did",
        )
    # Provider-facing content is not blocked, just flagged
    assert result["passed"] is True
    assert result["action"] == "sanitize"
    assert len(result["violations"]) > 0
    # Content is preserved (original, not fallback)
    assert "diagnose" in result["content"]


# OS-4: validate_nudge_batch filters out blocked nudges
def test_nudge_batch_filters_blocked_nudges():
    # Create mock NudgeContent objects
    nudge_clean = MagicMock()
    nudge_clean.target = "patient"
    nudge_clean.channels = {
        "sms": "Great progress on your walk goal!",
        "portal": "Keep up the exercise routine.",
    }
    nudge_clean.nudge_id = "n1"

    nudge_phi = MagicMock()
    nudge_phi.target = "patient"
    nudge_phi.channels = {
        "sms": "Call 555-123-4567 for your results",
        "portal": "Your SSN 123-45-6789 is on file.",
    }
    nudge_phi.nudge_id = "n2"

    nudge_care_team = MagicMock()
    nudge_care_team.target = "care_team"
    nudge_care_team.channels = {"portal": "Review patient labs."}
    nudge_care_team.nudge_id = "n3"

    call_count = [0]

    def mock_validate(response):
        call_count[0] += 1
        if "555-123-4567" in response or "123-45-6789" in response:
            return _make_unsafe_phi(response)
        return _make_safe(response)

    with patch(
        "server.deliberation.output_safety.validate_output",
        side_effect=mock_validate,
    ):
        result = validate_nudge_batch(
            nudges=[nudge_clean, nudge_phi, nudge_care_team],
            patient_id="pid",
            deliberation_id="did",
        )

    # nudge_phi should be filtered out; nudge_clean and care_team should remain
    assert len(result) == 2
    assert nudge_clean in result
    assert nudge_care_team in result
    assert nudge_phi not in result


# OS-5: Empty content passes without calling validator
def test_empty_content_passes_without_calling_validator():
    with patch("server.deliberation.output_safety.validate_output") as mock_v:
        result = validate_deliberation_output(
            content="",
            output_type="patient_nudges",
            patient_id="pid",
            deliberation_id="did",
        )
    mock_v.assert_not_called()
    assert result["passed"] is True
    assert result["action"] == "pass"


# OS-6: Validator exception blocks patient-facing content
def test_validator_exception_blocks_patient_facing():
    with patch(
        "server.deliberation.output_safety.validate_output",
        side_effect=RuntimeError("guardrail internal error"),
    ):
        result = validate_deliberation_output(
            content="Some nudge content",
            output_type="patient_nudges",
            patient_id="pid",
            deliberation_id="did",
        )
    assert result["passed"] is False
    assert result["action"] == "block"
    assert any("exception" in v.lower() for v in result["violations"])


# OS-7: Non-blocking flags (MISSING_CITATION) still pass content through
def test_missing_citation_warning_still_passes():
    with patch(
        "server.deliberation.output_safety.validate_output",
        side_effect=lambda response: _make_safe_with_citation_warning(response),
    ):
        result = validate_deliberation_output(
            content="Consider adjusting your medication schedule.",
            output_type="patient_nudges",
            patient_id="pid",
            deliberation_id="did",
        )
    assert result["passed"] is True
    assert result["action"] == "sanitize"  # has flags, so "sanitize" not "pass"
    assert "Consider adjusting" in result["content"]
    assert any("MISSING_CITATION" in v for v in result["violations"])


# Additional: validate_nudge_dicts works for progressive mode dict nudges
def test_nudge_dicts_filters_blocked():
    nudges = [
        {"content": "You have been diagnosed with X", "target": "patient"},
        {"content": "Great progress on your walk goal!", "target": "patient"},
        {"content": "Review patient labs", "target": "care_team"},
    ]

    def mock_validate(response):
        if "diagnosed" in response.lower():
            return _make_unsafe_diagnostic(response)
        return _make_safe(response)

    with patch(
        "server.deliberation.output_safety.validate_output",
        side_effect=mock_validate,
    ):
        result = validate_nudge_dicts(nudges, "pid", "did")

    # "diagnosed" nudge blocked (patient-facing + DIAGNOSTIC_LANGUAGE)
    assert len(result) == 2
    assert any("Great progress" in n.get("content", "") for n in result)
    assert any(n.get("target") == "care_team" for n in result)
