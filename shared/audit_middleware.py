"""FastMCP middleware that audits every tools/call invocation.

Add to a server once (after registering tools)::

    from shared.audit_middleware import AuditMiddleware
    mcp.add_middleware(AuditMiddleware("skills", get_pool))

The middleware lazily initialises the ``CallRecorder`` on the first inbound
tool call so it does not require a running event loop at import time.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Callable

import mcp.types as mt
from fastmcp.server.middleware import CallNext, Middleware, MiddlewareContext
from fastmcp.tools.base import ToolResult

from shared.call_recorder import CallRecorder

logger = logging.getLogger(__name__)


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
        input_args: dict = dict(context.message.arguments or {})
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
                # Schedule recording without blocking the response
                asyncio.create_task(
                    rec.record(
                        tool_name=tool_name,
                        input_params=input_args,
                        output_text=_text_from_result(result),
                        output_data=_data_from_result(result),
                        duration_ms=duration_ms,
                        outcome=outcome,
                        error_message=error_msg,
                    )
                )
            except Exception as exc:
                logger.warning("audit_middleware: recorder unavailable: %s", exc)
