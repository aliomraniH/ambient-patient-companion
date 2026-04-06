"""Ambient Patient Companion — Dev Config Dashboard (Replit-aware).

Priority order for reading values:
  1. os.environ  (Replit Secrets — set via Settings > Secrets)
  2. local .env  (dashboard-local fallback for dev)

Three key categories:
  AUTO        — pre-filled from Replit env or sensible defaults, no user action needed
  SELF_HOSTED — point at services you run yourself; suggestions included
  THIRD_PARTY — must be obtained from an external provider
"""

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, Response
from dotenv import dotenv_values, set_key
import httpx
import os
import time
import json
from pathlib import Path

# ---------------------------------------------------------------------------
# Key catalogue
# ---------------------------------------------------------------------------

ENV_FILE = Path(__file__).parent / ".env"
ENV_FILE.touch(exist_ok=True)

# Categories: AUTO | SELF_HOSTED | THIRD_PARTY
KEY_META = {
    # ── Auto-configured ────────────────────────────────────────────────────
    "DATABASE_URL": {
        "category":    "AUTO",
        "label":       "PostgreSQL Database URL",
        "description": "Replit provisions this automatically — already set.",
        "secret":      True,
        "default":     "",          # comes from Replit env at runtime
        "help_url":    None,
    },
    "CLAUDE_MODEL": {
        "category":    "AUTO",
        "label":       "Claude Model ID",
        "description": "Which Anthropic model to use. Default is fine for dev.",
        "secret":      False,
        "default":     "claude-sonnet-4-5",
        "help_url":    "https://docs.anthropic.com/en/docs/models-overview",
    },
    "CLAUDE_THINKING_MODE": {
        "category":    "AUTO",
        "label":       "Extended Thinking",
        "description": "Enable Claude's extended thinking. Use 'enabled' or 'disabled'.",
        "secret":      False,
        "default":     "disabled",
        "help_url":    None,
    },
    "MCP_SYNTHETIC_PATIENT_URL": {
        "category":    "AUTO",
        "label":       "MCP · Synthetic Patient",
        "description": "Local FastMCP server — runs in this Repl.",
        "secret":      False,
        "default":     "http://localhost:9001/mcp",
        "help_url":    None,
    },
    "MCP_EHR_INTEGRATION_URL": {
        "category":    "AUTO",
        "label":       "MCP · EHR Integration",
        "description": "Local FastMCP server — runs in this Repl.",
        "secret":      False,
        "default":     "http://localhost:9002/mcp",
        "help_url":    None,
    },
    "MCP_CARE_GAP_ANALYZER_URL": {
        "category":    "AUTO",
        "label":       "MCP · Care Gap Analyzer",
        "description": "Local FastMCP server — runs in this Repl.",
        "secret":      False,
        "default":     "http://localhost:9003/mcp",
        "help_url":    None,
    },
    "MCP_LAB_PROCESSOR_URL": {
        "category":    "AUTO",
        "label":       "MCP · Lab Processor",
        "description": "Local FastMCP server — runs in this Repl.",
        "secret":      False,
        "default":     "http://localhost:9004/mcp",
        "help_url":    None,
    },
    "MCP_LANGSMITH_FEEDBACK_URL": {
        "category":    "AUTO",
        "label":       "MCP · LangSmith Feedback",
        "description": "Local FastMCP server — runs in this Repl.",
        "secret":      False,
        "default":     "http://localhost:9005/mcp",
        "help_url":    None,
    },
    "MCP_CLINICAL_INTELLIGENCE_URL": {
        "category":    "AUTO",
        "label":       "MCP · Clinical Intelligence",
        "description": "FastMCP clinical decision support server (Phase 1 + Deliberation) — guardrails, guidelines, drug interactions, dual-LLM deliberation.",
        "secret":      False,
        "default":     "http://localhost:8001/mcp",
        "help_url":    None,
    },
    "DELIBERATION_ENABLED": {
        "category":    "AUTO",
        "label":       "Deliberation Engine Enabled",
        "description": "Enable the Dual-LLM Deliberation Engine (Claude + GPT-4). Requires OPENAI_API_KEY.",
        "secret":      False,
        "default":     "true",
        "help_url":    None,
    },
    "DELIBERATION_MAX_ROUNDS": {
        "category":    "AUTO",
        "label":       "Deliberation Max Critique Rounds",
        "description": "Maximum cross-critique rounds before forced synthesis. Default 3.",
        "secret":      False,
        "default":     "3",
        "help_url":    None,
    },
    "DELIBERATION_CONVERGENCE_THRESHOLD": {
        "category":    "AUTO",
        "label":       "Deliberation Convergence Threshold",
        "description": "Similarity score (0.0–1.0) at which Claude + GPT-4 are considered converged. Default 0.90.",
        "secret":      False,
        "default":     "0.90",
        "help_url":    None,
    },

    # ── Self-hosted ────────────────────────────────────────────────────────
    "VECTOR_STORE_URL": {
        "category":    "SELF_HOSTED",
        "label":       "Vector Store URL",
        "description": "Use pgvector (already in your Replit PostgreSQL!) or run Chroma locally.",
        "secret":      False,
        "default":     "",
        "suggestion":  "pgvector — use your existing DATABASE_URL + pgvector extension",
        "setup_cmd":   "pip install pgvector && psql $DATABASE_URL -c 'CREATE EXTENSION IF NOT EXISTS vector;'",
        "alt":         "OR run Chroma locally:  pip install chromadb && chroma run --port 9010",
        "help_url":    "https://github.com/pgvector/pgvector",
    },
    "VECTOR_STORE_API_KEY": {
        "category":    "SELF_HOSTED",
        "label":       "Vector Store API Key",
        "description": "Not required for pgvector or local Chroma (leave blank).",
        "secret":      True,
        "default":     "",
        "suggestion":  "Leave empty if using pgvector or local Chroma.",
        "help_url":    None,
    },
    "VECTOR_STORE_INDEX": {
        "category":    "SELF_HOSTED",
        "label":       "Vector Store Index / Collection",
        "description": "Table or collection name for embeddings.",
        "secret":      False,
        "default":     "patient_embeddings",
        "suggestion":  "patient_embeddings",
        "help_url":    None,
    },
    "FHIR_BASE_URL": {
        "category":    "SELF_HOSTED",
        "label":       "FHIR Server Base URL",
        "description": "Run HAPI FHIR locally — a free, open-source R4 server. No account needed.",
        "secret":      False,
        "default":     "http://localhost:9090/fhir",
        "suggestion":  "Run HAPI FHIR with Docker (free, no signup):",
        "setup_cmd":   "docker run -p 9090:8080 hapiproject/hapi:latest",
        "alt":         "Or use Synthea output directly — already configured in this project.",
        "help_url":    "https://hapifhir.io/hapi-fhir/docs/server_jpa/get_started.html",
    },
    "FHIR_CLIENT_ID": {
        "category":    "SELF_HOSTED",
        "label":       "FHIR Client ID",
        "description": "Not required for local HAPI FHIR — leave blank for dev.",
        "secret":      False,
        "default":     "",
        "suggestion":  "Leave empty for local HAPI FHIR.",
        "help_url":    None,
    },
    "FHIR_CLIENT_SECRET": {
        "category":    "SELF_HOSTED",
        "label":       "FHIR Client Secret",
        "description": "Not required for local HAPI FHIR — leave blank for dev.",
        "secret":      True,
        "default":     "",
        "suggestion":  "Leave empty for local HAPI FHIR.",
        "help_url":    None,
    },
    "LANGSMITH_PROJECT": {
        "category":    "SELF_HOSTED",
        "label":       "LangSmith Project Name",
        "description": "Just a label — no auth required. Only matters if you have a LangSmith API key.",
        "secret":      False,
        "default":     "ambient-patient-companion",
        "suggestion":  "ambient-patient-companion",
        "help_url":    None,
    },

    # ── Third-party tokens ─────────────────────────────────────────────────
    "OPENAI_API_KEY": {
        "category":    "THIRD_PARTY",
        "label":       "OpenAI API Key",
        "description": "Required for GPT-4 in the Dual-LLM Deliberation Engine. Claude and GPT-4 cross-critique each other's clinical analysis.",
        "secret":      True,
        "default":     "",
        "provider":    "OpenAI",
        "get_url":     "https://platform.openai.com/api-keys",
        "get_steps":   "Sign up → API Keys → Create new secret key",
        "free_tier":   False,
        "help_url":    "https://platform.openai.com/api-keys",
    },
    "ANTHROPIC_API_KEY": {
        "category":    "THIRD_PARTY",
        "label":       "Anthropic API Key",
        "description": "Required to call Claude. Get a free key from Anthropic Console.",
        "secret":      True,
        "default":     "",
        "provider":    "Anthropic",
        "get_url":     "https://console.anthropic.com/account/keys",
        "get_steps":   "Sign up → Settings → API Keys → Create Key",
        "free_tier":   True,
        "help_url":    "https://console.anthropic.com/account/keys",
    },
    "LANGSMITH_API_KEY": {
        "category":    "THIRD_PARTY",
        "label":       "LangSmith API Key",
        "description": "Optional — enables tracing and observability. Free tier available.",
        "secret":      True,
        "default":     "",
        "provider":    "LangChain / LangSmith",
        "get_url":     "https://smith.langchain.com/settings",
        "get_steps":   "Sign up (free) → Settings → Create API Key",
        "free_tier":   True,
        "optional":    True,
        "help_url":    "https://smith.langchain.com/settings",
    },
}

