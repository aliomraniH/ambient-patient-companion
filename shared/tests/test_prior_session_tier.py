"""Phase 6 — PRIOR_SESSION provenance tier tests."""
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from shared.provenance.verifier import (
    VALID_TIERS, validate_section, render_recommendation,
)


def test_prior_session_is_valid_tier():
    """PRIOR_SESSION must be a member of VALID_TIERS."""
    assert "PRIOR_SESSION" in VALID_TIERS


def test_prior_session_does_not_trigger_untagged_claim():
    """A section with PRIOR_SESSION tier must NOT produce UNTAGGED_CLAIM."""
    section = {
        "section_id": "s1",
        "declared_tier": "PRIOR_SESSION",
        "agent": "ARIA",
    }
    violations = validate_section(section)
    rule_names = [v["rule"] for v in violations]
    assert "UNTAGGED_CLAIM" not in rule_names


def test_staleness_warns_when_old():
    """A PRIOR_SESSION section with triggered_at > 72h should get a WARN."""
    old_ts = (datetime.now(timezone.utc) - timedelta(hours=100)).isoformat()
    section = {
        "section_id": "s2",
        "declared_tier": "PRIOR_SESSION",
        "agent": "ARIA",
        "triggered_at": old_ts,
    }
    violations = validate_section(section)
    warn_rules = [v["rule"] for v in violations if v["severity"] == "WARN"]
    assert "PRIOR_SESSION_STALENESS" in warn_rules


def test_no_warning_when_fresh():
    """A PRIOR_SESSION section newer than 72h should NOT produce a staleness WARN."""
    fresh_ts = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
    section = {
        "section_id": "s3",
        "declared_tier": "PRIOR_SESSION",
        "agent": "ARIA",
        "triggered_at": fresh_ts,
    }
    violations = validate_section(section)
    warn_rules = [v["rule"] for v in violations if v["severity"] == "WARN"]
    assert "PRIOR_SESSION_STALENESS" not in warn_rules


def test_prior_session_render_recommendation_reduced_authority():
    """PRIOR_SESSION sections with no BLOCK violations → REDUCED_AUTHORITY."""
    section = {
        "section_id": "s4",
        "declared_tier": "PRIOR_SESSION",
        "agent": "ARIA",
    }
    violations = validate_section(section)
    recommendation = render_recommendation(section, violations)
    assert recommendation == "REDUCED_AUTHORITY", (
        f"Expected REDUCED_AUTHORITY for clean PRIOR_SESSION, got {recommendation}"
    )


def test_prior_session_stale_still_reduced_not_withheld():
    """Staleness is a WARN (not BLOCK) so stale PRIOR_SESSION → REDUCED_AUTHORITY."""
    old_ts = (datetime.now(timezone.utc) - timedelta(hours=200)).isoformat()
    section = {
        "section_id": "s5",
        "declared_tier": "PRIOR_SESSION",
        "agent": "ARIA",
        "triggered_at": old_ts,
    }
    violations = validate_section(section)
    recommendation = render_recommendation(section, violations)
    has_block = any(v["severity"] == "BLOCK" for v in violations)
    assert not has_block, "Staleness should be WARN, not BLOCK"
    assert recommendation == "REDUCED_AUTHORITY"
