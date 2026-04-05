"""Ambient Patient Companion — Config Dashboard Backend.

FastAPI server that manages API keys, MCP server URLs, and generates
Claude Desktop configuration files.
"""

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse, Response
from dotenv import dotenv_values, set_key
import httpx
import os
import time
import json
from pathlib import Path

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

ENV_FILE = Path(__file__).parent / ".env"
ENV_FILE.touch(exist_ok=True)

ALL_KEYS = [
    "ANTHROPIC_API_KEY", "CLAUDE_MODEL", "CLAUDE_THINKING_MODE",
    "LANGSMITH_API_KEY", "LANGSMITH_PROJECT",
    "VECTOR_STORE_URL", "VECTOR_STORE_API_KEY", "VECTOR_STORE_INDEX",
    "FHIR_BASE_URL", "FHIR_CLIENT_ID", "FHIR_CLIENT_SECRET",
    "DATABASE_URL",
    "MCP_SYNTHETIC_PATIENT_URL", "MCP_EHR_INTEGRATION_URL",
    "MCP_CARE_GAP_ANALYZER_URL", "MCP_LAB_PROCESSOR_URL",
    "MCP_LANGSMITH_FEEDBACK_URL",
]

SECRET_KEYS = {
    "ANTHROPIC_API_KEY", "LANGSMITH_API_KEY", "VECTOR_STORE_API_KEY",
    "FHIR_CLIENT_SECRET", "DATABASE_URL",
}

SERVER_MAP = {
    "synthetic_patient":  "MCP_SYNTHETIC_PATIENT_URL",
    "ehr_integration":    "MCP_EHR_INTEGRATION_URL",
    "care_gap_analyzer":  "MCP_CARE_GAP_ANALYZER_URL",
    "lab_processor":      "MCP_LAB_PROCESSOR_URL",
    "langsmith_feedback": "MCP_LANGSMITH_FEEDBACK_URL",
}

MCP_DISPLAY_NAMES = {
    "synthetic_patient":  "synthetic-patient",
    "ehr_integration":    "ehr-integration",
    "care_gap_analyzer":  "care-gap-analyzer",
    "lab_processor":      "lab-processor",
    "langsmith_feedback": "langsmith-feedback",
}

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def read_env() -> dict:
    return dotenv_values(ENV_FILE)


def mask(key: str, value: str) -> str:
    if key in SECRET_KEYS and value:
        return "\u2022\u2022\u2022\u2022\u2022\u2022\u2022\u2022"
    return value


def completeness(env: dict) -> dict:
    set_count = sum(1 for k in ALL_KEYS if env.get(k))
    total = len(ALL_KEYS)
    return {"set": set_count, "total": total, "pct": int(set_count / total * 100)}


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

app = FastAPI(title="Ambient Companion Config Dashboard")

# ---- Serve index.html ----------------------------------------------------

INDEX_HTML = Path(__file__).parent / "index.html"


@app.get("/", response_class=HTMLResponse)
async def serve_index():
    return INDEX_HTML.read_text()


# ---- Config CRUD ---------------------------------------------------------

@app.get("/api/config")
async def get_config():
    env = read_env()
    keys = {}
    for k in ALL_KEYS:
        val = env.get(k, "")
        keys[k] = {"value": mask(k, val), "set": bool(val)}
    return {"keys": keys, "completeness": completeness(env)}


@app.post("/api/config")
async def save_config(body: dict):
    saved = []
    for k, v in body.items():
        if k in ALL_KEYS:
            set_key(str(ENV_FILE), k, v)
            saved.append(k)
    env = read_env()
    return {"saved": saved, "completeness": completeness(env)}


@app.get("/api/reveal/{key}")
async def reveal_key(key: str):
    if key not in ALL_KEYS:
        raise HTTPException(404, f"Unknown key: {key}")
    env = read_env()
    return {"key": key, "value": env.get(key, "")}


# ---- Connectivity tests --------------------------------------------------

