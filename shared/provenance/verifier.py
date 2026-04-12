"""Pure validation logic for provenance auditing.

No FastMCP, no asyncpg, no IO. All functions are synchronous and fully
unit-testable in isolation.
"""

import hashlib
from datetime import datetime, timezone

from .domain_registry import (
    KNOWN_TOOL_DOMAINS,
    AGENT_RULES,
    RULE_6_AGENTS,
)

VALID_TIERS = {"TOOL", "RETRIEVAL", "SYNTHESIZED", "PENDING"}


def hash_mrn(mrn: str) -> str:
    """Return the SHA-256 hex digest of an MRN.

    Raw MRNs are never stored in the audit log. Callers must hash
    before writing to provenance_audit_log.
    """
    return hashlib.sha256(mrn.encode()).hexdigest()


def check_agent_tier_constraint(section: dict) -> dict | None:
    """Return a BLOCK violation if the agent's declared tier is
    forbidden by AGENT_RULES, else None.

    This pre-check runs before per-tier validation so that an ARIA
    SYNTHESIZED section is caught immediately without evaluating
    Rules 4 or 6.
    """
    agent = section.get("agent", "")
    tier = section.get("declared_tier", "")
    rules = AGENT_RULES.get(agent, {})

    if tier in rules.get("forbidden_tiers", []):
        return {
            "rule": "AGENT_TIER_CONSTRAINT_VIOLATED",
            "severity": "BLOCK",
            "message": rules.get(
                "forbidden_tier_message",
                f"Agent {agent} may not emit {tier} tier outputs.",
            ),
        }
    return None


def check_known_domain_synthesis(section: dict) -> dict | None:
    """Rule 6: return a BLOCK violation if a section in a known-tool
    domain is declared SYNTHESIZED, or declared TOOL with a tool_name
    that does not match the expected tool for that domain.

    Only fires for agents in RULE_6_AGENTS (MIRA, THEO, SYNTHESIS).
    """
    agent = section.get("agent", "")
    tier = section.get("declared_tier", "")
    sid = section.get("section_id", "unknown")

    if agent not in RULE_6_AGENTS:
        return None
    if tier not in ("SYNTHESIZED", "TOOL"):
        return None

    summary = (section.get("content_summary") or "").lower()
    for keyword, tool_name in KNOWN_TOOL_DOMAINS.items():
        if keyword.lower() in summary:
            declared_tool = section.get("tool_name", "")
            # TOOL tier is fine only if the exact right tool was called.
            if tier == "TOOL" and declared_tool == tool_name:
                continue
            return {
                "rule": "KNOWN_DOMAIN_SYNTHESIZED_INSTEAD_OF_PENDING",
                "severity": "BLOCK",
                "message": (
                    f"Section '{sid}' ({agent}): content references domain "
                    f"'{keyword}' where dedicated tool '{tool_name}' "
                    f"exists. Declared tier: {tier}. Reclassify as PENDING "
                    f"and call: {tool_name}"
                ),
                "pending_tool": tool_name,
            }
    return None


