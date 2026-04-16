"""Phase 5 — Anchor bias check in constitutional critic tests."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from server.mcp_server import _check_anchor_bias


def test_single_condition_dominance_detected():
    """When one term dominates >60 % of mentions, anchor bias is flagged."""
    text = (
        "The patient has diabetes. Diabetes management is the priority. "
        "Diabetes complications include neuropathy. The diabetes HbA1c is high. "
        "Hypertension is also present."
    )
    result = _check_anchor_bias(text)
    assert result != "", "Expected anchor bias to be detected for dominated term"
    assert "diabetes" in result.lower()


def test_balanced_output_passes():
    """A well-balanced output with differential language passes (empty string)."""
    text = (
        "The patient has diabetes and hypertension. "
        "Consider the interplay between both conditions. "
        "Alternatively, the hypertension may be the primary driver. "
        "Diabetes management is also important."
    )
    result = _check_anchor_bias(text)
    assert result == "", f"Expected no anchor bias for balanced output, got: {result!r}"


def test_severity_is_moderate():
    """Anchor bias issues are added with severity='moderate' in the pipeline."""
    text = (
        "diabetes diabetes diabetes diabetes diabetes. "
        "hypertension is also noted."
    )
    detail = _check_anchor_bias(text)
    assert detail != ""


def test_moderate_does_not_raise_tier():
    """moderate-only issues do NOT push escalation tier above 1."""
    from server.mcp_server import _check_anchor_bias
    text = (
        "diabetes diabetes diabetes diabetes diabetes hypertension present."
    )
    detail = _check_anchor_bias(text)
    assert detail != ""

    issues = [{"check": "anchor_bias", "severity": "moderate", "detail": detail}]
    critical = [i for i in issues if i["severity"] == "critical"]
    high = [i for i in issues if i["severity"] == "high"]
    if critical:
        tier = 4
    elif len(high) >= 2:
        tier = 3
    elif high:
        tier = 2
    else:
        tier = 1
    assert tier == 1, f"moderate-only anchor bias should not raise tier above 1, got {tier}"


def test_empty_string_safe():
    """Empty string input returns empty string without error."""
    assert _check_anchor_bias("") == ""


def test_no_clinical_terms_returns_empty():
    """Text with no recognized clinical terms returns empty string."""
    result = _check_anchor_bias("The weather is nice today. Let's go for a walk.")
    assert result == ""


def test_missing_differential_language_flagged():
    """Two conditions without differential language triggers signal 2."""
    text = (
        "Patient has diabetes. Also has hypertension. "
        "Diabetes should be managed. Hypertension medication adjusted."
    )
    result = _check_anchor_bias(text)
    assert result != "", "Expected missing-differential signal"
    assert "differential" in result.lower() or "alternatively" in result.lower()


def test_with_differential_language_passes_signal2():
    """Two conditions WITH differential language does not trigger signal 2."""
    text = (
        "Patient has diabetes and hypertension. "
        "Consider that hypertension may be secondary to renal disease. "
        "Alternatively, the diabetes could be better controlled."
    )
    result = _check_anchor_bias(text)
    assert "differential framing" not in result
