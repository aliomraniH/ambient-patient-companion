# Ambient Patient Companion

A multi-agent AI health system that generates a continuously derived patient health UX from Role × Context × Patient State × Time — `S = f(R, C, P, T)`.

## What This System Does

The Ambient Patient Companion connects Claude to a live clinical intelligence layer built for primary care and care management. It provides:

- **Real patient data** from a 35-table PostgreSQL warehouse (Synthea + HealthEx FHIR)
- **Dual-LLM Deliberation Engine** — Claude Sonnet + GPT-4o independently analyze clinical context, cross-critique across multiple rounds, then synthesize into 5 structured output categories
- **3-Layer Clinical Guardrail Pipeline** — input validation → escalation rules → output safety on every AI call
- **5 Data Quality Validators (F1–F5)** — FHIR conformance, clinical plausibility, source anchoring, self-consistency, clinical text sanitization; flagging written to `transfer_log.quality_status`
- **Convergence Gate** — deliberation synthesis only proceeds when Claude + GPT-4o agree (score ≥ 0.40); low-convergence results return `recommendation=null` with provider note
- **Mode Elicitation Protocol** — `run_deliberation` with no mode returns `mode_selection_required` + selection token; explicit modes: `triage` (Sonnet-only, ~1 LLM call), `progressive`, `full`
- **`verify_output_provenance` — universal provenance gate** across all 3 MCP servers; blocks MIRA/ARIA/THEO/SYNTHESIS outputs with undeclared or domain-mismatched tiers; audit trail in `provenance_audit_log`
- **MCP Audit Log** — every external tool call recorded to `mcp_call_log` with session tracking, inputs, outputs, timing; queryable via 4 dedicated tools on the Skills server
- **50+ MCP tools** across 3 servers, all accessible to Claude via OAuth-authenticated HTTPS

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
      │   23 tools · 3-layer guardrails · Dual-LLM Deliberation · Gap-Aware · Flag Lifecycle
      │   AuditMiddleware: every tool call logged to mcp_call_log
      │
      ├── MCP Server 2 — ambient-skills-companion (port 8002)
      │   22+ tools · 21 skill modules auto-discovered from mcp-server/skills/
      │   AuditMiddleware: every tool call logged to mcp_call_log
      │
      └── MCP Server 3 — ambient-ingestion (port 8003)
          4 tools · HealthEx ETL pipeline (5 format parsers) · Staleness Detection · Extended Search
          AuditMiddleware: every tool call logged to mcp_call_log
      │
      └── PostgreSQL Warehouse — 35 tables
          patients · biometrics · deliberations · flags · ingestion_plans · mcp_call_log · …
