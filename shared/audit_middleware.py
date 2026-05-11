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
    "call_slm": [
        ("prompt",         "prompt_hash",            "hash_sha256_16"),
        ("system_message", "system_message_redacted", "redact_flag"),
    ],
}

# Unconditional flags written after field-level sanitisation.
# Guarantees the key is present even when the caller omitted the source field.
_GUARANTEE_FLAGS: dict[str, dict[str, object]] = {
    "call_slm": {"system_message_redacted": True},
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
