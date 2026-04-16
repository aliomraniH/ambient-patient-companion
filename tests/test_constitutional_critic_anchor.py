"""Tests for the anchor / primacy bias check added to
run_constitutional_critic (Phase 5).

The check is a heuristic over the draft output text. It flags:
  1. Dominance of the first-mentioned clinical term (> 60% of all mentions)
  2. Absence of differential language when ≥ 2 clinical terms are present

Severity is always "moderate" — advisory, does not force a reframe.
"""
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parent.parent
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from server.mcp_server import _check_anchor_bias, run_constitutional_critic

ZERO_UUID = "00000000-0000-0000-0000-000000000000"


# ── Unit tests for the pure heuristic ───────────────────────────────────────

def test_empty_input_returns_empty_string():
    assert _check_anchor_bias("") == ""
    assert _check_anchor_bias(None) == ""


def test_short_input_returns_empty_string():
    # < 80 chars is too short to meaningfully evaluate
    assert _check_anchor_bias("Patient has diabetes.") == ""


def test_single_term_no_bias_flag():
    # Only one clinical term — nothing to compare against, should pass
    text = (
        "Patient presents with type 2 diabetes. We should continue metformin "
        "therapy and monitor the response at next visit in three months."
    )
    assert _check_anchor_bias(text) == ""


def test_balanced_multi_term_with_differential_passes():
    text = (
        "Patient presents with diabetes and hypertension. Alternatively, the "
        "fatigue could also reflect early-stage hepatitis. We should rule out "
        "hepatitis before adjusting the antihypertensive."
    )
    # Differential language present and no dominance
    assert _check_anchor_bias(text) == ""


def test_dominant_first_term_flagged():
    text = (
        "The patient's diabetes is the key issue. The diabetes needs tighter "
        "control. Diabetes management should be the focus. Also notes mild "
        "hypertension but diabetes remains primary."
    )
    out = _check_anchor_bias(text)
    assert "primacy anchor" in out or "dominates" in out


def test_no_differential_language_flagged():
    # Two terms, no dominance, but zero differential cues
    text = (
        "Patient has diabetes. Patient has hypertension. Continue current "
        "regimen for both conditions and review labs in three months "
        "at the next clinic visit."
    )
    out = _check_anchor_bias(text)
    assert "differential language" in out


# ── Integration with run_constitutional_critic ──────────────────────────────

@pytest.mark.asyncio
async def test_anchor_bias_surfaced_in_issues():
    draft = (
        "The patient's diabetes is the key issue. The diabetes needs tighter "
        "control. Diabetes management should be the focus. Also notes mild "
        "hypertension but diabetes remains primary."
    )
    r = await run_constitutional_critic(
        patient_id=ZERO_UUID,
        draft_output=draft,
        originating_agent="ARIA",
        output_type="clinical_recommendation",
    )
    issues_checks = [i["check"] for i in r["issues"]]
    assert "anchor_bias" in issues_checks


@pytest.mark.asyncio
async def test_anchor_bias_severity_is_moderate():
    draft = (
        "The patient's diabetes is the key issue. The diabetes needs tighter "
        "control. Diabetes management should be the focus. Also notes mild "
        "hypertension but diabetes remains primary."
    )
    r = await run_constitutional_critic(
        patient_id=ZERO_UUID,
        draft_output=draft,
        originating_agent="ARIA",
        output_type="clinical_recommendation",
    )
    anchor_issue = next(i for i in r["issues"] if i["check"] == "anchor_bias")
    assert anchor_issue["severity"] == "moderate"


@pytest.mark.asyncio
async def test_moderate_only_does_not_force_reframe():
    # Anchor bias alone should be advisory — reframe_required must stay False
    draft = (
        "The patient's diabetes is the key issue. The diabetes needs tighter "
        "control. Diabetes management should be the focus. Also notes mild "
        "hypertension but diabetes remains primary."
    )
    r = await run_constitutional_critic(
        patient_id=ZERO_UUID,
        draft_output=draft,
        originating_agent="ARIA",
        output_type="clinical_recommendation",
    )
    # No high/critical issues expected — only moderate anchor bias
    assert r["reframe_required"] is False
    assert r["escalation_tier"] == 2  # moderate-only → tier 2 advisory


@pytest.mark.asyncio
async def test_clean_output_still_passes_after_phase5():
    # Baseline regression: benign output must still pass cleanly
    r = await run_constitutional_critic(
        patient_id=ZERO_UUID,
        draft_output="Consider discussing statin therapy with your provider.",
        originating_agent="ARIA",
        output_type="clinical_recommendation",
    )
    assert r["passed"] is True
    assert r["escalation_tier"] == 1
    assert r["reframe_required"] is False