```

---

## Project Structure

```
ambient-patient-companion/
│
├── server/                      ← Server 1: ambient-clinical-intelligence (port 8001)
│   ├── mcp_server.py            ← FastMCP("ambient-clinical-intelligence") — 23 tools + REST wrappers
│   │                              + AuditMiddleware("clinical", _get_db_pool) wired at end of file
│   ├── guardrails/              ← input_validator · output_validator · clinical_rules
│   └── deliberation/            ← Dual-LLM Deliberation Engine
│       ├── engine.py            ← 6-phase orchestrator (+ Phase 0.1, 0.5, 3.25, 3.5, 5.5)
│       ├── planner.py           ← Phase 0.5: pre-deliberation agenda builder (Haiku)
│       ├── context_compiler.py  ← Phase 0: assemble patient EHR context
│       ├── tiered_context_loader.py  ← 3-tier budget-capped loading (11K limit)
│       ├── analyst.py           ← Phase 1: parallel Claude Sonnet + GPT-4o analysis
│       ├── critic.py            ← Phase 2: cross-critique rounds with convergence
│       ├── synthesizer.py       ← Phase 3: unified synthesis → DeliberationResult
│       ├── synthesis_reviewer.py← Phase 3.25: post-synthesis domain review (Haiku)
│       ├── output_safety.py     ← Phase 3.5: guardrail wrapper on deliberation output
│       ├── convergence_gate.py  ← Convergence Gate: score < 0.40 → null recommendations
│       ├── behavioral_adapter.py← Phase 4: SMS/push nudge formatting
│       ├── knowledge_store.py   ← Phase 5: atomic DB commit (confidence via coerce_confidence)
│       ├── gap_validation.py    ← Phase 0.1 (pre-dispatch) + Phase 5.5 (gap artifact collection)
│       ├── flag_reviewer.py     ← LLM-powered flag lifecycle review (Haiku)
│       ├── flag_writer.py       ← Flag registry writes with data provenance
│       ├── json_utils.py        ← strip_markdown_fences() + safe_json_loads()
│       ├── schemas.py           ← 20+ Pydantic models
│       ├── prompts/             ← XML LLM prompt templates (synthesizer.xml has OUTPUT TYPE CONTRACT)
│       ├── migrations/001–006   ← Deliberation + flag lifecycle + gap-aware + quality tables
│       └── tests/               ← 290+ deliberation unit tests
│
├── mcp-server/                  ← Server 2: ambient-skills-companion (port 8002)
│   ├── server.py                ← FastMCP("ambient-skills-companion") — auto-discovers skills
│   │                              + sys.path insert for shared/ + AuditMiddleware("skills", get_pool)
│   ├── skills/                  ← 21 skill modules (register(mcp) convention)
│   │   ├── atom_vector_search.py       ← search_similar_atoms, search_behavioral_atoms_cohort
│   │   ├── behavioral_atom_extractor.py← (helper, no register) — uses shared.coercion
│   │   ├── behavioral_atom_pressure.py ← behavioral pressure tools
│   │   ├── behavioral_atoms.py         ← behavioral atom tools — uses shared.coercion
│   │   ├── behavioral_cards.py         ← behavioral card tools
│   │   ├── behavioral_gap_detector.py  ← (helper) — uses shared.datetime_utils
│   │   ├── behavioral_screening_ingestor.py  ← (helper, no register)
│   │   ├── behavioral_section_builder.py← (helper) — uses shared.coercion
│   │   ├── behavioral_tools.py         ← behavioral tool wrappers
│   │   ├── call_history.py             ← NEW: 4 audit query tools
│   │   │   ├── get_current_session     ← live session IDs + call counts (all servers)
│   │   │   ├── list_sessions           ← recent sessions with tools used + error counts
│   │   │   ├── get_session_transcript  ← full chronological call log for a session
│   │   │   └── search_tool_calls       ← filter by tool/server/time/outcome
│   │   ├── clinical_knowledge.py       ← search_clinical_knowledge (OpenFDA/RxNorm/PubMed)
│   │   ├── compute_obt_score.py        ← Optimal Being Trajectory score
│   │   ├── compute_provider_risk.py    ← provider chase list score
│   │   ├── crisis_escalation.py        ← behavioral crisis detection
│   │   ├── food_access_nudge.py        ← end-of-month SDoH nudge
│   │   ├── generate_checkins.py        ← idempotent daily check-in seed
│   │   ├── generate_patient.py         ← FHIR bundle → PostgreSQL
│   │   ├── generate_vitals.py          ← biometric reading seed
│   │   ├── ingestion_tools.py          ← 10 tools: freshness · ingestion · conflicts · tracks
│   │   │                                  _is_stale() uses ensure_aware() from shared.datetime_utils
│   │   ├── patient_state_readers.py    ← patient state reader tools
│   │   ├── previsit_brief.py           ← pre-encounter synthesis (uses ensure_aware)
│   │   ├── screening_registry.py       ← screening registry tools
│   │   ├── sdoh_assessment.py          ← social determinants assessment
│   │   ├── sdoh_registry.py            ← SDoH registry tools
│   │   └── verify_output_provenance.py ← shared adapter (source_server='ambient-skills-companion')
│   ├── db/schema.sql            ← 22-table PostgreSQL base schema (source of truth)
│   ├── transforms/              ← FHIR-to-schema transformers (5 resource types)
│   ├── seed.py                  ← python mcp-server/seed.py --patients 10 --months 6
│   └── tests/                   ← 110 backend tests (+ 3 freshness regression tests)
│
├── ingestion/                   ← Server 3: ambient-ingestion (port 8003)
│   ├── server.py                ← FastMCP("ambient-ingestion") — 4 tools
│   │                              + AuditMiddleware("ingestion", _get_provenance_pool)
│   ├── pipeline.py              ← ETL orchestrator (uses shared.datetime_utils.ensure_aware)
│   ├── conflict_resolver.py     ← Multi-source conflict resolution
│   ├── validators/              ← F1–F5 Data Quality Validators
│   └── adapters/healthex/       ← 5-format adaptive parser + audit trail
│
├── shared/                      ← Cross-server Python utilities (repo root on sys.path in all servers)
│   ├── coercion.py              ← coerce_confidence(): normalises LLM confidence strings/ints to float [0,1]
│   │                              Maps "high"→0.80, "moderate"→0.60, etc.; int %→divide by 100; float→clamp
│   ├── datetime_utils.py        ← ensure_aware(): attaches UTC tzinfo to naive DB datetimes before arithmetic
│   ├── call_recorder.py         ← CallRecorder: session tracking (30-min idle→new session UUID),
│   │                              asyncpg DB write, module-level _REGISTRY for live session queries
│   ├── audit_middleware.py      ← AuditMiddleware(Middleware): FastMCP on_call_tool hook;
│   │                              captures tool_name, inputs, output_text, output_data, timing, outcome
│   ├── claude-client.js         ← Shared JS MCP client
│   ├── provenance/              ← Universal provenance gate (all 3 MCP servers)
│   └── tests/                   ← Unit tests for shared modules
│       ├── test_coerce_confidence.py ← 28 unit tests for coerce_confidence
│       └── test_datetime_utils.py    ← 6 unit tests for ensure_aware
│
├── replit-app/                  ← Next.js 16 frontend (port 5000)
│   ├── next.config.ts           ← Proxy rewrites → 3 MCP servers
│   ├── app/                     ← App Router pages + API routes
│   │   ├── .well-known/         ← OAuth discovery (RFC 9728 + RFC 8414)
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

