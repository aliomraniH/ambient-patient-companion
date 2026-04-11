# Replit Agent Deployment Prompt: Gap-Aware Deliberation Engine Integration

## Mission

Deploy the gap-aware deliberation engine integration from branch `claude/extend-mcp-servers-1OZzW`. This adds pre-dispatch context staleness detection and post-deliberation gap artifact collection to the dual-LLM deliberation pipeline — the final piece connecting the 7 gap-aware MCP tools (already deployed) to the deliberation engine itself.

**No new servers, no new ports, no new environment variables.** This is a code-only deployment into the existing 3-server architecture.

---

## Pre-Deployment Checklist

### 1. Pull the branch

```bash
git fetch origin claude/extend-mcp-servers-1OZzW
git checkout claude/extend-mcp-servers-1OZzW
git pull origin claude/extend-mcp-servers-1OZzW
```

### 2. Verify the 4 gap-aware tables exist

The tables were created by migration 006 in a prior deployment. Confirm they are present:

```bash
psql $DATABASE_URL -c "\dt reasoning_gaps; \dt clarification_requests; \dt gap_triggers; \dt knowledge_search_cache;"
```

**If any table is missing**, apply the migration:

```bash
psql $DATABASE_URL -f server/migrations/006_gap_aware_tables.sql
```

This migration is idempotent (`CREATE TABLE IF NOT EXISTS`) — safe to re-run.

### 3. Verify Python dependencies

No new dependencies were added. The existing `requirements.txt` already includes everything needed:
- `asyncpg` — DB access (existing)
- `pydantic` — models (existing)
- `anthropic` — Claude API (existing)
- `httpx` — HTTP client for knowledge search (existing)
- `fastmcp` — MCP framework (existing)

```bash
pip install -r requirements.txt
```

---

## What Changed (4 files)

### New Files

| File | Purpose |
|------|---------|
| `server/deliberation/gap_validation.py` | Pre-dispatch context staleness detection, automated data refresh, post-deliberation gap artifact collection. 8 functions, ~330 lines. |
| `server/deliberation/tests/test_gap_validation.py` | 30 unit tests covering element extraction, staleness detection, data refresh, gap summary, and full orchestration. |

### Modified Files

| File | Change |
|------|--------|
| `server/deliberation/engine.py` | Added import of `gap_validation`. Inserted Phase 0.1 (context validation) between Phase 0 and Phase 0.5 in both `run()` and `run_progressive()`. Inserted Phase 5.5 (gap artifact collection) after Phase 5 commit in both methods. All new code is wrapped in try/except for non-fatal degradation. |
| `server/deliberation/schemas.py` | Added 3 optional fields to `DeliberationResult`: `gap_artifacts: list[dict]`, `gap_summary: str`, `context_validation: dict`. All have defaults — fully backward-compatible. |

### NOT Modified

- `server/mcp_server.py` — no changes (gap-aware tools were deployed previously)
- `mcp-server/server.py` — no changes
- `ingestion/server.py` — no changes
- `start.sh` — no changes needed
- `gap_aware/` module — no changes (already deployed)
- Agent prompt templates — no changes (gap protocol already injected)
- Migration files — no new migrations

---

## Deployment Steps

### Step 1: Run tests

```bash
# New gap_validation tests (30 tests)
python -m pytest server/deliberation/tests/test_gap_validation.py -v

# Existing gap-aware tests (should still pass)
python -m pytest tests/phase2/test_gap_aware_models.py tests/phase2/test_gap_aware_tools.py tests/phase2/test_gap_prompt_injection.py -v

# Engine integration tests (should still pass)
python -m pytest server/deliberation/tests/test_engine_integration.py -v

# Full Phase 2 suite
python -m pytest tests/phase2/ -v
```

**Expected**: All tests pass. The full Phase 2 suite should show 137 passed.

### Step 2: Restart the services

Only Server 1 (ambient-clinical-intelligence on port 8001) needs a restart because `engine.py` and `schemas.py` are loaded by that server. Servers 2 and 3 are unchanged.

