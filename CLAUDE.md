# CLAUDE.md — Ambient Patient Companion
## Implementation Guide for Claude Code

> **Project**: Ambient Patient Companion
> **Model**: Ambient Action Model — `S = f(R, C, P, T)`
> **Stack**: FastMCP 3.2 · Next.js 16 · PostgreSQL · Claude Sonnet + GPT-4o
> **GitHub**: https://github.com/aliomraniH/ambient-patient-companion

---

## 1. What This System Does

The Ambient Patient Companion gives Claude a real-time clinical intelligence layer for primary care. It connects to a live PostgreSQL warehouse (**35 tables**, Synthea + HealthEx FHIR) through three FastMCP servers, all proxied through Next.js 16 with a full OAuth 2.0 PKCE layer. Every tool call from Claude is automatically recorded to the `mcp_call_log` audit table via `AuditMiddleware` on all three servers.

The system surface (`S`) is derived from:

| Variable | Meaning | Example |
|---|---|---|
| `R` | Role | Patient, Provider, Care Coordinator |
| `C` | Context | Pre-visit, In-encounter, Post-visit, Async |
| `P` | Patient State | HbA1c trend, BP readings, SDoH flags, med adherence |
| `T` | Time | Timestamp, time-since-last-contact, care gap age |

**Core design principle**: Zero activation cost. The right action surfaces before the clinician thinks to look for it.

---

## 2. Architecture

```
Claude Web / API
      │ OAuth PKCE (auto-handled by Next.js OAuth layer)
      ▼
Next.js 16 (port 5000)
  ├── OAuth discovery:  /.well-known/oauth-protected-resource (RFC 9728)
  │                    /.well-known/oauth-authorization-server (RFC 8414)
  │                    /register · /authorize · /token
  ├── MCP proxy:  /mcp          → localhost:8001
  │               /mcp-skills   → localhost:8002
  │               /mcp-ingestion→ localhost:8003
  └── REST proxy: /tools/*      → localhost:8001/tools/*
      │
      ├── MCP Server 1 — ambient-clinical-intelligence (port 8001)
      │   23 tools · 3-layer guardrails · Dual-LLM Deliberation · Flag Lifecycle
      │   AuditMiddleware("clinical", _get_db_pool) — all calls → mcp_call_log
      │   Model: claude-sonnet-4-20250514 (clinical), gpt-4o (critic),
      │          claude-haiku-4-5-20251001 (flag reviewer · planner · synthesis reviewer)
      │
      ├── MCP Server 2 — ambient-skills-companion (port 8002)
      │   22+ tools · 21 skill modules auto-discovered from mcp-server/skills/
      │   AuditMiddleware("skills", get_pool) — all calls → mcp_call_log
      │   Includes call_history.py: 4 audit query tools for inspecting mcp_call_log
      │
      └── MCP Server 3 — ambient-ingestion (port 8003)
          4 tools · HealthEx ETL (5 format parsers: A/B/C/D/JSON-dict)
          AuditMiddleware("ingestion", _get_provenance_pool) — all calls → mcp_call_log
      │
      └── PostgreSQL — 35 tables (DATABASE_URL env var, auto-set by Replit)
```

**Public base URL**: `https://[your-replit-domain]`

| Server | Path | Tools |
|--------|------|-------|
| `ambient-clinical-intelligence` | `/mcp` | 23 |
| `ambient-skills-companion` | `/mcp-skills` | 22+ |
| `ambient-ingestion` | `/mcp-ingestion` | 4 |

---

## 3. Repository Structure

