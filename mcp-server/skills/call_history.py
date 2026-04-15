"""
call_history — query the MCP audit log.

Tools
-----
get_current_session     Session IDs + live stats for every running server.
list_sessions           Recent sessions ordered by last activity.
get_session_transcript  Full chronological call list for one session.
search_tool_calls       Filter by tool name, server, time window, outcome.
"""

from __future__ import annotations

import logging
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastmcp import FastMCP

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from db.connection import get_pool
from shared.call_recorder import get_registry

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _row_to_dict(row) -> dict[str, Any]:
    d = dict(row)
    for k, v in d.items():
        if isinstance(v, datetime):
            d[k] = v.isoformat()
    return d


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

def register(mcp: FastMCP) -> None:

    @mcp.tool()
    async def get_current_session() -> dict:
        """Return the active session ID and live call stats for every MCP
        server that is currently running in this process.

        Returns a dict keyed by server name, each containing:
          - session_id: current session UUID
          - session_started_at: ISO timestamp when this session began
          - calls_this_session: seq counter (calls since session started)
          - total_calls: all calls since server boot
        """
        registry = get_registry()
        result: dict[str, Any] = {}
        for name, rec in registry.items():
            result[name] = {
                "session_id": rec.session_id,
                "session_started_at": rec.session_started_at.isoformat(),
                "calls_this_session": rec.seq,
                "total_calls_since_boot": rec.total_calls,
            }
        if not result:
            result["note"] = (
                "No recorders registered yet — tool calls have not been made "
                "since the server started, or AuditMiddleware was not attached."
            )
        return result

    @mcp.tool()
    async def list_sessions(
        limit: int = 20,
        server_name: str | None = None,
    ) -> list[dict]:
        """List recent sessions ordered by last activity (newest first).

        Each entry contains:
          - session_id
          - server_name
          - first_call_at / last_call_at
          - call_count
          - tools_used: sorted list of distinct tool names
          - error_count
        """
        pool = await get_pool()
        where = "WHERE server_name = $2" if server_name else ""
        args: list[Any] = [limit]
        if server_name:
            args.append(server_name)

        sql = f"""
            SELECT
                session_id,
                server_name,
                min(called_at)                          AS first_call_at,
                max(called_at)                          AS last_call_at,
                count(*)                                AS call_count,
                array_agg(DISTINCT tool_name ORDER BY tool_name)
                                                        AS tools_used,
                count(*) FILTER (WHERE outcome = 'error') AS error_count
            FROM mcp_call_log
            {where}
            GROUP BY session_id, server_name
            ORDER BY max(called_at) DESC
            LIMIT $1
        """
        async with pool.acquire() as conn:
            rows = await conn.fetch(sql, *args)

        result = []
        for row in rows:
            d = _row_to_dict(row)
            d["tools_used"] = list(d.get("tools_used") or [])
            result.append(d)
        return result

    @mcp.tool()
    async def get_session_transcript(
        session_id: str | None = None,
        server_name: str | None = None,
        include_full_output: bool = False,
    ) -> dict:
        """Return the complete call transcript for a session.

        Parameters
        ----------
        session_id:
            UUID of the session to retrieve. If omitted, the most recent
            session on this server (or the one specified by *server_name*)
            is used.
        server_name:
            Restrict to a single server when looking up the latest session.
        include_full_output:
            If True, ``output_text`` is included in each call entry.
            If False, only the first 300 chars are returned to keep the
            response compact.

        Returns
        -------
        {
          "session_id": "...",
          "server_name": "...",
          "first_call_at": "...",
          "last_call_at": "...",
          "total_calls": N,
          "calls": [
            {
              "seq": 1,
              "tool_name": "...",
              "server_name": "...",
              "called_at": "...",
              "duration_ms": 45,
              "outcome": "success",
              "input_params": {...},
              "output_preview": "...",
              "output_data": {...},
              "error_message": null
            },
            ...
          ]
        }
        """
        pool = await get_pool()
        async with pool.acquire() as conn:
            # Resolve session_id if not provided
            if not session_id:
                where = "WHERE server_name = $1" if server_name else ""
                args: list[Any] = []
                if server_name:
                    args.append(server_name)
                row = await conn.fetchrow(
                    f"""
                    SELECT session_id, server_name
                    FROM mcp_call_log
                    {where}
                    ORDER BY called_at DESC
                    LIMIT 1
                    """,
                    *args,
                )
                if not row:
                    return {"error": "No calls recorded yet."}
                session_id = row["session_id"]
                resolved_server = row["server_name"]
            else:
                resolved_server = server_name or "all"

            # Fetch all rows for this session
            rows = await conn.fetch(
                """
                SELECT
                    seq, tool_name, server_name, called_at,
                    duration_ms, outcome, input_params, output_text,
                    output_data, error_message
                FROM mcp_call_log
                WHERE session_id = $1
                ORDER BY called_at ASC, seq ASC
                """,
                session_id,
            )

        if not rows:
            return {"error": f"No rows found for session {session_id}"}

        calls = []
        for row in rows:
            entry: dict[str, Any] = {
                "seq": row["seq"],
                "tool_name": row["tool_name"],
                "server_name": row["server_name"],
                "called_at": row["called_at"].isoformat() if row["called_at"] else None,
                "duration_ms": row["duration_ms"],
                "outcome": row["outcome"],
                "input_params": row["input_params"],
                "output_data": row["output_data"],
                "error_message": row["error_message"],
            }
            raw_text = row["output_text"] or ""
            if include_full_output:
                entry["output_text"] = raw_text
            else:
                entry["output_preview"] = (
                    raw_text[:300] + ("…" if len(raw_text) > 300 else "")
                )
            calls.append(entry)

        first_at = rows[0]["called_at"]
        last_at = rows[-1]["called_at"]
        return {
            "session_id": session_id,
            "server_name": resolved_server,
            "first_call_at": first_at.isoformat() if first_at else None,
            "last_call_at": last_at.isoformat() if last_at else None,
            "total_calls": len(calls),
            "calls": calls,
        }

    @mcp.tool()
    async def search_tool_calls(
        tool_name: str | None = None,
        server_name: str | None = None,
        session_id: str | None = None,
        outcome: str | None = None,
        from_minutes_ago: int = 60,
        limit: int = 50,
    ) -> list[dict]:
        """Search the call log with flexible filters.

        Parameters
        ----------
        tool_name:
            Substring match on tool name (case-insensitive).
        server_name:
            Exact match on server name (``clinical``, ``skills``, ``ingestion``).
        session_id:
            Exact match on session UUID.
        outcome:
            ``"success"`` or ``"error"``.
        from_minutes_ago:
            Look back this many minutes from now (default 60).
        limit:
            Maximum rows to return (default 50, max 500).

        Returns a list of call records, newest first.
        Each record has: id, seq, session_id, server_name, tool_name,
        called_at, duration_ms, outcome, input_params, output_preview,
        output_data, error_message.
        """
        limit = min(limit, 500)
        pool = await get_pool()

        conditions = ["called_at >= now() - ($1 * INTERVAL '1 minute')"]
        args: list[Any] = [from_minutes_ago]

        if tool_name:
            args.append(f"%{tool_name.lower()}%")
            conditions.append(f"lower(tool_name) LIKE ${len(args)}")
        if server_name:
            args.append(server_name)
            conditions.append(f"server_name = ${len(args)}")
        if session_id:
            args.append(session_id)
            conditions.append(f"session_id = ${len(args)}")
        if outcome:
            args.append(outcome)
            conditions.append(f"outcome = ${len(args)}")

        args.append(limit)
        where_clause = " AND ".join(conditions)

        sql = f"""
            SELECT
                id, seq, session_id, server_name, tool_name, called_at,
                duration_ms, outcome, input_params,
                left(output_text, 300)  AS output_preview,
                output_data, error_message
            FROM mcp_call_log
            WHERE {where_clause}
            ORDER BY called_at DESC
            LIMIT ${len(args)}
        """
        async with pool.acquire() as conn:
            rows = await conn.fetch(sql, *args)

        return [_row_to_dict(row) for row in rows]
