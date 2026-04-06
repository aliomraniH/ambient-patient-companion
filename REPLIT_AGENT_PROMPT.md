# Replit Agent Prompt — Execute & Test Dual-LLM Deliberation Engine

> **Copy this entire file into Replit Agent.**
> It will set up the environment, run the database migration, start the
> server, and execute all tests for the newly added Deliberation Engine.

---

## Context

Read CLAUDE.md before doing anything. It is the source of truth for this
project.

The Ambient Patient Companion project now has a **Dual-LLM Deliberation
Engine** on branch `claude/dual-llm-deliberation-B3owM`. This is an async
pipeline where Claude (Anthropic) and GPT-4 (OpenAI) independently analyze
a patient's clinical context, cross-critique each other, and synthesize
their combined reasoning into 5 structured output categories:

1. **Anticipatory Scenarios** — clinical scenarios likely in next 30/90/180 days
2. **Predicted Patient Questions** — questions the patient may ask
3. **Missing Data Flags** — data gaps both models identified
4. **Patient/Care Team Nudges** — BCT-formatted behavioral nudges
5. **Knowledge Updates** — accumulated patient-specific knowledge

The feature adds:
- 8 new Python modules in `server/deliberation/`
- 4 new PostgreSQL tables (migration in `server/deliberation/migrations/`)
- 4 new MCP tools + REST endpoints appended to `server/mcp_server.py`
- A new UI panel in `prototypes/components/deliberation-panel.js`
- 32 passing unit/integration tests in `server/deliberation/tests/`

Your job: get everything running, run the database migration, execute
all tests, and verify the MCP server starts with the new tools.

---

## Step 1 — Install Dependencies

```bash
pip install -r requirements.txt
```

Verify the critical new packages are installed:
```bash
python -c "import pydantic; print(f'pydantic {pydantic.__version__}')"
python -c "import openai; print(f'openai {openai.__version__}')"
python -c "import anthropic; print(f'anthropic {anthropic.__version__}')"
```

---

## Step 2 — Set Required Environment Variables (Replit Secrets)

These must be set in the Replit Secrets panel:

| Secret Name | Required | Description |
|---|---|---|
| `ANTHROPIC_API_KEY` | Yes | Anthropic API key (`sk-ant-...`) |
| `OPENAI_API_KEY` | Yes | OpenAI API key (`sk-...`) for GPT-4 deliberation |
| `DATABASE_URL` | Yes | PostgreSQL connection string (Replit provides this if PostgreSQL module is enabled) |

Optional (defaults are fine for testing):
| Secret Name | Default | Description |
|---|---|---|
| `DELIBERATION_ENABLED` | `true` | Enable deliberation engine |
| `DELIBERATION_MAX_ROUNDS` | `3` | Max cross-critique rounds |
| `DELIBERATION_CONVERGENCE_THRESHOLD` | `0.90` | Early-stop threshold |
| `MCP_TRANSPORT` | `streamable-http` | MCP transport mode |
| `MCP_PORT` | `8001` | MCP server port |

---

## Step 3 — Run Database Migration

Run the base schema first if tables don't exist yet:
```bash
psql $DATABASE_URL -f mcp-server/db/schema.sql
```

Then run the deliberation engine migration (4 new tables):
```bash
psql $DATABASE_URL -f server/deliberation/migrations/001_deliberation_tables.sql
```

Verify the tables were created:
```bash
psql $DATABASE_URL -c "SELECT tablename FROM pg_tables WHERE schemaname='public' AND tablename IN ('deliberations','deliberation_outputs','patient_knowledge','core_knowledge_updates') ORDER BY tablename;"
```

Expected: all 4 table names should appear.

---

## Step 4 — Verify Python Imports

```bash
python -c "from server.deliberation.schemas import DeliberationResult; print('schemas OK')"
python -c "from server.deliberation.engine import DeliberationEngine; print('engine OK')"
python -c "from server.deliberation.analyst import run_parallel_analysis; print('analyst OK')"
python -c "from server.deliberation.critic import run_critique_rounds; print('critic OK')"
python -c "from server.deliberation.synthesizer import synthesize; print('synthesizer OK')"
python -c "from server.deliberation.behavioral_adapter import adapt_nudges; print('adapter OK')"
python -c "from server.deliberation.knowledge_store import commit_deliberation; print('store OK')"
```

All 7 should print "OK".

---

## Step 5 — Run the Deliberation Test Suite (Mocked — No API Keys Needed)

```bash
python -m pytest server/deliberation/tests/ -v --tb=short
```

Expected output: **32 passed, 1 skipped**. The skipped test is the live
API integration test, which requires `RUN_LIVE_TESTS=true`.

The tests cover:
- Pydantic schema validation and fixture loading (Maria Chen, MRN 4829341)
- Convergence detection (Jaccard similarity — identical, partial, empty)
- Behavioral adapter (SMS truncation at 160 chars, reading level estimation,
  provider disclaimers automatically appended to patient nudges)
