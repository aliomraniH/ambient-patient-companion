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
├── replit-app/          ← Next.js 16 frontend (main web UI)
│   ├── app/             ← App Router pages + API routes
│   ├── components/      ← React UI components
│   └── lib/db.ts        ← PostgreSQL pool (pg)
├── mcp-server/          ← FastMCP Python agent server
│   ├── db/schema.sql    ← 22-table PostgreSQL schema (source of truth)
│   ├── skills/          ← 10 MCP agent skill implementations
│   ├── seed.py          ← Data seeding: python mcp-server/seed.py --patients 10 --months 6
│   ├── orchestrator.py  ← Daily pipeline sequencer
│   └── tests/           ← pytest test suite (44 backend tests)
└── ingestion/           ← Data ingestion service (Synthea FHIR)
```

## Running the App

- **Workflow**: "Start application" runs `cd replit-app && npm run dev` on port 5000
- **Dev server**: Next.js on `0.0.0.0:5000`

## Database

- **Provider**: Replit built-in PostgreSQL
- **Schema**: `mcp-server/db/schema.sql` (22 tables, fully FK-constrained)
- **Connection**: `DATABASE_URL` environment variable (auto-set by Replit)
- **Key constraints**:
  - `is_stale` in `source_freshness` is a regular boolean (not generated — PostgreSQL requires immutable expressions for generated columns)
  - `biometric_readings` has a UNIQUE index on `(patient_id, metric_type, measured_at)` for idempotent inserts

## Environment Variables

- `DATABASE_URL` — PostgreSQL connection string (set automatically by Replit database)
- `PGHOST`, `PGPORT`, `PGUSER`, `PGPASSWORD`, `PGDATABASE` — also set automatically
- `SYNTHEA_OUTPUT_DIR=/home/runner/synthea-output` — FHIR fixture directory
- `DATA_TRACK=synthea` — data track selector

## Seeding Data

```bash
python mcp-server/seed.py --patients 10 --months 6
```

Generates synthetic FHIR fixtures first:
```bash
python mcp-server/scripts/create_minimal_fixtures.py
```

## Testing

### Backend (Python/pytest) — 44 tests
```bash
cd mcp-server && pytest tests/ -v
```

### Frontend (Next.js/Jest) — 37 tests
```bash
cd replit-app && npm test
```

## Package Manager

- Frontend: `npm` (package-lock.json in replit-app/)
- Backend: Python 3.12 (pip / requirements); pytest-asyncio==0.21.2 required

## MCP Skills (10 implemented)

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

## Key Bug Fixes Applied

1. **generate_patient.py**: `birth_date` string→`date` object conversion for asyncpg
2. **compute_obt_score.py**: Pre-computed `target_plus_one` to avoid asyncpg type error with `$N + INTERVAL '1 day'`; returns JSON
3. **crisis_escalation.py**: Same INTERVAL fix; returns JSON with `escalation_triggered` field
4. **pytest-asyncio**: Pinned to 0.21.2 (1.x broke session-scoped event_loop pattern)
5. **schema.sql**: Added FK constraints to 10 previously unlinked tables; added UNIQUE index on biometric_readings

## Config Dashboard (replit_dashboard/)

A standalone FastAPI + vanilla JS control panel for managing API keys, MCP server URLs, and generating Claude Desktop configuration.

### Quick Start

```bash
# Step 1: Install dashboard dependencies
cd replit_dashboard && pip install -r requirements.txt

# Step 2: Start the dashboard server (port 8080)
python server.py
```

The dashboard is now live at `http://localhost:8080`.

### Step-by-Step Walkthrough

1. **Open the dashboard** — navigate to port 8080 in the Replit webview or browser
2. **Enter API keys** — go to the "API Keys" section in the sidebar, fill in `ANTHROPIC_API_KEY` and `CLAUDE_MODEL` (`claude-sonnet-4-6`)
3. **Test connectivity** — click the "Test" button next to Anthropic; a green dot + latency toast confirms success
4. **Add LangSmith** — enter `LANGSMITH_API_KEY` and `LANGSMITH_PROJECT` (`ambient-patient-companion`), click "Test"
5. **Register MCP servers** — go to "MCP Tools", paste each deployed Replit URL (e.g. `https://synthetic-patient.replit.app/mcp`), click "Test" per server
6. **Generate Claude config** — go to "Claude Config", click "Regenerate"; copy the JSON into `claude_desktop_config.json` or use the CLI commands
7. **Export .env** — go to ".env Export", click "Download .env" to get a ready-to-use environment file
8. **Check status** — the "Status" section shows a full service grid; "Setup Guide" lists implementation priorities

### Dashboard API Endpoints

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/` | Serve dashboard HTML |
| GET | `/api/config` | Return all 17 keys (secrets masked) |
| POST | `/api/config` | Save key-value pairs to `.env` |
| GET | `/api/reveal/{key}` | Unmask a single secret key |
| POST | `/api/test/anthropic` | Test Anthropic API connectivity |
| POST | `/api/test/langsmith` | Test LangSmith connectivity |
| POST | `/api/test/mcp/{server_id}` | Test MCP server health |
| GET | `/api/generate/claude-config` | Generate Claude Desktop JSON + CLI commands |
| GET | `/api/export/env` | Preview/download `.env` file |

### Running Dashboard Tests

```bash
cd replit_dashboard && pytest tests/ -v
```

## Key Notes

- Next.js configured with `-p 5000 -H 0.0.0.0` for Replit compatibility
- All DB queries are server-side only (API routes + server components)
- Phase 1: Synthea synthetic data only; Phase 2+ adds HealthEx, device APIs, multi-user auth
- asyncpg rule: never pass Python `date` objects in `$N + INTERVAL` expressions — pre-compute bounds in Python
- MCP rule: never use `print()` in skills — all logging goes to `sys.stderr`
