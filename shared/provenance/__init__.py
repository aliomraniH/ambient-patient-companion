"""Universal output provenance verification for the Ambient Patient Companion.

Registered on all three MCP servers. Enforces a declared trust tier
(TOOL / RETRIEVAL / SYNTHESIZED / PENDING) on every claim in an
assembled multi-agent output before SYNTHESIS gates it for render.
"""

from .tool_adapter import register_provenance_tool
from .verifier import (
    validate_section,
    render_recommendation,
    build_gate_decision,
    hash_mrn,
)
from .domain_registry import (
    KNOWN_TOOL_DOMAINS,
    AGENT_RULES,
    RULE_6_AGENTS,
    ALL_AUDITED_AGENTS,
)

__all__ = [
    "register_provenance_tool",
    "validate_section",
    "render_recommendation",
    "build_gate_decision",
    "hash_mrn",
    "KNOWN_TOOL_DOMAINS",
    "AGENT_RULES",
    "RULE_6_AGENTS",
    "ALL_AUDITED_AGENTS",
]