- Knowledge store (DB write with mocked asyncpg pool and transaction)
- Prompt template loading and placeholder substitution
- Analysis round-trip serialization (JSON serialize/deserialize)
- Analysis-from-revision conversion (claude gets diagnostic_reasoning emphasis,
  gpt4 gets treatment_optimization emphasis)

---

## Step 6 — Run Phase 1 Regression Tests

Make sure the deliberation changes didn't break existing functionality:

```bash
python -m pytest tests/phase1/ -v --tb=short
```

Expected: all Phase 1 guardrail tests should still pass (jailbreak blocking,
PHI detection, escalation triggers, drug interactions, screening checks).

---

## Step 7 — Start the MCP Server and Verify New Tools

Start the server:
```bash
MCP_TRANSPORT=streamable-http MCP_PORT=8001 python -m server.mcp_server &
sleep 3
```

Test each of the 4 new REST endpoints:

```bash
# 1. Get deliberation results (should return "no_deliberations_found")
curl -s -X POST http://localhost:8001/tools/get_deliberation_results \
  -H "Content-Type: application/json" \
  -d '{"patient_id": "4829341", "output_type": "all", "limit": 1}'

# 2. Get patient knowledge (should return empty entries)
curl -s -X POST http://localhost:8001/tools/get_patient_knowledge \
  -H "Content-Type: application/json" \
  -d '{"patient_id": "4829341", "knowledge_type": "all"}'

# 3. Get pending nudges (should return 0 pending)
curl -s -X POST http://localhost:8001/tools/get_pending_nudges \
  -H "Content-Type: application/json" \
  -d '{"patient_id": "4829341", "target": "patient"}'

# 4. Verify existing tools still work
curl -s http://localhost:8001/tools/get_synthetic_patient?mrn=4829341
```

All endpoints should return valid JSON (no 500 errors).

Stop the background server when done:
```bash
kill %1 2>/dev/null
```

---

## Step 8 — (Optional) Run Full Live Deliberation

Only run this if both API keys are set and you want a real end-to-end test.
This costs ~$0.15-0.30 per run and takes ~60 seconds:

```bash
# Start the MCP server
MCP_TRANSPORT=streamable-http MCP_PORT=8001 python -m server.mcp_server &
sleep 3

# Trigger a deliberation via REST API
curl -s -X POST http://localhost:8001/tools/run_deliberation \
  -H "Content-Type: application/json" \
  -d '{"patient_id": "4829341", "trigger_type": "manual", "max_rounds": 2}'

# Then retrieve results
curl -s -X POST http://localhost:8001/tools/get_deliberation_results \
  -H "Content-Type: application/json" \
  -d '{"patient_id": "4829341", "output_type": "all", "limit": 1}'

kill %1 2>/dev/null
```

---

## Step 9 — Verify UI Integration

Open the prototype in the Replit webview or browser:
```
prototypes/pcp-encounter.html
```

Check:
1. Two tabs appear: **"Clinical Workspace"** and **"AI Deliberation"**
2. Click "AI Deliberation" tab
3. Panel loads (shows "No deliberation results yet" or results)
4. "Run Deliberation Now" button is visible
5. No JavaScript console errors
6. Tab switching works correctly (clinical content hides, deliberation shows)

---

## Step 10 — Security Verification

```bash
# No hardcoded API keys
grep -r "sk-ant-\|sk-proj-" server/ --include="*.py"

# No PHI in log statements
grep -r "print.*patient_name\|logging.*mrn\|print.*mrn" server/ --include="*.py"

# OPENAI_API_KEY only from env, never hardcoded
grep -r "OPENAI_API_KEY" server/ --include="*.py" | grep -v "os.environ" | grep -v "# "
```

All three should return empty (no matches).

---

## Project Services (for reference)

| Service | Port | Start Command |
|---|---|---|
| Clinical MCP Server | 8001 | `MCP_TRANSPORT=streamable-http MCP_PORT=8001 python -m server.mcp_server` |
| Config Dashboard | 8080 | `cd replit_dashboard && python server.py` |
| Next.js Frontend | 5000 | `cd replit-app && npm run dev` |
| All together | — | `bash start.sh` |

---

## Troubleshooting

| Issue | Fix |
|---|---|
| `ModuleNotFoundError: No module named 'pydantic'` | `pip install pydantic openai anthropic` |
| `openai.OpenAIError: api_key must be set` | Set `OPENAI_API_KEY` in Replit Secrets |
| `psql: command not found` | PostgreSQL module not enabled — check `.replit` has `postgresql-16` |
| `relation "deliberations" does not exist` | Run migration: `psql $DATABASE_URL -f server/deliberation/migrations/001_deliberation_tables.sql` |
| `connection refused` on port 8001 | Server not started, or port conflict — `lsof -i :8001` |
| Tests show `31 passed, 1 failed` | Check traceback — likely an async mock issue |
