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

mcp = FastMCP(
    "ambient-skills-companion",
    instructions="Ambient Skills Companion — specialized clinical skills and knowledge tools including motivational interviewing, health literacy assessment, care gap analysis, OBT scoring, patient education, call history auditing, and multi-domain clinical reasoning. Use these tools to enhance patient engagement and apply evidence-based clinical skills.",
)


@mcp.custom_route("/health", methods=["GET"])
async def rest_health(request: Request) -> JSONResponse:
    return JSONResponse({"ok": True, "server": "ambient-skills-companion", "version": "1.0.0"})

# Auto-discover and register all skill tools
load_skills(mcp)

# Audit every tool call — records inputs, outputs, timing and session to mcp_call_log
mcp.add_middleware(AuditMiddleware("skills", get_pool))

if __name__ == "__main__":
    import os
    transport = os.environ.get("MCP_TRANSPORT", "stdio")
    host = os.environ.get("MCP_HOST", "0.0.0.0")
    port = int(os.environ.get("MCP_PORT", "8002"))
    if transport == "streamable-http":
        mcp.run(transport="streamable-http", host=host, port=port, json_response=True)
    else:
        mcp.run(transport="stdio")