If using workflows:
- Stop and restart the `MCP Server 1` workflow

If using `start.sh` (production deployment):
- The next deployment will automatically pick up the changes since `start.sh` runs `python -m server.mcp_server`

### Step 3: Verify health endpoints

```bash
curl http://localhost:8001/health
# → {"ok":true,"server":"ambient-clinical-intelligence","version":"1.0.0"}

curl http://localhost:8002/health
# → {"ok":true,"server":"ambient-skills-companion","version":"1.0.0"}

curl http://localhost:8003/health
# → {"ok":true,"server":"ambient-ingestion","version":"1.0.0"}
```

---

## Post-Deployment Verification

### Test 1: Deliberation includes gap validation metadata

Trigger a deliberation and verify the response now includes gap-aware fields:

```bash
curl -X POST http://localhost:8001/tools/run_deliberation \
  -H "Content-Type: application/json" \
  -d '{"patient_id": "<any_valid_mrn>", "trigger_type": "manual", "mode": "progressive"}'
```

Then retrieve results:

```bash
curl -X POST http://localhost:8001/tools/get_deliberation_results \
  -H "Content-Type: application/json" \
  -d '{"patient_id": "<same_mrn>"}'
```

**Verify the response includes**:
- `"gap_artifacts"` — array (may be empty if no gaps were emitted)
- `"gap_summary"` — string
- `"context_validation"` — object with `freshness_score`, `stale_elements_detected`, `elements_refreshed`

### Test 2: Context staleness is logged

Check server logs after triggering a deliberation:

```
[INFO] Context validation: freshness=X.XX stale=N refreshed=N
```

or for progressive mode:

```
[INFO] Progressive context validation: freshness=X.XX stale=N refreshed=N
```

### Test 3: Verify backward compatibility

Existing deliberation results should still work exactly as before. The 3 new fields (`gap_artifacts`, `gap_summary`, `context_validation`) all have default values, so no existing code that reads `DeliberationResult` will break.

---

## Architecture After Deployment

```
Deliberation Pipeline (both run() and run_progressive()):

Phase 0    Context Compilation     ← existing, unchanged
Phase 0.1  Gap-Aware Validation    ← NEW: staleness detection + auto-refresh
Phase 0.5  Agenda Builder          ← existing, unchanged
Phase 1    Parallel Analysis       ← existing, unchanged (ARIA + MIRA)
Phase 2    Cross-Critique          ← existing, unchanged (THEO)
Phase 3    Synthesis               ← existing, unchanged (SYNTHESIS)
Phase 3.25 Synthesis Review        ← existing, unchanged
Phase 3.5  Output Safety           ← existing, unchanged
Phase 4    Behavioral Adaptation   ← existing, unchanged
Phase 5    Knowledge Commit        ← existing, unchanged
Phase 5.5  Gap Artifact Collection ← NEW: collects reasoning_gaps from DB
```

**Non-fatal design**: Both Phase 0.1 and Phase 5.5 are wrapped in try/except. If the gap-aware logic fails for any reason (missing tables, DB timeout, etc.), the deliberation continues exactly as before with a warning logged. No existing functionality is affected.

---

## Rollback

If any issues arise, the changes are fully reversible:

```bash
git checkout main -- server/deliberation/engine.py server/deliberation/schemas.py
rm server/deliberation/gap_validation.py
rm server/deliberation/tests/test_gap_validation.py
```

Then restart Server 1. The gap-aware MCP tools will continue to work independently — they do not depend on the engine integration.

---

## Files Reference

```
server/deliberation/
├── engine.py                    ← MODIFIED: +Phase 0.1, +Phase 5.5
├── schemas.py                   ← MODIFIED: +3 fields on DeliberationResult
├── gap_validation.py            ← NEW: 8 functions, 330 lines
├── tests/
│   ├── test_gap_validation.py   ← NEW: 30 tests
│   └── test_engine_integration.py  ← UNCHANGED (still passes)
└── [all other files unchanged]
```