```
ambient-patient-companion/
├── CLAUDE.md                    ← This file
├── README.md                    ← Project overview + public URLs
├── replit.md                    ← Always-loaded agent memory
├── requirements.txt             ← Python deps (pytest-asyncio==0.21.2 pinned)
├── start.sh                     ← Production startup:
│                                   1. python scripts/generate_mcp_json.py
│                                   2. Start all 3 MCP servers
│                                   3. Start Config Dashboard
│                                   4. Start Next.js (foreground)
├── .mcp.json                    ← MCP client discovery (auto-regenerated at startup)
│
├── scripts/
│   └── generate_mcp_json.py     ← Reads $REPLIT_DEV_DOMAIN → writes .mcp.json
│
├── server/                      ← Server 1: ambient-clinical-intelligence (port 8001)
│   ├── mcp_server.py            ← FastMCP("ambient-clinical-intelligence"), 19 tools
│   │                              + REST wrappers at /tools/<name>
│   │                              + GET /health → {"ok":true,"server":"ambient-clinical-intelligence"}
│   ├── guardrails/
│   │   ├── input_validator.py   ← PHI detection, jailbreak, scope, emotional tone
│   │   ├── output_validator.py  ← Citation check, PHI leakage, diagnostic flags
│   │   └── clinical_rules.py    ← Escalation: life-threatening, substances, pediatric
│   └── deliberation/
│       ├── engine.py            ← 6-phase pipeline orchestrator
│       ├── planner.py           ← Phase 0.5: agenda builder (Haiku)
│       ├── context_compiler.py  ← Phase 0: assemble EHR context (tiered, 11K budget)
│       ├── tiered_context_loader.py  ← 3-tier budget-capped context loading
│       ├── analyst.py           ← Phase 1: parallel Claude Sonnet + GPT-4o
│       ├── critic.py            ← Phase 2: cross-critique + convergence scoring
│       ├── synthesizer.py       ← Phase 3: unified synthesis → DeliberationResult
│       ├── synthesis_reviewer.py← Phase 3.25: post-synthesis domain review (Haiku)
│       ├── output_safety.py     ← Phase 3.5: guardrail wrapper on deliberation output
│       ├── behavioral_adapter.py← Phase 4: SMS/push nudge formatting
│       ├── knowledge_store.py   ← Phase 5: atomic DB commit
│       ├── flag_reviewer.py     ← LLM-powered flag lifecycle review (Haiku)
│       ├── flag_writer.py       ← Flag registry writes with data provenance
│       ├── data_request_parser.py  ← Parse agent data requests between rounds
│       ├── json_utils.py        ← strip_markdown_fences() + safe_json_loads()
│       ├── schemas.py           ← 20+ Pydantic models for deliberation data flow
│       ├── prompts/             ← XML LLM prompt templates (5 roles)
│       ├── migrations/001_deliberation_tables.sql
│       ├── migrations/002_data_requests.sql
│       ├── migrations/003_transfer_log.sql
│       ├── migrations/004_flag_lifecycle.sql
│       └── tests/               ← 109 deliberation unit tests
│
├── mcp-server/                  ← Server 2: ambient-skills-companion (port 8002)
│   ├── server.py                ← FastMCP("ambient-skills-companion")
│   │                              sys.path insert for shared/ + AuditMiddleware("skills", get_pool)
│   │                              GET /health → {"ok":true,"server":"ambient-skills-companion"}
│   ├── skills/                  ← 21 skill modules (register(mcp) convention)
│   │   ├── call_history.py      ← 4 audit query tools (get_current_session, list_sessions,
│   │   │                              get_session_transcript, search_tool_calls)
│   │   ├── atom_vector_search.py← search_similar_atoms, search_behavioral_atoms_cohort
│   │   ├── behavioral_atoms.py  ← behavioral atom tools (uses shared.coercion)
│   │   ├── compute_obt_score.py
│   │   ├── compute_provider_risk.py
│   │   ├── crisis_escalation.py
│   │   ├── food_access_nudge.py
│   │   ├── generate_checkins.py
│   │   ├── generate_patient.py  ← FHIR bundle → PostgreSQL
│   │   ├── generate_vitals.py
│   │   ├── ingestion_tools.py   ← 5+ tools: freshness · ingestion · conflicts · tracks
│   │   │                              _is_stale() uses shared.datetime_utils.ensure_aware
│   │   ├── previsit_brief.py    ← uses ensure_aware for deadline arithmetic
│   │   ├── screening_registry.py
│   │   ├── sdoh_assessment.py
│   │   ├── sdoh_registry.py
│   │   ├── clinical_knowledge.py← search_clinical_knowledge (OpenFDA/RxNorm/PubMed)
│   │   ├── patient_state_readers.py
│   │   └── verify_output_provenance.py ← shared adapter, source_server=skills
│   ├── db/schema.sql            ← 22-table base schema (authoritative source of truth)
│   ├── transforms/              ← FHIR-to-schema transformers (5 resource types)
│   ├── seed.py                  ← python mcp-server/seed.py --patients 10 --months 6
│   └── tests/                   ← 110 backend tests
│
├── ingestion/                   ← Server 3: ambient-ingestion (port 8003)
│   ├── server.py                ← FastMCP("ambient-ingestion"), trigger_ingestion tool
│   │                              GET /health → {"ok":true,"server":"ambient-ingestion"}
│   ├── pipeline.py              ← ETL orchestrator
│   ├── conflict_resolver.py     ← Multi-source conflict resolution
│   └── adapters/healthex/
│       ├── format_detector.py   ← detect_format() → 5 formats
│       ├── ingest.py            ← adaptive_parse() entry point
│       ├── planner.py           ← Two-phase ingest planner
│       ├── executor.py          ← Phase 2 worker + TracedWriter
│       ├── content_router.py    ← TEXT/STRUCT/REF/unknown classification
│       ├── llm_fallback.py      ← Claude fallback + PHI scan
│       ├── transfer_planner.py  ← Size-aware TransferPlan
│       ├── traced_writer.py     ← Per-record writer + transfer_log audit
│       └── parsers/             ← format_a / format_b / format_c / format_d / json_dict
│
├── shared/                      ← Cross-server Python utilities (repo root on sys.path in all 3 servers)
│   ├── coercion.py              ← coerce_confidence(): normalises LLM-produced confidence values
│   │                              bool→None | float→clamp[0,1] | int≤1→float | int>1→÷100
│   │                              str numeric(int-valued>1)→÷100 | categorical→_CONFIDENCE_MAP
│   │                              ("high"→0.80,"moderate"→0.60,"critical"→0.95,"very high"→0.90,
│   │                               "low"→0.35,"none"→0.05)
│   ├── datetime_utils.py        ← ensure_aware(dt): attaches UTC tzinfo to naive DB datetimes
│   │                              (asyncpg returns TIMESTAMPTZ as aware, TIMESTAMP as naive;
│   │                               mixing aware+naive raises TypeError in timedelta arithmetic)
│   ├── call_recorder.py         ← CallRecorder: session_id UUID, 30-min idle→new UUID,
│   │                              asyncpg write to mcp_call_log, _REGISTRY for live queries
│   ├── audit_middleware.py      ← AuditMiddleware(Middleware): FastMCP on_call_tool hook
│   │                              captures tool_name, input_params, output_text, output_data,
│   │                              duration_ms, outcome ("success"|"error")
│   │                              Attached via mcp.add_middleware() after tool registration
│   ├── claude-client.js         ← Shared JS MCP client (direct tool endpoint calls)
│   ├── provenance/              ← Universal provenance gate (shared across all 3 servers)
│   └── tests/                   ← 34 unit tests
│       ├── test_coerce_confidence.py ← 28 tests for coerce_confidence
│       └── test_datetime_utils.py    ← 6 tests for ensure_aware
│
├── replit-app/                  ← Next.js 16 frontend (port 5000)
│   ├── next.config.ts           ← Proxy rewrites to all 3 MCP servers
│   ├── lib/oauth-store.ts       ← In-memory OAuth state (clients, codes, tokens)
│   ├── app/
│   │   ├── .well-known/
│   │   │   ├── oauth-protected-resource/[[...slug]]/route.ts  ← RFC 9728
│   │   │   └── oauth-authorization-server/route.ts            ← RFC 8414
│   │   ├── authorize/route.ts   ← GET: issues auth code immediately (no login)
│   │   ├── token/route.ts       ← POST: exchanges code for Bearer token
│   │   ├── register/route.ts    ← POST: dynamic client registration (RFC 7591)
│   │   └── api/
│   │       ├── patients/        ← GET/POST + [id]/ GET/PUT/DELETE
│   │       ├── checkin/         ← Daily check-in endpoints
│   │       ├── vitals/          ← Biometric readings
│   │       ├── obt/             ← OBT score
│   │       ├── mcp/             ← MCP proxy
│   │       └── sse/             ← Server-sent events
│   └── components/
│       └── PatientManager.tsx   ← Patient CRUD UI (search · add · edit · delete)
│
├── replit_dashboard/            ← Config Dashboard (port 8080)
│   ├── server.py                ← FastAPI: env key management + Claude config download
│   ├── index.html               ← Single-page dashboard UI
│   └── tests/                   ← 30 dashboard tests
│
├── tests/
│   ├── phase1/                  ← 196 Phase 1 clinical intelligence tests
│   ├── phase2/                  ← 95 Phase 2 deliberation + flag lifecycle tests
│   ├── e2e/                     ← 28 end-to-end MCP use-case tests (UC-01→UC-18+)
│   ├── test_mcp_smoke.py        ← 24 MCP smoke tests
│   └── test_mcp_discovery.py    ← 26 MCP discovery + OAuth tests (DN-1–DN-26)
│       Classes: TestServerNaming · TestHealthCheckContract · TestStartupTopology
│                TestCrossServerConsistency · TestOAuthDiscovery
│
├── config/system_prompts/       ← Role-based system prompts (pcp · care_manager · patient)
├── prototypes/                  ← 4 HTML proof-of-concept prototypes
└── submission/README.md         ← MCP marketplace submission
```

