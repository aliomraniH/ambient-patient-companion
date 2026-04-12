"""Structural tests for the provenance registry.

Prevents drift between ALL_AUDITED_AGENTS, AGENT_RULES, and
RULE_6_AGENTS when new agents are added or renamed.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from shared.provenance.domain_registry import (  # noqa: E402
    ALL_AUDITED_AGENTS,
    AGENT_RULES,
    RULE_6_AGENTS,
    KNOWN_TOOL_DOMAINS,
)


def test_every_audited_agent_has_rules():
    """Every agent subject to audit must have an AGENT_RULES entry.

    An agent without rules silently bypasses the per-agent tier
    pre-check, which is exactly how the MIRA incident slipped through
    before — so guard against that regressing.
    """
    missing = ALL_AUDITED_AGENTS - set(AGENT_RULES.keys())
    assert not missing, (
        f"Audited agents missing from AGENT_RULES: {sorted(missing)}. "
        "Add an entry to shared/provenance/domain_registry.py."
    )


def test_rule_6_agents_are_audited():
    """Every agent that Rule 6 applies to must also be audited."""
    extras = RULE_6_AGENTS - ALL_AUDITED_AGENTS
    assert not extras, (
        f"RULE_6_AGENTS has agents not in ALL_AUDITED_AGENTS: "
        f"{sorted(extras)}."
    )


def test_agent_rules_have_required_keys():
    """Every AGENT_RULES entry must define the structural fields the
    verifier consults. Missing keys throw KeyError at runtime, which is
    a worse failure mode than a test assertion."""
    for agent, rules in AGENT_RULES.items():
        assert "forbidden_tiers" in rules, (
            f"{agent} missing 'forbidden_tiers' (may be empty list)"
        )
        assert isinstance(rules["forbidden_tiers"], list), (
            f"{agent} forbidden_tiers must be a list"
        )
        assert "tool_tier_tools" in rules, (
            f"{agent} missing 'tool_tier_tools' (may be empty list)"
        )
        assert isinstance(rules["tool_tier_tools"], list), (
            f"{agent} tool_tier_tools must be a list"
        )
        # forbidden_tier_message is required if any tier is forbidden.
        if rules["forbidden_tiers"]:
            assert rules.get("forbidden_tier_message"), (
                f"{agent} forbids tiers "
                f"{rules['forbidden_tiers']} but has no "
                "forbidden_tier_message"
            )


def test_known_domain_tools_are_nonempty_strings():
    for keyword, tool in KNOWN_TOOL_DOMAINS.items():
        assert isinstance(keyword, str) and keyword, (
            f"KNOWN_TOOL_DOMAINS has invalid key: {keyword!r}"
        )
        assert isinstance(tool, str) and tool, (
            f"KNOWN_TOOL_DOMAINS[{keyword!r}] has invalid value: {tool!r}"
        )
