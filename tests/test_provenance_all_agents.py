"""Unit tests for shared/provenance/verifier.py.

Covers all 8 rules across all 4 agents (ARIA, MIRA, THEO, SYNTHESIS).
No DB, no FastMCP — pure validation logic only.
"""

import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from shared.provenance.verifier import (  # noqa: E402
    validate_section,
    render_recommendation,
    hash_mrn,
)


# ── Pre-check: AGENT_TIER_CONSTRAINT_VIOLATED ─────────────────────────

def test_aria_synthesized_tier_blocked():
    v = validate_section({
        "section_id": "aria-synth",
        "agent": "ARIA",
        "declared_tier": "SYNTHESIZED",
        "content_summary": "Cardiovascular risk reasoning.",
        "synthesis_basis": "clinical training",
    })
    assert any(r["rule"] == "AGENT_TIER_CONSTRAINT_VIOLATED" for r in v)


def test_synthesis_synthesized_rationale_allowed_with_basis():
    v = validate_section({
        "section_id": "synthesis-rationale",
        "agent": "SYNTHESIS",
        "declared_tier": "SYNTHESIZED",
        "content_summary": "Gate decision: mood-first protocol engaged.",
        "synthesis_basis": "deliberation council outputs",
    })
    assert not any(r["rule"] == "AGENT_TIER_CONSTRAINT_VIOLATED" for r in v)


# ── Rule 1: UNTAGGED_CLAIM ────────────────────────────────────────────

def test_untagged_claim_fires_for_all_agents():
    for agent in ["ARIA", "MIRA", "THEO", "SYNTHESIS"]:
        v = validate_section({
            "section_id": f"{agent.lower()}-untagged",
            "agent": agent,
            "declared_tier": None,
            "content_summary": "content",
        })
        assert any(r["rule"] == "UNTAGGED_CLAIM" for r in v), agent


def test_invalid_tier_fires_untagged():
    v = validate_section({
        "section_id": "mira-bad-tier",
        "agent": "MIRA",
        "declared_tier": "GUESS",
        "content_summary": "content",
    })
    assert any(r["rule"] == "UNTAGGED_CLAIM" for r in v)


# ── Rule 2: TOOL_MISSING_CALL_EVIDENCE ────────────────────────────────

def test_tool_missing_call_evidence():
    v = validate_section({
        "section_id": "theo-tool-nodates",
        "agent": "THEO",
        "declared_tier": "TOOL",
        "content_summary": "Drug interaction check.",
        "tool_name": None,
        "tool_called_at": None,
    })
    assert any(r["rule"] == "TOOL_MISSING_CALL_EVIDENCE" for r in v)


# ── Rule 3: RETRIEVAL_GAP_NOT_DECLARED ────────────────────────────────

def test_retrieval_gap_not_declared():
    v = validate_section({
        "section_id": "aria-ret",
        "agent": "ARIA",
        "declared_tier": "RETRIEVAL",
        "content_summary": "Cardiovascular risk from clinical_query.",
        "evidence_gap_flagged": None,
    })
    assert any(r["rule"] == "RETRIEVAL_GAP_NOT_DECLARED" for r in v)


# ── Rule 4: SYNTHESIZED_NO_BASIS ──────────────────────────────────────

def test_synthesized_no_basis():
    v = validate_section({
        "section_id": "mira-synth-nobasis",
        "agent": "MIRA",
        "declared_tier": "SYNTHESIZED",
        "content_summary": "Patient seems motivated for change.",
        "synthesis_basis": "",
    })
    assert any(r["rule"] == "SYNTHESIZED_NO_BASIS" for r in v)


# ── Rule 5: PENDING_NO_TOOL_NAMED ─────────────────────────────────────

def test_pending_no_tool_named():
    v = validate_section({
        "section_id": "synthesis-pending",
        "agent": "SYNTHESIS",
        "declared_tier": "PENDING",
        "content_summary": "Awaiting nudge score.",
        "pending_tool_name": None,
    })
    assert any(r["rule"] == "PENDING_NO_TOOL_NAMED" for r in v)


# ── Rule 6: KNOWN_DOMAIN_SYNTHESIZED_INSTEAD_OF_PENDING ───────────────

def test_rule6_mira_llm_health():
    v = validate_section({
        "section_id": "mira-llm-health",
        "agent": "MIRA",
        "declared_tier": "SYNTHESIZED",
        "content_summary": (
            "LLM interaction health flag: patient uses AI tools daily. "
            "OpenAI/MIT RCT 2025 indicates risk of over-reliance."
        ),
        "synthesis_basis": "architecture spec",
    })
    r6 = [r for r in v
          if r["rule"] == "KNOWN_DOMAIN_SYNTHESIZED_INSTEAD_OF_PENDING"]
    assert r6 and "score_llm_interaction_health" in r6[0]["message"]


def test_rule6_theo_ppi_safety():
    v = validate_section({
        "section_id": "theo-ppi",
        "agent": "THEO",
        "declared_tier": "SYNTHESIZED",
        "content_summary": (
            "PPI safety: pantoprazole long-term raises hypomagnesemia risk."
        ),
        "synthesis_basis": "clinical training",
    })
    r6 = [r for r in v
          if r["rule"] == "KNOWN_DOMAIN_SYNTHESIZED_INSTEAD_OF_PENDING"]
    assert r6 and "search_clinical_knowledge" in r6[0]["message"]


def test_rule6_synthesis_obt_score():
    v = validate_section({
        "section_id": "synthesis-obt",
        "agent": "SYNTHESIS",
        "declared_tier": "SYNTHESIZED",
        "content_summary": "OBT score estimated at 62 based on recent labs.",
        "synthesis_basis": "deliberation outputs",
    })
    r6 = [r for r in v
          if r["rule"] == "KNOWN_DOMAIN_SYNTHESIZED_INSTEAD_OF_PENDING"]
    assert r6 and "compute_obt_score" in r6[0]["message"]