---

## 4. Environment Variables

All secrets are Replit Secrets (never in `.env` files):

| Key | Type | Purpose |
|-----|------|---------|
| `ANTHROPIC_API_KEY` | Secret | Claude Sonnet + Haiku API calls |
| `OPENAI_API_KEY` | Secret | GPT-4o deliberation critic |
| `LANGSMITH_API_KEY` | Secret | Optional LangSmith tracing |
| `GITHUB_TOKEN` | Secret | GitHub push access |
| `DATABASE_URL` | Auto (Replit) | PostgreSQL connection string |
| `REPLIT_DEV_DOMAIN` | Auto (Replit) | Public domain — used by `generate_mcp_json.py` |

**Models in use:**
- `claude-sonnet-4-20250514` — clinical_query (via guardrails) + deliberation synthesis
- `gpt-4o` — deliberation critic (Phase 1 + Phase 2)
- `claude-haiku-4-5-20251001` — flag reviewer + planner (Phase 0.5) + synthesis reviewer (Phase 3.25)

---

## 5. MCP Tool Registry

> **Live counts** (post audit-log system, 2026-04-15): S1 = 23, S2 = 22+, S3 = 4.
> Re-derive anytime with `curl /tools` on each `/health`-responsive server.
>
> `verify_output_provenance` is **registered on all three servers by design**
> via `shared/provenance/tool_adapter.register_provenance_tool()`. Each server
> passes its own `source_server` tag (`ambient-clinical-intelligence` |
> `ambient-skills-companion` | `ambient-ingestion`) so `provenance_audit_log`
> rows record which pipeline performed the verification. This is NOT a
> dedup target.

### Server 1 — ambient-clinical-intelligence (23 tools)

```python
# server/mcp_server.py
mcp = FastMCP("ambient-clinical-intelligence")

@mcp.tool    clinical_query(question, patient_id, role)
             # role: 'pcp' | 'care_manager' | 'patient'
             # 'lab_tech' NOT YET IMPLEMENTED — ValueError if passed
@mcp.tool    get_guideline(guideline_id)
@mcp.tool    check_screening_due(age, sex, conditions)
@mcp.tool    flag_drug_interaction(medications)
@mcp.tool    get_synthetic_patient()                    # queries live DB
@mcp.tool    use_healthex()
@mcp.tool    use_demo_data()
@mcp.tool    switch_data_track(track)                   # synthea|healthex|auto
@mcp.tool    get_data_source_status()
@mcp.tool    register_healthex_patient(patient_data)    # MUST call before ingest_from_healthex
@mcp.tool    ingest_from_healthex(patient_id, payload)
@mcp.tool    execute_pending_plans(patient_id)
@mcp.tool    get_ingestion_plans(patient_id)
@mcp.tool    get_transfer_audit(patient_id)
@mcp.tool    run_deliberation(patient_id, mode, selection_token)
             # mode: "ask"|"triage"|"progressive"|"full" (omit/"ask" elicits choice)
@mcp.tool    get_deliberation_results(patient_id)       # poll for results
@mcp.tool    get_flag_review_status(patient_id)
@mcp.tool    get_patient_knowledge(patient_id)
@mcp.tool    get_pending_nudges(patient_id, target)
             # target: "patient" | "care_team" | ["patient","care_team"]
             # list form returns {by_target: {...}, total_count} in one call
# — Gap-aware reasoning tools (shared/gap_aware/*) —
@mcp.tool    assess_reasoning_confidence(...)
@mcp.tool    request_clarification(...)                  # enum-validated recipient/urgency
@mcp.tool    emit_reasoning_gap_artifact(...)            # enum-validated gap_type/severity/agent
@mcp.tool    register_gap_trigger(...)
@mcp.tool    verify_output_provenance(payload, ...)      # shared adapter, source_server=S1

# REST wrappers (browser direct-call):
GET  /health                   → {"ok":true,"server":"ambient-clinical-intelligence","version":"1.0.0"}
POST /tools/<tool_name>        → same response as MCP tool call
```

### Server 2 — ambient-skills-companion (22+ tools)

Auto-discovered from `mcp-server/skills/` via `load_skills(mcp)`. 21 modules loaded.
The 6 cross-server duplicates were removed post-`bd4216f` — those live on S1 only.
4 new audit query tools added via `call_history.py`.

```
# Clinical skills
compute_obt_score · compute_provider_risk · run_crisis_escalation · run_food_access_nudge
generate_daily_checkins · generate_patient · generate_daily_vitals · generate_previsit_brief
run_sdoh_assessment · search_clinical_knowledge

# Data + ingestion skills
check_data_freshness · run_ingestion · get_source_conflicts · orchestrate_refresh

# Behavioral + vector skills
search_similar_atoms · search_behavioral_atoms_cohort · behavioral_pressure_tools
behavioral_card_tools

# Audit query tools (NEW — call_history.py)
get_current_session · list_sessions · get_session_transcript · search_tool_calls

# System
verify_output_provenance

GET /health → {"ok":true,"server":"ambient-skills-companion","version":"1.0.0"}
```