@app.post("/api/test/anthropic")
async def test_anthropic():
    env = read_env()
    api_key = env.get("ANTHROPIC_API_KEY")
    if not api_key:
        return {"ok": False, "error": "No ANTHROPIC_API_KEY set"}
    model = env.get("CLAUDE_MODEL", "claude-sonnet-4-6")
    try:
        async with httpx.AsyncClient() as client:
            t0 = time.monotonic()
            r = await client.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": api_key,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json={
                    "model": model,
                    "max_tokens": 5,
                    "messages": [{"role": "user", "content": "ping"}],
                },
                timeout=15,
            )
            latency = int((time.monotonic() - t0) * 1000)
            if r.status_code == 200:
                return {"ok": True, "model": model, "latency_ms": latency}
            return {"ok": False, "error": f"{r.status_code} {r.text[:200]}"}
    except Exception as e:
        return {"ok": False, "error": str(e)[:200]}


@app.post("/api/test/langsmith")
async def test_langsmith():
    env = read_env()
    api_key = env.get("LANGSMITH_API_KEY")
    if not api_key:
        return {"ok": False, "error": "No LANGSMITH_API_KEY set"}
    try:
        async with httpx.AsyncClient() as client:
            t0 = time.monotonic()
            r = await client.get(
                "https://api.smith.langchain.com/info",
                headers={"x-api-key": api_key},
                timeout=10,
            )
            latency = int((time.monotonic() - t0) * 1000)
            if r.status_code == 200:
                return {"ok": True, "latency_ms": latency}
            return {"ok": False, "error": f"{r.status_code}"}
    except Exception as e:
        return {"ok": False, "error": str(e)[:200]}


@app.post("/api/test/mcp/{server_id}")
async def test_mcp(server_id: str):
    if server_id not in SERVER_MAP:
        raise HTTPException(404, f"Unknown server: {server_id}")
    env = read_env()
    url = env.get(SERVER_MAP[server_id])
    if not url:
        return {"ok": False, "server": server_id, "error": "No URL configured"}
    # Strip trailing /mcp if present to hit base health endpoint
    base = url.rstrip("/")
    if base.endswith("/mcp"):
        base = base[:-4]
    try:
        async with httpx.AsyncClient() as client:
            t0 = time.monotonic()
            r = await client.get(f"{base}/health", timeout=4)
            latency = int((time.monotonic() - t0) * 1000)
            if r.status_code == 200:
                return {"ok": True, "server": server_id, "latency_ms": latency}
            return {"ok": False, "server": server_id, "error": f"{r.status_code}"}
    except Exception as e:
        return {"ok": False, "server": server_id, "error": str(e)[:200]}


# ---- Config generation ---------------------------------------------------

@app.get("/api/generate/claude-config")
async def generate_claude_config():
    env = read_env()
    mcp_servers = {}
    cli_commands = []
    configured = 0
    for sid, env_key in SERVER_MAP.items():
        url = env.get(env_key)
        if url:
            configured += 1
            display = MCP_DISPLAY_NAMES[sid]
            mcp_servers[display] = {
                "command": "npx",
                "args": ["mcp-remote", url],
            }
            cli_commands.append(
                f"claude mcp add --transport streamable-http {display} {url}"
            )
    return {
        "config": {"mcpServers": mcp_servers},
        "claude_code_commands": cli_commands,
        "servers_configured": configured,
        "servers_total": len(SERVER_MAP),
    }


# ---- .env export ---------------------------------------------------------

@app.get("/api/export/env")
async def export_env(download: bool = Query(False)):
    env = read_env()
    lines = []
    for k in ALL_KEYS:
        val = env.get(k, "")
        if download:
            lines.append(f"{k}={val}")
        else:
            if val:
                display = mask(k, val)
                lines.append(f"{k}={display}")
            else:
                lines.append(f"# {k}=  # not set")
    content = "\n".join(lines) + "\n"
    if download:
        return Response(
            content,
            media_type="text/plain",
            headers={"Content-Disposition": "attachment; filename=.env"},
        )
    return Response(content, media_type="text/plain")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("server:app", host="0.0.0.0", port=8080, reload=True)
