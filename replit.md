# Ambient Patient Companion

A multi-agent AI health system that generates a continuously derived patient health UX from Role × Context × Patient State × Time — `S = f(R, C, P, T)`.

## What This System Does

The Ambient Patient Companion connects Claude to a live clinical intelligence layer built for primary care and care management. It provides:

- **Real patient data** from a 34-table PostgreSQL warehouse (Synthea + HealthEx FHIR)
- **Dual-LLM Deliberation Engine** — Claude Sonnet + GPT-4o independently analyze clinical context, cross-critique across multiple rounds, then synthesize into 5 structured output categories
- **3-Layer Clinical Guardrail Pipeline** — input validation → escalation rules → output safety on every AI call
- **19 MCP tools** across 3 servers, all accessible to Claude via OAuth-authenticated HTTPS

---

## Architecture

```
S = f(R, C, P, T)  →  optimal clinical surface
```

```
Claude Web / API
      │ OAuth PKCE (auto-handled)
      ▼
Next.js 16 (port 5000)
  ├── Proxy: /mcp          → localhost:8001 (ambient-clinical-intelligence)
  ├── Proxy: /mcp-skills   → localhost:8002 (ambient-skills-companion)
  ├── Proxy: /mcp-ingestion→ localhost:8003 (ambient-ingestion)
  └── OAuth: /.well-known/oauth-protected-resource
           /.well-known/oauth-authorization-server
           /register  /authorize  /token
      │
      ├── MCP Server 1 — ambient-clinical-intelligence (port 8001)
      │   19 tools · 3-layer guardrails · Dual-LLM Deliberation · Flag Lifecycle
      │
      ├── MCP Server 2 — ambient-skills-companion (port 8002)
      │   18 tools · 10 skill modules · OBT Score · SDOH · pre-visit brief
      │
      └── MCP Server 3 — ambient-ingestion (port 8003)
          1 tool · HealthEx ETL pipeline (5 format parsers)
      │
      └── PostgreSQL Warehouse — 34 tables
          patients · biometrics · deliberations · flags · ingestion_plans · …
```

---

## Project Structure

