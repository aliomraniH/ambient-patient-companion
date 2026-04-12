"""FastMCP wrapper — the only module in shared/provenance that touches FastMCP.

Each server imports register_provenance_tool and passes its own
source_server name plus a zero-arg awaitable `get_pool` callable that
returns an asyncpg.Pool. This avoids hard-coding a DB import path that
differs between servers (gap_aware.db vs db.connection vs per-call
ephemeral pools).
"""

import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Awaitable, Callable

from fastmcp import FastMCP

from .verifier import (
    validate_section,
    render_recommendation,
    build_gate_decision,
    hash_mrn,
)
from .audit_writer import write_provenance_audit

logger = logging.getLogger(__name__)


def register_provenance_tool(
    mcp: FastMCP,
    source_server: str,
    get_pool: Callable[[], Awaitable],
) -> None:
    """Register verify_output_provenance on the given FastMCP instance.

    Args:
        mcp: the server's FastMCP instance.
        source_server: one of 'ambient-clinical-intelligence',
            'ambient-skills-companion', 'ambient-ingestion'. Written to
            provenance_audit_log.source_server so failures can be traced
            to the correct pipeline.
        get_pool: zero-arg async callable returning an asyncpg.Pool.
            Each server passes its own process-local pool factory.

    Example:
        from gap_aware.db import get_pool as get_gap_pool
        register_provenance_tool(mcp, "ambient-clinical-intelligence",
                                 get_gap_pool)
    """

    @mcp.tool
    async def verify_output_provenance(
        payload: str,
        deliberation_id: str = "",
        patient_mrn: str = "",
        strict_mode: bool = True,
    ) -> str:
        """Audit an assembled multi-agent output for source provenance.

        Must be called between output assembly and any render step.
        Enforces a declared trust tier on every section:

          TOOL        — live MCP tool call this session (highest trust)
          RETRIEVAL   — retrieval pipeline, corpus-bounded
          SYNTHESIZED — LLM reasoning / memory / architecture spec
          PENDING     — known domain, dedicated tool not yet called

        Agent constraints enforced:
          ARIA      — may not emit SYNTHESIZED (corpus-bound)
          MIRA      — known-tool domains (LLM health, COM-B, ...) → PENDING
          THEO      — known pharmacology domains → RETRIEVAL+GAP not
                      SYNTHESIZED
          SYNTHESIS — scored outputs (OBT, impactability) must be TOOL;
                      rationale and gating logic may be SYNTHESIZED

        Args:
            payload: JSON string with a 'sections' array. Each section:
                { section_id, agent, content_summary, declared_tier,
                  tool_name, tool_called_at, corpus_name,
                  evidence_gap_flagged, synthesis_basis,
                  pending_tool_name, citations }
            deliberation_id: UUID of the deliberation session (optional).
            patient_mrn: hashed before storage. Never logged raw.
            strict_mode: True (default) — any BLOCK gates the report to
                BLOCKED. False — BLOCKs degrade to
                APPROVED_WITH_WARNINGS.

        Returns:
            JSON provenance report with gate_decision, section_results,
            and pending_tools_needed.
        """
        report_id = str(uuid.uuid4())
        assessed_at = datetime.now(timezone.utc).isoformat()

        try:
            data = (
                json.loads(payload)
                if isinstance(payload, str)
                else payload
            )
        except (json.JSONDecodeError, TypeError) as e:
            error = {
                "provenance_report_id": report_id,
                "deliberation_id": deliberation_id,
                "output_id": None,
                "assessed_at": assessed_at,
                "gate_decision": "BLOCKED",
                "block_reason": f"Malformed payload: {e}",
                "section_results": [],
                "summary": {
                    "total_sections": 0,
                    "approved": 0,
                    "warned": 0,
                    "blocked": 0,
                    "pending_tools_needed": [],
                },
            }
            logger.error("verify_output_provenance: bad payload: %s", e)
            return json.dumps(error)

        output_id = data.get("output_id", "unknown")
        assembled_by = data.get("assembled_by", "unknown")
        sections = data.get("sections", [])
        if not isinstance(sections, list):
            sections = []

        section_results: list[dict] = []
        all_pending_tools: list[str] = []

        for section in sections:
            violations = validate_section(section)

            # Collect Rule 6 pending tool suggestions.
            for v in violations:
                pt = v.get("pending_tool")
                if pt and pt not in all_pending_tools:
                    all_pending_tools.append(pt)

            # Collect PENDING-tier tool names (intentional, not violations).
            if section.get("declared_tier") == "PENDING":
                pt = section.get("pending_tool_name")
                if pt and pt not in all_pending_tools:
                    all_pending_tools.append(pt)

            tier_confirmed = (
                section.get("declared_tier")
                in {"TOOL", "RETRIEVAL", "SYNTHESIZED", "PENDING"}
                and not any(v["severity"] == "BLOCK" for v in violations)
            )

            section_results.append({
                "section_id": section.get("section_id", "unknown"),
                "agent": section.get("agent", "unknown"),
                "declared_tier": section.get("declared_tier"),
                "violations": violations,
                "tier_confirmed": tier_confirmed,
                "render_recommendation": render_recommendation(
                    section, violations
                ),
            })

        total = len(section_results)
        blocked = sum(
            1 for s in section_results
            if any(v["severity"] == "BLOCK" for v in s["violations"])
        )
        warned = sum(
            1 for s in section_results
            if (
                any(v["severity"] == "WARN" for v in s["violations"])
                and not any(v["severity"] == "BLOCK" for v in s["violations"])
            )
        )
        approved = total - blocked - warned

        gate_decision, block_reason = build_gate_decision(
            section_results, strict_mode
        )

        report = {
            "provenance_report_id": report_id,
            "deliberation_id": deliberation_id,
            "output_id": output_id,
            "assessed_at": assessed_at,
            "gate_decision": gate_decision,
            "block_reason": block_reason,
            "section_results": section_results,
            "summary": {
                "total_sections": total,
                "approved": approved,
                "warned": warned,
                "blocked": blocked,
                "pending_tools_needed": all_pending_tools,
            },
        }

        try:
            pool = await get_pool()
        except Exception as e:
            logger.error("provenance_audit: get_pool failed: %s", e)
            pool = None

        if pool is not None:
            await write_provenance_audit(
                pool,
                provenance_report_id=report_id,
                deliberation_id=deliberation_id,
                output_id=output_id,
                patient_mrn_hash=(
                    hash_mrn(patient_mrn) if patient_mrn else None
                ),
                source_server=source_server,
                assembled_by=assembled_by,
                gate_decision=gate_decision,
                block_reason=block_reason,
                total_sections=total,
                blocked_count=blocked,
                warned_count=warned,
                approved_count=approved,
                pending_tools_needed=all_pending_tools,
                section_results=section_results,
                strict_mode=strict_mode,
            )

        return json.dumps(report)
