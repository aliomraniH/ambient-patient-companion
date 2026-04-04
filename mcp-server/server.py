"""FastMCP server entry point for the Ambient Patient Companion."""

import logging
import sys

logging.basicConfig(level=logging.INFO, stream=sys.stderr)
logger = logging.getLogger(__name__)

from fastmcp import FastMCP
from skills import load_skills

mcp = FastMCP("PatientCompanion")

# Auto-discover and register all skill tools
load_skills(mcp)

if __name__ == "__main__":
    mcp.run(transport="stdio")