**Tool statuses:**
- `search_clinical_knowledge` — REAL external-API tool (OpenFDA, RxNorm, PubMed
  via `gap_aware/knowledge_searcher.py`). Fully functional. NOT a vector stub.
- `generate_previsit_brief` — cache-aware reader. Includes
  `recent_deliberation` section when a complete deliberation exists within
  the last 24 hours. NEVER synchronously triggers `run_deliberation`.
  Uses `shared.datetime_utils.ensure_aware()` for deadline arithmetic.
- `check_data_freshness` — **orchestration-phase completeness**: checks that
  all pipeline stages (ingest, normalize, warehouse write) have run for a
  patient. Different from S3's `detect_context_staleness` (below).
- `verify_output_provenance` — shared adapter, `source_server='ambient-skills-companion'`.
- `get_current_session` — queries `mcp_call_log` via `shared.call_recorder._REGISTRY` for
  live session IDs + call counts across all running servers.
- `get_session_transcript` — full chronological tool call log for a session (latest if None).
- `search_tool_calls` — filter by tool_name, server_name, outcome, from_minutes_ago.

### Server 3 — ambient-ingestion (4 tools)

```python
# ingestion/server.py
mcp = FastMCP("ambient-ingestion")

@mcp.tool    trigger_ingestion(patient_id, source, force_refresh)
@mcp.tool    detect_context_staleness(patient_id, clinical_scenario)
             # LOINC-keyed clinical freshness per evidence-based thresholds
             # (pre-encounter vs acute event). Returns freshness_score +
             # recommended_refreshes. Different from S2's check_data_freshness.
@mcp.tool    search_patient_data_extended(patient_id, query, ...)
@mcp.tool    verify_output_provenance(payload, ...)      # shared adapter, source_server=S3

GET /health → {"ok":true,"server":"ambient-ingestion","version":"1.0.0"}
```

**[PLANNED — not yet implemented]** — see plan file for the full T/P/C/R
dimension-getter batch (Tier 2.a, 10 read-only tools) and the behavioral
science stack (Tier 2.b, 12 tools + migration 008).

### Planned vector-store stub — `_VectorStorePlaceholder`

Defined in `server/mcp_server.py` and consumed by `context_compiler.py` §12
(`applicable_guidelines` pre-fetch). Returns `[]` until migration
`009_pgvector_guidelines.sql` + MedCPT embeddings are loaded. Downstream
deliberation tolerates empty results. Unrelated to `search_clinical_knowledge`
above, which is a real external-API tool.

---

## 6. Dual-LLM Deliberation Engine

### 6-Phase Pipeline

```
Phase 0.5  planner.py            → Pre-deliberation agenda (Haiku, deterministic fallback)
Phase 0    context_compiler.py   → EHR context: 11 data categories, 11K token budget
Phase 1    analyst.py            → Claude Sonnet + GPT-4o in parallel, independent
Phase 2    critic.py             → Cross-critique: each model reviews the other's output
Phase 3    synthesizer.py        → Unified synthesis: 5 output categories
Phase 3.25 synthesis_reviewer.py → Domain review by Haiku; triggers re-deliberation if needed
Phase 3.5  output_safety.py      → Guardrail wrapper: blocks unsafe deliberation outputs
Phase 4    behavioral_adapter.py → SMS/push nudge formatting
Phase 5    knowledge_store.py    → Atomic write to DB: deliberations + patient_knowledge
```

### Invocation Pattern

Three execution modes plus an **elicitation** protocol when the caller wants the
tool to pick (or confirm) the mode based on deliberation history.

| Mode          | Agents                                   | Est. cost | When |
|---------------|------------------------------------------|-----------|------|
| `triage`      | Claude Sonnet only (planner optional)    | ~1 call   | Initial screening, no prior deliberations |
| `progressive` | Haiku loop, tiered demand-fetch context  | 1-5 calls | Follow-up pass with high prior convergence |
| `full`        | Sonnet + GPT-4o + critic + synthesis     | 6-12 calls| Deep re-analysis, low prior convergence |

```python
# ── Direct call: caller knows the mode ────────────────────────────────
await run_deliberation(patient_id="uuid", mode="triage")       # or progressive / full

# ── Two-call elicitation: tool asks the caller ────────────────────────
# Call 1 — omit mode (or pass mode="ask"). Tool inspects deliberations
# history and returns a recommendation + options + selection_token.
offer = await run_deliberation(patient_id="uuid")
# → {"status": "mode_selection_required",
#    "selection_token": "…", "recommended_mode": "triage",
#    "is_initial_run": true, "prior_deliberations": 0,
#    "options": [{mode, description, est_latency_sec, est_llm_calls}, …],
#    "expires_in_sec": 300, "instructions": "…"}

# Call 2 — re-invoke with the chosen mode and the token.
await run_deliberation(
    patient_id="uuid",
    mode=offer["recommended_mode"],
    selection_token=offer["selection_token"],
)

# Poll for results (all modes persist to the deliberations table)
results = await get_deliberation_results(patient_id="uuid")
```

Invalid mode strings now return `{"status": "invalid_mode", …}` instead of
silently falling through to the most expensive (full) path. An expired or
patient-mismatched `selection_token` returns `{"status": "invalid_selection_token"}`.
The token cache is in-memory (5-minute TTL); state loss just forces re-asking.

### Flag Lifecycle

After synthesis, `flag_reviewer.py` (Haiku) screens all deliberation flags:
- Flags with `had_zero_values=True` or `requires_human=True` are held for human review
- Flags can transition: `open` → `retracted` | `needs_review` | `confirmed`
- `get_flag_review_status(patient_id)` shows current lifecycle state

---

## 7. OAuth 2.0 Discovery Layer

**Why it exists**: Claude requires OAuth PKCE before connecting to any remote MCP server. Without these endpoints, Claude reports "server appears to be sleeping".

**How it works** (public server — no actual user login):