All three are proxied through Next.js (port 5000). Claude connects via OAuth PKCE — the `/authorize` endpoint auto-issues a code with no login screen (public server). All three servers have `AuditMiddleware` attached — every tool call is recorded to `mcp_call_log`.

| FastMCP Name | Port | Public Path | Tools | Health |
|---|---|---|---|---|
| `ambient-clinical-intelligence` | 8001 | `/mcp` | 23 | `GET /health` |
| `ambient-skills-companion` | 8002 | `/mcp-skills` | 22+ | `GET /health` |
| `ambient-ingestion` | 8003 | `/mcp-ingestion` | 4 | `GET /health` |

**Public base URL:** `https://[your-replit-domain]`

### Server 1 — ambient-clinical-intelligence (`server/mcp_server.py`)

23 tools at `https://[domain]/mcp`:

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
| `register_healthex_patient` | Create/upsert HealthEx patient row — writes `last_ingested_at=NULL` |
| `ingest_from_healthex` | Two-phase ingest: plan (fast) + execute (write rows) |
| `execute_pending_plans` | Re-execute failed/pending ingestion plans |
| `get_ingestion_plans` | Read plan summaries + insights_summary |
| `get_transfer_audit` | Per-record transfer_log audit trail |
| `run_deliberation` | Dual-LLM deliberation (mode: ask/triage/progressive/full) |
| `get_deliberation_results` | Retrieve stored deliberation outputs |
| `get_flag_review_status` | Flag lifecycle status (open/retracted/pending) |
| `get_patient_knowledge` | Accumulated patient-specific knowledge |
| `get_pending_nudges` | Queued nudges for delivery scheduling |
| `assess_reasoning_confidence` | Gap-aware confidence assessment |
| `request_clarification` | Enum-validated clarification request |
| `emit_reasoning_gap_artifact` | Enum-validated gap artifact |
| `register_gap_trigger` | Register gap trigger |
| `verify_output_provenance` | Shared provenance gate (source_server=clinical) |

