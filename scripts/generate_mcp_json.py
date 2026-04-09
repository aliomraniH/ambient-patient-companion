"""Regenerate .mcp.json using the current $REPLIT_DEV_DOMAIN.

Run at startup (from start.sh) so the MCP discovery file always points to the
correct public HTTPS URL, whether in dev or production.

Usage:
    python scripts/generate_mcp_json.py
"""

import json
import os
import pathlib

ROOT = pathlib.Path(__file__).parent.parent
OUTPUT = ROOT / ".mcp.json"

domain = os.environ.get("REPLIT_DEV_DOMAIN", "").strip()

if not domain:
    print("[generate_mcp_json] WARNING: REPLIT_DEV_DOMAIN not set — leaving .mcp.json unchanged")
    raise SystemExit(0)

base = f"https://{domain}"

config = {
    "mcpServers": {
        "ambient-clinical-intelligence": {
            "url": f"{base}/mcp",
            "transport": "streamable-http",
            "description": "ambient-clinical-intelligence — Phase 1+2 clinical decision support, deliberation engine, 19 tools",
        },
        "ambient-skills-companion": {
            "url": f"{base}/mcp-skills",
            "transport": "streamable-http",
            "description": "ambient-skills-companion — skills server (OBT score, SDOH, nudges, pre-visit brief, vitals), 17 tools",
        },
        "ambient-ingestion": {
            "url": f"{base}/mcp-ingestion",
            "transport": "streamable-http",
            "description": "ambient-ingestion — HealthEx adaptive ingestion pipeline, 1 tool",
        },
    }
}

OUTPUT.write_text(json.dumps(config, indent=2) + "\n")
print(f"[generate_mcp_json] Written .mcp.json with base URL: {base}")