def validate_section(section: dict) -> list[dict]:
    """Run all validation rules against one section.

    Rule execution order:
      Pre-check: AGENT_TIER_CONSTRAINT_VIOLATED
      1. UNTAGGED_CLAIM
      2. TOOL_MISSING_CALL_EVIDENCE
      3. RETRIEVAL_GAP_NOT_DECLARED
      4. SYNTHESIZED_NO_BASIS
      5. PENDING_NO_TOOL_NAMED
      6. KNOWN_DOMAIN_SYNTHESIZED_INSTEAD_OF_PENDING
      7. RETRIEVAL_GAP_SILENCED   (WARN)
      8. TIMESTAMP_STALENESS      (WARN)

    Returns a list of violation dicts. Empty list means the section is
    clean.
    """
    violations: list[dict] = []
    sid = section.get("section_id", "unknown")
    tier = section.get("declared_tier")

    agent_v = check_agent_tier_constraint(section)
    if agent_v:
        violations.append(agent_v)

    # Rule 1: UNTAGGED_CLAIM — short-circuit if tier is missing/invalid.
    if not tier or tier not in VALID_TIERS:
        violations.append({
            "rule": "UNTAGGED_CLAIM",
            "severity": "BLOCK",
            "message": (
                f"Section '{sid}' has no valid tier declaration. Must be "
                f"one of: {sorted(VALID_TIERS)}. All claims must be "
                "tagged before render."
            ),
        })
        return violations

    # Rule 2: TOOL_MISSING_CALL_EVIDENCE
    if tier == "TOOL":
        if not section.get("tool_name") or not section.get("tool_called_at"):
            violations.append({
                "rule": "TOOL_MISSING_CALL_EVIDENCE",
                "severity": "BLOCK",
                "message": (
                    f"Section '{sid}' declares TOOL tier but tool_name or "
                    "tool_called_at is missing. Cannot verify. Reclassify "
                    "as SYNTHESIZED or call the tool first."
                ),
            })

    # Rule 3: RETRIEVAL_GAP_NOT_DECLARED
    if tier == "RETRIEVAL" and section.get("evidence_gap_flagged") is None:
        violations.append({
            "rule": "RETRIEVAL_GAP_NOT_DECLARED",
            "severity": "BLOCK",
            "message": (
                f"Section '{sid}' is RETRIEVAL tier but "
                "evidence_gap_flagged is null. Must be explicitly true "
                "or false."
            ),
        })

    # Rule 4: SYNTHESIZED_NO_BASIS
    if tier == "SYNTHESIZED":
        basis = (section.get("synthesis_basis") or "").strip()
        if not basis:
            violations.append({
                "rule": "SYNTHESIZED_NO_BASIS",
                "severity": "BLOCK",
                "message": (
                    f"Section '{sid}' declares SYNTHESIZED but "
                    "synthesis_basis is empty. State the source of "
                    "reasoning explicitly."
                ),
            })

    # Rule 5: PENDING_NO_TOOL_NAMED
    if tier == "PENDING" and not section.get("pending_tool_name"):
        violations.append({
            "rule": "PENDING_NO_TOOL_NAMED",
            "severity": "BLOCK",
            "message": (
                f"Section '{sid}' declares PENDING but pending_tool_name "
                "is missing. Name the tool that must be called."
            ),
        })

    # Rule 6: KNOWN_DOMAIN_SYNTHESIZED_INSTEAD_OF_PENDING
    r6 = check_known_domain_synthesis(section)
    if r6:
        violations.append(r6)

    # Rule 7: RETRIEVAL_GAP_SILENCED (WARN)
    if (
        tier == "RETRIEVAL"
        and section.get("evidence_gap_flagged") is True
        and section.get("citations")
    ):
        summary = (section.get("content_summary") or "").lower()
        gap_words = [
            "gap", "insufficient", "clinician judgment", "limited evidence",
            "outside corpus", "not available", "phase 2",
        ]
        if not any(w in summary for w in gap_words):
            violations.append({
                "rule": "RETRIEVAL_GAP_SILENCED",
                "severity": "WARN",
                "message": (
                    f"Section '{sid}': evidence_gap_flagged=True but "
                    "content_summary does not mention the gap. Gap must "
                    "be visible in rendered output, not only in metadata."
                ),
            })

    # Rule 8: TIMESTAMP_STALENESS (WARN)
    if tier == "TOOL" and section.get("tool_called_at"):
        try:
            called_at = datetime.fromisoformat(
                section["tool_called_at"].replace("Z", "+00:00")
            )
            age_hours = (
                datetime.now(timezone.utc) - called_at
            ).total_seconds() / 3600
            if age_hours > 24:
                violations.append({
                    "rule": "TIMESTAMP_STALENESS",
                    "severity": "WARN",
                    "message": (
                        f"Section '{sid}': tool_called_at is "
                        f"{age_hours:.1f}h ago (>24h threshold). Consider "
                        "a fresh call before rendering."
                    ),
                })
        except (ValueError, TypeError):
            # Malformed timestamps are ignored — Rule 2 handles missing
            # call evidence; a bad timestamp here is not a BLOCK by itself.
            pass

    return violations


def render_recommendation(section: dict, violations: list[dict]) -> str:
    """Return one of FULL_AUTHORITY | REDUCED_AUTHORITY | WITHHELD | PENDING."""
    tier = section.get("declared_tier", "")
    has_block = any(v["severity"] == "BLOCK" for v in violations)

    if has_block or tier not in VALID_TIERS:
        return "WITHHELD"
    if tier == "PENDING":
        return "PENDING"
    if tier == "SYNTHESIZED":
        return "REDUCED_AUTHORITY"
    if tier == "RETRIEVAL":
        return (
            "REDUCED_AUTHORITY"
            if section.get("evidence_gap_flagged")
            else "FULL_AUTHORITY"
        )
    if tier == "TOOL":
        return "FULL_AUTHORITY"
    return "WITHHELD"


def build_gate_decision(
    section_results: list[dict],
    strict_mode: bool,
) -> tuple[str, str | None]:
    """Aggregate section violations into (gate_decision, block_reason).

    In strict_mode, any BLOCK violation gates the report to BLOCKED.
    Otherwise BLOCKs degrade to APPROVED_WITH_WARNINGS.
    """
    block_messages: list[str] = []
    has_warn = False

    for s in section_results:
        for v in s["violations"]:
            if v["severity"] == "BLOCK":
                block_messages.append(v["message"])
            elif v["severity"] == "WARN":
                has_warn = True

    if block_messages and strict_mode:
        return "BLOCKED", block_messages[0]
    if has_warn or (block_messages and not strict_mode):
        return "APPROVED_WITH_WARNINGS", None
    return "APPROVED", None