```
1. Claude: GET /.well-known/oauth-protected-resource
   → {resource: "https://domain", authorization_servers: ["https://domain"]}

2. Claude: GET /.well-known/oauth-authorization-server
   → {issuer, authorization_endpoint, token_endpoint, registration_endpoint, ...}

3. Claude: POST /register  {redirect_uris: ["https://claude.ai/callback"]}
   → {client_id: "mcp-abc123...", client_secret: "...", ...}

4. Claude opens browser: GET /authorize?response_type=code&client_id=...&code_challenge=...
   → 302 redirect to redirect_uri?code=<code>&state=<state>
   (No login screen — code issued immediately for public server)

5. Claude: POST /token  {grant_type=authorization_code, code=..., client_id=...}
   → {access_token: "...", token_type: "Bearer", expires_in: 86400}

6. Claude: POST /mcp  Authorization: Bearer <token>
   → MCP server accepts (token not validated — public server)
```

State is in `replit-app/lib/oauth-store.ts` (in-memory Map, ephemeral — clients re-authorize on restart).

---

## 8. Database Schema

### Key Tables

```sql
-- biometric_readings (IMPORTANT: column names)
metric_type    VARCHAR   -- NOT "observation_type"
measured_at    TIMESTAMP -- NOT "recorded_at"
UNIQUE INDEX on (patient_id, metric_type, measured_at)  -- idempotent upserts

-- patients
first_name    VARCHAR NULLABLE  -- many HealthEx patients have NULL names
last_name     VARCHAR NULLABLE

-- system_config (data track state)
key   = 'DATA_TRACK'
value = 'synthea' | 'healthex' | 'auto'

-- deliberation_flags (PostgreSQL ENUMs)
lifecycle_state: flag_lifecycle_state  -- open | retracted | needs_review | confirmed
flag_basis:      flag_basis            -- deliberation | rule | manual
priority:        flag_priority         -- critical | high | medium | low
correction_action: correction_action   -- retract | confirm | escalate

-- mcp_call_log (audit log — 35th table)
id           BIGSERIAL PRIMARY KEY
session_id   TEXT            -- UUID, 30-min idle → new UUID per server
server_name  TEXT            -- 'clinical' | 'skills' | 'ingestion'
tool_name    TEXT
called_at    TIMESTAMPTZ DEFAULT NOW()
duration_ms  INT
input_params JSONB
output_text  TEXT            -- first 4000 chars of string output
output_data  JSONB           -- structured output (if result is dict/list)
outcome      TEXT            -- 'success' | 'error'
error_message TEXT
seq          INT             -- call number within session
-- 4 indexes: called_at, session_id, tool_name, (server_name, tool_name)
```

### Migration Order

```
mcp-server/db/schema.sql                            ← 22 base tables
server/deliberation/migrations/001_deliberation_tables.sql  ← deliberations, outputs, knowledge
server/migrations/002_ingestion_plans.sql           ← ingestion_plans + raw_fhir_cache columns
server/migrations/003_transfer_log.sql              ← transfer_log (28 cols)
server/deliberation/migrations/004_flag_lifecycle.sql ← deliberation_flags, reviews, corrections
server/migrations/004_content_router_tables.sql     ← clinical_notes, media_references
server/migrations/005_text_columns.sql              ← TEXT not VARCHAR (UCUM unit codes)
shared/call_recorder.py (runtime CREATE TABLE IF NOT EXISTS)  ← mcp_call_log (35th table)
```

### DB Access in Python

```python
# Always use asyncpg (not SQLAlchemy)
import asyncpg
pool = await asyncpg.create_pool(os.environ["DATABASE_URL"])

# CORRECT: pre-compute date bounds
cutoff = datetime.now() - timedelta(days=30)
await pool.fetch("SELECT * FROM biometric_readings WHERE measured_at > $1", cutoff)

# WRONG: asyncpg can't bind INTERVAL arithmetic
# await pool.fetch("... WHERE measured_at > NOW() - $1 * INTERVAL '1 day'", 30)

# WRONG: 'do' is a reserved PostgreSQL keyword
# "... JOIN deliberation_outputs do ON ..."  ← crashes
# CORRECT:
# "... JOIN deliberation_outputs dout ON ..."

# IMPORTANT: asyncpg returns TIMESTAMPTZ as aware, TIMESTAMP as naive.
# Never compute timedelta with a DB-read datetime without ensure_aware():
from shared.datetime_utils import ensure_aware
row = await pool.fetchrow("SELECT last_ingested_at FROM source_freshness WHERE patient_id=$1", pid)
last_ingested_at = ensure_aware(row["last_ingested_at"])  # safe even if NULL is handled upstream
age = datetime.now(tz=timezone.utc) - last_ingested_at   # TypeError-safe

# CONFIDENCE VALUES: Always normalise before writing to Postgres:
from shared.coercion import coerce_confidence
confidence = coerce_confidence(llm_value)  # float→clamp; int>1→÷100; "high"→0.80; bool→None
await pool.execute("INSERT INTO deliberation_outputs (confidence) VALUES ($1)", confidence)
```

---

## 9. Testing

```bash
# Run individual suites
python -m pytest tests/phase1/ -v                    # 196 tests
python -m pytest tests/phase2/ -v                    # 95 tests
python -m pytest server/deliberation/tests/ -v       # 290+ deliberation unit tests
python -m pytest ingestion/tests/ -v                 # 152 tests
python -m pytest shared/tests/ -v                   # 34 tests (coercion + datetime)
python -m pytest tests/e2e/ -v                       # 28 tests
python -m pytest tests/test_mcp_discovery.py -v      # 26 tests (DN-1–DN-26)
python -m pytest tests/test_mcp_smoke.py -v          # 24 tests
cd mcp-server && python -m pytest tests/ -v          # 110 tests
cd replit-app && npm test                            # 37 Jest tests
cd replit_dashboard && python -m pytest tests/ -v    # 30 tests

# Run all Python tests at once
python -m pytest tests/ server/deliberation/tests/ ingestion/tests/ shared/tests/ -v
```

### pytest configuration (pytest.ini)
```ini
asyncio_mode = auto
addopts = --import-mode=importlib
```

### pytest-asyncio version
```
pytest-asyncio==0.21.2  ← PINNED. Do not upgrade to 1.x — breaks session-scoped event_loop
```

### MCP Discovery Test Suite (DN-1 to DN-26)

```
TestServerNaming        DN-1  to DN-9+  FastMCP names, .mcp.json, HTTPS URLs, generation script
TestHealthCheckContract DN-7  to DN-20  /health on all 3 servers, shape validation
TestStartupTopology     DN-10 to DN-15  start.sh, next.config.ts proxies
TestCrossServerConsistency DN-16 to DN-17  replit.md, submission/README.md
TestOAuthDiscovery      DN-24 to DN-26  OAuth route files, oauth-store.ts, response shape
```

