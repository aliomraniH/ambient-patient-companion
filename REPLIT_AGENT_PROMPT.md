Read CLAUDE.md and replit.md before doing anything. They are the authoritative source
of truth for this project.

---

# Ambient Patient Companion — Current State Guide

This document describes the current state of the codebase (as of 2026-04-15, commit `94d0fe6`)
for any agent or collaborator picking up the project.

---

## What Has Been Built

A production multi-agent AI health system:

- **S = f(R, C, P, T)** — Optimal clinical surface derived from Role × Context × Patient State × Time
- **35-table PostgreSQL warehouse** — Synthea + HealthEx FHIR data
- **3 FastMCP Python servers** with OAuth PKCE — Clinical (8001), Skills (8002), Ingestion (8003)
- **Next.js 16 frontend** (port 5000) — proxies all 3 MCP servers, serves OAuth discovery
- **Phase 2 Dual-LLM Deliberation Engine** — Claude Sonnet + GPT-4o (6-phase pipeline)
- **3-Layer Clinical Guardrail Pipeline** — input validation → escalation rules → output safety
- **5 Data Quality Validators (F1–F5)** — FHIR conformance, clinical plausibility, source anchoring
- **MCP Audit Log System** — every Claude tool call recorded to `mcp_call_log` (35th table)
- **Universal Provenance Gate** — `verify_output_provenance` on all 3 servers

---

## Running Services

Start everything:
```bash
bash start.sh
```

Or use the 5 Replit Workflows:

| Workflow | Command | Port |
|---------|---------|------|
| Start application | `cd replit-app && npm run dev` | 5000 |
| Config Dashboard | `cd replit_dashboard && python server.py` | 8080 |
| Clinical MCP Server | `MCP_TRANSPORT=streamable-http MCP_PORT=8001 python -m server.mcp_server` | 8001 |
| Skills MCP Server | `cd mcp-server && MCP_TRANSPORT=streamable-http MCP_PORT=8002 python server.py` | 8002 |
| Ingestion MCP Server | `MCP_TRANSPORT=streamable-http MCP_PORT=8003 python -m ingestion.server` | 8003 |

Health checks (after startup):
```bash
curl http://localhost:8001/health  # {"ok":true,"server":"ambient-clinical-intelligence"}
curl http://localhost:8002/health  # {"ok":true,"server":"ambient-skills-companion"}
curl http://localhost:8003/health  # {"ok":true,"server":"ambient-ingestion"}
```

---

## Adding Claude as MCP Client

Settings → Integrations → Add custom integration → paste public HTTPS URL:

| Server | URL |
|--------|-----|
| `ambient-clinical-intelligence` | `https://[your-replit-domain]/mcp` |
| `ambient-skills-companion` | `https://[your-replit-domain]/mcp-skills` |
| `ambient-ingestion` | `https://[your-replit-domain]/mcp-ingestion` |

OAuth PKCE handshake completes automatically — no login screen (public server).

---

## Tool Counts (current)

| Server | Tools | Key tools |
|--------|-------|-----------|
| `ambient-clinical-intelligence` | 23 | clinical_query, run_deliberation, verify_output_provenance + 20 more |
| `ambient-skills-companion` | 22+ | compute_obt_score, get_current_session, search_similar_atoms + 19 more |
| `ambient-ingestion` | 4 | trigger_ingestion, detect_context_staleness + 2 more |

---

## MCP Audit Log

Every tool call from Claude is automatically logged:

```
Table: mcp_call_log (35th table)
Columns: id, session_id, server_name, tool_name, called_at, duration_ms,
         input_params (JSONB), output_text, output_data (JSONB), outcome, error_message, seq
```

Query the audit log from Claude using 4 tools on the Skills server:
```
get_current_session()                            → live sessions + call counts
list_sessions(limit=10, server_name="clinical")  → recent sessions
get_session_transcript(session_id=None)          → full chronological call log
search_tool_calls(tool_name="run_deliberation")  → flexible filter
```

---

## Key Files Changed in Last Session

| File | Change |
|------|--------|
| `shared/coercion.py` | NEW — coerce_confidence(): normalises LLM confidence values |
| `shared/datetime_utils.py` | NEW — ensure_aware(): UTC tzinfo for naive DB datetimes |
| `shared/call_recorder.py` | NEW — CallRecorder + session tracking for audit log |
| `shared/audit_middleware.py` | NEW — AuditMiddleware(Middleware) FastMCP hook |
| `shared/tests/test_coerce_confidence.py` | NEW — 28 unit tests |
| `shared/tests/test_datetime_utils.py` | NEW — 6 unit tests |
| `mcp-server/skills/call_history.py` | NEW — 4 audit query MCP tools |
| `mcp-server/server.py` | MODIFIED — sys.path fix + AuditMiddleware wired |
| `server/mcp_server.py` | MODIFIED — AuditMiddleware("clinical") wired |
| `ingestion/server.py` | MODIFIED — AuditMiddleware("ingestion") wired |
| `replit.md` | UPDATED — reflects all current state |
| `README.md` | UPDATED — reflects all current state |
| `CLAUDE.md` | UPDATED — reflects all current state |

---

## Bug Fixes Applied

1. **coerce_confidence**: `float > 1.0` now clamps to 1.0 (not ÷100); `int > 1` divides by 100
2. **source_freshness**: `register_healthex_patient` writes `last_ingested_at = NULL` (never `NOW()`)
3. **ensure_aware**: prevents `TypeError: can't subtract offset-naive and offset-aware datetimes`

---

## Running Tests

```bash
# All Python tests
python -m pytest tests/phase1/ -v                    # 196 Phase 1
python -m pytest tests/phase2/ -v                    # 95 Phase 2
python -m pytest server/deliberation/tests/ -v       # 290+ deliberation unit
python -m pytest ingestion/tests/ -v                 # 152 ingestion
python -m pytest shared/tests/ -v                   # 34 shared utilities
python -m pytest tests/e2e/ -v                       # 28 end-to-end
python -m pytest tests/test_mcp_discovery.py -v      # 26 discovery + OAuth
python -m pytest tests/test_mcp_smoke.py -v          # 24 smoke
cd mcp-server && python -m pytest tests/ -v          # 110 skills backend
cd replit_dashboard && python -m pytest tests/ -v    # 30 dashboard

# Frontend
cd replit-app && npm test                            # 37 Jest tests
```

---

## Constraints — Do Not Violate

- `FastMCP("name")` — no `description=` kwarg — causes startup crash
- Never use `print()` in `@mcp.tool` functions — log to `sys.stderr`
- Always call `coerce_confidence()` before writing float columns from LLM output
- Always call `ensure_aware()` before datetime arithmetic on DB-read TIMESTAMP columns
- `last_ingested_at` must be written as `NULL` on patient registration (never `NOW()`)
- `AuditMiddleware` must be added AFTER tool registration (`mcp.add_middleware(...)` at end of server module)
- Model names: `claude-sonnet-4-20250514`, `gpt-4o`, `claude-haiku-4-5-20251001`
- `pytest-asyncio==0.21.2` pinned — do NOT upgrade to 1.x
- Import shared utilities as `from shared.coercion import ...` (repo root is on sys.path in all 3 servers)
- Do NOT store real patient data — all data is synthetic (Maria Chen MRN 4829341 is the demo patient)

---

## GitHub

Repository: https://github.com/aliomraniH/ambient-patient-companion  
Last commit: `94d0fe6`
