"""Comprehensive tests for the new agent structure introduced in PR #21.

New additions tested here:
  - SYSTEM agent: PENDING forbidden, generic rules still fire
  - PATIENT_FACING agent: SYNTHESIZED forbidden, Rule 10 (AB 3030 disclosure)
  - ARIA added to RULE_6_AGENTS: cross-domain TOOL mismatch caught
  - Rule 9 (CORPUS_BOUND_DOMAIN_NO_GAP, WARN, THEO only) — all domains
  - Rule 10 (AI_DISCLOSURE_MISSING, BLOCK) — all disclosure edge cases
  - build_gate_decision with new agents (strict and lenient modes)
  - render_recommendation for SYSTEM and PATIENT_FACING
  - Multi-agent full pipeline: all 6 agents producing a clean gate

No DB, no FastMCP — pure validation logic only.
"""

import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from shared.provenance.verifier import (  # noqa: E402
    validate_section,
    build_gate_decision,
    render_recommendation,
)
from shared.provenance.domain_registry import (  # noqa: E402
    AGENT_RULES,
    ALL_AUDITED_AGENTS,
    RULE_6_AGENTS,
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _section_result(section: dict) -> dict:
    """Build the dict shape build_gate_decision expects."""
    violations = validate_section(section)
    return {"section": section, "violations": violations}


# ═══════════════════════════════════════════════════════════════════════
# Group 1: SYSTEM agent
# ═══════════════════════════════════════════════════════════════════════

class TestSystemAgent:

    def test_system_tool_tier_clean(self):
        v = validate_section({
            "section_id": "sys-model",
            "agent": "SYSTEM",
            "declared_tier": "TOOL",
            "content_summary": "Model used: claude-sonnet-3-7.",
            "tool_name": "get_model_id",
            "tool_called_at": _now(),
        })
        assert v == []

    def test_system_retrieval_tier_clean(self):
        v = validate_section({
            "section_id": "sys-config",
            "agent": "SYSTEM",
            "declared_tier": "RETRIEVAL",
            "content_summary": "Config: progressive mode, timeout 30s.",
            "evidence_gap_flagged": False,
        })
        assert v == []

    def test_system_synthesized_with_basis_clean(self):
        v = validate_section({
            "section_id": "sys-mode-rationale",
            "agent": "SYSTEM",
            "declared_tier": "SYNTHESIZED",
            "content_summary": "Triage mode selected due to low convergence history.",
            "synthesis_basis": "mode elicitation logic + prior convergence score",
        })
        assert v == []

    def test_system_pending_is_blocked(self):
        v = validate_section({
            "section_id": "sys-waiting",
            "agent": "SYSTEM",
            "declared_tier": "PENDING",
            "content_summary": "Waiting for latency report.",
            "pending_tool_name": "get_latency_report",
        })
        blocked = [r for r in v if r["rule"] == "AGENT_TIER_CONSTRAINT_VIOLATED"]
        assert blocked and blocked[0]["severity"] == "BLOCK"

    def test_system_untagged_fires(self):
        v = validate_section({
            "section_id": "sys-notagged",
            "agent": "SYSTEM",
            "declared_tier": None,
            "content_summary": "Pipeline run at 14:30 UTC.",
        })
        assert any(r["rule"] == "UNTAGGED_CLAIM" for r in v)

    def test_system_synthesized_no_basis_fires(self):
        v = validate_section({
            "section_id": "sys-no-basis",
            "agent": "SYSTEM",
            "declared_tier": "SYNTHESIZED",
            "content_summary": "Deliberation favoured progressive mode.",
            "synthesis_basis": "",
        })
        assert any(r["rule"] == "SYNTHESIZED_NO_BASIS" for r in v)

    def test_system_tool_missing_call_evidence(self):
        v = validate_section({
            "section_id": "sys-tool-no-evidence",
            "agent": "SYSTEM",
            "declared_tier": "TOOL",
            "content_summary": "Latency: 480ms.",
            "tool_name": None,
            "tool_called_at": None,
        })
        assert any(r["rule"] == "TOOL_MISSING_CALL_EVIDENCE" for r in v)

    def test_system_rule6_does_not_fire(self):
        # SYSTEM is not in RULE_6_AGENTS, so it can mention known-domain
        # keywords without being caught by Rule 6.
        v = validate_section({
            "section_id": "sys-obt-mention",
            "agent": "SYSTEM",
            "declared_tier": "SYNTHESIZED",
            "content_summary": "OBT score computation delegated to SYNTHESIS.",
            "synthesis_basis": "orchestration log",
        })
        assert not any(
            r["rule"] == "KNOWN_DOMAIN_SYNTHESIZED_INSTEAD_OF_PENDING"
            for r in v
        )

    def test_system_timestamp_staleness_warns(self):
        v = validate_section({
            "section_id": "sys-stale",
            "agent": "SYSTEM",
            "declared_tier": "TOOL",
            "content_summary": "Run metadata from prior session.",
            "tool_name": "get_run_metadata",
            "tool_called_at": "2024-01-01T00:00:00Z",
        })
        stale = [r for r in v if r["rule"] == "TIMESTAMP_STALENESS"]
        assert stale and stale[0]["severity"] == "WARN"

    def test_system_not_in_rule_6_agents(self):
        assert "SYSTEM" not in RULE_6_AGENTS

    def test_system_is_audited(self):
        assert "SYSTEM" in ALL_AUDITED_AGENTS

    def test_system_has_forbidden_tiers(self):
        assert "PENDING" in AGENT_RULES["SYSTEM"]["forbidden_tiers"]


# ═══════════════════════════════════════════════════════════════════════
# Group 2: PATIENT_FACING agent
# ═══════════════════════════════════════════════════════════════════════

class TestPatientFacingAgent:

    def test_pf_tool_tier_with_string_disclosure_clean(self):
        v = validate_section({
            "section_id": "pf-nudge",
            "agent": "PATIENT_FACING",
            "declared_tier": "TOOL",
            "content_summary": "Schedule your A1c test soon.",
            "tool_name": "select_nudge_type",
            "tool_called_at": _now(),
            "ai_disclosure": "This message was assisted by AI.",
        })
        assert v == []

    def test_pf_tool_tier_with_bool_disclosure_tag_clean(self):
        v = validate_section({
            "section_id": "pf-previsit",
            "agent": "PATIENT_FACING",
            "declared_tier": "TOOL",
            "content_summary": "Pre-visit summary ready.",
            "tool_name": "generate_previsit_brief",
            "tool_called_at": _now(),
            "ai_disclosure_tag": True,
        })
        assert v == []

    def test_pf_retrieval_with_disclosure_clean(self):
        v = validate_section({
            "section_id": "pf-guideline",
            "agent": "PATIENT_FACING",
            "declared_tier": "RETRIEVAL",
            "content_summary": "ADA recommends A1c testing every 3 months.",
            "evidence_gap_flagged": False,
            "ai_disclosure": True,
        })
        assert v == []

    def test_pf_synthesized_blocked(self):
        v = validate_section({
            "section_id": "pf-synth",
            "agent": "PATIENT_FACING",
            "declared_tier": "SYNTHESIZED",
            "content_summary": "You should walk 30 minutes daily.",
            "synthesis_basis": "general health guidelines",
            "ai_disclosure": True,
        })
        tier_block = [r for r in v if r["rule"] == "AGENT_TIER_CONSTRAINT_VIOLATED"]
        assert tier_block and tier_block[0]["severity"] == "BLOCK"

    def test_pf_missing_disclosure_blocks(self):
        v = validate_section({
            "section_id": "pf-no-disclosure",
            "agent": "PATIENT_FACING",
            "declared_tier": "TOOL",
            "content_summary": "Nudge: join the DPP program.",
            "tool_name": "select_nudge_type",
            "tool_called_at": _now(),
        })
        r10 = [r for r in v if r["rule"] == "AI_DISCLOSURE_MISSING"]
        assert r10 and r10[0]["severity"] == "BLOCK"

    def test_pf_empty_string_disclosure_blocks(self):
        v = validate_section({
            "section_id": "pf-empty-disclosure",
            "agent": "PATIENT_FACING",
            "declared_tier": "TOOL",
            "content_summary": "Nudge: reduce sugar intake.",
            "tool_name": "select_nudge_type",
            "tool_called_at": _now(),
            "ai_disclosure": "",
        })
        r10 = [r for r in v if r["rule"] == "AI_DISCLOSURE_MISSING"]
        assert r10 and r10[0]["severity"] == "BLOCK"

    def test_pf_none_disclosure_blocks(self):
        v = validate_section({
            "section_id": "pf-none-disclosure",
            "agent": "PATIENT_FACING",
            "declared_tier": "TOOL",
            "content_summary": "Nudge: take your medication.",
            "tool_name": "select_nudge_type",
            "tool_called_at": _now(),
            "ai_disclosure": None,
            "ai_disclosure_tag": None,
        })
        r10 = [r for r in v if r["rule"] == "AI_DISCLOSURE_MISSING"]
        assert r10 and r10[0]["severity"] == "BLOCK"

    def test_pf_false_disclosure_blocks(self):
        v = validate_section({
            "section_id": "pf-false-disclosure",
            "agent": "PATIENT_FACING",
            "declared_tier": "TOOL",
            "content_summary": "Nudge: check blood pressure today.",
            "tool_name": "select_nudge_type",
            "tool_called_at": _now(),
            "ai_disclosure": False,
        })
        r10 = [r for r in v if r["rule"] == "AI_DISCLOSURE_MISSING"]
        assert r10 and r10[0]["severity"] == "BLOCK"

    def test_pf_disclosure_tag_empty_string_blocks(self):
        v = validate_section({
            "section_id": "pf-tag-empty",
            "agent": "PATIENT_FACING",
            "declared_tier": "TOOL",
            "content_summary": "Nudge: exercise this week.",
            "tool_name": "select_nudge_type",
            "tool_called_at": _now(),
            "ai_disclosure_tag": "",
        })
        r10 = [r for r in v if r["rule"] == "AI_DISCLOSURE_MISSING"]
        assert r10 and r10[0]["severity"] == "BLOCK"

    def test_pf_compute_obt_score_tool_clean(self):
        v = validate_section({
            "section_id": "pf-obt",
            "agent": "PATIENT_FACING",
            "declared_tier": "TOOL",
            "content_summary": "Your wellness score this month.",
            "tool_name": "compute_obt_score",
            "tool_called_at": _now(),
            "ai_disclosure": "Computed using AI analysis.",
        })
        assert v == []

    def test_pf_rule9_does_not_fire(self):
        # Rule 9 is THEO-only. PATIENT_FACING mentioning PPI should not
        # trigger Rule 9.
        v = validate_section({
            "section_id": "pf-ppi",
            "agent": "PATIENT_FACING",
            "declared_tier": "RETRIEVAL",
            "content_summary": "PPI therapy: your doctor monitors for risks.",
            "evidence_gap_flagged": False,
            "ai_disclosure": True,
        })
        assert not any(r["rule"] == "CORPUS_BOUND_DOMAIN_NO_GAP" for r in v)

    def test_pf_pending_section_still_needs_disclosure(self):
        v = validate_section({
            "section_id": "pf-pending",
            "agent": "PATIENT_FACING",
            "declared_tier": "PENDING",
            "content_summary": "Awaiting nudge selection.",
            "pending_tool_name": "select_nudge_type",
        })
        r10 = [r for r in v if r["rule"] == "AI_DISCLOSURE_MISSING"]
        assert r10 and r10[0]["severity"] == "BLOCK"

    def test_pf_is_audited_and_not_in_rule6(self):
        assert "PATIENT_FACING" in ALL_AUDITED_AGENTS
        assert "PATIENT_FACING" not in RULE_6_AGENTS

    def test_pf_synthesized_is_forbidden(self):
        assert "SYNTHESIZED" in AGENT_RULES["PATIENT_FACING"]["forbidden_tiers"]


# ═══════════════════════════════════════════════════════════════════════
# Group 3: ARIA added to RULE_6_AGENTS — cross-domain TOOL mismatch
# ═══════════════════════════════════════════════════════════════════════

class TestAriaRule6CrossDomain:

    def _aria_tool(self, content: str, tool_name: str = "clinical_query") -> list:
        return validate_section({
            "section_id": "aria-cross",
            "agent": "ARIA",
            "declared_tier": "TOOL",
            "content_summary": content,
            "tool_name": tool_name,
            "tool_called_at": _now(),
        })

    def test_aria_gout_pharmacology_blocked(self):
        v = self._aria_tool("Gout treatment: allopurinol first-line.")
        assert any(
            r["rule"] == "KNOWN_DOMAIN_SYNTHESIZED_INSTEAD_OF_PENDING" for r in v
        )

    def test_aria_polypharmacy_blocked(self):
        v = self._aria_tool("Polypharmacy risk is elevated given 8 medications.")
        assert any(
            r["rule"] == "KNOWN_DOMAIN_SYNTHESIZED_INSTEAD_OF_PENDING" for r in v
        )

    def test_aria_drug_drug_interaction_blocked(self):
        v = self._aria_tool("Drug-drug: warfarin + aspirin increases bleed risk.")
        assert any(
            r["rule"] == "KNOWN_DOMAIN_SYNTHESIZED_INSTEAD_OF_PENDING" for r in v
        )

    def test_aria_statin_hepatotoxicity_blocked(self):
        v = self._aria_tool("Statin hepatotoxicity: atorvastatin LFT elevation.")
        assert any(
            r["rule"] == "KNOWN_DOMAIN_SYNTHESIZED_INSTEAD_OF_PENDING" for r in v
        )

    def test_aria_dosing_adjustment_blocked(self):
        v = self._aria_tool("Dosing adjustment needed for CKD stage 3.")
        assert any(
            r["rule"] == "KNOWN_DOMAIN_SYNTHESIZED_INSTEAD_OF_PENDING" for r in v
        )

    def test_aria_contraindication_blocked(self):
        v = self._aria_tool("Contraindication: metformin with eGFR < 30.")
        assert any(
            r["rule"] == "KNOWN_DOMAIN_SYNTHESIZED_INSTEAD_OF_PENDING" for r in v
        )

    def test_aria_pantoprazole_long_term_blocked(self):
        v = self._aria_tool("Pantoprazole long-term use in GERD management.")
        assert any(
            r["rule"] == "KNOWN_DOMAIN_SYNTHESIZED_INSTEAD_OF_PENDING" for r in v
        )

    def test_aria_retrieval_tier_rule6_does_not_fire(self):
        # Rule 6 only fires on TOOL or SYNTHESIZED tiers. RETRIEVAL is exempt.
        v = validate_section({
            "section_id": "aria-retrieval-ppi",
            "agent": "ARIA",
            "declared_tier": "RETRIEVAL",
            "content_summary": "PPI safety: pantoprazole long-term data.",
            "evidence_gap_flagged": True,
        })
        assert not any(
            r["rule"] == "KNOWN_DOMAIN_SYNTHESIZED_INSTEAD_OF_PENDING" for r in v
        )

    def test_aria_correct_tool_for_cardiovascular_is_clean(self):
        # ARIA using clinical_query for cardiovascular content is correct.
        v = validate_section({
            "section_id": "aria-cardio-ok",
            "agent": "ARIA",
            "declared_tier": "TOOL",
            "content_summary": "LDL 112 warrants ASCVD risk assessment.",
            "tool_name": "clinical_query",
            "tool_called_at": _now(),
            "evidence_gap_flagged": False,
        })
        assert not any(
            r["rule"] == "KNOWN_DOMAIN_SYNTHESIZED_INSTEAD_OF_PENDING" for r in v
        )

    def test_aria_in_rule6_agents(self):
        assert "ARIA" in RULE_6_AGENTS


# ═══════════════════════════════════════════════════════════════════════
# Group 4: Rule 9 — CORPUS_BOUND_DOMAIN_NO_GAP (WARN, THEO only)
# ═══════════════════════════════════════════════════════════════════════

class TestRule9CorpusBoundDomain:

    def _theo_retrieval(self, content: str, gap: bool = False) -> list:
        return validate_section({
            "section_id": "theo-r9",
            "agent": "THEO",
            "declared_tier": "RETRIEVAL",
            "content_summary": content,
            "evidence_gap_flagged": gap,
        })

    def test_statin_no_gap_warns(self):
        v = self._theo_retrieval("Statin therapy is safe in the given lipid profile.")
        warn = [r for r in v if r["rule"] == "CORPUS_BOUND_DOMAIN_NO_GAP"]
        assert warn and warn[0]["severity"] == "WARN"

    def test_gout_no_gap_warns(self):
        v = self._theo_retrieval("Gout pharmacology: allopurinol is first-line.")
        warn = [r for r in v if r["rule"] == "CORPUS_BOUND_DOMAIN_NO_GAP"]
        assert warn and warn[0]["severity"] == "WARN"

    def test_dosing_no_gap_warns(self):
        v = self._theo_retrieval("Dosing is appropriate for the patient's renal function.")
        warn = [r for r in v if r["rule"] == "CORPUS_BOUND_DOMAIN_NO_GAP"]
        assert warn and warn[0]["severity"] == "WARN"

    def test_hypomagnesemia_no_gap_warns(self):
        v = self._theo_retrieval("Hypomagnesemia: PPI-induced risk is well documented.")
        warn = [r for r in v if r["rule"] == "CORPUS_BOUND_DOMAIN_NO_GAP"]
        assert warn and warn[0]["severity"] == "WARN"

    def test_uric_acid_no_gap_warns(self):
        v = self._theo_retrieval("Uric acid management: target < 6 mg/dL.")
        warn = [r for r in v if r["rule"] == "CORPUS_BOUND_DOMAIN_NO_GAP"]
        assert warn and warn[0]["severity"] == "WARN"

    def test_drug_interaction_no_gap_warns(self):
        v = self._theo_retrieval("Drug interaction between lisinopril and K+ supplements.")
        warn = [r for r in v if r["rule"] == "CORPUS_BOUND_DOMAIN_NO_GAP"]
        assert warn and warn[0]["severity"] == "WARN"

    def test_polypharmacy_no_gap_warns(self):
        v = self._theo_retrieval("Polypharmacy burden: patient on 9 medications.")
        warn = [r for r in v if r["rule"] == "CORPUS_BOUND_DOMAIN_NO_GAP"]
        assert warn and warn[0]["severity"] == "WARN"

    def test_pantoprazole_no_gap_warns(self):
        v = self._theo_retrieval("Pantoprazole is well tolerated in this population.")
        warn = [r for r in v if r["rule"] == "CORPUS_BOUND_DOMAIN_NO_GAP"]
        assert warn and warn[0]["severity"] == "WARN"

    def test_gap_declared_suppresses_rule9(self):
        # evidence_gap_flagged=True means the agent already called the gap out
        # → Rule 9 should not fire.
        v = self._theo_retrieval("PPI therapy: gap flagged for pantoprazole.", gap=True)
        assert not any(r["rule"] == "CORPUS_BOUND_DOMAIN_NO_GAP" for r in v)

    def test_rule9_does_not_fire_for_tool_tier(self):
        # Rule 9 only fires on RETRIEVAL. THEO TOOL tier with corpus content
        # is already covered by Rule 6 — no double-firing with Rule 9.
        v = validate_section({
            "section_id": "theo-tool-statin",
            "agent": "THEO",
            "declared_tier": "TOOL",
            "content_summary": "Statin safety confirmed via flag_drug_interaction.",
            "tool_name": "flag_drug_interaction",
            "tool_called_at": _now(),
        })
        assert not any(r["rule"] == "CORPUS_BOUND_DOMAIN_NO_GAP" for r in v)

    def test_rule9_does_not_fire_for_synthesized_tier(self):
        v = validate_section({
            "section_id": "theo-synth-statin",
            "agent": "THEO",
            "declared_tier": "SYNTHESIZED",
            "content_summary": "Statin risk is overall low given the profile.",
            "synthesis_basis": "clinical training",
        })
        assert not any(r["rule"] == "CORPUS_BOUND_DOMAIN_NO_GAP" for r in v)

    def test_rule9_does_not_fire_when_gap_is_none(self):
        # evidence_gap_flagged=None triggers Rule 3 (RETRIEVAL_GAP_NOT_DECLARED)
        # but should NOT independently trigger Rule 9 (gap is not False).
        v = validate_section({
            "section_id": "theo-gap-none",
            "agent": "THEO",
            "declared_tier": "RETRIEVAL",
            "content_summary": "PPI data suggests low risk.",
            "evidence_gap_flagged": None,
        })
        assert any(r["rule"] == "RETRIEVAL_GAP_NOT_DECLARED" for r in v)
        assert not any(r["rule"] == "CORPUS_BOUND_DOMAIN_NO_GAP" for r in v)

    def test_rule9_only_fires_for_theo(self):
        for agent in ["ARIA", "MIRA", "SYNTHESIS", "SYSTEM", "PATIENT_FACING"]:
            v = validate_section({
                "section_id": f"{agent.lower()}-statin",
                "agent": agent,
                "declared_tier": "RETRIEVAL",
                "content_summary": "Statin therapy is generally safe.",
                "evidence_gap_flagged": False,
                "ai_disclosure": True,
            })
            assert not any(
                r["rule"] == "CORPUS_BOUND_DOMAIN_NO_GAP" for r in v
            ), f"Rule 9 incorrectly fired for {agent}"

    def test_rule9_content_check_is_case_insensitive(self):
        v = self._theo_retrieval("STATIN SAFETY: atorvastatin liver risk.")
        warn = [r for r in v if r["rule"] == "CORPUS_BOUND_DOMAIN_NO_GAP"]
        assert warn and warn[0]["severity"] == "WARN"

    def test_rule9_non_corpus_content_is_clean(self):
        # Cardiovascular content with no corpus-bound keywords → Rule 9 must
        # not fire, even for THEO RETRIEVAL.
        v = self._theo_retrieval(
            "ASCVD 10-year risk is 8.4% based on ACC/AHA calculator."
        )
        assert not any(r["rule"] == "CORPUS_BOUND_DOMAIN_NO_GAP" for r in v)


# ═══════════════════════════════════════════════════════════════════════
# Group 5: build_gate_decision — new agents in pipeline
# ═══════════════════════════════════════════════════════════════════════

class TestBuildGateDecisionNewAgents:

    def test_system_section_approved_in_gate(self):
        results = [
            _section_result({
                "section_id": "sys-latency",
                "agent": "SYSTEM",
                "declared_tier": "TOOL",
                "content_summary": "Latency: 310ms.",
                "tool_name": "get_latency",
                "tool_called_at": _now(),
            })
        ]
        gate, reason = build_gate_decision(results, strict_mode=True)
        assert gate == "APPROVED"
        assert reason is None

    def test_system_pending_blocks_gate_strict(self):
        results = [
            _section_result({
                "section_id": "sys-blocked",
                "agent": "SYSTEM",
                "declared_tier": "PENDING",
                "content_summary": "Awaiting model info.",
                "pending_tool_name": "get_model",
            })
        ]
        gate, _ = build_gate_decision(results, strict_mode=True)
        assert gate == "BLOCKED"

    def test_patient_facing_no_disclosure_blocks_gate_strict(self):
        results = [
            _section_result({
                "section_id": "pf-nodisclosure",
                "agent": "PATIENT_FACING",
                "declared_tier": "TOOL",
                "content_summary": "Take your metformin daily.",
                "tool_name": "select_nudge_type",
                "tool_called_at": _now(),
            })
        ]
        gate, reason = build_gate_decision(results, strict_mode=True)
        assert gate == "BLOCKED"
        assert reason is not None

    def test_patient_facing_with_disclosure_approved(self):
        results = [
            _section_result({
                "section_id": "pf-ok",
                "agent": "PATIENT_FACING",
                "declared_tier": "TOOL",
                "content_summary": "Schedule your annual eye exam.",
                "tool_name": "generate_previsit_brief",
                "tool_called_at": _now(),
                "ai_disclosure_tag": True,
            })
        ]
        gate, _ = build_gate_decision(results, strict_mode=True)
        assert gate == "APPROVED"

    def test_rule9_warn_produces_approved_with_warnings(self):
        results = [
            _section_result({
                "section_id": "theo-statin-no-gap",
                "agent": "THEO",
                "declared_tier": "RETRIEVAL",
                "content_summary": "Statin safety is well documented.",
                "evidence_gap_flagged": False,
            })
        ]
        gate, _ = build_gate_decision(results, strict_mode=True)
        assert gate == "APPROVED_WITH_WARNINGS"

    def test_block_lenient_mode_approved_with_warnings(self):
        # In non-strict mode, a BLOCK downgrades to APPROVED_WITH_WARNINGS.
        results = [
            _section_result({
                "section_id": "aria-synth",
                "agent": "ARIA",
                "declared_tier": "SYNTHESIZED",
                "content_summary": "Cardiovascular risk reasoned.",
                "synthesis_basis": "clinical training",
            })
        ]
        gate, _ = build_gate_decision(results, strict_mode=False)
        assert gate == "APPROVED_WITH_WARNINGS"

    def test_mixed_pipeline_with_all_new_agents_approved(self):
        results = [
            _section_result({
                "section_id": "sys-meta",
                "agent": "SYSTEM",
                "declared_tier": "TOOL",
                "content_summary": "Model: claude-sonnet-3-7, mode: progressive.",
                "tool_name": "get_run_metadata",
                "tool_called_at": _now(),
            }),
            _section_result({
                "section_id": "pf-nudge",
                "agent": "PATIENT_FACING",
                "declared_tier": "TOOL",
                "content_summary": "Join the DPP diabetes prevention program.",
                "tool_name": "select_nudge_type",
                "tool_called_at": _now(),
                "ai_disclosure": "Generated with AI assistance.",
            }),
        ]
        gate, _ = build_gate_decision(results, strict_mode=True)
        assert gate == "APPROVED"

    def test_mixed_pipeline_blocked_by_pf_no_disclosure(self):
        results = [
            _section_result({
                "section_id": "sys-meta",
                "agent": "SYSTEM",
                "declared_tier": "TOOL",
                "content_summary": "Model: gpt-4o, mode: triage.",
                "tool_name": "get_run_metadata",
                "tool_called_at": _now(),
            }),
            _section_result({
                "section_id": "pf-missing",
                "agent": "PATIENT_FACING",
                "declared_tier": "TOOL",
                "content_summary": "Check your blood pressure today.",
                "tool_name": "select_nudge_type",
                "tool_called_at": _now(),
                # No ai_disclosure
            }),
        ]
        gate, _ = build_gate_decision(results, strict_mode=True)
        assert gate == "BLOCKED"


# ═══════════════════════════════════════════════════════════════════════
# Group 6: render_recommendation for new agents
# ═══════════════════════════════════════════════════════════════════════

class TestRenderRecommendationNewAgents:

    def test_system_tool_clean_is_full_authority(self):
        section = {"declared_tier": "TOOL", "tool_name": "get_latency", "tool_called_at": _now()}
        assert render_recommendation(section, []) == "FULL_AUTHORITY"

    def test_system_synthesized_clean_is_reduced(self):
        section = {"declared_tier": "SYNTHESIZED", "synthesis_basis": "mode logic"}
        assert render_recommendation(section, []) == "REDUCED_AUTHORITY"

    def test_system_retrieval_no_gap_is_full_authority(self):
        section = {"declared_tier": "RETRIEVAL", "evidence_gap_flagged": False}
        assert render_recommendation(section, []) == "FULL_AUTHORITY"

    def test_system_pending_blocked_becomes_withheld(self):
        violations = [{"rule": "AGENT_TIER_CONSTRAINT_VIOLATED", "severity": "BLOCK",
                       "message": "SYSTEM cannot be PENDING."}]
        section = {"declared_tier": "PENDING"}
        assert render_recommendation(section, violations) == "WITHHELD"

    def test_pf_tool_clean_with_disclosure_is_full_authority(self):
        section = {
            "declared_tier": "TOOL",
            "tool_name": "select_nudge_type",
            "tool_called_at": _now(),
            "ai_disclosure": "AI assisted.",
        }
        assert render_recommendation(section, []) == "FULL_AUTHORITY"

    def test_pf_missing_disclosure_block_is_withheld(self):
        violations = [{"rule": "AI_DISCLOSURE_MISSING", "severity": "BLOCK", "message": "m"}]
        section = {"declared_tier": "TOOL"}
        assert render_recommendation(section, violations) == "WITHHELD"

    def test_pf_retrieval_with_gap_is_reduced(self):
        section = {"declared_tier": "RETRIEVAL", "evidence_gap_flagged": True}
        assert render_recommendation(section, []) == "REDUCED_AUTHORITY"

    def test_pf_synthesized_blocked_is_withheld(self):
        violations = [{"rule": "AGENT_TIER_CONSTRAINT_VIOLATED", "severity": "BLOCK", "message": "m"}]
        section = {"declared_tier": "SYNTHESIZED"}
        assert render_recommendation(section, violations) == "WITHHELD"


# ═══════════════════════════════════════════════════════════════════════
# Group 7: Full 6-agent clinical pipeline — clean end-to-end gate
# ═══════════════════════════════════════════════════════════════════════

class TestFullSixAgentPipeline:
    """Realistic pipeline with all 6 audited agents.

    ARIA   → cardiovascular RETRIEVAL (no gap, clinical_query would give TOOL;
             here gap flagged intentionally for REDUCED_AUTHORITY)
    MIRA   → COM-B barrier via classify_com_b_barrier (TOOL)
    THEO   → drug interaction via flag_drug_interaction (TOOL)
    SYNTHESIS → gate decision SYNTHESIZED with basis
    SYSTEM    → run metadata TOOL
    PATIENT_FACING → nudge TOOL with disclosure
    """

    def _pipeline(self):
        return [
            _section_result({
                "section_id": "aria-ascvd",
                "agent": "ARIA",
                "declared_tier": "RETRIEVAL",
                "content_summary": "ASCVD 10-year risk: 9.2%. Guideline: statin if >7.5%.",
                "evidence_gap_flagged": True,
                "citations": ["ACC/AHA 2019"],
            }),
            _section_result({
                "section_id": "mira-comb",
                "agent": "MIRA",
                "declared_tier": "TOOL",
                "content_summary": "COM-B barrier: motivation barrier around medication adherence.",
                "tool_name": "classify_com_b_barrier",
                "tool_called_at": _now(),
            }),
            _section_result({
                "section_id": "theo-drug",
                "agent": "THEO",
                "declared_tier": "TOOL",
                "content_summary": "Drug interaction: no interaction found between lisinopril and metformin.",
                "tool_name": "flag_drug_interaction",
                "tool_called_at": _now(),
            }),
            _section_result({
                "section_id": "synthesis-rationale",
                "agent": "SYNTHESIS",
                "declared_tier": "SYNTHESIZED",
                "content_summary": "Gate decision: mood-first protocol engaged; statin discussion deferred.",
                "synthesis_basis": "ARIA ASCVD + MIRA barrier + THEO no-interaction",
            }),
            _section_result({
                "section_id": "sys-meta",
                "agent": "SYSTEM",
                "declared_tier": "TOOL",
                "content_summary": "Model: claude-sonnet-3-7, mode: progressive, latency: 620ms.",
                "tool_name": "get_run_metadata",
                "tool_called_at": _now(),
            }),
            _section_result({
                "section_id": "pf-statin-nudge",
                "agent": "PATIENT_FACING",
                "declared_tier": "TOOL",
                "content_summary": "Talk to your doctor about a cholesterol medication.",
                "tool_name": "select_nudge_type",
                "tool_called_at": _now(),
                "ai_disclosure": "This message was generated with AI assistance (AB 3030).",
            }),
        ]

    def test_all_sections_pass_individually(self):
        for sr in self._pipeline():
            blocks = [v for v in sr["violations"] if v["severity"] == "BLOCK"]
            assert not blocks, (
                f"Section '{sr['section']['section_id']}' has unexpected "
                f"BLOCK violations: {blocks}"
            )

    def test_gate_decision_approved_with_warnings(self):
        # ARIA gap-flagged RETRIEVAL produces RETRIEVAL_GAP_SILENCED WARN →
        # gate should be APPROVED_WITH_WARNINGS (not BLOCKED).
        gate, reason = build_gate_decision(self._pipeline(), strict_mode=True)
        assert gate in ("APPROVED", "APPROVED_WITH_WARNINGS")
        assert reason is None

    def test_render_all_sections(self):
        expected = {
            "aria-ascvd": "REDUCED_AUTHORITY",
            "mira-comb": "FULL_AUTHORITY",
            "theo-drug": "FULL_AUTHORITY",
            "synthesis-rationale": "REDUCED_AUTHORITY",
            "sys-meta": "FULL_AUTHORITY",
            "pf-statin-nudge": "FULL_AUTHORITY",
        }
        for sr in self._pipeline():
            sid = sr["section"]["section_id"]
            rec = render_recommendation(sr["section"], sr["violations"])
            assert rec == expected[sid], (
                f"Section '{sid}': expected {expected[sid]}, got {rec}. "
                f"Violations: {sr['violations']}"
            )

    def test_removing_pf_disclosure_blocks_gate(self):
        pipeline = self._pipeline()
        for sr in pipeline:
            if sr["section"]["section_id"] == "pf-statin-nudge":
                del sr["section"]["ai_disclosure"]
                sr["violations"] = validate_section(sr["section"])
        gate, _ = build_gate_decision(pipeline, strict_mode=True)
        assert gate == "BLOCKED"

    def test_aria_synthesized_injection_blocks_gate(self):
        # Inject a bad ARIA SYNTHESIZED section into an otherwise-clean
        # pipeline and verify the gate catches it.
        pipeline = self._pipeline()
        pipeline.append(_section_result({
            "section_id": "aria-bad-synth",
            "agent": "ARIA",
            "declared_tier": "SYNTHESIZED",
            "content_summary": "ASCVD risk appears manageable with lifestyle alone.",
            "synthesis_basis": "clinical training",
        }))
        gate, _ = build_gate_decision(pipeline, strict_mode=True)
        assert gate == "BLOCKED"


# ═══════════════════════════════════════════════════════════════════════
# Group 8: Registry integrity (extension of structural tests)
# ═══════════════════════════════════════════════════════════════════════

class TestRegistryIntegrityExtended:

    def test_all_rule6_agents_are_audited(self):
        for agent in RULE_6_AGENTS:
            assert agent in ALL_AUDITED_AGENTS, (
                f"RULE_6_AGENTS member {agent!r} not in ALL_AUDITED_AGENTS"
            )

    def test_all_audited_agents_have_rules(self):
        missing = ALL_AUDITED_AGENTS - set(AGENT_RULES.keys())
        assert not missing, f"Agents without rules: {sorted(missing)}"

    def test_system_and_patient_facing_registered(self):
        assert "SYSTEM" in AGENT_RULES
        assert "PATIENT_FACING" in AGENT_RULES

    def test_aria_registered_in_rule6(self):
        assert "ARIA" in RULE_6_AGENTS

    def test_six_audited_agents_total(self):
        assert len(ALL_AUDITED_AGENTS) == 6, (
            f"Expected 6 audited agents, got {len(ALL_AUDITED_AGENTS)}: "
            f"{sorted(ALL_AUDITED_AGENTS)}"
        )

    def test_theo_corpus_bound_domains_populated(self):
        domains = AGENT_RULES.get("THEO", {}).get("corpus_bound_domains", [])
        assert len(domains) >= 5, (
            f"THEO corpus_bound_domains too small: {domains}"
        )
        assert any("ppi" in d.lower() or "pantoprazole" in d.lower()
                   for d in domains)
        assert any("statin" in d.lower() for d in domains)

    def test_patient_facing_tool_tier_tools_populated(self):
        tools = AGENT_RULES["PATIENT_FACING"].get("tool_tier_tools", [])
        assert "select_nudge_type" in tools
        assert "generate_previsit_brief" in tools
        assert "compute_obt_score" in tools
