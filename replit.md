# Ambient Patient Companion

A multi-agent AI system that generates a continuously derived patient health UX from Role x Context x Patient State x Time.

## Architecture

```
S = f(R, C, P, T)  →  optimal clinical surface
```

Seven specialized agents communicate through a shared MCP tool registry. All agents read from a local PostgreSQL warehouse. No agent calls an external API directly.

## Project Structure

```
ambient-patient-companion/
├── replit-app/          ← Next.js 16 frontend (main web UI, port 5000)
│   ├── app/             ← App Router pages + API routes
│   │   └── api/mcp/[port]/[[...segments]]/route.ts  ← MCP proxy (→ localhost:8001/2/3)
│   ├── components/      ← React UI components
│   └── lib/db.ts        ← PostgreSQL pool (pg)
├── server/              ← Phase 1 Clinical Intelligence FastMCP server (port 8001)
│   ├── mcp_server.py    ← FastMCP server: 13 tools + REST wrappers + guardrails
│   └── guardrails/      ← input_validator, output_validator, clinical_rules
├── mcp-server/          ← FastMCP Python agent server
│   ├── db/schema.sql    ← 22-table PostgreSQL schema (source of truth)
│   ├── skills/          ← 12 MCP agent skill implementations
│   ├── seed.py          ← Data seeding: python mcp-server/seed.py --patients 10 --months 6
│   ├── orchestrator.py  ← Daily pipeline sequencer
│   └── tests/           ← pytest test suite (49 backend tests)
├── docs/                ← Planning documents (mcp_use_cases.md — story line + action plan)
├── tests/e2e/           ← End-to-end use-case suite (15 tests, all 15 MCP tools)
│   ├── data_entry_agent.py  ← PatientDataEntryAgent: seeds 6 months of Maria Chen history
│   ├── conftest.py          ← Session-scoped DB pool + maria_chen fixture
│   └── test_all_mcp_tools.py ← 15 use-case tests (UC-01 → UC-15)
├── replit_dashboard/    ← FastAPI config dashboard (API keys, MCP URLs, Claude config)
│   ├── server.py        ← FastAPI app (port 8080)
│   ├── index.html       ← Single-page dashboard UI
│   └── tests/           ← 30 dashboard tests (anyio-based)
├── shared/              ← Shared JS client (claude-client.js)
├── prototypes/          ← 4 HTML proof-of-concept prototypes
├── config/system_prompts/ ← Role-based system prompts (pcp, care_manager, patient)
├── tests/phase1/        ← 100 Phase 1 integration tests
├── ingestion/           ← Data ingestion service (Synthea FHIR)
├── CLAUDE.md            ← Full implementation guide for Claude Code agents
└── requirements.txt     ← Root Python dependencies (pytest-asyncio==0.21.2)
```

## Workflows (5 active)

| Workflow | Command | Port |
|---------|---------|------|
| Start application | `cd replit-app && npm run dev` | 5000 |
| Config Dashboard | `cd replit_dashboard && python server.py` | 8080 |
| Clinical MCP Server | `MCP_TRANSPORT=streamable-http MCP_PORT=8001 python -m server.mcp_server` | 8001 |
| Skills MCP Server | `cd mcp-server && MCP_TRANSPORT=streamable-http MCP_PORT=8002 python server.py` | 8002 |
| Ingestion MCP Server | `MCP_TRANSPORT=streamable-http MCP_PORT=8003 python -m ingestion.server` | 8003 |

## Three MCP Servers (all public via Next.js proxy)

| Server | Port | Public Path | Tools | Claude Web Name |
|--------|------|-------------|-------|-----------------|
| ClinicalIntelligence | 8001 | `/mcp` | 9 | `ambient-clinical-intelligence` |
| PatientCompanion (Skills) | 8002 | `/mcp-skills` | 17 | `ambient-skills-companion` |
| PatientIngestion | 8003 | `/mcp-ingestion` | 1 | `ambient-ingestion` |

All three are proxied through Next.js (port 5000) — no port number in public URLs.

### Server 1 — ClinicalIntelligence (`server/mcp_server.py`)

Thirteen tools at `https://[domain]/mcp` (9 Phase 1 + 4 Deliberation):

| Tool | Description |
|------|-------------|
| `clinical_query` | 3-layer guardrail pipeline → Claude |
| `get_guideline` | Fetch USPSTF/ADA guideline by ID |
| `check_screening_due` | Overdue screenings for patient profile |
| `flag_drug_interaction` | Known drug interactions |
| `get_synthetic_patient` | Maria Chen demo patient (MRN 4829341) |
| `use_healthex` | Switch data track to HealthEx real records |
| `use_demo_data` | Switch data track to Synthea demo data |
| `switch_data_track` | Switch to named track (synthea/healthex/auto) |
| `get_data_source_status` | Report active track + available sources |
| `run_deliberation` | Trigger async dual-LLM deliberation for a patient |
| `get_deliberation_results` | Retrieve stored deliberation outputs |
| `get_patient_knowledge` | Fetch accumulated patient-specific knowledge |
| `get_pending_nudges` | List undelivered nudges for patient or care team |

