# Replit Agent: Deploy Progressive Context Loading for Deliberation

Read CLAUDE.md and replit.md before doing anything. They are the source of
truth for this project.

## What Changed

Branch `claude/progressive-context-loading-gfLyD` adds a three-tier progressive
context loading system to the Dual-LLM Deliberation Engine. This fixes a crash
where `run_deliberation` produces a 16,000+ char context that breaks `json.loads()`
on unescaped quotes, and replaces the monolithic context dump with a demand-fetch
loop where the agent starts with minimal data and requests more as needed.

### New Files (5)

| File | Purpose |
|------|---------|
| `server/deliberation/tiered_context_loader.py` | `TieredContextLoader` class — loads patient data in 3 budget-capped tiers |
| `server/deliberation/data_request_parser.py` | Parses deliberation round output for `data_requests` and `missing_data_flags` |
| `server/deliberation/migrations/002_data_requests.sql` | New `deliberation_data_requests` table (per-round request tracking) |
| `server/deliberation/tests/test_tiered_context_loader.py` | 26 unit tests for the tiered loader |
| `server/deliberation/tests/test_data_request_parser.py` | 20 unit tests for the data request parser |

### Modified Files (2)

| File | Changes |
|------|---------|
| `server/deliberation/engine.py` | Added `run_progressive()` method to `DeliberationEngine` — progressive deliberation loop with tiered loading, lightweight single-model rounds (Claude Haiku), synthesis, and DB commit. Original `run()` preserved for backward compatibility. |
| `server/mcp_server.py` | `run_deliberation` MCP tool now defaults to `mode="progressive"` (tiered loading). `mode="full"` still available for original dual-LLM pipeline. REST wrapper updated to pass `mode` parameter. |

### Architecture: Three Tiers + Demand-Fetch Loop

```
TIER 1 — Critical structured data (always loaded, ~1,500 chars)
  Active conditions with onset dates
  Most recent value per distinct lab/metric type
  Last encounter: date + type + description
  Active medications
  Data inventory + available media references
  → Deliberation starts here.

TIER 2 — Trend data (loaded when agent signals gaps, ~6,000 chars)
  Full lab/biometric history for flagged tests
  All encounters past 2 years
  Condition timeline (all including inactive)
  → Only loaded when round output contains missing_data_flags

TIER 3 — Narrative detail (on-demand by resource_id, ~4,000 chars)
  Clinical note text (from clinical_notes table, sanitized)
  Imaging report text (specific observation by id)
  Lab trend for a specific test
  Specific encounter detail
  → Only loaded when agent emits explicit data_request

TOTAL BUDGET: 11,000 chars (~2,750 tokens) — well below the 16,190 crash zone.
Binary HTML/RTF blobs are NEVER loaded into context directly.
```

### Progressive Deliberation Flow

```
BEFORE (broken):
  run_deliberation() → context compiler dumps everything → 16,413 chars → json.loads() crash

AFTER (working):
  run_deliberation(mode="progressive")
    Round 1: Tier 1 only (~1,800 chars) → deliberation runs clean
    Agent outputs: missing_data_flags: [A1c placeholder, imaging needed]
    DataRequestParser: load_tier2=True, on_demand=[imaging_report]
    Round 2: Tier 1 + Tier 2 (~6,000 chars) + imaging note (~800 chars)
    Agent outputs: full scenarios with lab trend data + ultrasound findings
    No further data_requests → deliberation complete
    2 rounds, ~8,600 chars, context crash eliminated structurally
```

---

## Step 1: Pull the Branch

```bash
git fetch origin claude/progressive-context-loading-gfLyD
git checkout claude/progressive-context-loading-gfLyD
```

---

## Step 2: Apply Database Migration

Run the migration against the Replit PostgreSQL database. This is safe to run
multiple times (`IF NOT EXISTS`).

```bash
psql $DATABASE_URL -f server/deliberation/migrations/002_data_requests.sql
```