---

## 10. Key Engineering Rules

### asyncpg
- **Never** `$N + INTERVAL '1 day'` — pre-compute bounds in Python, pass as datetime
- **Never** use `do` as a SQL table alias — reserved keyword; use `dout`, `dres`, etc.
- **Never** `$N::uuid` in prepared statements with asyncpg for UUID columns — pass Python `uuid.UUID` directly
- All date arithmetic must happen in Python before passing parameters

### FastMCP
- `FastMCP("name")` — no `description=` kwarg, no `version=` kwarg — causes startup crash
- `@mcp.tool` functions must never use `print()` — use `logging` to `sys.stderr`
- `@mcp.custom_route("/health", methods=["GET"])` — add to all 3 servers for health checks
- Transport: `MCP_TRANSPORT=streamable-http MCP_PORT=800N python -m server.module`

### .mcp.json and OAuth
- `.mcp.json` must use public HTTPS URLs (not `http://localhost:...`)
- `scripts/generate_mcp_json.py` regenerates it from `$REPLIT_DEV_DOMAIN` — called from `start.sh` step 0
- OAuth routes must exist — Claude hits `/.well-known/oauth-protected-resource` before connecting; 404 → "server sleeping"
- `replit-app/lib/oauth-store.ts` state is ephemeral — clients re-authorize on restart (acceptable for dev)

### Deliberation Engine
- `run_deliberation` is async fire-and-forget — always poll `get_deliberation_results`
- `register_healthex_patient` MUST precede `ingest_from_healthex` — bootstraps the `patients` row
- LLMs wrap JSON in ` ```json ``` ` fences despite instructions — `json_utils.strip_markdown_fences()` is applied before every `model_validate_json` in analyst.py, critic.py, synthesizer.py
- Convergence threshold: `>= 0.75` triggers early exit from critic rounds

### Patients Table
- `first_name` / `last_name` are nullable — many HealthEx patients have NULL names
- `get_synthetic_patient` queries the live DB, not a hardcoded dict — uses `patients`, `patient_conditions`, `patient_medications`, `biometric_readings`, `care_gaps`
- Patient CRUD: `POST /api/patients/`, `GET/PUT/DELETE /api/patients/[id]/`

### Skills Server
- `load_skills(mcp)` in `mcp-server/server.py` auto-discovers all `.py` files in `mcp-server/skills/` with a `register(mcp)` function
- Each skill module must export `register(mcp: FastMCP) -> None`; modules without `register()` log a WARNING and are skipped (expected for helpers)
- 21 modules loaded; 22+ tools total (4 from call_history.py, multiple from ingestion_tools.py + behavioral stack)
- `mcp-server/server.py` must insert repo root into `sys.path` at the top — `mcp-server/` is one level deeper than repo root, so `_REPO_ROOT = Path(__file__).resolve().parent.parent`

### AuditMiddleware
- `shared/audit_middleware.py` provides `AuditMiddleware(Middleware)` — a FastMCP middleware subclass
- Attach to any FastMCP server: `mcp.add_middleware(AuditMiddleware(server_name, get_pool_fn))`
  - `server_name`: `"clinical"` | `"skills"` | `"ingestion"`
  - `get_pool_fn`: zero-argument async callable returning the asyncpg pool
- Wired AFTER all `@mcp.tool` registrations and `load_skills()` calls (order matters)
- Lazy init: pool is requested on first tool call, not at module load
- Fire-and-forget: DB write does NOT block the tool response (asyncio.create_task)
- Session tracking via `shared.call_recorder.CallRecorder`: 30-min idle → new session UUID

### shared/ Utilities
- **coerce_confidence(value) → Optional[float]**
  - `bool` → `None` (do not pass True/False as confidence)
  - `float` → clamp to `[0.0, 1.0]`
  - `int` ≤ 1 → cast to float literal (0 → 0.0, 1 → 1.0)
  - `int` > 1 → divide by 100 (interpret as percentage: 85 → 0.85)
  - `str` numeric, integer-valued > 1 → divide by 100; float-valued → clamp
  - `str` categorical → `_CONFIDENCE_MAP`: high→0.80, moderate→0.60, critical→0.95, very high→0.90, low→0.35, none→0.05
  - **Import**: `from shared.coercion import coerce_confidence`
  - **Use at all 5 DB bind sites** in `knowledge_store.py` before writing float columns

- **ensure_aware(dt) → Optional[datetime]**
  - If `dt` is `None` → returns `None`
  - If `dt` is already timezone-aware → returns unchanged
  - If `dt` is naive → attaches `timezone.utc` (assumes UTC, per asyncpg convention)
  - **Import**: `from shared.datetime_utils import ensure_aware`
  - **Use whenever** reading `last_ingested_at`, `measured_at`, or any TIMESTAMP column before timedelta arithmetic

### source_freshness Init Rule
- `register_healthex_patient` writes `last_ingested_at = NULL` (not `NOW()`)
- `_is_stale(None, ttl)` returns `True` immediately → ensures first ingest always fires
- Never write `NOW()` as `last_ingested_at` on registration — that would suppress the required first ingest

### Frontend
- Patient date rendering uses UTC-aware formatting to avoid SSR/client hydration mismatch
- MCP proxy: browser calls `/api/mcp/<port>/tools/<name>` → Next.js proxies to `http://localhost:<port>/tools/<name>`
- OAuth state in `lib/oauth-store.ts` is a module-level `Map` — shared across all requests in the same Node.js process

---

## 11. HealthEx Ingestion Protocol

```
Step 1: use_healthex()                   ← switch DATA_TRACK to 'healthex'
Step 2: register_healthex_patient({...}) ← upsert patients row, returns patient_id UUID
Step 3: ingest_from_healthex(            ← adaptive_parse() → 5 format detection
          patient_id=uuid,
          payload=<FHIR data>)           ← returns format_detected, parser_used, records_written (dict)
Step 4: execute_pending_plans(uuid)      ← write rows, build transfer_log audit trail
Step 5: get_ingestion_plans(uuid)        ← check insights_summary, rows_written per plan
Step 6: get_transfer_audit(uuid)         ← per-record status, timing, error details
Step 7: run_deliberation(uuid, mode)     ← trigger 6-phase deliberation (fire-and-forget)
Step 8: get_deliberation_results(uuid)   ← poll until results ready
Step 9: get_flag_review_status(uuid)     ← check flag lifecycle state
Step 10: get_pending_nudges(uuid)        ← retrieve nudges for delivery
```

