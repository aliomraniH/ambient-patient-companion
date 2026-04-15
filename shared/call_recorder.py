"""MCP call recorder — writes every tool invocation to mcp_call_log.

Every MCP server creates an ``AuditMiddleware`` (see ``shared/audit_middleware.py``)
that calls ``CallRecorder.record()`` after each tool call.  The recorder:

* Groups calls into sessions separated by 30 minutes of inactivity.
* Writes one row per call to ``mcp_call_log`` with timing, inputs, and outputs.
* Registers itself in the module-level ``_REGISTRY`` so query tools can look up
  the current session ID and live call count for any running server.

Usage (in each server's entry point)::

    from shared.audit_middleware import AuditMiddleware
    mcp.add_middleware(AuditMiddleware("skills", get_pool))
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)

_SESSION_IDLE_TIMEOUT_S: int = 1800  # 30 min of inactivity → new session

# ---------------------------------------------------------------------------
# Global registry — maps server_name → CallRecorder so query tools can
# introspect the current session without coupling to a specific server.
# ---------------------------------------------------------------------------
_REGISTRY: dict[str, "CallRecorder"] = {}


def register_recorder(recorder: "CallRecorder") -> None:
    _REGISTRY[recorder.server_name] = recorder


def get_registry() -> dict[str, "CallRecorder"]:
    return dict(_REGISTRY)


# ---------------------------------------------------------------------------
# DDL — executed once on startup if the table does not exist.
# ---------------------------------------------------------------------------
_DDL_STATEMENTS = [
    """
    CREATE TABLE IF NOT EXISTS mcp_call_log (
        id              BIGSERIAL PRIMARY KEY,
        session_id      TEXT        NOT NULL,
        server_name     TEXT        NOT NULL,
        tool_name       TEXT        NOT NULL,
        called_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
        duration_ms     INTEGER,
        input_params    JSONB,
        output_text     TEXT,
        output_data     JSONB,
        outcome         TEXT        NOT NULL DEFAULT 'success',
        error_message   TEXT,
        seq             INTEGER
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_mcl_session ON mcp_call_log(session_id, called_at)",
    "CREATE INDEX IF NOT EXISTS idx_mcl_tool    ON mcp_call_log(tool_name, called_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_mcl_ts      ON mcp_call_log(called_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_mcl_server  ON mcp_call_log(server_name, called_at DESC)",
]


# ---------------------------------------------------------------------------
# Serialisation helper
# ---------------------------------------------------------------------------
def _safe_json(obj: Any, depth: int = 6) -> Any:
    """Recursively serialise *obj* to a JSON-safe structure, capping depth and
    string length so large payloads don't blow up the DB row."""
    if depth <= 0:
        return "…"
    if obj is None or isinstance(obj, (bool, int, float)):
        return obj
    if isinstance(obj, str):
        return obj[:4000] + ("… [truncated]" if len(obj) > 4000 else "")
    if isinstance(obj, dict):
        return {str(k): _safe_json(v, depth - 1) for k, v in list(obj.items())[:50]}
    if isinstance(obj, (list, tuple)):
        return [_safe_json(v, depth - 1) for v in list(obj)[:100]]
    return str(obj)[:2000]


# ---------------------------------------------------------------------------
# CallRecorder
# ---------------------------------------------------------------------------
class CallRecorder:
    """Per-server recorder: tracks session state and writes audit rows."""

    def __init__(self, server_name: str, pool: Any) -> None:
        self._server_name = server_name
        self._pool = pool
        # Session state
        self._session_id: str = str(uuid.uuid4())
        self._session_started_at: datetime = datetime.now(timezone.utc)
        self._last_call_at: float = 0.0
        self._seq: int = 0
        self._total_calls: int = 0
        # Self-register
        register_recorder(self)

    # ---------------------------------------------------------------- public

    @property
    def server_name(self) -> str:
        return self._server_name

    @property
    def session_id(self) -> str:
        return self._session_id

    @property
    def session_started_at(self) -> datetime:
        return self._session_started_at

    @property
    def total_calls(self) -> int:
        return self._total_calls

    @property
    def seq(self) -> int:
        return self._seq

    async def ensure_table(self) -> None:
        """Create mcp_call_log and its indexes if they do not exist."""
        async with self._pool.acquire() as conn:
            for stmt in _DDL_STATEMENTS:
                try:
                    await conn.execute(stmt)
                except Exception as exc:
                    logger.debug("ensure_table: %s", exc)

    async def record(
        self,
        *,
        tool_name: str,
        input_params: dict,
        output_text: str | None,
        output_data: Any,
        duration_ms: int,
        outcome: str,
        error_message: str | None = None,
    ) -> None:
        """Write one audit row.  Swallows all errors so recording never
        disrupts tool execution."""
        session_id, seq = self._tick()
        try:
            async with self._pool.acquire() as conn:
                await conn.execute(
                    """
                    INSERT INTO mcp_call_log
                        (session_id, server_name, tool_name, called_at,
                         duration_ms, input_params, output_text, output_data,
                         outcome, error_message, seq)
                    VALUES ($1, $2, $3, now(), $4, $5::jsonb, $6, $7::jsonb,
                            $8, $9, $10)
                    """,
                    session_id,
                    self._server_name,
                    tool_name,
                    duration_ms,
                    json.dumps(_safe_json(input_params), default=str),
                    output_text,
                    json.dumps(_safe_json(output_data), default=str),
                    outcome,
                    error_message,
                    seq,
                )
        except Exception as exc:
            logger.warning("call_recorder: DB write failed (%s): %s",
                           tool_name, exc)

    # --------------------------------------------------------------- private

    def _tick(self) -> tuple[str, int]:
        """Update session / sequence counter; return (session_id, seq)."""
        now = time.monotonic()
        if now - self._last_call_at > _SESSION_IDLE_TIMEOUT_S:
            old = self._session_id
            self._session_id = str(uuid.uuid4())
            self._session_started_at = datetime.now(timezone.utc)
            self._seq = 0
            logger.info(
                "call_recorder[%s]: session rolled over %s → %s",
                self._server_name, old, self._session_id,
            )
        self._last_call_at = now
        self._seq += 1
        self._total_calls += 1
        return self._session_id, self._seq