Also has REST wrappers at `/tools/<name>` and liveness check at `/health`.

### Server 2 — PatientCompanion (`mcp-server/server.py`)

Seventeen tools at `https://[domain]/mcp-skills` (auto-discovered from `mcp-server/skills/`):
`compute_obt_score`, `compute_provider_risk`, `run_crisis_escalation`, `run_food_access_nudge`,
`generate_daily_checkins`, `generate_patient`, `generate_daily_vitals`, `generate_previsit_brief`,
`run_sdoh_assessment`, `use_healthex`, `use_demo_data`, `switch_data_track`,
`get_data_source_status`, `check_data_freshness`, `run_ingestion`, `get_source_conflicts`,
`ingest_from_healthex`

### Server 3 — PatientIngestion (`ingestion/server.py`)

One tool at `https://[domain]/mcp-ingestion`:
`trigger_ingestion` — runs the full ETL pipeline for a patient from a named source adapter.

**Claude web MCP config** — download from Config Dashboard (port 8080):
- Dev (always available): `GET /api/generate/mcp-config?env=dev`
- Prod (after deployment): `GET /api/generate/mcp-config?env=prod`
- Full summary (both URLs): `GET /api/generate/claude-config`

Guardrails pipeline:
1. **Input**: PHI detection, jailbreak blocking, scope check, emotional tone flag
2. **Escalation rules**: life-threatening, controlled substances, pediatric, pregnancy
3. **Output**: citation check, PHI leakage scan, diagnostic language flags, drug grounding

## Phase 2 — Dual-LLM Deliberation Engine (`server/deliberation/`)

An async pre-computation layer where Claude (Anthropic) and GPT-4 (OpenAI) independently analyze a patient's clinical context, cross-critique each other, then synthesize into 5 structured output categories:

```
server/deliberation/
├── schemas.py          ← 20 Pydantic models for all data flow
├── engine.py           ← 5-phase pipeline orchestrator
├── context_compiler.py ← Phase 0: assemble patient EHR context
├── analyst.py          ← Phase 1: parallel Claude + GPT-4 analysis
├── critic.py           ← Phase 2: cross-critique with convergence
├── synthesizer.py      ← Phase 3: unified synthesis
├── behavioral_adapter.py ← Phase 4: SMS/nudge formatting
├── knowledge_store.py  ← Phase 5: atomic DB commit
├── prompts/            ← 5 XML prompt templates
└── migrations/001_deliberation_tables.sql  ← 4 new tables
```

4 new DB tables: `deliberations`, `deliberation_outputs`, `patient_knowledge`, `core_knowledge_updates`

UI: `prototypes/pcp-encounter.html` has 2 tabs — **Clinical Workspace** and **AI Deliberation** — with `prototypes/components/deliberation-panel.js` handling the deliberation panel.

## Database

- **Provider**: Replit built-in PostgreSQL
- **Schema**: `mcp-server/db/schema.sql` (22 core tables) + `server/deliberation/migrations/001_deliberation_tables.sql` (4 deliberation tables = 26 total)
- **Connection**: `DATABASE_URL` environment variable (auto-set by Replit)
- **Key constraints**:
  - `is_stale` in `source_freshness` is a regular boolean (not generated — PostgreSQL requires immutable expressions for generated columns)
  - `biometric_readings` has a UNIQUE index on `(patient_id, metric_type, measured_at)` for idempotent inserts

## Environment Variables / Secrets

| Key | Category | Notes |
|-----|----------|-------|
| `ANTHROPIC_API_KEY` | THIRD_PARTY | Replit Secret — used by clinical MCP server |
| `LANGSMITH_API_KEY` | THIRD_PARTY | Replit Secret — optional tracing |
| `DATABASE_URL` | AUTO | Replit PostgreSQL (auto-set) |
| `CLAUDE_MODEL` | AUTO | Default: `claude-sonnet-4-5` |
| `MCP_CLINICAL_INTELLIGENCE_URL` | AUTO | Default: `http://localhost:8001/mcp` |
| `SYNTHEA_OUTPUT_DIR` | AUTO | Default: `/home/runner/synthea-output` |

Config dashboard at port 8080 manages all 18 keys in three categories (AUTO / SELF_HOSTED / THIRD_PARTY).

## Seeding Data

```bash
python mcp-server/seed.py --patients 10 --months 6
```

Generates synthetic FHIR fixtures first:
```bash
python mcp-server/scripts/create_minimal_fixtures.py
```

## Testing

### Phase 1 Clinical Intelligence — 100 tests
```bash
python -m pytest tests/phase1/ -v
```

### Phase 2 Deliberation Engine — 32 unit tests + 50 feature tests
```bash
python -m pytest server/deliberation/tests/ -v          # 32 passed, 1 skipped
python -m pytest tests/phase2/test_deliberation_features.py -v  # 50 passed
```