**5 payload format parsers** (`ingestion/adapters/healthex/parsers/`):
- Format A: plain text (tab/comma-delimited clinical notes)
- Format B: compressed table (`#` prefix, pipe-separated)
- Format C: flat FHIR text (section headers + key:value)
- Format D: FHIR Bundle JSON
- JSON-dict: `{resource_type: [{...}, ...]}` arrays

---

## 12. Startup Sequence (start.sh)

```bash
# Step 0: Regenerate .mcp.json with current public HTTPS domain
python scripts/generate_mcp_json.py

# Step 1: Start Clinical MCP Server (background)
MCP_TRANSPORT=streamable-http MCP_PORT=8001 python -m server.mcp_server &

# Step 2: Start Skills MCP Server (background)
(cd mcp-server && MCP_TRANSPORT=streamable-http MCP_PORT=8002 python server.py) &

# Step 3: Start Ingestion MCP Server (background)
MCP_TRANSPORT=streamable-http MCP_PORT=8003 python -m ingestion.server &

# Step 4: Start Config Dashboard (background)
(cd replit_dashboard && python server.py) &

# Step 5: Start Next.js (foreground — production build required)
cd replit-app && npm run start
```

For development, each service runs as a separate Replit workflow. `start.sh` is only used for the production deployment run command.

---

## 13. Common Debugging

| Symptom | Cause | Fix |
|---------|-------|-----|
| "server appears to be sleeping" in Claude | `.mcp.json` has `localhost` URLs or OAuth endpoints missing | Run `python scripts/generate_mcp_json.py`; verify all 5 OAuth routes exist |
| `406 Not Acceptable` from `/mcp` | Missing `Accept: application/json, text/event-stream` header | Add both Accept types to the request |
| `model_validate_json` crash in deliberation | LLM returned JSON wrapped in ` ```json ``` ` fences | Ensure `strip_markdown_fences()` is called before parsing |
| `asyncpg.PostgresSyntaxError` with INTERVAL | `$N + INTERVAL` not supported | Pre-compute datetime in Python, pass as parameter |
| `asyncpg.UndefinedColumnError` on biometrics | Using wrong column names | Use `metric_type` + `measured_at` (not `observation_type` / `recorded_at`) |
| `KeyError: 'do'` in SQL query | `do` is a reserved PostgreSQL keyword | Rename table alias to `dout` or similar |
| Deliberation returns no results | `run_deliberation` is async | Poll `get_deliberation_results` after a delay |
| Skills server shows wrong tool count | Skills counted incorrectly | `ingestion_tools.py` alone contributes 8 tools (total = 18) |
| `FastMCP` crash on startup | `description=` or `version=` kwarg used | Remove — FastMCP does not accept these kwargs |

---

## 14. Adding a New MCP Tool

### To Server 1 (clinical server)

```python
# In server/mcp_server.py

@mcp.tool
async def my_new_tool(param1: str, param2: int) -> str:
    """Tool description shown to Claude."""
    # No print() — use logging
    logger.info(f"my_new_tool called with {param1}")
    # DB access
    async with pool.acquire() as conn:
        result = await conn.fetchrow("SELECT ...", param1)
    return json.dumps({"result": str(result)})

# Add REST wrapper (optional, for browser direct-call):
@mcp.custom_route("/tools/my_new_tool", methods=["POST"])
async def rest_my_new_tool(request: Request) -> JSONResponse:
    body = await request.json()
    result = await my_new_tool(**body)
    return JSONResponse(json.loads(result))
```

### To Server 2 (skills server)

```python
# Create mcp-server/skills/my_skill.py

import logging
from fastmcp import FastMCP

logger = logging.getLogger(__name__)

def register(mcp: FastMCP) -> None:
    @mcp.tool
    async def my_skill_tool(patient_id: str) -> str:
        """Skill tool description."""
        logger.info(f"my_skill_tool: {patient_id}", file=sys.stderr)
        return json.dumps({"result": "..."})
```

The skill is auto-discovered by `load_skills(mcp)` on server startup — no registration needed anywhere else.

### Update DN tests

After adding tools, update `tests/test_mcp_discovery.py` if tool counts are asserted anywhere.

---

## 15. Behavioral atom detection — dual-mode system (added 2026-04)

### Architecture overview
The behavioral health surface now has two output modes driven by
`behavioral_phenotypes.evidence_mode`:

- **primary_evidence (Mode B)** — ATOM signals exist, no formal PHQ-9 on
  file. Atoms ARE the clinical evidence. A row is written to
  `behavioral_screening_gaps` and the phenotype is upserted with
  `evidence_mode = 'primary_evidence'`. MIRA / SYNTHESIS surface atoms
  directly to the care team with a screening recommendation.
- **contextual (Mode A)** — PHQ-9 exists. Atoms serve as historical
  context that enriches score interpretation. Atoms never reach the
  patient surface in this mode.

### New tables (migration 010_behavioral_atoms.sql)
- `behavioral_signal_atoms` — extracted behavioral signals from
  clinical notes (pgvector embedding column for Phase 2 RAG)
- `behavioral_screening_gaps` — tracks Mode B patients until a formal
  screen arrives
- `behavioral_phenotypes` — per-patient evidence_mode state
- `phq9_observations` — structured PHQ-9 score history
- `atom_pressure_scores` (materialized view) — time-decayed
  (120-day half-life) signal density per patient

### New MCP tools (ambient-skills-companion)
- `get_behavioral_context(patient_id)` — returns mode-aware behavioral
  context (`mode`, `atoms`, `recommended_instruments`, …)
- `run_behavioral_gap_check(patient_id)` — idempotent gap-detector call;
  safe to run on every note ingest

### Deliberation engine integration
- `server/deliberation/schemas.py::DeliberationResult.behavioral_section`
  — new optional dict field populated post-synthesis.
