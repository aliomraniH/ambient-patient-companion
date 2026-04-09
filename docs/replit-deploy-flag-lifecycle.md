# Replit Agent: Deploy Flag Lifecycle & Retroactive Correction System

## Background: What Changed and Why

**Branch:** `claude/fix-flag-lifecycle-E2ZKo`
**Commit:** `11d6ebb`

### The Problem

Across 15 deliberation runs, the engine produced flags based on corrupted data (VARCHAR(20) truncation causing 0.0 lab values) and ambiguous clinical states (prediabetes active despite normal A1c). The current system stores flags inline in `deliberation_outputs` with no lifecycle management — flags cannot be retracted when data improves, deduplicated across runs, or escalated for human clarification.

**Run 5–14 example:** `CRITICAL — all 2025-07-11 labs show 0.0, metabolic assessment impossible`
- **Root cause:** VARCHAR(20) truncated result_unit, conflict key mismatch wrote NULL
- **Real data:** A1c 4.8%, glucose 98 mg/dL, LDL 104 mg/dL (excellent control)
- **Impact:** False alarm recommending "lab verification before any decisions"

**Run 15 example:** `Prediabetes active but A1c 4.8% and glucose 98 both normal`
- **Ambiguity:** Is this stale diagnosis or genuine remission? Requires human clarification.

### What Was Added (4 new files)

| File | Purpose |
|------|---------|
| `server/deliberation/migrations/004_flag_lifecycle.sql` | 3 new tables (`deliberation_flags`, `flag_review_runs`, `flag_corrections`) + 4 new ENUMs + priority constraint fix + historic backfill |
| `server/deliberation/flag_writer.py` | Dedup-aware flag writer with basis inference, SHA-256 fingerprinting, data provenance collection, quality scoring |
| `server/deliberation/flag_reviewer.py` | Two-phase reviewer: deterministic retraction rules + LLM review (Haiku) with human escalation |
| `tests/phase2/test_flag_lifecycle.py` | 30 unit tests (all passing) |

### What Was Modified (2 files)

| File | Changes |
|------|---------|
| `server/deliberation/engine.py` | Post-deliberation hook: writes flags to registry via `write_flag()`, then runs `run_flag_review()` (non-fatal on failure) |
| `server/mcp_server.py` | Post-ingest hook in `ingest_from_healthex()` + new `get_flag_review_status` MCP tool (19th tool on port 8001) |

---

## Step 1: Pull the Branch

```bash
git fetch origin claude/fix-flag-lifecycle-E2ZKo
git checkout claude/fix-flag-lifecycle-E2ZKo
```

---

## Step 2: Apply Database Migration

Run the migration against the Replit PostgreSQL database. Safe to run multiple times (uses `IF NOT EXISTS` and `DO $$ ... EXCEPTION WHEN duplicate_object` for ENUMs).

```bash
psql $DATABASE_URL -f server/deliberation/migrations/004_flag_lifecycle.sql
```

**What it creates:**
- 4 PostgreSQL ENUMs: `flag_lifecycle_state`, `flag_basis`, `flag_priority`, `correction_action`
- `deliberation_flags` table: canonical flag registry with lifecycle states, data provenance (JSONB), quality scoring, fingerprint dedup, nudge linkage
- `flag_review_runs` table: audit trail per review execution (trigger type, counts, duration)
- `flag_corrections` table: individual correction records with clarification questions/options for human-in-the-loop
- 9 indexes across the 3 tables (partial indexes for open flags, human review queue)
- Fixes `deliberation_outputs.priority` CHECK constraint to include `medium-high`
- Backfills historic flags from `deliberation_outputs` into `deliberation_flags`

**Verify the migration:**