```
ambient-patient-companion/
│
├── server/                      ← Server 1: ambient-clinical-intelligence (port 8001)
│   ├── mcp_server.py            ← FastMCP("ambient-clinical-intelligence") — 19 tools + REST wrappers
│   ├── guardrails/              ← input_validator · output_validator · clinical_rules
│   └── deliberation/            ← Dual-LLM Deliberation Engine
│       ├── engine.py            ← 5-phase orchestrator (+ Phase 0.5, 3.25, 3.5)
│       ├── planner.py           ← Phase 0.5: pre-deliberation agenda builder (Haiku)
│       ├── context_compiler.py  ← Phase 0: assemble patient EHR context
│       ├── tiered_context_loader.py  ← 3-tier budget-capped loading (11K limit)
│       ├── analyst.py           ← Phase 1: parallel Claude Sonnet + GPT-4o analysis
│       ├── critic.py            ← Phase 2: cross-critique rounds with convergence
│       ├── synthesizer.py       ← Phase 3: unified synthesis → DeliberationResult
│       ├── synthesis_reviewer.py← Phase 3.25: post-synthesis domain review (Haiku)
│       ├── output_safety.py     ← Phase 3.5: guardrail wrapper on deliberation output
│       ├── behavioral_adapter.py← Phase 4: SMS/push nudge formatting
│       ├── knowledge_store.py   ← Phase 5: atomic DB commit
│       ├── flag_reviewer.py     ← LLM-powered flag lifecycle review (Haiku)
│       ├── flag_writer.py       ← Flag registry writes with data provenance
│       ├── data_request_parser.py  ← Parse agent data requests between rounds
│       ├── json_utils.py        ← strip_markdown_fences() + safe_json_loads()
│       ├── schemas.py           ← 20+ Pydantic models
│       ├── prompts/             ← XML LLM prompt templates
│       ├── migrations/001–004   ← Deliberation + flag lifecycle tables
│       └── tests/               ← 109 deliberation unit tests
│
├── mcp-server/                  ← Server 2: ambient-skills-companion (port 8002)
│   ├── server.py                ← FastMCP("ambient-skills-companion") — auto-discovers skills
│   ├── skills/                  ← 10 skill modules (register(mcp) convention)
│   │   ├── compute_obt_score.py
│   │   ├── compute_provider_risk.py
│   │   ├── crisis_escalation.py
│   │   ├── food_access_nudge.py
│   │   ├── generate_checkins.py
│   │   ├── generate_patient.py
│   │   ├── generate_vitals.py
│   │   ├── ingestion_tools.py   ← 8 tools: freshness · ingestion · conflicts · data tracks
│   │   ├── previsit_brief.py
│   │   └── sdoh_assessment.py
│   ├── db/schema.sql            ← 22-table PostgreSQL base schema (source of truth)
│   ├── transforms/              ← FHIR-to-schema transformers (5 resource types)
│   ├── seed.py                  ← python mcp-server/seed.py --patients 10 --months 6
│   └── tests/                   ← 92 backend tests
│
├── ingestion/                   ← Server 3: ambient-ingestion (port 8003)
│   ├── server.py                ← FastMCP("ambient-ingestion") — trigger_ingestion tool
│   ├── pipeline.py              ← ETL orchestrator
│   ├── conflict_resolver.py     ← Multi-source conflict resolution
│   └── adapters/healthex/
│       ├── format_detector.py   ← detect_format() → 5 formats (A/B/C/D/JSON-dict)
│       ├── ingest.py            ← adaptive_parse() entry point
│       ├── planner.py           ← Two-phase ingest planner (ingestion_plans table)
│       ├── executor.py          ← Phase 2 worker + TracedWriter audit trail
│       ├── content_router.py    ← TEXT/STRUCT/REF content classification
│       ├── llm_fallback.py      ← Claude fallback for unrecognised payloads (+ PHI scan)
│       ├── transfer_planner.py  ← Size-aware TransferPlan + TransferRecord
│       ├── traced_writer.py     ← Per-record async writer + transfer_log
│       └── parsers/             ← format_a/b/c/d + json_dict parsers
│
├── replit-app/                  ← Next.js 16 frontend (port 5000)
│   ├── next.config.ts           ← Proxy rewrites → 3 MCP servers
│   ├── app/                     ← App Router pages + API routes
│   │   ├── .well-known/
│   │   │   ├── oauth-protected-resource/[[...slug]]/route.ts  ← RFC 9728
│   │   │   └── oauth-authorization-server/route.ts            ← RFC 8414
│   │   ├── authorize/route.ts   ← OAuth authorization_code grant (auto-issues code)
│   │   ├── token/route.ts       ← Token exchange endpoint
│   │   ├── register/route.ts    ← RFC 7591 dynamic client registration
│   │   └── api/                 ← patients · vitals · checkin · obt · mcp · sse
│   ├── lib/oauth-store.ts       ← In-memory OAuth client/code/token store
│   └── components/
│       └── PatientManager.tsx   ← Patient CRUD UI (search · add · edit · delete)
│
├── replit_dashboard/            ← Config Dashboard (port 8080)
│   ├── server.py                ← FastAPI — 18 env keys + Claude config download
│   ├── index.html               ← Single-page dashboard UI
│   └── tests/                   ← 30 dashboard tests
│
├── scripts/
│   └── generate_mcp_json.py     ← Regenerates .mcp.json from $REPLIT_DEV_DOMAIN at startup
│
├── tests/
│   ├── phase1/                  ← 196 Phase 1 clinical intelligence tests
│   ├── phase2/                  ← 95 Phase 2 deliberation + flag lifecycle tests
│   ├── e2e/                     ← 28 end-to-end MCP use-case tests
│   ├── test_mcp_smoke.py        ← 24 MCP smoke tests
│   └── test_mcp_discovery.py    ← 26 MCP discovery + OAuth regression tests (DN-1–DN-26)
│
├── .mcp.json                    ← MCP client discovery (public HTTPS URLs, auto-regenerated)
├── start.sh                     ← Production startup: regenerates .mcp.json → starts all 5 services
├── config/system_prompts/       ← Role-based prompts (pcp · care_manager · patient)
├── shared/claude-client.js      ← Shared JS MCP client
├── prototypes/                  ← 4 HTML proof-of-concept prototypes
├── submission/README.md         ← MCP marketplace submission
├── CLAUDE.md                    ← Full implementation guide for Claude Code
└── requirements.txt             ← Python deps (pytest-asyncio==0.21.2 pinned)
```

---

## Workflows (5 active)

| Workflow | Command | Port |
|---------|---------|------|
| Start application | `cd replit-app && npm run dev` | 5000 |
| Config Dashboard | `cd replit_dashboard && python server.py` | 8080 |
| Clinical MCP Server | `MCP_TRANSPORT=streamable-http MCP_PORT=8001 python -m server.mcp_server` | 8001 |
| Skills MCP Server | `cd mcp-server && MCP_TRANSPORT=streamable-http MCP_PORT=8002 python server.py` | 8002 |
| Ingestion MCP Server | `MCP_TRANSPORT=streamable-http MCP_PORT=8003 python -m ingestion.server` | 8003 |

---

## Three MCP Servers

All three are proxied through Next.js (port 5000). Claude connects via OAuth PKCE — the `/authorize` endpoint auto-issues a code with no login screen (public server).