ALL_KEYS = list(KEY_META.keys())
SECRET_KEYS = {k for k, m in KEY_META.items() if m["secret"]}

SERVER_MAP = {
    "synthetic_patient":      "MCP_SYNTHETIC_PATIENT_URL",
    "ehr_integration":        "MCP_EHR_INTEGRATION_URL",
    "care_gap_analyzer":      "MCP_CARE_GAP_ANALYZER_URL",
    "lab_processor":          "MCP_LAB_PROCESSOR_URL",
    "langsmith_feedback":     "MCP_LANGSMITH_FEEDBACK_URL",
    "clinical_intelligence":  "MCP_CLINICAL_INTELLIGENCE_URL",
}

MCP_DISPLAY_NAMES = {
    "synthetic_patient":      "synthetic-patient",
    "ehr_integration":        "ehr-integration",
    "care_gap_analyzer":      "care-gap-analyzer",
    "lab_processor":          "lab-processor",
    "langsmith_feedback":     "langsmith-feedback",
    "clinical_intelligence":  "clinical-intelligence",
}

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def read_env() -> dict[str, str]:
    """Merge: os.environ (Replit Secrets) wins over local .env, then defaults."""
    local = dotenv_values(ENV_FILE)
    merged = {}
    for k in ALL_KEYS:
        if os.environ.get(k):
            merged[k] = os.environ[k]
        elif local.get(k):
            merged[k] = local[k]
        else:
            merged[k] = KEY_META[k]["default"]
    return merged


