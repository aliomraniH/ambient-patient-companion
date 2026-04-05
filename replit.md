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
│   ├── components/      ← React UI components
│   └── lib/db.ts        ← PostgreSQL pool (pg)
├── server/              ← Phase 1 Clinical Intelligence FastMCP server (port 8000)
│   ├── mcp_server.py    ← FastMCP server: 5 tools + REST wrappers + guardrails
│   └── guardrails/      ← input_validator, output_validator, clinical_rules
├── mcp-server/          ← FastMCP Python agent server
│   ├── db/schema.sql    ← 22-table PostgreSQL schema (source of truth)
│   ├── skills/          ← 10 MCP agent skill implementations
│   ├── seed.py          ← Data seeding: python mcp-server/seed.py --patients 10 --months 6
│   ├── orchestrator.py  ← Daily pipeline sequencer
│   └── tests/           ← pytest test suite (44 backend tests)
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

## Workflows (3 active)

| Workflow | Command | Port |
|---------|---------|------|
| Start application | `cd replit-app && npm run dev` | 5000 |
| Config Dashboard | `cd replit_dashboard && python server.py` | 8080 |
| Clinical MCP Server | `MCP_TRANSPORT=streamable-http MCP_PORT=8000 python -m server.mcp_server` | 8000 |

## Phase 1 Clinical Intelligence (server/)

Five tools live at `http://localhost:8000`:

| Endpoint | Tool | Description |
|---------|------|-------------|
| `GET /health` | — | Liveness check |
| `POST /tools/clinical_query` | `clinical_query` | 3-layer guardrail pipeline → Claude |
| `GET /tools/get_guideline` | `get_guideline` | Fetch USPSTF/ADA guideline by ID |
| `POST /tools/check_screening_due` | `check_screening_due` | Overdue screenings for patient |
| `POST /tools/flag_drug_interaction` | `flag_drug_interaction` | Known drug interactions |
| `GET /tools/get_synthetic_patient` | `get_synthetic_patient` | Maria Chen demo patient (MRN 4829341) |

Also accessible via MCP protocol at `http://localhost:8000/mcp` (streamable-http transport).

Guardrails pipeline:
1. **Input**: PHI detection, jailbreak blocking, scope check, emotional tone flag
2. **Escalation rules**: life-threatening, controlled substances, pediatric, pregnancy
3. **Output**: citation check, PHI leakage scan, diagnostic language flags, drug grounding

## Database

- **Provider**: Replit built-in PostgreSQL
- **Schema**: `mcp-server/db/schema.sql` (22 tables, fully FK-constrained)
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
| `MCP_CLINICAL_INTELLIGENCE_URL` | AUTO | Default: `http://localhost:8000/mcp` |
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

### Backend (Python/pytest) — 44 tests
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

**Total: 211 tests, all passing.**

## Package Manager

- Frontend: `npm` (package-lock.json in replit-app/)
- Backend: Python 3.12 (pip / requirements); pytest-asyncio==0.21.2 required

## MCP Skills (10 implemented in mcp-server/)

| Skill | Function |
|-------|----------|
| `generate_patient.py` | Imports FHIR patient bundles into PostgreSQL |
| `generate_vitals.py` | Generates daily biometric readings (idempotent) |
| `generate_checkins.py` | Creates daily check-in records |
| `compute_obt_score.py` | Computes Optimal Being Trajectory scores (returns JSON) |
| `crisis_escalation.py` | Detects crisis indicators (returns JSON with escalation_triggered) |
| `sdoh_assessment.py` | Social Determinants of Health assessment |
| `ingestion_tools.py` | Data freshness checks and source status |
| `previsit_brief.py` | Pre-visit clinical brief generation |
| `food_access_nudge.py` | Food access intervention nudges |
| `compute_provider_risk.py` | Provider-level risk score computation |

## Key Engineering Rules

- **asyncpg**: Never use `$N + INTERVAL '1 day'` — pre-compute bounds in Python
- **MCP skills**: Never use `print()` — all logging goes to `sys.stderr`
- **Model name**: `claude-sonnet-4-5` (hardcoded in mcp_server.py, verified by tests)
- **pytest-asyncio**: Pinned to 0.21.2 — 1.x breaks session-scoped event_loop
- **Replit Secrets**: Take priority over local `.env` in dashboard and connectivity tests
- **Dashboard tests**: `clean_env` fixture pops ALL_KEYS from os.environ — isolates from Replit Secrets
- **Port config**: Next.js=5000, Config Dashboard=8080, Clinical MCP Server=8000

## Key Bug Fixes Applied

1. **generate_patient.py**: `birth_date` string→`date` object conversion for asyncpg
2. **compute_obt_score.py**: Pre-computed `target_plus_one` to avoid asyncpg type error; returns JSON
3. **crisis_escalation.py**: Same INTERVAL fix; returns JSON with `escalation_triggered` field
4. **pytest-asyncio**: Pinned to 0.21.2 (1.x broke session-scoped event_loop pattern)
5. **schema.sql**: Added FK constraints to 10 previously unlinked tables; added UNIQUE index on biometric_readings
6. **dashboard completeness**: Uses `_explicitly_set()` — defaults don't count as user-configured
7. **FHIR fixtures**: 10 minimal Synthea bundles in `/home/runner/synthea-output/fhir/`