Also has REST wrappers at `/tools/<name>` for direct browser calls.

### Server 2 — ambient-skills-companion (`mcp-server/server.py`)

22+ tools at `https://[domain]/mcp-skills` (auto-discovered from `mcp-server/skills/`):

**Clinical skills**: `compute_obt_score`, `compute_provider_risk`, `run_crisis_escalation`, `run_food_access_nudge`, `generate_daily_checkins`, `generate_patient`, `generate_daily_vitals`, `generate_previsit_brief`, `run_sdoh_assessment`, `search_clinical_knowledge`

**Data + ingestion**: `check_data_freshness`, `run_ingestion`, `get_source_conflicts`, `orchestrate_refresh`, `register_healthex_patient` (Skills-side)

**Behavioral stack**: `search_similar_atoms`, `search_behavioral_atoms_cohort`, behavioral pressure + card tools

**Audit query tools** (NEW — `call_history.py`):
- `get_current_session` — live session IDs + call counts for every running server
- `list_sessions(limit, server_name)` — recent sessions ordered by last activity
- `get_session_transcript(session_id, include_full_output)` — full chronological call log
- `search_tool_calls(tool_name, server_name, outcome, from_minutes_ago)` — flexible filter

**System**: `verify_output_provenance` (shared, source_server=skills)

### Server 3 — ambient-ingestion (`ingestion/server.py`)

4 tools at `https://[domain]/mcp-ingestion`:
- `trigger_ingestion(patient_id, source, force_refresh)` — full ETL pipeline
- `detect_context_staleness(patient_id, clinical_scenario)` — LOINC-keyed clinical freshness
- `search_patient_data_extended(patient_id, query, ...)` — extended data search
- `verify_output_provenance(payload, ...)` — shared adapter (source_server=ingestion)

---

## MCP Audit Log System

Every tool call made by Claude (or any external MCP client) is automatically recorded.

**Table:** `mcp_call_log` — `id`, `session_id`, `server_name`, `tool_name`, `called_at`, `duration_ms`, `input_params` (JSONB), `output_text`, `output_data` (JSONB), `outcome`, `error_message`, `seq`

**Session tracking:** 30 minutes of inactivity → new session UUID. `seq` is call number within session.

**Query via MCP tools** on the Skills server (port 8002):
```
get_current_session()                          → live session IDs + stats
list_sessions(limit=10)                        → recent sessions
get_session_transcript(session_id=None)        → full call log (latest session by default)
search_tool_calls(tool_name="run_deliberation") → filter by tool/server/time/outcome
```

---

## OAuth 2.0 Discovery Layer

| Endpoint | RFC | Purpose |
|---|---|---|
| `GET /.well-known/oauth-protected-resource` | RFC 9728 | Declares auth server URL |
| `GET /.well-known/oauth-authorization-server` | RFC 8414 | Lists OAuth endpoints |
| `POST /register` | RFC 7591 | Dynamic client registration (redirect URIs validated: https only, localhost in dev) |
| `GET /authorize` | RFC 6749 | Issues auth code with PKCE S256 (auto-issues, no login — public server) |
| `POST /token` | RFC 6749 | Exchanges code for Bearer token (PKCE S256 verified, redirect_uri required) |
| `POST /api/auth/session` | — | Dashboard session cookie (httpOnly, strict sameSite) |

State managed in `replit-app/lib/oauth-store.ts` (in-memory, ephemeral — clients re-authorize on restart).

### Security Controls (added 2026-04-15)