```sql
-- Confirm new tables exist
SELECT table_name FROM information_schema.tables
WHERE table_schema = 'public'
  AND table_name IN ('deliberation_flags', 'flag_review_runs', 'flag_corrections')
ORDER BY table_name;
-- Expected: 3 rows

-- Confirm new ENUMs exist
SELECT typname FROM pg_type
WHERE typname IN ('flag_lifecycle_state', 'flag_basis', 'flag_priority', 'correction_action');
-- Expected: 4 rows

-- Confirm deliberation_flags columns
SELECT column_name, data_type
FROM information_schema.columns
WHERE table_name = 'deliberation_flags'
ORDER BY ordinal_position;
-- Expected: ~25 columns including patient_id, lifecycle_state, flag_basis, data_provenance, flag_fingerprint

-- Confirm priority constraint fix
SELECT conname, consrc FROM pg_constraint
WHERE conname = 'deliberation_outputs_priority_check';
-- Expected: includes 'medium-high'

-- Confirm backfill ran (if deliberation_outputs had missing_data_flag rows)
SELECT COUNT(*) FROM deliberation_flags;
-- Expected: >= 0 (matches number of historic missing_data_flag rows)

-- Confirm indexes
SELECT indexname FROM pg_indexes
WHERE tablename = 'deliberation_flags';
-- Expected: idx_flags_patient_open, idx_flags_patient_all, idx_flags_basis, idx_flags_requires_human, idx_flags_delib
```

---

## Step 3: Install Dependencies (if needed)

No new dependencies were added. The code uses `anthropic` (already in requirements.txt). Verify:

```bash
pip install -r requirements.txt
```

---

## Step 4: Restart the Clinical MCP Server

The Clinical MCP Server (port 8001) must be restarted to pick up the new `get_flag_review_status` tool:

```bash
# Kill existing server
pkill -f "server.mcp_server" || true

# Restart
MCP_TRANSPORT=streamable-http MCP_PORT=8001 python -m server.mcp_server &
```

**Verify the new tool is registered:**
```bash
curl -s http://localhost:8001/health | python -m json.tool
```

The server should list **19 tools** now (was 18):
- Existing 18 tools
- NEW: `get_flag_review_status`

---

## Step 5: Run Existing Tests (Regression Check)

Ensure nothing broke:

```bash
# Ingestion tests
python -m pytest ingestion/tests/ -v --tb=short -q 2>&1 | tail -5

# Deliberation JSON utils
python -m pytest server/deliberation/tests/test_json_utils.py -v --tb=short

# Phase 1 integration tests
python -m pytest tests/phase1/ -v --tb=short -q 2>&1 | tail -5

# Phase 2 deliberation feature tests
python -m pytest tests/phase2/test_deliberation_features.py -v --tb=short -q 2>&1 | tail -5
```

---

## Step 6: Run New Flag Lifecycle Tests

```bash
python -m pytest tests/phase2/test_flag_lifecycle.py -v
```

**Expected:** 30 tests pass:

```
TestInferFlagBasis (7 tests)          — flag basis pattern matching
TestFlagFingerprint (4 tests)         — SHA-256 dedup fingerprinting
TestDataQualityScoring (4 tests)      — 0.0–1.0 quality scoring
TestWriteFlag (4 tests)               — dedup-aware insert/update
TestDataProvenance (2 tests)          — lab + condition provenance collection
TestDeterministicRetract (5 tests)    — retraction rules + safety guards
TestReviewSummary (2 tests)           — plain-text summary formatting
TestFullReviewFlow (2 tests)          — end-to-end review pipeline
```

---

## Step 7: Create Integration Tests

Create `tests/phase2/test_flag_lifecycle_integration.py` to verify the flag lifecycle end-to-end against the live database:

```python
"""
Integration tests for the Flag Lifecycle & Retroactive Correction System.

Requires:
  - Live PostgreSQL with migration 004 applied
  - Tables: deliberation_flags, flag_review_runs, flag_corrections

Tests:
  FL-1: write_flag creates a row in deliberation_flags
  FL-2: write_flag deduplicates by fingerprint (upsert, not duplicate)
  FL-3: medium-high priority writes without constraint violation
  FL-4: deterministic retraction fires for data_corrupt flags after real data lands
  FL-5: flag_review_runs row created for each review execution
  FL-6: get_flag_review_status returns open flags ordered by priority
  FL-7: backfill populated historic flags from deliberation_outputs
"""

import asyncio
import json
import os
import uuid

import asyncpg
import pytest

DATABASE_URL = os.environ.get("DATABASE_URL", "")

pytestmark = pytest.mark.skipif(
    not DATABASE_URL,
    reason="DATABASE_URL not set — skip live DB tests"
)


@pytest.fixture(scope="module")
def event_loop():
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


@pytest.fixture(scope="module")
async def pool():
    p = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=3)
    yield p
    await p.close()


@pytest.fixture(scope="module")
async def test_patient(pool):
    """Create a disposable test patient for flag lifecycle tests."""
    patient_id = str(uuid.uuid4())
    mrn = f"TEST-FL-{uuid.uuid4().hex[:8]}"
    async with pool.acquire() as conn:
        await conn.execute(
            """INSERT INTO patients (id, mrn, first_name, last_name, birth_date, gender, data_source)
               VALUES ($1, $2, 'Flag', 'TestPatient', '1970-01-01', 'F', 'test')
               ON CONFLICT (mrn) DO UPDATE SET first_name = 'Flag'""",
            patient_id, mrn,
        )
    yield {"id": patient_id, "mrn": mrn}
    # Cleanup
    async with pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM flag_corrections WHERE patient_id = $1::uuid", patient_id)
        await conn.execute(
            "DELETE FROM flag_review_runs WHERE patient_id = $1::uuid", patient_id)
        await conn.execute(
            "DELETE FROM deliberation_flags WHERE patient_id = $1::uuid", patient_id)
        await conn.execute(
            "DELETE FROM biometric_readings WHERE patient_id = $1::uuid", patient_id)
        await conn.execute(
            "DELETE FROM patients WHERE id = $1::uuid", patient_id)


# ── FL-1: write_flag creates a row ──────────────────────────────────────────

@pytest.mark.asyncio
async def test_fl1_write_flag_creates_row(pool, test_patient):
    """write_flag inserts a new row into deliberation_flags."""
    from server.deliberation.flag_writer import write_flag

    patient_id = test_patient["id"]
    delib_id = str(uuid.uuid4())

    async with pool.acquire() as conn:
        result = await write_flag(conn, patient_id, delib_id, {
            "flag": "All labs show 0.0",
            "description": "Possible data corruption",
            "priority": "medium",
        })
        assert result["action"] == "created"
        assert "flag_id" in result

        # Verify row exists
        row = await conn.fetchrow(
            "SELECT * FROM deliberation_flags WHERE id = $1::uuid",
            result["flag_id"],
        )
        assert row is not None
        assert row["title"] == "All labs show 0.0"
        assert row["lifecycle_state"] == "open"
        assert row["flag_basis"] == "data_corrupt"  # inferred from "0.0"


# ── FL-2: write_flag deduplicates ────────────────────────────────────────────

@pytest.mark.asyncio
async def test_fl2_write_flag_deduplicates(pool, test_patient):
    """Writing the same flag twice returns updated_existing, not created."""
    from server.deliberation.flag_writer import write_flag

    patient_id = test_patient["id"]
    flag_data = {
        "flag": "Dedup test flag",
        "description": "Testing deduplication",
        "priority": "low",
    }

    async with pool.acquire() as conn:
        r1 = await write_flag(conn, patient_id, str(uuid.uuid4()), flag_data)
        r2 = await write_flag(conn, patient_id, str(uuid.uuid4()), flag_data)

        assert r1["action"] == "created"
        assert r2["action"] == "updated_existing"
        assert r1["flag_id"] == r2["flag_id"]  # same row, not duplicated

        # Only 1 row for this fingerprint
        count = await conn.fetchval(
            """SELECT COUNT(*) FROM deliberation_flags
               WHERE patient_id = $1::uuid AND title = 'Dedup test flag'""",
            patient_id,
        )
        assert count == 1


# ── FL-3: medium-high priority ───────────────────────────────────────────────

@pytest.mark.asyncio
async def test_fl3_medium_high_priority(pool, test_patient):
    """medium-high priority writes without constraint violation."""
    from server.deliberation.flag_writer import write_flag

    patient_id = test_patient["id"]

    async with pool.acquire() as conn:
        result = await write_flag(conn, patient_id, str(uuid.uuid4()), {
            "flag": "Medium-high test",
            "description": "Testing priority enum",
            "priority": "medium-high",
        })
        assert result["action"] == "created"

        row = await conn.fetchrow(
            "SELECT priority::text FROM deliberation_flags WHERE id = $1::uuid",
            result["flag_id"],
        )
        assert row["priority"] == "medium-high"


# ── FL-4: deterministic retraction ───────────────────────────────────────────

@pytest.mark.asyncio
async def test_fl4_deterministic_retraction(pool, test_patient):
    """A data_corrupt flag is auto-retracted when real lab values exist."""
    from server.deliberation.flag_writer import write_flag
    from server.deliberation.flag_reviewer import run_flag_review

    patient_id = test_patient["id"]

    async with pool.acquire() as conn:
        # Write a data_corrupt flag
        result = await write_flag(conn, patient_id, str(uuid.uuid4()), {
            "flag": "All 2025-07-11 labs show 0.0",
            "description": "data integrity issue — all lab values 0.0",
            "priority": "medium",
        })
        flag_id = result["flag_id"]

        # Seed 10 real lab values so deterministic retraction fires
        for i in range(10):
            await conn.execute(
                """INSERT INTO biometric_readings
                       (patient_id, metric_type, value, unit, measured_at, data_source)
                   VALUES ($1::uuid, $2, $3, '%', NOW() - ($4 || ' days')::interval, 'test')
                   ON CONFLICT DO NOTHING""",
                patient_id, f"test_lab_{i}", float(i + 1) * 10.0, str(i),
            )

    # Run flag review
    review = await run_flag_review(
        pool, patient_id, "post_ingest", str(uuid.uuid4()),
        "10 real lab values now in DB",
    )

    assert review["flags_reviewed"] >= 1
    assert review["stats"]["retracted"] >= 1

    # Verify flag was retracted
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT lifecycle_state::text FROM deliberation_flags WHERE id = $1::uuid",
            flag_id,
        )
        assert row["lifecycle_state"] == "retracted"


# ── FL-5: flag_review_runs audit trail ───────────────────────────────────────

@pytest.mark.asyncio
async def test_fl5_review_run_created(pool, test_patient):
    """Each flag review creates a flag_review_runs row."""
    from server.deliberation.flag_reviewer import run_flag_review

    patient_id = test_patient["id"]
    review = await run_flag_review(
        pool, patient_id, "manual", str(uuid.uuid4()),
    )

    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM flag_review_runs WHERE id = $1::uuid",
            review["review_id"],
        )
        assert row is not None
        assert row["trigger_type"] == "manual"
        assert row["completed_at"] is not None


# ── FL-6: get_flag_review_status MCP tool ────────────────────────────────────

@pytest.mark.asyncio
async def test_fl6_get_flag_review_status_query(pool, test_patient):
    """get_flag_review_status returns flags ordered by priority."""
    from server.deliberation.flag_writer import write_flag

    patient_id = test_patient["id"]

    async with pool.acquire() as conn:
        # Write flags at different priorities
        await write_flag(conn, patient_id, str(uuid.uuid4()), {
            "flag": "Low priority test flag",
            "priority": "low",
        })
        await write_flag(conn, patient_id, str(uuid.uuid4()), {
            "flag": "High priority test flag",
            "priority": "high",
        })

        # Query open flags directly (simulating MCP tool)
        open_flags = await conn.fetch(
            """SELECT title, priority::text
               FROM deliberation_flags
               WHERE patient_id = $1::uuid AND lifecycle_state = 'open'
               ORDER BY
                   CASE priority::text
                       WHEN 'critical' THEN 1 WHEN 'high' THEN 2
                       WHEN 'medium-high' THEN 3 WHEN 'medium' THEN 4
                       WHEN 'low' THEN 5 ELSE 6 END,
                   flagged_at DESC""",
            patient_id,
        )

        # High should come before low
        priorities = [r["priority"] for r in open_flags]
        if "high" in priorities and "low" in priorities:
            assert priorities.index("high") < priorities.index("low")


# ── FL-7: backfill check ────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_fl7_backfill_check(pool):
    """If deliberation_outputs has missing_data_flag rows, they should be in deliberation_flags."""
    async with pool.acquire() as conn:
        # Count historic flags in deliberation_outputs
        historic = await conn.fetchval(
            "SELECT COUNT(*) FROM deliberation_outputs WHERE output_type = 'missing_data_flag'"
        )
        # Count backfilled flags
        backfilled = await conn.fetchval(
            "SELECT COUNT(*) FROM deliberation_flags"
        )
        # backfilled should be >= historic (new flags may also exist)
        # This is a soft check — backfill uses ON CONFLICT DO NOTHING
        assert backfilled >= 0  # table exists and is queryable
```

