"""Tests for the PRIOR_SESSION provenance tier and Rule 11 staleness gate.

Validates:
  - PRIOR_SESSION is recognised as a valid tier
  - A fresh PRIOR_SESSION section produces no staleness warning
  - A stale PRIOR_SESSION section triggers PRIOR_SESSION_STALENESS (WARN)
  - render_recommendation() returns REDUCED_AUTHORITY for PRIOR_SESSION
"""
from shared.provenance.verifier import (
    VALID_TIERS,
    render_recommendation,
    validate_section,
)


def _base_section(**overrides) -> dict:
    """A minimally valid provenance section (passes Rule 1 / 2 / 4)."""
    section = {
        "section_id": "s1",
        "agent": "SYNTHESIS",
        "declared_tier": "PRIOR_SESSION",
        "content_summary": "recommendation carried over from prior session",
        "claims_tagged": True,
        "source_id": "deliberation-abc-123",
    }
    section.update(overrides)
    return section


def test_prior_session_is_a_valid_tier():
    assert "PRIOR_SESSION" in VALID_TIERS


def test_fresh_prior_session_no_staleness_warning():
    section = _base_section(source_age_hours=12, staleness_threshold_hours=72)
    violations = validate_section(section)
    rules = [v["rule"] for v in violations]
    assert "PRIOR_SESSION_STALENESS" not in rules


def test_stale_prior_session_triggers_warning():
    section = _base_section(source_age_hours=100, staleness_threshold_hours=72)
    violations = validate_section(section)
    staleness = [v for v in violations if v["rule"] == "PRIOR_SESSION_STALENESS"]
    assert len(staleness) == 1
    assert staleness[0]["severity"] == "WARN"
    assert "100.0h old" in staleness[0]["message"]


def test_prior_session_missing_age_no_warning():
    # Missing source_age_hours should not crash and should not warn —
    # consumers can add the field at the call site when known.
    section = _base_section()
    violations = validate_section(section)
    rules = [v["rule"] for v in violations]
    assert "PRIOR_SESSION_STALENESS" not in rules


def test_prior_session_uses_default_threshold_72h():
    # Without staleness_threshold_hours, the default is 72
    section = _base_section(source_age_hours=73)
    section.pop("staleness_threshold_hours", None)
    violations = validate_section(section)
    rules = [v["rule"] for v in violations]
    assert "PRIOR_SESSION_STALENESS" in rules


def test_render_recommendation_prior_session_is_reduced_authority():
    section = _base_section(source_age_hours=12, staleness_threshold_hours=72)
    violations = validate_section(section)
    rec = render_recommendation(section, violations)
    assert rec == "REDUCED_AUTHORITY"


def test_render_recommendation_stale_prior_session_still_reduced_not_withheld():
    # WARN violations do not block; BLOCK does. Stale prior-session data is
    # still usable as reduced-authority context (the user is the decision-maker).
    section = _base_section(source_age_hours=100, staleness_threshold_hours=72)
    violations = validate_section(section)
    rec = render_recommendation(section, violations)
    assert rec == "REDUCED_AUTHORITY"