def _explicitly_set(key: str) -> str:
    """Return the value if set via Replit Secret or local .env; else empty string."""
    local = dotenv_values(ENV_FILE)
    return os.environ.get(key) or local.get(key, "")


def value_source(key: str, env: dict) -> str:
    """Return where the value came from: replit_secret | local_env | default | unset."""
    local = dotenv_values(ENV_FILE)
    if os.environ.get(key):
        return "replit_secret"
    if local.get(key):
        return "local_env"
    if KEY_META[key]["default"]:
        return "default"
    return "unset"


def mask(key: str, value: str) -> str:
    if key in SECRET_KEYS and value:
        return "••••••••"
    return value


def completeness() -> dict:
    """Count only keys explicitly set by the user (Replit Secret or local .env)."""
    required = [k for k in ALL_KEYS if not KEY_META[k].get("optional")]
    set_count = sum(1 for k in required if _explicitly_set(k))
    total = len(required)
    return {
        "set": set_count,
        "total": total,
        "pct": int(set_count / total * 100) if total else 0,
    }


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

app = FastAPI(title="Ambient Companion — Dev Setup Dashboard")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)

INDEX_HTML = Path(__file__).parent / "index.html"


@app.get("/", response_class=HTMLResponse)
async def serve_index():
    return INDEX_HTML.read_text()


# ---- Config read ---------------------------------------------------------

@app.get("/api/config")
async def get_config():
    env = read_env()
    keys = {}
    for k in ALL_KEYS:
        meta = KEY_META[k]
        source = value_source(k, env)
        xval = _explicitly_set(k)
        keys[k] = {
            "value":       mask(k, xval),
            "raw_value":   xval if not meta["secret"] else "",
            "set":         bool(xval),
            "source":      source,
            "category":    meta["category"],
            "label":       meta["label"],
            "description": meta["description"],
            "secret":      meta["secret"],
            "default":     meta.get("default", ""),
            "optional":    meta.get("optional", False),
            "suggestion":  meta.get("suggestion", ""),
            "setup_cmd":   meta.get("setup_cmd", ""),
            "alt":         meta.get("alt", ""),
            "provider":    meta.get("provider", ""),
            "get_url":     meta.get("get_url", ""),
            "get_steps":   meta.get("get_steps", ""),
            "free_tier":   meta.get("free_tier", False),
            "help_url":    meta.get("help_url", ""),
        }
    return {"keys": keys, "completeness": completeness()}