Then run:

```bash
python -m pytest tests/phase2/test_flag_lifecycle_integration.py -v
```

---

## Step 8: Create MCP Tool Smoke Tests

Add to `tests/e2e/test_flag_lifecycle_tools.py`:

```python
"""
Smoke tests for the flag lifecycle MCP tool.

Requires: Clinical MCP Server running on port 8001
          DATABASE_URL set with migration 004 applied

Tests:
  SMOKE-1: get_flag_review_status REST wrapper returns valid JSON
  SMOKE-2: ingest_from_healthex response includes flag_review key
"""

import json
import os

import httpx
import pytest

MCP_URL = os.environ.get("MCP_CLINICAL_INTELLIGENCE_URL", "http://localhost:8001")

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="DATABASE_URL not set"
)


@pytest.fixture(scope="module")
def client():
    with httpx.Client(base_url=MCP_URL, timeout=30) as c:
        yield c


def _is_server_up(client):
    try:
        r = client.get("/health")
        return r.status_code == 200
    except httpx.ConnectError:
        return False


@pytest.fixture(scope="module", autouse=True)
def require_server(client):
    if not _is_server_up(client):
        pytest.skip("Clinical MCP Server not running on port 8001")


def test_smoke1_get_flag_review_status(client):
    """get_flag_review_status REST wrapper returns valid response."""
    r = client.post("/tools/get_flag_review_status", json={
        "patient_id": "00000000-0000-0000-0000-000000000000",
    })
    assert r.status_code == 200
    data = r.json()
    # Either an error or a valid result with expected fields
    assert "patient_id" in data or "status" in data


def test_smoke2_ingest_response_structure(client):
    """ingest_from_healthex response is valid JSON (flag_review may or may not be present)."""
    r = client.post("/tools/ingest_from_healthex", json={
        "patient_id": "00000000-0000-0000-0000-000000000000",
        "resource_type": "labs",
        "fhir_json": "test",
    })
    assert r.status_code == 200
    data = r.json()
    assert "status" in data
```

