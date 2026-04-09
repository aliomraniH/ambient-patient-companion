"""FastMCP server entry point for the Ambient Patient Companion."""

import logging
import sys

logging.basicConfig(level=logging.INFO, stream=sys.stderr)
logger = logging.getLogger(__name__)

from fastmcp import FastMCP
from skills import load_skills
from starlette.requests import Request
from starlette.responses import JSONResponse

mcp = FastMCP("ambient-skills-companion")


@mcp.custom_route("/health", methods=["GET"])
async def rest_health(request: Request) -> JSONResponse:
    return JSONResponse({"ok": True, "server": "ambient-skills-companion", "version": "1.0.0"})

# Auto-discover and register all skill tools
load_skills(mcp)

if __name__ == "__main__":
    import os
    transport = os.environ.get("MCP_TRANSPORT", "stdio")
    host = os.environ.get("MCP_HOST", "0.0.0.0")
    port = int(os.environ.get("MCP_PORT", "8002"))
    if transport == "streamable-http":
        mcp.run(transport="streamable-http", host=host, port=port)
    else:
        mcp.run(transport="stdio")