| FastMCP Name | Port | Public Path | Tools | Health |
|---|---|---|---|---|
| `ambient-clinical-intelligence` | 8001 | `/mcp` | 19 | `GET /health` |
| `ambient-skills-companion` | 8002 | `/mcp-skills` | 18 | `GET /health` |
| `ambient-ingestion` | 8003 | `/mcp-ingestion` | 1 | `GET /health` |

**Public base URL:** `https://[your-replit-domain]`

### Server 1 — ambient-clinical-intelligence (`server/mcp_server.py`)

19 tools at `https://[domain]/mcp`:

| Tool | Description |
|------|-------------|
| `clinical_query` | 3-layer guardrail pipeline → Claude Sonnet |
| `get_guideline` | Fetch USPSTF/ADA guideline by ID |
| `check_screening_due` | Overdue screenings for patient profile |
| `flag_drug_interaction` | Known drug interactions |
| `get_synthetic_patient` | Demo patient from live DB (MRN 4829341) |
| `use_healthex` | Switch data track to HealthEx real records |
| `use_demo_data` | Switch data track to Synthea demo data |
| `switch_data_track` | Switch to named track (synthea/healthex/auto) |
| `get_data_source_status` | Report active track + available sources |
| `register_healthex_patient` | Create/upsert HealthEx patient row, return UUID |
| `ingest_from_healthex` | Two-phase ingest: plan (fast) + execute (write rows) |
| `execute_pending_plans` | Re-execute failed/pending ingestion plans |
| `get_ingestion_plans` | Read plan summaries + insights_summary |
| `get_transfer_audit` | Per-record transfer_log audit trail |
| `run_deliberation` | Dual-LLM deliberation (mode: "progressive" or "full") |
| `get_deliberation_results` | Retrieve stored deliberation outputs |
| `get_flag_review_status` | Flag lifecycle status (open/retracted/pending human review) |
| `get_patient_knowledge` | Accumulated patient-specific knowledge |
| `get_pending_nudges` | Queued nudges for delivery scheduling |

Also has REST wrappers at `/tools/<name>` for direct browser calls.

### Server 2 — ambient-skills-companion (`mcp-server/server.py`)

18 tools at `https://[domain]/mcp-skills` (auto-discovered from `mcp-server/skills/`):
`compute_obt_score`, `compute_provider_risk`, `run_crisis_escalation`, `run_food_access_nudge`,
`generate_daily_checkins`, `generate_patient`, `generate_daily_vitals`, `generate_previsit_brief`,
`run_sdoh_assessment`, `use_healthex`, `use_demo_data`, `switch_data_track`,
`get_data_source_status`, `check_data_freshness`, `run_ingestion`, `get_source_conflicts`,
`ingest_from_healthex`, `register_healthex_patient`

### Server 3 — ambient-ingestion (`ingestion/server.py`)

1 tool at `https://[domain]/mcp-ingestion`:
`trigger_ingestion(patient_id, source, force_refresh)` — full ETL pipeline for a patient.

---

## OAuth 2.0 Discovery Layer

Claude requires OAuth PKCE before connecting to any remote MCP server. Five Next.js routes handle this:

| Endpoint | RFC | Purpose |
|---|---|---|
| `GET /.well-known/oauth-protected-resource` | RFC 9728 | Declares auth server URL |
| `GET /.well-known/oauth-authorization-server` | RFC 8414 | Lists OAuth endpoints |
| `POST /register` | RFC 7591 | Dynamic client registration |
| `GET /authorize` | RFC 6749 | Issues auth code immediately (no login — public server) |
| `POST /token` | RFC 6749 | Exchanges code for Bearer token |

State is managed in `replit-app/lib/oauth-store.ts` (in-memory, ephemeral — clients re-authorize on restart).

---

## Dual-LLM Deliberation Engine (`server/deliberation/`)

6-phase async pre-computation pipeline (Claude Sonnet + GPT-4o):

```
Phase 0.5  planner.py           Pre-deliberation agenda builder (Haiku)
Phase 0    context_compiler.py  Assemble patient EHR context (tiered, 11K budget)
Phase 1    analyst.py           Parallel Claude Sonnet + GPT-4o independent analysis
Phase 2    critic.py            Cross-critique rounds with convergence scoring
Phase 3    synthesizer.py       Unified synthesis → 5 output categories
Phase 3.25 synthesis_reviewer.py Post-synthesis domain review, re-deliberation trigger (Haiku)
Phase 3.5  output_safety.py     Guardrail wrapper on deliberation output
Phase 4    behavioral_adapter.py SMS/push nudge formatting
Phase 5    knowledge_store.py   Atomic DB commit
```

4 DB tables: `deliberations`, `deliberation_outputs`, `patient_knowledge`, `core_knowledge_updates`
3 flag tables: `deliberation_flags`, `flag_review_runs`, `flag_corrections`

---

## Database — 34 Tables

