"""Permanent regression test for the MIRA hallucination incident.

Encodes the exact section that caused Failure Incident B: an LLM
Interaction Health Flag synthesized from memory context and presented
as a clinical_query tool output. This test must never be removed.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from shared.provenance.verifier import validate_section  # noqa: E402


MIRA_INCIDENT = {
    "section_id":        "mira-llm-health-flag",
    "agent":             "MIRA",
    # Claude reported this as TOOL tier with clinical_query as the source
    # tool. In fact it was synthesized — and clinical_query is not the
    # right tool for the LLM-interaction-health domain; that domain
    # requires score_llm_interaction_health.
    "declared_tier":     "TOOL",
    "content_summary": (
        "LLM Interaction Health Flag: patient is developer using AI health "
        "tools daily. Risk of over-reliance per 2025 OpenAI/MIT RCT. "
        "score_llm_interaction_health domain assessment."
    ),
    "tool_name":         "clinical_query",
    "tool_called_at":    "2026-04-12T03:34:02Z",
    "corpus_name":       None,
    "evidence_gap_flagged": None,
    "synthesis_basis":   None,
    "pending_tool_name": None,
    "citations":         [],
}


def test_mira_incident_is_blocked():
    violations = validate_section(MIRA_INCIDENT)
    blocks = [v for v in violations if v["severity"] == "BLOCK"]

    assert blocks, "REGRESSION FAIL: MIRA incident not blocked"

    # The key point: the LLM-health domain must be identified as
    # belonging to score_llm_interaction_health, not clinical_query.
    assert any(
        "score_llm_interaction_health" in v.get("message", "")
        or "openai" in v.get("message", "").lower()
        for v in violations
    ), "REGRESSION FAIL: LLM health domain not caught"


if __name__ == "__main__":
    test_mira_incident_is_blocked()
    print("MIRA incident correctly BLOCKED")
