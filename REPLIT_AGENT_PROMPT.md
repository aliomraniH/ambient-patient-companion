# Replit Agent — Deploy & Test Dual-LLM Deliberation Engine

Read CLAUDE.md and replit.md before doing anything. They are the source of
truth for this project.

---

## What Changed

Branch `claude/dual-llm-deliberation-B3owM` adds a **Dual-LLM Deliberation
Engine** to the Ambient Patient Companion. This is an async pre-computation
layer where Claude (Anthropic) and GPT-4 (OpenAI) independently analyze a
patient's clinical context, cross-critique each other through structured
debate rounds, then synthesize their combined reasoning into 5 structured
output categories that surface on the provider dashboard.

### New Files

```
server/deliberation/               ← NEW PACKAGE (8 modules)
├── __init__.py
├── schemas.py                     ← 20 Pydantic models for all data flow
├── engine.py                      ← 5-phase pipeline orchestrator
├── context_compiler.py            ← Phase 0: assemble patient EHR context
├── analyst.py                     ← Phase 1: parallel Claude + GPT-4 analysis
├── critic.py                      ← Phase 2: cross-critique with convergence
├── synthesizer.py                 ← Phase 3: unified synthesis
├── behavioral_adapter.py          ← Phase 4: SMS/nudge formatting
├── knowledge_store.py             ← Phase 5: atomic DB commit
├── prompts/                       ← 5 XML prompt templates
│   ├── analyst_claude.xml
│   ├── analyst_gpt4.xml
│   ├── critic_claude.xml
│   ├── critic_gpt4.xml
│   └── synthesizer.xml
├── migrations/
│   └── 001_deliberation_tables.sql  ← 4 new PostgreSQL tables
└── tests/                         ← 32 unit/integration tests
    ├── fixtures/maria_chen_context.json
    ├── test_engine_integration.py
    ├── test_analyst.py
    ├── test_context_compiler.py
    ├── test_critic.py
    ├── test_synthesizer.py
    └── test_knowledge_store.py

prototypes/components/
└── deliberation-panel.js          ← UI widget for provider dashboard

tests/phase2/
└── test_deliberation_features.py  ← 50 comprehensive feature tests

.env.example                       ← All required environment variables
```

### Modified Files

```
server/mcp_server.py               ← 4 new MCP tools + 4 REST wrappers appended
prototypes/pcp-encounter.html      ← Tab navigation + deliberation panel integration
requirements.txt                   ← Added: openai, pydantic
CLAUDE.md                          ← Section 8b: deliberation engine docs
```

### 4 New Database Tables

- `deliberations` — session record with convergence score and transcript
- `deliberation_outputs` — five output categories per deliberation
- `patient_knowledge` — accumulated patient-specific knowledge
- `core_knowledge_updates` — shared clinical knowledge reinforcements

### 4 New MCP Tools (+ REST endpoints)

| Tool | REST Endpoint | Method |
|---|---|---|
| `run_deliberation` | `/tools/run_deliberation` | POST |
| `get_deliberation_results` | `/tools/get_deliberation_results` | POST |
| `get_patient_knowledge` | `/tools/get_patient_knowledge` | POST |
| `get_pending_nudges` | `/tools/get_pending_nudges` | POST |

---

## Deployment Steps

Execute these steps in order. Do not skip any step.

### 1. Pull the branch and install dependencies

```bash
git fetch origin claude/dual-llm-deliberation-B3owM
git checkout claude/dual-llm-deliberation-B3owM
pip install -r requirements.txt
pip install -r replit_dashboard/requirements.txt
```

Verify critical packages:
```bash
python -c "import pydantic; print(f'pydantic {pydantic.__version__}')"
python -c "import openai; print(f'openai {openai.__version__}')"
python -c "import anthropic; print(f'anthropic {anthropic.__version__}')"
python -c "import asyncpg; print(f'asyncpg {asyncpg.__version__}')"
python -c "import fastmcp; print(f'fastmcp {fastmcp.__version__}')"
```

All 5 must print version numbers without error.

### 2. Configure Replit Secrets

Set these in the Replit Secrets panel (or `.env` file):

| Secret | Required | Value |
|---|---|---|
| `ANTHROPIC_API_KEY` | Yes | Your Anthropic API key |
| `OPENAI_API_KEY` | Yes | Your OpenAI API key |
| `DATABASE_URL` | Yes | PostgreSQL connection string (Replit auto-provides if PostgreSQL module is enabled) |

These are optional with sensible defaults:
| Secret | Default |
|---|---|
| `MCP_TRANSPORT` | `streamable-http` |
| `MCP_PORT` | `8001` |
| `DELIBERATION_ENABLED` | `true` |
| `DELIBERATION_MAX_ROUNDS` | `3` |
| `DELIBERATION_CONVERGENCE_THRESHOLD` | `0.90` |