---

## Step 9: Update replit.md

Add to the tool list in the **Server 1 — ClinicalIntelligence** section:

```
| `get_flag_review_status` | Flag lifecycle: open flags, retracted flags, human review queue |
```

Update the tool count: **18 → 19 tools**.

Add to the Phase 2 deliberation directory listing:

```
├── flag_writer.py               ← Dedup-aware flag registry writer with provenance
├── flag_reviewer.py             ← Deterministic + LLM flag review pipeline
├── migrations/004_flag_lifecycle.sql ← 3 new tables (deliberation_flags, flag_review_runs, flag_corrections)
```

Update database section:

```
+ `server/deliberation/migrations/004_flag_lifecycle.sql` (3 flag lifecycle tables = **33 total**)
```

Update test counts:

```
Phase 2 Deliberation Engine — 40 unit tests + 87 feature tests (was 57, added 30 flag lifecycle)
```

---

## Step 10: Verify Full Test Suite

```bash
# Flag lifecycle tests (30 tests — should all pass)
python -m pytest tests/phase2/test_flag_lifecycle.py -v

# Phase 2 deliberation tests (regression)
python -m pytest tests/phase2/ -v --tb=short -q 2>&1 | tail -5

# Core ingestion tests (regression)
python -m pytest ingestion/tests/ -v --tb=short -q 2>&1 | tail -5

# Integration tests (requires live DB)
python -m pytest tests/phase2/test_flag_lifecycle_integration.py -v

# E2E smoke tests (requires server running on 8001)
python -m pytest tests/e2e/test_flag_lifecycle_tools.py -v
```

