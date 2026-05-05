"""FastMCP server entry point for the Ambient Patient Companion."""

import logging
import sys
from pathlib import Path

logging.basicConfig(level=logging.INFO, stream=sys.stderr)
logger = logging.getLogger(__name__)

# Make shared/ importable — mcp-server/ is one level below the repo root.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from fastmcp import FastMCP
from skills import load_skills
from starlette.requests import Request
from starlette.responses import JSONResponse

from db.connection import get_pool
from shared.audit_middleware import AuditMiddleware
from runtime.agent_runtime import get_runtime
from runtime.watchers import register_watchers

# ── Agent runtime — autonomous background watchers ────────────────────────────
# Uses the module-level singleton so any skill module that exports
# register_watchers(runtime) can declare its own background tasks — the same
# instance is passed to load_skills() below.
#
# Two built-in watchers start via register_watchers() from runtime/watchers.py:
#   • crisis_scan_watcher   (every 60 min) — crisis escalation for recent patients
#   • care_gap_watcher      (every 24 h)   — flag overdue open care gaps
#
# One skill-owned watcher is registered by skills/behavioral_atoms.py:
#   • checkin_atom_watcher  (every 5 min)  — atom extraction for new check-ins
runtime = get_runtime()
register_watchers(runtime)

mcp = FastMCP(
    "ambient-skills-companion",
    instructions=(
        "Ambient Skills Companion — specialized clinical skills and knowledge tools "
        "including motivational interviewing, health literacy assessment, care gap "
        "analysis, OBT scoring, patient education, call history auditing, and "
        "multi-domain clinical reasoning. Use these tools to enhance patient "
        "engagement and apply evidence-based clinical skills."
    ),
    lifespan=runtime.lifespan,
)


@mcp.custom_route("/health", methods=["GET"])
async def rest_health(request: Request) -> JSONResponse:
    return JSONResponse({"ok": True, "server": "ambient-skills-companion", "version": "1.0.0"})


@mcp.custom_route("/api/agent-runtime/status", methods=["GET"])
async def agent_runtime_status(request: Request) -> JSONResponse:
    """Return health snapshot for all registered autonomous watchers.

    Shape::

        {
          "watcher_count": 3,
          "watchers": [
            {
              "name": "checkin_atom_watcher",
              "interval_seconds": 300,
              "run_count": 12,
              "last_run": "2025-05-05T14:30:00+00:00",
              "last_error": null,
              "healthy": true
            },
            ...
          ]
        }
    """
    return JSONResponse(runtime.status())


# Auto-discover and register all skill tools.
# Passing runtime lets any skill that exports register_watchers(runtime)
# declare its own background watchers without editing watchers.py.
load_skills(mcp, runtime=runtime)

# Audit every tool call — records inputs, outputs, timing and session to mcp_call_log
mcp.add_middleware(AuditMiddleware("skills", get_pool))

if __name__ == "__main__":
    import os
    transport = os.environ.get("MCP_TRANSPORT", "stdio")
    host = os.environ.get("MCP_HOST", "0.0.0.0")
    port = int(os.environ.get("MCP_PORT", "8002"))
    if transport == "streamable-http":
        mcp.run(transport="streamable-http", host=host, port=port, json_response=True, stateless=True)
    else:
        mcp.run(transport="stdio")