### 3. Run database migrations

Run the base schema first (skip if tables already exist):
```bash
psql $DATABASE_URL -f mcp-server/db/schema.sql
```

Then run the new deliberation tables:
```bash
psql $DATABASE_URL -f server/deliberation/migrations/001_deliberation_tables.sql
```

Verify all 4 new tables exist:
```bash
psql $DATABASE_URL -c "SELECT tablename FROM pg_tables WHERE schemaname='public' AND tablename IN ('deliberations','deliberation_outputs','patient_knowledge','core_knowledge_updates') ORDER BY tablename;"
```

Expected output: 4 rows listing all table names.

### 4. Verify all Python imports

```bash
python -c "from server.deliberation.schemas import DeliberationResult; print('OK')"
python -c "from server.deliberation.engine import DeliberationEngine; print('OK')"
python -c "from server.deliberation.analyst import run_parallel_analysis; print('OK')"
python -c "from server.deliberation.critic import run_critique_rounds; print('OK')"
python -c "from server.deliberation.synthesizer import synthesize; print('OK')"
python -c "from server.deliberation.behavioral_adapter import adapt_nudges; print('OK')"
python -c "from server.deliberation.knowledge_store import commit_deliberation; print('OK')"
```

All 7 must print "OK".

### 5. Run the deliberation unit tests (no API keys needed)

```bash
python -m pytest server/deliberation/tests/ -v --tb=short
```

**Expected: 32 passed, 1 skipped.**
The skipped test is the live API test (requires `RUN_LIVE_TESTS=true`).

### 6. Run the comprehensive Phase 2 feature tests

```bash
python -m pytest tests/phase2/test_deliberation_features.py -v --tb=short
```

**Expected: 50 passed.**
Covers: schema validation, fixtures, prompt loading, convergence detection,
synthesizer outputs, behavioral adaptation, knowledge store, MCP tool imports,
and security compliance.

### 7. Run Phase 1 regression tests

```bash
python -m pytest tests/phase1/ -v --tb=short
```

All existing guardrail tests must still pass (jailbreak blocking, PHI detection,
escalation triggers, drug interactions, screening checks).

### 8. Start the MCP server and test REST endpoints

```bash
MCP_TRANSPORT=streamable-http MCP_PORT=8001 python -m server.mcp_server &
MCP_PID=$!
sleep 3
echo "MCP server running on PID $MCP_PID"
```

Test each new endpoint:

```bash
# Get deliberation results (should return "no_deliberations_found")
curl -s -X POST http://localhost:8001/tools/get_deliberation_results \
  -H "Content-Type: application/json" \
  -d '{"patient_id": "4829341", "output_type": "all", "limit": 1}'

# Get patient knowledge (should return empty entries)
curl -s -X POST http://localhost:8001/tools/get_patient_knowledge \
  -H "Content-Type: application/json" \
  -d '{"patient_id": "4829341", "knowledge_type": "all"}'

# Get pending nudges (should return 0 pending)
curl -s -X POST http://localhost:8001/tools/get_pending_nudges \
  -H "Content-Type: application/json" \
  -d '{"patient_id": "4829341", "target": "patient"}'

# Verify existing tools still work
curl -s http://localhost:8001/tools/get_synthetic_patient?mrn=4829341
```

All 4 must return valid JSON responses (no 500 errors). Then stop the server:
```bash
kill $MCP_PID 2>/dev/null
```

### 9. Start all services together

```bash
bash start.sh
```

This starts 3 services in parallel:
- Clinical MCP Server on port 8001
- Config Dashboard on port 8080
- Next.js frontend on port 5000

Verify all 3 respond:
```bash
curl -s http://localhost:8001/tools/get_synthetic_patient?mrn=4829341 | head -c 100
curl -s http://localhost:8080/ | head -c 100
curl -s http://localhost:5000/ | head -c 100
```

### 10. Verify UI integration

Open `prototypes/pcp-encounter.html` in the browser. Confirm:

1. Two tabs at top: **"Clinical Workspace"** and **"AI Deliberation"**
2. Clinical Workspace shows the existing CDS panel (Maria Chen, vitals, care gaps)
3. Click **"AI Deliberation"** tab — deliberation panel loads
4. Panel shows "No deliberation results yet" with a "Run Deliberation Now" button
5. No JavaScript console errors
6. Tab switching hides/shows content correctly

### 11. (Optional) Run a live end-to-end deliberation

Only if both API keys are set and you want to test with real LLM calls.
Costs ~$0.15-0.30 per run, takes ~60 seconds:

```bash
MCP_TRANSPORT=streamable-http MCP_PORT=8001 python -m server.mcp_server &
sleep 3

curl -s -X POST http://localhost:8001/tools/run_deliberation \
  -H "Content-Type: application/json" \
  -d '{"patient_id": "4829341", "trigger_type": "manual", "max_rounds": 2}'

# Wait for it to complete, then retrieve results:
curl -s -X POST http://localhost:8001/tools/get_deliberation_results \
  -H "Content-Type: application/json" \
  -d '{"patient_id": "4829341", "output_type": "all", "limit": 1}'

kill %1 2>/dev/null
```

### 12. Security verification

```bash
# No hardcoded API keys in any Python file
grep -r "sk-ant-\|sk-proj-" server/ --include="*.py"

# No PHI in log statements
grep -r "print.*patient_name\|logging.*mrn\|print.*mrn" server/ --include="*.py"

# OPENAI_API_KEY only read from environment
grep -r "OPENAI_API_KEY" server/ --include="*.py" | grep -v "os.environ" | grep -v "# "
```

All 3 commands must return empty (no matches).

---

## Success Criteria

All of the following must be true before marking deployment complete:

- [ ] `pip install -r requirements.txt` succeeds
- [ ] 4 new database tables exist in PostgreSQL
- [ ] All 7 deliberation module imports return "OK"
- [ ] `pytest server/deliberation/tests/` → **32 passed, 1 skipped**
- [ ] `pytest tests/phase2/` → **50 passed**
- [ ] `pytest tests/phase1/` → all passed (regression)
- [ ] 4 new REST endpoints return valid JSON (no 500s)
- [ ] Existing `/tools/get_synthetic_patient` still works
- [ ] `bash start.sh` starts all 3 services without crash
- [ ] pcp-encounter.html shows 2 tabs, deliberation panel loads
- [ ] Security checks return no matches

---

## Troubleshooting

| Problem | Solution |
|---|---|
| `ModuleNotFoundError: No module named 'pydantic'` | `pip install pydantic openai anthropic asyncpg fastmcp` |
| `openai.OpenAIError: api_key must be set` | Set `OPENAI_API_KEY` in Replit Secrets |
| `psql: command not found` | Enable PostgreSQL module in `.replit` (modules line) |
| `relation "deliberations" does not exist` | Run: `psql $DATABASE_URL -f server/deliberation/migrations/001_deliberation_tables.sql` |
| `relation "patients" does not exist` | Run base schema first: `psql $DATABASE_URL -f mcp-server/db/schema.sql` |
| Port 8001 in use | `lsof -i :8001` then `kill <PID>` |
| `ImportError: cannot import name 'run_deliberation'` | Make sure you're on the right branch: `git checkout claude/dual-llm-deliberation-B3owM` |
| Tests show warnings about coroutines | These are harmless mock warnings — tests still pass |

---

## Architecture Reference

```
┌─────────────────────────────────────────────────────────┐
│  Layer 1: Prototypes (pcp-encounter.html)                │
│  ├── Clinical Workspace tab (Phase 1 — existing)         │
│  └── AI Deliberation tab (Phase 2 — NEW)                 │
│       └── deliberation-panel.js                          │
│            calls _mcpPost("/tools/run_deliberation")     │
│            calls _mcpPost("/tools/get_deliberation_results") │
│            calls _mcpPost("/tools/get_pending_nudges")   │
└──────────────────────┬──────────────────────────────────┘
                       │ HTTP (port 8001)
┌──────────────────────▼──────────────────────────────────┐
│  Layer 2: FastMCP Server (server/mcp_server.py)          │
│  13 tools total: 9 Phase 1 + 4 Deliberation (NEW)       │
│  3-layer guardrail pipeline (input → Claude → output)    │
└──────────────────────┬──────────────────────────────────┘
                       │
┌──────────────────────▼──────────────────────────────────┐
│  Layer 3: Deliberation Engine (server/deliberation/)     │
│  Phase 0: Context Compilation (EHR data assembly)        │
│  Phase 1: Parallel Analysis (Claude + GPT-4)             │
│  Phase 2: Cross-Critique (up to 3 rounds, convergence)   │
│  Phase 3: Unified Synthesis → 5 output categories        │
│  Phase 4: Behavioral Adaptation (SMS/nudges/reading lvl) │
│  Phase 5: Knowledge Commit (atomic write to 4 tables)    │
└──────────────────────┬──────────────────────────────────┘
                       │
┌──────────────────────▼──────────────────────────────────┐
│  Layer 4: PostgreSQL (26 tables: 22 existing + 4 NEW)    │
│  NEW: deliberations | deliberation_outputs               │
│  NEW: patient_knowledge | core_knowledge_updates         │
└─────────────────────────────────────────────────────────┘
```