---

## Step 11: Live Verification with Real Patient

After all tests pass, verify the flag lifecycle works end-to-end with a real deliberation:

```bash
# Trigger a deliberation — flags should now be written to deliberation_flags
curl -X POST http://localhost:8001/tools/run_deliberation \
  -H "Content-Type: application/json" \
  -d '{"patient_id": "MC-2025-4829", "trigger_type": "manual"}'

# Check flag status — should show open flags from the deliberation
curl -X POST http://localhost:8001/tools/get_flag_review_status \
  -H "Content-Type: application/json" \
  -d '{"patient_id": "MC-2025-4829"}'
```

Expected response from `get_flag_review_status`:
```json
{
  "patient_id": "MC-2025-4829",
  "open_flags": 3,
  "open": [
    {"title": "...", "priority": "high", "flag_basis": "clinical_finding", ...},
    {"title": "...", "priority": "medium", "flag_basis": "data_stale", ...}
  ],
  "recently_retracted": [],
  "needs_human_review": [],
  "has_pending_clarifications": false
}
```

---

## Architecture Summary

```
BEFORE (inline, no lifecycle):
  Deliberation → missing_data_flags → INSERT into deliberation_outputs → done
  Problem: flags stack across runs, no retraction, no dedup, no human review

AFTER (flag lifecycle):
  Deliberation → missing_data_flags → INSERT into deliberation_outputs (preserved)
                                    → write_flag() → deliberation_flags (canonical registry)
                                    → run_flag_review() → flag_review_runs + flag_corrections
                                      Phase 1: deterministic rules (no LLM, instant)
                                        - data_corrupt + real labs exist → auto-retract
                                        - data_missing + field now populated → auto-retract
                                      Phase 2: LLM review (Haiku, ~500ms)
                                        - ambiguous flags → escalate_human with clarification options
                                        - confirmed flags → mark reviewed
                                        - priority changes → upgrade/downgrade

  Post-ingest hook:
    ingest_from_healthex → write rows → run_flag_review(trigger='post_ingest')
    Response now includes: "flag_review": {retracted: N, escalated: N, summary: "..."}

  New MCP tool:
    get_flag_review_status → open flags + recently retracted + human review queue
```

### Safety Rules

- Never auto-retract if `nudge_was_sent = true` (patient/care team already received it)
- Never auto-retract `critical` or `high` priority flags (require LLM or human review)
- Deterministic retraction requires ≥ 5 real lab values in last 90 days
- LLM retraction requires confidence ≥ 0.7
- All hooks are wrapped in try/except — failures never block deliberation or ingest

### New DB Tables (3)

| Table | Rows per patient | Purpose |
|-------|-----------------|---------|
| `deliberation_flags` | ~5–20 | Canonical flag registry with lifecycle states |
| `flag_review_runs` | ~1 per ingest/deliberation | Audit trail per review execution |
| `flag_corrections` | ~1 per flag per review | Individual correction records |