def test_rule6_mira_comb_barrier():
    v = validate_section({
        "section_id": "mira-comb",
        "agent": "MIRA",
        "declared_tier": "SYNTHESIZED",
        "content_summary": (
            "COM-B barrier: motivation barrier around DPP enrollment."
        ),
        "synthesis_basis": "architecture spec",
    })
    r6 = [r for r in v
          if r["rule"] == "KNOWN_DOMAIN_SYNTHESIZED_INSTEAD_OF_PENDING"]
    assert r6 and "classify_com_b_barrier" in r6[0]["message"]


def test_rule6_synthesis_nudge_impactability():
    v = validate_section({
        "section_id": "synthesis-nudge",
        "agent": "SYNTHESIS",
        "declared_tier": "SYNTHESIZED",
        "content_summary": (
            "Nudge impactability is high given receptivity signals."
        ),
        "synthesis_basis": "deliberation outputs",
    })
    r6 = [r for r in v
          if r["rule"] == "KNOWN_DOMAIN_SYNTHESIZED_INSTEAD_OF_PENDING"]
    assert r6 and "score_nudge_impactability" in r6[0]["message"]


def test_rule6_tool_tier_with_correct_tool_is_clean():
    # TOOL tier in a known domain with the correct tool_name must NOT
    # trigger Rule 6.
    v = validate_section({
        "section_id": "mira-llm-health-ok",
        "agent": "MIRA",
        "declared_tier": "TOOL",
        "content_summary": (
            "LLM interaction health: score=0.42 (moderate over-reliance)."
        ),
        "tool_name": "score_llm_interaction_health",
        "tool_called_at": datetime.now(timezone.utc).isoformat(),
    })
    assert not any(
        r["rule"] == "KNOWN_DOMAIN_SYNTHESIZED_INSTEAD_OF_PENDING" for r in v
    )


# ── Rule 7: RETRIEVAL_GAP_SILENCED (WARN) ─────────────────────────────

def test_retrieval_gap_silenced_warns():
    v = validate_section({
        "section_id": "theo-gap-silent",
        "agent": "THEO",
        "declared_tier": "RETRIEVAL",
        "content_summary": "Statin safety is well established.",
        "evidence_gap_flagged": True,
        "citations": ["ADA 2026 Rec 9.2a"],
    })
    gap = [r for r in v if r["rule"] == "RETRIEVAL_GAP_SILENCED"]
    assert gap and gap[0]["severity"] == "WARN"


# ── Rule 8: TIMESTAMP_STALENESS (WARN) ────────────────────────────────

def test_timestamp_staleness_warns():
    v = validate_section({
        "section_id": "aria-stale",
        "agent": "ARIA",
        "declared_tier": "TOOL",
        "content_summary": (
            "LDL 104 warrants cardiovascular evaluation. Gap: no."
        ),
        "tool_name": "clinical_query",
        "tool_called_at": "2024-01-01T00:00:00Z",
        "citations": ["ADA 2026"],
    })
    stale = [r for r in v if r["rule"] == "TIMESTAMP_STALENESS"]
    assert stale and stale[0]["severity"] == "WARN"


# ── Clean sections ────────────────────────────────────────────────────

def test_clean_aria_tool_section():
    now_iso = datetime.now(timezone.utc).isoformat()
    v = validate_section({
        "section_id": "aria-clean",
        "agent": "ARIA",
        "declared_tier": "TOOL",
        "content_summary": (
            "LDL 104 with family CAD warrants ASCVD calculation. "
            "Evidence gap: no."
        ),
        "tool_name": "clinical_query",
        "tool_called_at": now_iso,
        "corpus_name": "ADA-2026",
        "evidence_gap_flagged": False,
        "citations": ["ADA 2026 Rec 9.2a"],
    })
    assert v == []


def test_clean_synthesis_rationale():
    v = validate_section({
        "section_id": "synthesis-rationale-clean",
        "agent": "SYNTHESIS",
        "declared_tier": "SYNTHESIZED",
        "content_summary": (
            "Gate decision: mood-first protocol engaged given MIRA flag."
        ),
        "synthesis_basis": (
            "deliberation council outputs + MIRA behavioral flag"
        ),
    })
    assert v == []


# ── hash_mrn ──────────────────────────────────────────────────────────

def test_hash_mrn_properties():
    assert len(hash_mrn("4829341")) == 64
    assert hash_mrn("4829341") == hash_mrn("4829341")
    assert hash_mrn("4829341") != hash_mrn("9999999")


# ── render_recommendation ─────────────────────────────────────────────

def test_render_recommendation_tool_full_authority():
    now_iso = datetime.now(timezone.utc).isoformat()
    section = {
        "declared_tier": "TOOL",
        "tool_name": "clinical_query",
        "tool_called_at": now_iso,
    }
    assert render_recommendation(section, []) == "FULL_AUTHORITY"


def test_render_recommendation_retrieval_with_gap_is_reduced():
    section = {
        "declared_tier": "RETRIEVAL",
        "evidence_gap_flagged": True,
    }
    assert render_recommendation(section, []) == "REDUCED_AUTHORITY"


def test_render_recommendation_blocked_is_withheld():
    section = {"declared_tier": "TOOL"}
    violations = [{"rule": "X", "severity": "BLOCK", "message": "m"}]
    assert render_recommendation(section, violations) == "WITHHELD"


def test_render_recommendation_pending():
    section = {"declared_tier": "PENDING",
               "pending_tool_name": "score_llm_interaction_health"}
    assert render_recommendation(section, []) == "PENDING"