### End-to-end MCP use-case suite — 15 tests
```bash
python -m pytest tests/e2e/ -v
```

### Backend (Python/pytest) — 49 tests
```bash
cd mcp-server && pytest tests/ -v
```

### Frontend (Next.js/Jest) — 37 tests
```bash
cd replit-app && npm test
```

### Config Dashboard (anyio/pytest) — 30 tests
```bash
cd replit_dashboard && python -m pytest tests/ -v
```

### Ingestion & HealthEx Registration — 23 tests
```bash
python -m pytest ingestion/tests/ -v                        # 16 passed (adapters + pipeline)
python -m pytest ingestion/tests/test_healthex_registration.py -v  # 7 passed (HR-1→HR-7)
```

**Total: 338 tests, all passing**
- 100 Phase 1 clinical intelligence
- 82 Phase 2 deliberation (32 unit + 50 feature)
- 8 e2e deliberation tools (UC-16→UC-20b)
- 48 mcp-server backend
- 37 Next.js frontend
- 30 config dashboard
- 23 ingestion (16 pipeline/adapters + 7 HealthEx HR tests)

## Package Manager

- Frontend: `npm` (package-lock.json in replit-app/)
- Backend: Python 3.12 (pip / requirements); pytest-asyncio==0.21.2 required

## MCP Skills (12 implemented in mcp-server/)

| Skill | Function |
|-------|----------|
| `generate_patient.py` | Imports FHIR patient bundles into PostgreSQL |
| `generate_vitals.py` | Generates daily biometric readings (idempotent) |
| `generate_checkins.py` | Creates daily check-in records |
| `compute_obt_score.py` | Computes Optimal Being Trajectory scores (returns JSON) |
| `crisis_escalation.py` | Detects crisis indicators (returns JSON with escalation_triggered) |
| `sdoh_assessment.py` | Social Determinants of Health assessment |
| `ingestion_tools.py` | Data freshness, source status, `use_healthex()`, `use_demo_data()` |
| `previsit_brief.py` | Pre-visit clinical brief generation |
| `food_access_nudge.py` | Food access intervention nudges |
| `compute_provider_risk.py` | Provider-level risk score computation |

## Key Engineering Rules

- **asyncpg**: Never use `$N + INTERVAL '1 day'` — pre-compute bounds in Python
- **asyncpg**: Never use `do` as a SQL table alias — `do` is a reserved PostgreSQL keyword; use `dout` or similar
- **MCP skills**: Never use `print()` — all logging goes to `sys.stderr`
- **Model name**: `claude-sonnet-4-5` (hardcoded in mcp_server.py, verified by tests)
- **pytest-asyncio**: Pinned to 0.21.2 — 1.x breaks session-scoped event_loop
- **Replit Secrets**: Take priority over local `.env` in dashboard and connectivity tests
- **Dashboard tests**: `clean_env` fixture pops ALL_KEYS from os.environ — isolates from Replit Secrets
- **Port config**: Next.js=5000, Config Dashboard=8080, Clinical MCP=8001, Skills MCP=8002, Ingestion MCP=8003
- **FastMCP**: `FastMCP()` does NOT accept `description=` kwarg — causes startup crash
- **Deliberation**: `run_deliberation` is async fire-and-forget — poll `get_deliberation_results` for output
- **MCP Proxy**: Browser calls `/api/mcp/<port>/tools/<name>` → Next.js route proxies to `http://localhost:<port>/tools/<name>`; shared/claude-client.js uses relative `/api/mcp/8001` in browser context
- **HealthEx Protocol**: `register_healthex_patient` MUST be called before `ingest_from_healthex` — it bootstraps the `patients` row that `run_deliberation` requires. See CLAUDE.md Section 13.
- **Synthea fixtures**: `mcp-server/tests/fixtures/fhir/` holds 3 minimal FHIR bundles; conftest.py sets `SYNTHEA_OUTPUT_DIR` to fixtures when `/home/runner/synthea-output/fhir/` is absent

## Key Bug Fixes Applied

1. **get_pending_nudges SQL**: `do` is a reserved PostgreSQL keyword — renamed table alias to `dout` in deliberation JOIN query
2. **generate_patient.py**: `birth_date` string→`date` object conversion for asyncpg
3. **compute_obt_score.py**: Pre-computed `target_plus_one` to avoid asyncpg type error; returns JSON
4. **crisis_escalation.py**: Same INTERVAL fix; returns JSON with `escalation_triggered` field
5. **pytest-asyncio**: Pinned to 0.21.2 (1.x broke session-scoped event_loop pattern)
6. **schema.sql**: Added FK constraints to 10 previously unlinked tables; added UNIQUE index on biometric_readings
7. **dashboard completeness**: Uses `_explicitly_set()` — defaults don't count as user-configured
8. **FHIR fixtures**: 10 minimal Synthea bundles in `/home/runner/synthea-output/fhir/`