**What it creates:**
- `deliberation_data_requests` table with 12 columns (request tracking per deliberation round)
- 1 index on `(deliberation_id, round_number)`

**Verify the migration:**
```bash
psql $DATABASE_URL -c "\d deliberation_data_requests"
```

Expected columns: `id` (UUID), `deliberation_id` (UUID), `round_number` (INT),
`request_type` (VARCHAR), `resource_id` (VARCHAR), `date_from` (DATE),
`date_to` (DATE), `reason` (TEXT), `fulfilled` (BOOLEAN), `fulfilled_chars` (INT),
`requested_at` (TIMESTAMPTZ), `fulfilled_at` (TIMESTAMPTZ).

---

## Step 3: Install Dependencies (if needed)

No new dependencies were added. The new code uses only `anthropic` and standard
library modules already in requirements.txt. Verify:

```bash
pip install -r requirements.txt
```

---

## Step 4: Restart the Clinical MCP Server

The Clinical MCP Server (port 8001) must be restarted to pick up the updated
`run_deliberation` tool signature (new `mode` parameter):

```bash
# Kill existing server
pkill -f "server.mcp_server" || true

# Restart
MCP_TRANSPORT=streamable-http MCP_PORT=8001 python -m server.mcp_server &
```

**Verify the server is healthy:**
```bash
curl -s http://localhost:8001/health | python -m json.tool
```

The tool count should be 18 (unchanged — no new tools added, only the
`run_deliberation` tool signature was extended with a `mode` parameter).

---

## Step 5: Run New Unit Tests

These are the 46 new tests added by this branch. They use mocked DB connections
and do not require a live database or API keys.

```bash
# Tiered context loader tests (26 tests)
python -m pytest server/deliberation/tests/test_tiered_context_loader.py -v

# Data request parser tests (20 tests)
python -m pytest server/deliberation/tests/test_data_request_parser.py -v
```

All 46 tests should pass.

---

## Step 6: Run Existing Tests (Regression Check)

Ensure nothing broke. The following tests do NOT require pydantic or API keys:

```bash
# Deliberation JSON utils (verify strip_markdown_fences still works)
python -m pytest server/deliberation/tests/test_json_utils.py -v

# Adaptive ingestion pipeline
python -m pytest ingestion/tests/ -v --tb=short -q 2>&1 | tail -5

# Phase 1 integration
python -m pytest tests/phase1/ -v --tb=short -q 2>&1 | tail -5

# Phase 2 deliberation features
python -m pytest tests/phase2/ -v --tb=short -q 2>&1 | tail -5

# E2E (skip if servers not running)
python -m pytest tests/e2e/ -v --tb=short -q 2>&1 | tail -5
```

Note: Some deliberation tests (test_analyst.py, test_critic.py, etc.) require
pydantic installed. If pydantic is not available in the test runner, those
tests will show import errors — this is pre-existing and not caused by this branch.

---

## Step 7: Live Smoke Tests

With the Clinical MCP Server running on port 8001 and DATABASE_URL set, test
the progressive deliberation flow.

### 7a. Test progressive mode (default)

```bash
curl -s -X POST http://localhost:8001/tools/run_deliberation \
  -H "Content-Type: application/json" \
  -d '{
    "patient_id": "MC-2025-4829",
    "trigger_type": "manual",
    "max_rounds": 2
  }' | python -m json.tool
```

**Expected response structure:**
```json
{
  "deliberation_id": "<uuid>",
  "status": "complete",
  "patient_id": "MC-2025-4829",
  "rounds_completed": 2,
  "context_stats": {
    "chars_used": 8600,
    "chars_budget": 11000,
    "pct_used": 78.2,
    "tiers_loaded": [1, 2]
  },
  "summary": {
    "anticipatory_scenarios": 3,
    "predicted_questions": 2,
    "missing_data_flags": 1,
    "nudges_generated": 2,
    "knowledge_updates": 0
  }
}
```