@app.get("/api/reveal/{key}")
async def reveal_key(key: str):
    if key not in ALL_KEYS:
        raise HTTPException(404, f"Unknown key: {key}")
    env = read_env()
    return {
        "key":    key,
        "value":  _explicitly_set(key) if key not in SECRET_KEYS else env.get(key, ""),
        "source": value_source(key, env),
    }


# ---- Config write --------------------------------------------------------

@app.post("/api/config")
async def save_config(body: dict):
    """Save keys to local .env. Secrets should ALSO be added to Replit Secrets."""
    saved = []
    for k, v in body.items():
        if k in ALL_KEYS and v is not None:
            # Write to local .env
            set_key(str(ENV_FILE), k, str(v))
            # Also update current process environment
            os.environ[k] = str(v)
            saved.append(k)
    env = read_env()
    return {
        "saved":        saved,
        "completeness": completeness(),
        "note":         "Saved to local .env. For secrets, also add them to Replit Secrets (Settings → Secrets) so they persist across restarts.",
    }


@app.post("/api/apply-defaults")
async def apply_defaults():
    """Write all AUTO defaults to .env if not already set."""
    applied = []
    for k, meta in KEY_META.items():
        if meta["category"] == "AUTO" and meta.get("default"):
            env = dotenv_values(ENV_FILE)
            if not env.get(k) and not os.environ.get(k):
                set_key(str(ENV_FILE), k, meta["default"])
                os.environ[k] = meta["default"]
                applied.append(k)
    return {"applied": applied}


# ---- Connectivity tests --------------------------------------------------

@app.post("/api/test/anthropic")
async def test_anthropic():
    env = read_env()
    api_key = _explicitly_set("ANTHROPIC_API_KEY")
    if not api_key:
        return {"ok": False, "error": "No ANTHROPIC_API_KEY set — add it above"}
    model = env.get("CLAUDE_MODEL", "claude-sonnet-4-5")
    try:
        async with httpx.AsyncClient() as client:
            t0 = time.monotonic()
            r = await client.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key":          api_key,
                    "anthropic-version":  "2023-06-01",
                    "content-type":       "application/json",
                },
                json={"model": model, "max_tokens": 5,
                      "messages": [{"role": "user", "content": "ping"}]},
                timeout=15,
            )
            ms = int((time.monotonic() - t0) * 1000)
            if r.status_code == 200:
                return {"ok": True, "model": model, "latency_ms": ms}
            return {"ok": False, "error": f"HTTP {r.status_code}: {r.text[:200]}"}
    except Exception as e:
        return {"ok": False, "error": str(e)[:200]}


@app.post("/api/test/langsmith")
async def test_langsmith():
    env = read_env()
    api_key = _explicitly_set("LANGSMITH_API_KEY")
    if not api_key:
        return {"ok": False, "error": "No LANGSMITH_API_KEY set — optional, skip if not using tracing"}
    try:
        async with httpx.AsyncClient() as client:
            t0 = time.monotonic()
            r = await client.get(
                "https://api.smith.langchain.com/info",
                headers={"x-api-key": api_key},
                timeout=10,
            )
            ms = int((time.monotonic() - t0) * 1000)
            if r.status_code == 200:
                return {"ok": True, "latency_ms": ms}
            return {"ok": False, "error": f"HTTP {r.status_code}"}
    except Exception as e:
        return {"ok": False, "error": str(e)[:200]}


@app.post("/api/test/database")
async def test_database():
    db_url = os.environ.get("DATABASE_URL", "")
    if not db_url:
        return {"ok": False, "error": "DATABASE_URL not set — should be auto-configured by Replit"}
    try:
        import asyncpg
        conn = await asyncpg.connect(db_url)
        result = await conn.fetchval("SELECT COUNT(*) FROM patients")
        await conn.close()
        return {"ok": True, "patients": int(result), "source": "Replit PostgreSQL"}
    except Exception as e:
        return {"ok": False, "error": str(e)[:200]}