- `server/deliberation/behavioral_section_builder.py::augment_result_with_behavioral_section`
  — called by `engine.py` between Phase 3.25 and Phase 3.5. Role is
  derived from `context.deliberation_trigger`; defaults to `pcp`.

### Ingestion pipeline hook
- `ingestion/adapters/healthex/executor.py::_post_process_notes_for_atoms`
  runs after `route_and_write_resources()`. Queries clinical_notes rows
  newly written in this batch (by `ingested_at >= start_dt`), calls the
  extractor, inserts atoms, refreshes the pressure-score view, and runs
  the gap detector. Best-effort — never fails the ingest plan.

### Key thresholds (configurable in `behavioral_gap_detector.py`)
- `PRESSURE_THRESHOLD = 2.5` — minimum pressure score to trigger a gap
- `MIN_ATOM_COUNT = 3` — minimum distinct present atoms required
- `SCREENING_LOOKBACK_MONTHS = 12` — stale-screening window

### Temporal confidence levels
- `high` — latest atom < 1 year old
- `moderate` — 1–3 years
- `low` — 3–7 years
- `very_low` — > 7 years (suppressed from patient surface; PCP opt-in required)

### PHI rules for this module
- `signal_value` (the raw extracted phrase) is stored in DB but NEVER logged
- LLM extraction prompts contain NO patient identifiers — only the chunked
  note text and a section-type hint. `patient_id` UUID is used for DB
  linking only
- Mode B outputs never reach the patient surface with clinical framing —
  the patient builder emits a `behavioral_routing` message instead
- `very_low` temporal-confidence atoms never reach the patient companion

### PHQ-9 writer — not yet wired
No PHQ-9 ingest code exists at time of introduction. `resolve_gap_on_new_screening`
is implemented and ready; see the TODO marker in
`mcp-server/skills/behavioral_gap_detector.py`. Until a PHQ-9 writer is added,
`run_behavioral_gap_check` idempotency plus the nightly
`run_batch_gap_detector` keep state consistent.

## 16. Behavioral screening registry — v2 (added 2026-04)

V2 of the ATOM-first module replaces every hardcoded instrument reference
with calls against `mcp-server/skills/screening_registry.py` — a single
data module (17 instruments, 11 domains). Gap detection is now
**domain-based**: a gap fires only when atom pressure implicates a
domain AND no instrument in that domain has been administered within the
domain's lookback window. Multiple simultaneous domain gaps per patient
are supported.

### Domains and instruments (registry-driven)

```
domain                 instruments
────────────────────────────────────────────────────────────────
depression             phq9, phq2
anxiety                gad7, gad2
suicide_risk           cssrs, asq
bipolar                mdq
adhd                   asrs5
trauma                 pcptsd5, pcl5
alcohol_use            auditc (gender-aware cutoff), audit
substance_use          dast10, cagetaid
eating_disorder        scoff
psychosis              prodromal_q
cognitive              mini_cog
```

Adding a new instrument = one entry in `SCREENING_REGISTRY` + (optionally)
a row in `DOMAIN_LOOKBACK_DAYS`. No downstream code changes required.

### SDoH registry

`mcp-server/skills/sdoh_registry.py` mirrors the screening registry for
social-determinants instruments (PRAPARE, AHC-HRSN, Hunger Vital Sign,
WHO-QoL-BREF subset, SEEK). Parsed by the same ingestor; written to
`sdoh_screenings`. Item-level answers are preserved as JSONB.

### Generic ingestion path

`behavioral_screening_ingestor.py` accepts both **`Observation`** and
**`QuestionnaireResponse`** resources, detects the instrument via LOINC
panel, and extracts **item-level answers** (via `QuestionnaireResponse.item[]`
linkIds or `Observation.component[]`). Both total scores and per-item
answers are written — every agent downstream gets the individual answers,
not just the total.

### Migration 011 changes

- New tables: `behavioral_screenings`, `sdoh_screenings`.
- `behavioral_screening_gaps.triggered_domains TEXT[]` column added.
- Legacy `phq9_observations` rows migrated → `behavioral_screenings` then
  table dropped. Entire migration wrapped in one transaction.

### Cards-based resurfacing

`prepare_behavioral_cards(patient_id, role)` is the new MCP tool used by
SYNTHESIS, MIRA, and any future UI agent. It returns a role-filtered
`list[Card]` where every card is a flat dict (`card_id`, `card_type`,
`title`, `subtitle`, `domain`, `priority`, `body_text`, `evidence`,
`actions`, `critical_flags`, `temporal_confidence`, `show_to_roles`,
`source`). Card types emitted: `screening_gap`, `positive_screen`,
`critical_flag`, `sdoh_need`, `behavioral_routing` (patient-side companion
to any Mode B gap).

`DeliberationResult.behavioral_section` is now `list[dict]` (the card
list); the old Mode-A/Mode-B nested dict shape is gone.

### pgvector atom retrieval

`behavioral_signal_atoms.embedding vector(768)` (from migration 010) is
now populated at insert time via `atom_embedder.py`, which picks among
MedCPT (if `MEDCPT_MODEL_PATH` is set), OpenAI `text-embedding-3-small`
(projected to 768d, requires BAA for PHI), or a deterministic hash stub.

New MCP tool: `search_similar_atoms(patient_id, query_text=..., top_k=...,
scope='patient'|'cohort')`. Cohort scope redacts `signal_value` for
cross-patient PHI safety.

### Rules to remember

- NEVER hardcode an instrument abbreviation outside `screening_registry.py`.
  The `grep` guard in `tests/test_atom_detection.py` and the verification
  step in the v2 plan enforce this.
- `run_gap_detector_for_patient` now returns `list[dict]` — iterate.
- `resolve_gap_on_new_screening(conn, patient_id, new_screening_id,
  instrument_key, domain, screening_date)` takes **domain** and
  **instrument_key**, not `total_score`/`item_9_score`.
- Critical items (PHQ-9 item 9, C-SSRS item 3, ASQ item 4, etc.) are
  defined in the registry and flagged automatically at ingest time into
  `behavioral_screenings.triggered_critical` JSONB.
- Card `show_to_roles` is the single source of truth for patient-surface
  filtering. `very_low` temporal confidence + Mode B suppression happen
  there — callers should not re-filter.