**Key assertions:**
- `status` is `"complete"` (not `"error"`)
- `context_stats.chars_used` < 11,000 (within budget)
- `context_stats.chars_used` < 16,190 (below crash threshold)
- `rounds_completed` >= 1
- `context_stats.tiers_loaded` includes `1` (tier 1 always loaded)

If the patient `MC-2025-4829` does not exist in the DB, the response will
contain `"status": "error"` with `"Patient ... not found"`. In that case,
use whatever patient MRN exists in the `patients` table:

```bash
psql $DATABASE_URL -c "SELECT mrn, first_name, last_name FROM patients LIMIT 5"
```

### 7b. Test full mode (backward compatibility)

```bash
curl -s -X POST http://localhost:8001/tools/run_deliberation \
  -H "Content-Type: application/json" \
  -d '{
    "patient_id": "MC-2025-4829",
    "trigger_type": "manual",
    "max_rounds": 2,
    "mode": "full"
  }' | python -m json.tool
```

This should use the original dual-LLM pipeline. The response will have
`convergence_score` instead of `context_stats`. Requires both
`ANTHROPIC_API_KEY` and `OPENAI_API_KEY` to be set.

### 7c. Verify data requests are tracked in DB

After a successful progressive deliberation:

```bash
psql $DATABASE_URL -c "SELECT deliberation_id, round_number, request_type, fulfilled, fulfilled_chars FROM deliberation_data_requests ORDER BY requested_at DESC LIMIT 10"
```

If the table has rows, it means the progressive loop successfully parsed agent
output for data requests and recorded them.

### 7d. Test all 4 trigger types (if patient exists)

```bash
for trigger in manual scheduled_pre_encounter lab_result_received temporal_threshold; do
  echo "=== $trigger ==="
  curl -s -X POST http://localhost:8001/tools/run_deliberation \
    -H "Content-Type: application/json" \
    -d "{\"patient_id\": \"MC-2025-4829\", \"trigger_type\": \"$trigger\", \"max_rounds\": 2}" \
    | python -m json.tool | grep -E '"status"|"chars_used"|"rounds_completed"'
  echo
done
```

All 4 triggers should return `"status": "complete"` with `chars_used` < 11,000.

---

## Step 8: Update replit.md

Add to the **Phase 2 — Dual-LLM Deliberation Engine** section:

```markdown
### Progressive Context Loading (Run 12)

The deliberation engine supports two modes via `run_deliberation(mode=...)`:

| Mode | Description | Default |
|------|-------------|---------|
| `progressive` | Three-tier demand-fetch loop. Starts with Tier 1 (~1,500 chars), loads Tier 2/3 on demand. Uses Claude Haiku for fast rounds. | **Yes** |
| `full` | Original dual-LLM pipeline (Claude + GPT-4 cross-critique). Loads all context upfront. | No |

New files:
- `server/deliberation/tiered_context_loader.py` — budget-capped tier loading
- `server/deliberation/data_request_parser.py` — parses agent output for fetch signals

New migration: `server/deliberation/migrations/002_data_requests.sql` — `deliberation_data_requests` table (31 total tables)
```

Update the test count in the Testing section:
- Phase 2 Deliberation Engine: 40 → 86 unit tests (added 26 loader + 20 parser)
- Total: 586 → 632 tests

Update the Database section total:
- 30 total → 31 total tables

---

## Step 9: Verify Full Test Suite

Run all test suites and confirm no regressions:

```bash
# New progressive loading tests (must all pass: 46 tests)
python -m pytest server/deliberation/tests/test_tiered_context_loader.py server/deliberation/tests/test_data_request_parser.py -v

# Deliberation JSON utils (8 tests)
python -m pytest server/deliberation/tests/test_json_utils.py -v

# Core ingestion
python -m pytest ingestion/tests/ -v --tb=short -q 2>&1 | tail -5

# Phase 1 integration
python -m pytest tests/phase1/ -v --tb=short -q 2>&1 | tail -5

# Phase 2 features
python -m pytest tests/phase2/ -v --tb=short -q 2>&1 | tail -5

# E2E (skip if servers not running)
python -m pytest tests/e2e/ -v --tb=short -q 2>&1 | tail -5
```