@app.post("/api/test/mcp/{server_id}")
async def test_mcp(server_id: str):
    if server_id not in SERVER_MAP:
        raise HTTPException(404, f"Unknown server: {server_id}")
    env = read_env()
    url = _explicitly_set(SERVER_MAP[server_id])
    if not url:
        return {"ok": False, "server": server_id, "error": "No URL configured"}
    base = url.rstrip("/").removesuffix("/mcp")
    try:
        async with httpx.AsyncClient() as client:
            t0 = time.monotonic()
            r = await client.get(f"{base}/health", timeout=4)
            ms = int((time.monotonic() - t0) * 1000)
            if r.status_code == 200:
                return {"ok": True, "server": server_id, "latency_ms": ms}
            return {"ok": False, "server": server_id, "error": f"HTTP {r.status_code}"}
    except Exception as e:
        return {"ok": False, "server": server_id, "error": str(e)[:200]}


# ---- URL helpers ----------------------------------------------------------

def _dev_domain() -> str:
    """The live Replit dev domain (janeway.replit.dev)."""
    return os.environ.get("REPLIT_DEV_DOMAIN", "")


def _prod_domain() -> str:
    """The deployed production domain (.replit.app), if available.

    After deployment REPLIT_DOMAINS contains the .replit.app domain.
    Falls back to empty string so callers can detect 'not deployed yet'.
    """
    domains = os.environ.get("REPLIT_DOMAINS", "")
    for d in domains.split(","):
        d = d.strip()
        if d.endswith(".replit.app"):
            return d
    return ""


def _mcp_url(domain: str) -> str:
    """Public MCP URL — served via the Next.js reverse proxy at /mcp (no port required)."""
    return f"https://{domain}/mcp" if domain else ""


def _build_mcp_config(domain: str) -> dict:
    """Build the mcpServers block for a given domain.

    Uses streamable-http — compatible with Claude web (claude.ai),
    Claude Desktop, and the Claude Code CLI.
    The MCP server is proxied through Next.js at /mcp (standard HTTPS, no port).
    """
    servers: dict = {}
    if domain:
        servers["ambient-clinical-intelligence"] = {
            "type": "streamable-http",
            "url": _mcp_url(domain),
        }
    for sid, env_key in SERVER_MAP.items():
        if sid == "clinical_intelligence":
            continue
        url = _explicitly_set(env_key)
        if url:
            servers[MCP_DISPLAY_NAMES[sid]] = {
                "type": "streamable-http",
                "url": url,
            }
    return {"mcpServers": servers}


# ---- Claude config generation ---------------------------------------------

@app.get("/api/generate/claude-config")
async def generate_claude_config():
    """Claude Desktop / Claude Code config (npx mcp-remote format).

    Backward-compatible with existing tests and tooling.
    """
    env = read_env()
    mcp_servers, cli_commands = {}, []
    for sid, env_key in SERVER_MAP.items():
        url = _explicitly_set(env_key)
        if url:
            display = MCP_DISPLAY_NAMES[sid]
            mcp_servers[display] = {"command": "npx", "args": ["mcp-remote", url]}
            cli_commands.append(
                f"claude mcp add --transport streamable-http {display} {url}"
            )
    return {
        "config":               {"mcpServers": mcp_servers},
        "claude_code_commands": cli_commands,
        "servers_configured":   len(mcp_servers),
        "servers_total":        len(SERVER_MAP),
    }


@app.get("/api/generate/mcp-config")
async def generate_mcp_config(env: str = Query("dev")):
    """Return a downloadable Claude web MCP config JSON.

    ?env=dev  → uses the live REPLIT_DEV_DOMAIN (always available)
    ?env=prod → uses the deployed .replit.app domain (available after deployment)
    """
    domain = _dev_domain() if env != "prod" else _prod_domain()
    if not domain:
        raise HTTPException(
            status_code=404,
            detail=(
                "Production domain not found. "
                "Deploy the app first — the domain will be set automatically."
            ),
        )

    config = _build_mcp_config(domain)
    filename = f"claude_mcp_config_{env}.json"
    return Response(
        content=json.dumps(config, indent=2),
        media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


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
                lines.append(f"{k}={mask(k, val)}")
            else:
                lines.append(f"# {k}=  # not set")
    content = "\n".join(lines) + "\n"
    if download:
        return Response(content, media_type="text/plain",
                        headers={"Content-Disposition": "attachment; filename=.env"})
    return Response(content, media_type="text/plain")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("server:app", host="0.0.0.0", port=8080, reload=True)