- **Provider**: Replit built-in PostgreSQL (`DATABASE_URL` env var)
- **Base schema** (22 tables, `mcp-server/db/schema.sql`)
- **Deliberation** (4 tables, `server/deliberation/migrations/001`)
- **Ingestion** (4 tables, migrations 002–004): `ingestion_plans`, `transfer_log`, `clinical_notes`, `media_references`
- **Flag lifecycle** (3 tables, `server/deliberation/migrations/004`): `deliberation_flags`, `flag_review_runs`, `flag_corrections`
- **System**: `system_config` (data track, active model)

Key column names: `biometric_readings` uses `metric_type` (not `observation_type`) and `measured_at` (not `recorded_at`).

---

## Environment Variables

| Key | Purpose |
|-----|---------|
| `ANTHROPIC_API_KEY` | Replit Secret — Claude Sonnet + Haiku calls |
| `OPENAI_API_KEY` | Replit Secret — GPT-4o deliberation critic |
| `LANGSMITH_API_KEY` | Replit Secret — optional LangSmith tracing |
| `GITHUB_TOKEN` | Replit Secret — GitHub push access |
| `DATABASE_URL` | Auto-set by Replit PostgreSQL |
| `REPLIT_DEV_DOMAIN` | Auto-set — used by scripts/generate_mcp_json.py |

Config dashboard at port 8080 provides a UI for all env keys.

---

## Testing — ~670 tests

```bash
python -m pytest tests/phase1/ -v                    # 196 Phase 1 clinical intelligence
python -m pytest tests/phase2/ -v                    # 95 Phase 2 deliberation + flags
python -m pytest server/deliberation/tests/ -v       # 109 deliberation unit tests
python -m pytest tests/e2e/ -v                       # 28 end-to-end MCP use-cases
python -m pytest tests/test_mcp_smoke.py -v          # 24 MCP smoke tests
python -m pytest tests/test_mcp_discovery.py -v      # 26 MCP discovery + OAuth tests
python -m pytest ingestion/tests/ -v                 # 152 ingestion pipeline tests
cd mcp-server && python -m pytest tests/ -v          # 92 backend skills tests
cd replit-app && npm test                            # 37 Jest frontend tests
cd replit_dashboard && python -m pytest tests/ -v    # 30 dashboard tests
```

| Suite | Tests |
|-------|-------|
| Phase 1 clinical intelligence | 196 |
| Phase 2 deliberation + flag lifecycle | 95 |
| Deliberation engine unit | 109 |
| End-to-end MCP use-cases | 28 |
| MCP smoke tests | 24 |
| MCP discovery + OAuth (DN-1 to DN-26) | 26 |
| Ingestion pipeline | 152 |
| Skills MCP backend | 92 |
| Frontend (Jest) | 37 |
| Config dashboard | 30 |

---

## Key Engineering Rules

- **asyncpg**: Never use `$N + INTERVAL '1 day'` — pre-compute date bounds in Python before passing to asyncpg
- **asyncpg**: Never use `do` as a SQL alias — reserved PostgreSQL keyword; use `dout` or similar
- **FastMCP**: `FastMCP()` does NOT accept `description=` kwarg — causes startup crash
- **MCP tools**: Never use `print()` — all logging goes to `sys.stderr`
- **Model names**: `claude-sonnet-4-20250514` (clinical/synthesis), `gpt-4o` (deliberation critic), `claude-haiku-4-5-20251001` (flag reviewer + planner + synthesis reviewer)
- **pytest-asyncio**: Pinned to 0.21.2 — 1.x breaks session-scoped `event_loop` pattern
- **pytest.ini**: `asyncio_mode = auto` + `--import-mode=importlib` required
- **MCP discovery**: `.mcp.json` must use public HTTPS URLs (not localhost) — `scripts/generate_mcp_json.py` regenerates it from `$REPLIT_DEV_DOMAIN` at every startup via `start.sh`
- **OAuth**: All 5 OAuth routes must be present — Claude hits `/.well-known/oauth-protected-resource` before connecting; 404 here causes "server appears to be sleeping" error
- **Deliberation fire-and-forget**: `run_deliberation` is async — poll `get_deliberation_results` for output
- **HealthEx protocol**: `register_healthex_patient` MUST be called before `ingest_from_healthex` — it bootstraps the `patients` row that `run_deliberation` requires
- **Fence-stripping**: LLMs wrap JSON in ` ```json ``` ` fences — `json_utils.strip_markdown_fences()` is called in analyst.py, critic.py, synthesizer.py before `model_validate_json`
- **Patients table**: `first_name`, `last_name` are nullable (many HealthEx patients have NULL names)
- **Port config**: Next.js=5000, Config Dashboard=8080, Clinical MCP=8001, Skills MCP=8002, Ingestion MCP=8003