---

## Step 10: Final Verification Checklist

Report pass/fail for each item:

```
[ ] Migration applied: deliberation_data_requests table exists
[ ] New unit tests: 46/46 passing (26 loader + 20 parser)
[ ] Existing JSON utils tests: 8/8 passing
[ ] Existing ingestion tests: passing (report count)
[ ] Existing Phase 1 tests: passing (report count)
[ ] Existing Phase 2 tests: passing (report count)
[ ] Clinical server starts on port 8001 without errors
[ ] run_deliberation (progressive mode): returns status=complete
[ ] run_deliberation (progressive mode): context_stats.chars_used < 11,000
[ ] run_deliberation (progressive mode): rounds_completed >= 1
[ ] run_deliberation (full mode): still works (if API keys present)
[ ] deliberation_data_requests table has rows after progressive run
[ ] All 4 trigger types complete without errors
[ ] replit.md updated with progressive context loading documentation
[ ] Total DB tables: 31 (was 30)
```

---

## Constraints — do not violate these

- Do NOT modify `context_compiler.py` — it is still used by the `run()` method
  (full mode) and must remain backward-compatible
- Do NOT remove the original `run()` method from `engine.py` — `mode="full"`
  depends on it
- Do NOT use claude-opus-* models — progressive rounds use `claude-haiku-4-5-20251001`
- Do NOT increase TOTAL_BUDGET above 11,000 chars — this is the safety margin
  below the 16,190 crash threshold
- Do NOT load raw `raw_fhir_cache` blobs into deliberation context — only
  sanitized `note_text` from `clinical_notes` table is loaded on-demand
- If `ANTHROPIC_API_KEY` is not set, progressive mode will fail at the LLM
  call step (expected behavior). The migration and unit tests still work.
- If a patient MRN does not exist in the DB, `run_deliberation` returns
  `"status": "error"` with a descriptive message — this is correct behavior,
  not a bug

---

## Architecture Summary

```
BEFORE (monolithic — 16KB crash):
  run_deliberation()
    → compile_patient_context() loads ALL data (notes, labs, media, encounters)
    → 16,413 chars context
    → json.loads() crash on unescaped quote at char 16,190

AFTER (progressive — budget-capped):
  run_deliberation(mode="progressive")
    Round 1: Tier 1 only (~1,800 chars)
      → Claude Haiku deliberation round
      → Agent outputs missing_data_flags + data_requests
    DataRequestParser analyzes output:
      → load_tier2=True (lab flags detected)
      → on_demand=[imaging_report] (scenario references ultrasound)
    Round 2: Tier 1 + Tier 2 + imaging note (~8,600 chars)
      → Agent has full lab trends + ultrasound findings
      → No further data_requests → deliberation complete
    Synthesize all round outputs → commit to deliberations table
    Return: {status: complete, context_stats: {chars_used: 8600, ...}}

  run_deliberation(mode="full")
    → Original 5-phase dual-LLM pipeline (unchanged)
    → compile_patient_context() → Phase 1-5
```

### Key Table Mapping (prompt pseudo-code → actual DB)

The tiered loader was adapted from the design spec to match the actual
PostgreSQL schema:

| Design Reference | Actual Table | Notes |
|-----------------|-------------|-------|
| `patient_health_records` | `biometric_readings` | `test_name`→`metric_type`, `result_value`→`value`, `flag`→`is_abnormal` |
| `patient_encounters` | `clinical_events` | `encounter_date`→`event_date`, `diagnoses`→`description` |
| `patient_allergies` | *(does not exist)* | Skipped — not tracked in current schema |
| `patient_conditions.name` | `patient_conditions.display` | ICD-10 code in `code` column |
| `patient_medications.medication_name` | `patient_medications.display` | Status in `status` column |

All queries use asyncpg `$1, $2` parameterized syntax (not `?` placeholders).
