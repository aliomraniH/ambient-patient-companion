"""FastMCP middleware that audits every tools/call invocation.

Add to a server once (after registering tools)::

    from shared.audit_middleware import AuditMiddleware
    mcp.add_middleware(AuditMiddleware("skills", get_pool))

The middleware lazily initialises the ``CallRecorder`` on the first inbound
tool call so it does not require a running event loop at import time.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import time
from typing import Any, Callable

import mcp.types as mt
from fastmcp.server.middleware import CallNext, Middleware, MiddlewareContext
from fastmcp.tools.base import ToolResult

from shared.call_recorder import CallRecorder

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# PHI field sanitisation
# ---------------------------------------------------------------------------
# _SENSITIVE_PARAMS: maps tool_name → list of (src_field, dest_field, action)
#
# action values:
#   "hash_sha256_16"  — remove src, write SHA-256[:16] hex under dest
#   "redact_flag"     — remove src, write True under dest
#
# _GUARANTEE_FLAGS: maps tool_name → dict of {dest_field: value} that must
# always appear in the sanitised output regardless of which fields the caller
# supplied.  Use this to ensure required audit keys are never absent.
#
# Add entries here whenever a new tool accepts free-text clinical input.

_SENSITIVE_PARAMS: dict[str, list[tuple[str, str, str]]] = {
    # ── Server 1: Clinical Intelligence (server/mcp_server.py) ───────────────
    # clinical_query: free-text clinical question + PHI-dense patient context struct
    "clinical_query": [
        ("query",           "query_hash",            "hash_sha256_16"),
        ("patient_context", "patient_context_redacted", "redact_flag"),
    ],
    # triage_message: raw patient message content
    "triage_message": [
        ("content", "content_hash", "hash_sha256_16"),
    ],
    # request_clarification: clarifying question, rationale, and any free-text answer fields
    "request_clarification": [
        ("question_text",        "question_text_hash",          "hash_sha256_16"),
        ("clinical_rationale",   "clinical_rationale_hash",     "hash_sha256_16"),
        ("default_if_unanswered","default_if_unanswered_hash",  "hash_sha256_16"),
        ("suggested_options",    "suggested_options_redacted",  "redact_flag"),
    ],
    # emit_reasoning_gap_artifact: human-readable clinical gap description fields
    "emit_reasoning_gap_artifact": [
        ("description",                    "description_hash",                    "hash_sha256_16"),
        ("impact_statement",               "impact_statement_hash",               "hash_sha256_16"),
        ("recommended_action_for_synthesis","recommended_action_for_synthesis_hash","hash_sha256_16"),
        ("caveat_text",                    "caveat_text_hash",                    "hash_sha256_16"),
    ],
    # search_guidelines: free-text query against clinical guidelines
    "search_guidelines": [
        ("query", "query_hash", "hash_sha256_16"),
    ],
    # assess_reasoning_confidence: draft agent reasoning text
    "assess_reasoning_confidence": [
        ("reasoning_draft", "reasoning_draft_hash", "hash_sha256_16"),
    ],
    # check_sycophancy_risk: draft clinical output text
    "check_sycophancy_risk": [
        ("draft_output", "draft_output_hash", "hash_sha256_16"),
    ],
    # run_constitutional_critic: draft clinical output text
    "run_constitutional_critic": [
        ("draft_output", "draft_output_hash", "hash_sha256_16"),
    ],

    # ── Server 2: Skills Companion (mcp-server/) ─────────────────────────────
    # call_slm: prompt and system message for the local SLM
    "call_slm": [
        ("prompt",         "prompt_hash",            "hash_sha256_16"),
        ("system_message", "system_message_redacted", "redact_flag"),
    ],
    # extract_and_store_behavioral_atoms: conversation/clinical-note free text
    "extract_and_store_behavioral_atoms": [
        ("text", "text_hash", "hash_sha256_16"),
    ],
    # search_behavioral_atoms_cohort: semantic-search query text
    "search_behavioral_atoms_cohort": [
        ("query_text", "query_text_hash", "hash_sha256_16"),
    ],
    # search_similar_atoms: semantic-search query text (atom_vector_search)
    "search_similar_atoms": [
        ("query_text", "query_text_hash", "hash_sha256_16"),
    ],
    # detect_conversation_teachable_moment: conversation snippet
    "detect_conversation_teachable_moment": [
        ("conversation_text", "conversation_text_hash", "hash_sha256_16"),
    ],
    # score_llm_interaction_health: conversation excerpt for over-reliance detection
    "score_llm_interaction_health": [
        ("conversation_excerpt", "conversation_excerpt_hash", "hash_sha256_16"),
    ],
    # search_clinical_knowledge: free-text clinical query + patient context JSON
    "search_clinical_knowledge": [
        ("query",           "query_hash",            "hash_sha256_16"),
        ("patient_context", "patient_context_redacted", "redact_flag"),
    ],
    # ingest_behavioral_screening_fhir: raw FHIR JSON blob (PHI-dense)
    "ingest_behavioral_screening_fhir": [
        ("fhir_resource_json", "fhir_resource_json_redacted", "redact_flag"),
    ],

    # register_healthex_patient: raw JSON from HealthEx get_health_summary (PHI-dense)
    "register_healthex_patient": [
        ("health_summary_json", "health_summary_json_redacted", "redact_flag"),
    ],
    # ingest_from_healthex: raw HealthEx tool response (labs, meds, conditions, etc.)
    "ingest_from_healthex": [
        ("fhir_json", "fhir_json_redacted", "redact_flag"),
    ],
    # run_healthex_pipeline: all raw JSON payload args from HealthEx tools
    "run_healthex_pipeline": [
        ("health_summary_json",        "health_summary_json_redacted",        "redact_flag"),
        ("labs_json",                  "labs_json_redacted",                  "redact_flag"),
        ("medications_json",           "medications_json_redacted",           "redact_flag"),
        ("conditions_json",            "conditions_json_redacted",            "redact_flag"),
        ("encounters_json",            "encounters_json_redacted",            "redact_flag"),
        ("notes_json",                 "notes_json_redacted",                 "redact_flag"),
        ("behavioral_screenings_json", "behavioral_screenings_json_redacted", "redact_flag"),
    ],

    # propose_cohort_adapter: population_description is free-text clinical cohort
    # description that may contain diagnostic or demographic details
    "propose_cohort_adapter": [
        ("population_description", "population_description_hash", "hash_sha256_16"),
    ],
    # confirm_cohort_creation: proposal_json contains the full proposal blob including
    # population_description and research rationale text
    "confirm_cohort_creation": [
        ("proposal_json", "proposal_json_redacted", "redact_flag"),
    ],

    # ── Server 3: Ingestion (ingestion/server.py) ────────────────────────────
    # detect_healthex_format: raw HealthEx API response (may contain PHI)
    "detect_healthex_format": [
        ("raw_response", "raw_response_redacted", "redact_flag"),
    ],
}

# Unconditional flags written after field-level sanitisation.
# Guarantees the key is present even when the caller omitted the source field.
_GUARANTEE_FLAGS: dict[str, dict[str, object]] = {
    "call_slm":                         {"system_message_redacted": True},
    "clinical_query":                   {"patient_context_redacted": True},
    "search_clinical_knowledge":        {"patient_context_redacted": True},
    "ingest_behavioral_screening_fhir": {"fhir_resource_json_redacted": True},
    "register_healthex_patient":        {"health_summary_json_redacted": True},
    "ingest_from_healthex":             {"fhir_json_redacted": True},
    "detect_healthex_format":           {"raw_response_redacted": True},
    "confirm_cohort_creation":          {"proposal_json_redacted": True},
}


def _sanitise_input(tool_name: str, input_args: dict) -> dict:
    """Return a sanitised copy of *input_args* safe to write to the audit log.

    For each rule in ``_SENSITIVE_PARAMS[tool_name]``:
    - The source field is removed from the output dict.
    - A renamed dest field is added with either a short hash or a boolean flag.

    After field-level rules, ``_GUARANTEE_FLAGS[tool_name]`` keys are
    unconditionally set so required audit fields are never absent even when the
    caller omits optional source fields (e.g. ``system_message`` is optional in
    ``call_slm`` but ``system_message_redacted`` must always appear).

    All other tools and all other fields are returned unchanged.
    The original ``input_args`` dict is never mutated.
    """
    rules = _SENSITIVE_PARAMS.get(tool_name)
    if not rules and tool_name not in _GUARANTEE_FLAGS:
        return input_args

    sanitised = dict(input_args)

    for src_field, dest_field, action in (rules or []):
        if src_field not in sanitised:
            continue
        raw = sanitised.pop(src_field)
        if action == "hash_sha256_16":
            sanitised[dest_field] = (
                hashlib.sha256(raw.encode()).hexdigest()[:16]
                if isinstance(raw, str) else None
            )
        elif action == "redact_flag":
            sanitised[dest_field] = True

    # Apply unconditional guarantee flags (setdefault keeps a hash if already set)
    for dest_field, value in (_GUARANTEE_FLAGS.get(tool_name) or {}).items():
        sanitised.setdefault(dest_field, value)

    return sanitised


# ---------------------------------------------------------------------------
# Helpers: extract human-readable text and structured data from ToolResult
# ---------------------------------------------------------------------------

def _text_from_result(result: ToolResult | None) -> str | None:
    if result is None:
        return None
    parts = [block.text for block in result.content if hasattr(block, "text")]
    text = "\n".join(parts)
    return text[:8000] if text else None


def _data_from_result(result: ToolResult | None) -> Any:
    if result is None:
        return None
    if result.structured_content:
        return result.structured_content
    return None


# ---------------------------------------------------------------------------
# AuditMiddleware
# ---------------------------------------------------------------------------

class AuditMiddleware(Middleware):
    """Intercepts ``tools/call`` requests and writes one audit row per call.

    Parameters
    ----------
    server_name:
        Human-readable name stored in the ``server_name`` column
        (e.g. ``"skills"``, ``"clinical"``, ``"ingestion"``).
    get_pool:
        Zero-argument async callable that returns an asyncpg pool.
        Called once on the first tool invocation.
    """

    def __init__(self, server_name: str, get_pool: Callable) -> None:
        self._server_name = server_name
        self._get_pool = get_pool
        self._recorder: CallRecorder | None = None
        self._init_lock: asyncio.Lock | None = None

    # ---------------------------------------------------------------- private

    async def _get_recorder(self) -> CallRecorder:
        if self._recorder is not None:
            return self._recorder
        if self._init_lock is None:
            self._init_lock = asyncio.Lock()
        async with self._init_lock:
            if self._recorder is None:
                pool = await self._get_pool()
                rec = CallRecorder(self._server_name, pool)
                await rec.ensure_table()
                self._recorder = rec
                logger.info(
                    "audit_middleware[%s]: recorder ready, session=%s",
                    self._server_name, rec.session_id,
                )
        return self._recorder  # type: ignore[return-value]

    # --------------------------------------------------------------- override

    async def on_call_tool(
        self,
        context: MiddlewareContext[mt.CallToolRequestParams],
        call_next: CallNext[mt.CallToolRequestParams, ToolResult],
    ) -> ToolResult:
        tool_name: str = context.message.name
        raw_args: dict = dict(context.message.arguments or {})
        t0 = time.monotonic()
        outcome = "success"
        error_msg: str | None = None
        result: ToolResult | None = None

        try:
            result = await call_next(context)
            return result
        except Exception as exc:
            outcome = "error"
            error_msg = str(exc)
            raise
        finally:
            duration_ms = int((time.monotonic() - t0) * 1000)
            try:
                rec = await self._get_recorder()
                # Sanitise sensitive fields before writing to the audit log.
                # Prompt text and system messages are hashed / redacted so that
                # mcp_call_log never stores raw clinical free-text (PHI).
                safe_args = _sanitise_input(tool_name, raw_args)
                # Schedule recording without blocking the response
                asyncio.create_task(
                    rec.record(
                        tool_name=tool_name,
                        input_params=safe_args,
                        output_text=_text_from_result(result),
                        output_data=_data_from_result(result),
                        duration_ms=duration_ms,
                        outcome=outcome,
                        error_message=error_msg,
                    )
                )
            except Exception as exc:
                logger.warning("audit_middleware: recorder unavailable: %s", exc)