- **Bearer token enforcement**: All `/api/*` routes require valid bearer token OR httpOnly session cookie. External callers must use OAuth flow; dashboard gets auto-issued session cookie via `SessionProvider`.
- **PKCE S256**: `/authorize` requires `code_challenge_method=S256` when `code_challenge` is present. `/token` always verifies `code_verifier` against stored challenge using SHA-256 + base64url.
- **Redirect URI validation**: `/register` rejects non-https redirect URIs (http://localhost and http://127.0.0.1 allowed only when `NODE_ENV !== 'production'`).
- **CORS allowlist**: Wildcard `*` replaced with explicit origin allowlist built from `REPLIT_DEV_DOMAIN` + localhost. Configurable via `CORS_ALLOWED_ORIGINS` env var.
- **Rate limiting**: In-memory sliding-window rate limiter by IP on `/register` (5/min), `/authorize` (20/min), `/token` (10/min), `/api/mcp/*` (60/min).
- **Security lib files**: `lib/auth-middleware.ts`, `lib/cors.ts`, `lib/rate-limiter.ts`, `lib/redirect-uri-validator.ts`, `lib/session.ts`

---

## Dual-LLM Deliberation Engine (`server/deliberation/`)

6-phase async pre-computation pipeline (Claude Sonnet + GPT-4o):

```
Phase 0.5  planner.py           Pre-deliberation agenda builder (Haiku)
Phase 0    context_compiler.py  Assemble patient EHR context (tiered, 11K budget)
Phase 1    analyst.py           Parallel Claude Sonnet + GPT-4o independent analysis
Phase 2    critic.py            Cross-critique rounds with convergence scoring
Phase 3    synthesizer.py       Unified synthesis → 5 output categories
Phase 3.25 synthesis_reviewer.py Post-synthesis domain review (Haiku)
Phase 3.5  output_safety.py     Guardrail wrapper on deliberation output
Phase 4    behavioral_adapter.py SMS/push nudge formatting
Phase 5    knowledge_store.py   Atomic DB commit (confidence values via coerce_confidence())
```

**Output type contract (synthesizer.xml):** `confidence` and `likelihood` fields MUST be float 0.0–1.0 — never categorical strings like "high". Enforced by `shared/coercion.py:coerce_confidence()` at all 5 DB bind sites.

4 DB tables: `deliberations`, `deliberation_outputs`, `patient_knowledge`, `core_knowledge_updates`
3 flag tables: `deliberation_flags`, `flag_review_runs`, `flag_corrections`

---

## Database — 35 Tables

- **Provider**: Replit built-in PostgreSQL (`DATABASE_URL` env var)
- **Base schema** (22 tables, `mcp-server/db/schema.sql`)
- **Deliberation** (4 tables, `server/deliberation/migrations/001`)
- **Ingestion** (4 tables, migrations 002–004): `ingestion_plans`, `transfer_log`, `clinical_notes`, `media_references`
- **Flag lifecycle** (3 tables, `server/deliberation/migrations/004`): `deliberation_flags`, `flag_review_runs`, `flag_corrections`
- **Audit** (1 table): `mcp_call_log` — MCP tool call audit log with session tracking
- **System**: `system_config` (data track, active model)

Key column names: `biometric_readings` uses `metric_type` (not `observation_type`) and `measured_at` (not `recorded_at`).

Key source_freshness rule: `last_ingested_at` is written as `NULL` on patient registration — never `NOW()`. This ensures `_is_stale()` returns `True` on first call and ingest is never skipped.

---

## Environment Variables

| Key | Purpose |
|-----|---------|
| `ANTHROPIC_API_KEY` | Replit Secret — Claude Sonnet + Haiku calls |
| `OPENAI_API_KEY` | Replit Secret — GPT-4o deliberation critic |
| `LANGSMITH_API_KEY` | Replit Secret — optional LangSmith tracing |
| `HF_TOKEN` | Replit Secret — HuggingFace Pro (MedCPT-Article-Encoder, 768-dim embeddings) |
| `GITHUB_TOKEN` | Replit Secret — GitHub push access |
| `DATABASE_URL` | Auto-set by Replit PostgreSQL |
| `REPLIT_DEV_DOMAIN` | Auto-set — used by scripts/generate_mcp_json.py |

Config dashboard at port 8080 provides a UI for all env keys.

---

## Testing — ~800 tests

```bash
python -m pytest tests/phase1/ -v                    # 196 Phase 1 clinical intelligence
python -m pytest tests/phase2/ -v                    # 95 Phase 2 deliberation + flags
python -m pytest server/deliberation/tests/ -v       # 290+ deliberation unit tests
python -m pytest tests/e2e/ -v                       # 28 end-to-end MCP use-cases
python -m pytest tests/test_mcp_smoke.py -v          # 24 MCP smoke tests
python -m pytest tests/test_mcp_discovery.py -v      # 26 MCP discovery + OAuth tests
python -m pytest ingestion/tests/ -v                 # 152 ingestion pipeline tests
cd mcp-server && python -m pytest tests/ -v          # 110 backend skills tests
python -m pytest shared/tests/ -v                    # 34 shared utility unit tests
cd replit-app && npm test                            # 37 Jest frontend tests
cd replit_dashboard && python -m pytest tests/ -v    # 30 dashboard tests
```

---

## Key Engineering Rules

- **asyncpg**: Never use `$N * INTERVAL '1 day'` — use `$N * INTERVAL '1 day'` (preferred) OR pre-compute date bounds in Python. Never use `('$N' || ' days')::INTERVAL` (parameter quoting issue)
- **asyncpg**: Never use `do` as a SQL alias — reserved keyword; use `dout` or similar
- **FastMCP**: `FastMCP()` does NOT accept `description=` kwarg — causes startup crash
- **MCP tools**: Never use `print()` — all logging goes to `sys.stderr`
- **Model names**: `claude-sonnet-4-20250514` (clinical/synthesis), `gpt-4o` (deliberation critic), `claude-haiku-4-5-20251001` (flag reviewer + planner + synthesis reviewer)
- **pytest-asyncio**: Pinned to 0.21.2 — 1.x breaks session-scoped `event_loop` pattern
- **pytest.ini**: `asyncio_mode = auto` + `--import-mode=importlib` required
- **shared/ imports**: Repo root is on sys.path in all 3 servers. Import as `from shared.coercion import coerce_confidence` / `from shared.datetime_utils import ensure_aware` / `from shared.audit_middleware import AuditMiddleware`
- **coerce_confidence**: Wrap ALL LLM-produced confidence/likelihood values before writing to Postgres. Float→clamp; int>1→divide by 100; categorical string→map; bool→None
- **ensure_aware**: Call on any DB-read datetime before arithmetic. asyncpg returns TIMESTAMPTZ as aware, TIMESTAMP WITHOUT TIME ZONE as naive — mixing raises TypeError
- **source_freshness init**: Always write `last_ingested_at=NULL` on first registration, never `NOW()`. The `_is_stale(None, ttl)` call returns `True` immediately, triggering the first ingest
- **AuditMiddleware**: Attached to all 3 servers after tool registration via `mcp.add_middleware(AuditMiddleware(server_name, get_pool_fn))`. Initialises lazily on first tool call
- **MCP discovery**: `.mcp.json` must use public HTTPS URLs — `scripts/generate_mcp_json.py` regenerates from `$REPLIT_DEV_DOMAIN` at every startup via `start.sh`
- **OAuth**: All 5 OAuth routes must be present — Claude hits `/.well-known/oauth-protected-resource` before connecting; 404 causes "server appears to be sleeping" error
- **Deliberation fire-and-forget**: `run_deliberation` is async — poll `get_deliberation_results` for output
- **HealthEx protocol**: `register_healthex_patient` MUST be called before `ingest_from_healthex`
- **Fence-stripping**: LLMs wrap JSON in ```json``` fences — `json_utils.strip_markdown_fences()` called in analyst.py, critic.py, synthesizer.py before `model_validate_json`
- **Port config**: Next.js=5000, Config Dashboard=8080, Clinical MCP=8001, Skills MCP=8002, Ingestion MCP=8003
- **sys.path for shared**: Each server inserts repo root (`Path(__file__).resolve().parent.parent`) into `sys.path` at module top, before any imports. mcp-server/server.py also adds repo root (one extra level needed for Skills server)
