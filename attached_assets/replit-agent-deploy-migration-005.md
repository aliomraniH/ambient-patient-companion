# Replit Agent — Deploy Migration 005: Clinical Data Storage Fix

## Background: What Changed and Why

**Branch:** `claude/fix-clinical-data-storage-AXjlb`
**Commit:** `a94433d`

### The Problem

`VARCHAR(20)` on `biometric_readings.unit` was silently truncating clinical data. Real medical units exceed 20 characters:

| Value | Length | Truncated? |
|-------|--------|------------|
| `%{HemoglobinSaturation}` | 23 | YES |
| `{BoneCollagen}eq/mmol{Cre}` | 26 | YES |
| `mL/min/1.73 m2` | 15 | No |
| `Positive (ref: negative)` | 25 | YES |

Additionally, non-numeric lab values like "Reactive (Confirmed)" were being crammed into the `unit` field because there was no proper `result_text` column. The `value` column used `DOUBLE PRECISION` which has floating-point imprecision for clinical threshold comparisons (e.g., 126 mg/dL glucose cutoff for diabetes).

### What Was Fixed (8 files changed)

| File | Change |
|------|--------|
| `server/migrations/005_clinical_data_storage.sql` | **NEW** — migration: widen columns, add 11 new fields |
| `mcp-server/db/schema.sql` | Updated master schema for fresh deployments |
| `server/mcp_server.py` | Fixed write path: stop unit-field hack, populate new columns |
| `ingestion/adapters/healthex/traced_writer.py` | Fixed traced write path: same fixes |
| `mcp-server/transforms/fhir_to_schema.py` | Fixed FHIR transform: extract LOINC, ref ranges, qualitative results |
| `server/deliberation/context_compiler.py` | Fixed read path: use new columns with COALESCE fallback |
| `ingestion/adapters/healthex/parsers/format_b_parser.py` | Pass through ref_range and loinc_code |
| `ingestion/adapters/healthex/parsers/format_d_parser.py` | Extract reference ranges and LOINC from FHIR |

### New Columns Added to `biometric_readings`

```
result_text       TEXT          — qualitative results ("Reactive", "Positive")
result_numeric    NUMERIC       — exact numeric value (not FLOAT)
result_unit       TEXT          — proper unit (UCUM codes)
reference_text    TEXT          — original range text
reference_low     NUMERIC       — parsed lower bound
reference_high    NUMERIC       — parsed upper bound
loinc_code        VARCHAR(10)   — LOINC code
interpretation    VARCHAR(10)   — H, L, N, HH, LL, A, AA
source_record_id  UUID          — FK to raw_fhir_cache
fhir_extensions   JSONB         — overflow for FHIR extensions
is_out_of_range   BOOLEAN       — GENERATED column from result_numeric vs reference bounds
```

---

## Step 1: Pull Branch and Apply Migration

```bash
# Pull the branch
git fetch origin claude/fix-clinical-data-storage-AXjlb
git checkout claude/fix-clinical-data-storage-AXjlb

# Apply the migration (safe — metadata-only ALTER, ~30ms)
psql $DATABASE_URL -f server/migrations/005_clinical_data_storage.sql
```

**Verify migration applied:**

```sql
-- Confirm new columns exist
SELECT column_name, data_type
FROM information_schema.columns
WHERE table_name = 'biometric_readings'
  AND column_name IN ('result_text', 'result_numeric', 'result_unit',
                       'reference_text', 'reference_low', 'reference_high',
                       'loinc_code', 'interpretation', 'is_out_of_range',
                       'source_record_id', 'fhir_extensions')
ORDER BY column_name;
-- Expected: 11 rows

-- Confirm unit column is now TEXT (not VARCHAR(20))
SELECT column_name, data_type, character_maximum_length
FROM information_schema.columns
WHERE table_name = 'biometric_readings'
  AND column_name = 'unit';
-- Expected: data_type = 'text', character_maximum_length = NULL

-- Confirm metric_type column is now TEXT
SELECT data_type FROM information_schema.columns
WHERE table_name = 'biometric_readings' AND column_name = 'metric_type';
-- Expected: text

-- Confirm indexes created
SELECT indexname FROM pg_indexes
WHERE tablename = 'biometric_readings'
  AND indexname IN ('idx_biometric_loinc', 'idx_biometric_out_of_range');
-- Expected: 2 rows
```

---

## Step 2: Restart MCP Servers

```bash
# Kill existing servers
pkill -f "server.mcp_server" || true
pkill -f "mcp-server.*server.py" || true

# Restart Clinical MCP Server (port 8001)
MCP_TRANSPORT=streamable-http MCP_PORT=8001 python -m server.mcp_server &

# Restart Skills MCP Server (port 8002)
cd mcp-server && MCP_TRANSPORT=streamable-http MCP_PORT=8002 python server.py &
cd ..

# Wait for servers to be ready
sleep 3
curl -s http://localhost:8001/health || echo "8001 not ready yet"
curl -s http://localhost:8002/health || echo "8002 not ready yet"
```

---

## Step 3: Run Regression Test Suites

```bash
# Core MCP server tests (skip numpy-dependent test)
pytest mcp-server/tests/ -v --ignore=mcp-server/tests/test_generators.py --tb=short

# Deliberation engine tests (skip anthropic-dependent test)
pytest server/deliberation/tests/ -v --ignore=server/deliberation/tests/test_analyst.py --tb=short

# Ingestion pipeline tests
pytest ingestion/tests/ -v --tb=short

# Format integration tests
pytest tests/phase1/test_all_format_integration.py -v --tb=short

# Transfer pipeline tests
pytest tests/phase1/test_transfer_pipeline.py -v --tb=short
```

**Expected:** All previously-passing tests still pass. No regressions.

---

## Step 4: Create Edge-Case Test Data

Run the edge-case data creation script from the companion file
`attached_assets/replit-agent-migration-005-test-data.py`.

This script creates a stress-test patient with 10 years of clinical history
across all 5 data formats, exercising every edge case the migration fixes.

```bash
python attached_assets/replit-agent-migration-005-test-data.py
```

See that file for the full test data definitions and verification queries.

---

## Step 5: Verification SQL — Confirm Edge Cases Pass

After the test data is ingested, run these verification queries:

```sql
-- 1. Long UCUM unit codes stored without truncation
SELECT metric_type, unit, result_unit, length(result_unit) as len
FROM biometric_readings
WHERE length(result_unit) > 20
ORDER BY len DESC;
-- Expected: rows with %{HemoglobinSaturation} (23), etc.

-- 2. Qualitative results stored in result_text (NOT in unit)
SELECT metric_type, result_text, unit, result_unit
FROM biometric_readings
WHERE result_text IS NOT NULL
ORDER BY measured_at DESC
LIMIT 10;
-- Expected: "Reactive (Confirmed)", "Positive", etc. in result_text column

-- 3. NUMERIC precision for clinical thresholds
SELECT metric_type, result_numeric, result_numeric = 126.0 as exact_match
FROM biometric_readings
WHERE metric_type LIKE '%glucose%'
  AND result_numeric BETWEEN 125 AND 127
ORDER BY measured_at DESC;
-- Expected: exact_match = true for 126.0 (no floating-point drift)

-- 4. Generated is_out_of_range column works
SELECT metric_type, result_numeric, reference_low, reference_high, is_out_of_range
FROM biometric_readings
WHERE is_out_of_range = true
ORDER BY measured_at DESC
LIMIT 10;
-- Expected: result_numeric outside [reference_low, reference_high] bounds

-- 5. LOINC codes indexed and queryable
SELECT loinc_code, metric_type, result_numeric, result_unit, measured_at
FROM biometric_readings
WHERE loinc_code IS NOT NULL
ORDER BY loinc_code, measured_at DESC;
-- Expected: rows grouped by LOINC code

-- 6. Reference ranges stored properly
SELECT metric_type, reference_text, reference_low, reference_high
FROM biometric_readings
WHERE reference_text IS NOT NULL
  AND length(reference_text) > 20
ORDER BY measured_at DESC;
-- Expected: long reference ranges like "Male: 13.5-17.5 g/dL; Female: 12.0-16.0 g/dL"

-- 7. Backfill verification — existing data migrated
SELECT
  COUNT(*) as total,
  COUNT(result_numeric) as has_result_numeric,
  COUNT(result_unit) as has_result_unit
FROM biometric_readings;
-- Expected: has_result_numeric ≈ total (backfilled from value column)
```

---

## Step 6: Context Compiler Verification

Trigger a deliberation to confirm the context compiler reads the enriched data:

```bash
# Via MCP tool (if server running on 8001)
curl -X POST http://localhost:8001/tools/run_deliberation \
  -H "Content-Type: application/json" \
  -d '{"patient_id": "MC-2025-4829", "trigger_type": "manual"}'
```

Or via the MCP tool directly:
```
run_deliberation(patient_id="MC-2025-4829", trigger_type="manual")
```

Check that the deliberation context includes enriched lab data with reference ranges
and LOINC codes (not just raw value + unit).

---

## Expected Scorecard

| Edge Case | Before (broken) | After (fixed) |
|-----------|-----------------|---------------|
| Unit `%{HemoglobinSaturation}` (23 chars) | Truncated to `%{HemoglobinSatura` | Stored fully |
| "Reactive (Confirmed)" | Crammed into unit field | In `result_text` column |
| Glucose 126.0 mg/dL | 125.99999... (FLOAT) | Exact 126.0 (NUMERIC) |
| Reference "Male: 13.5-17.5 g/dL" | Truncated or rejected | Full text in `reference_text` |
| LOINC code 4548-4 | Not stored | In `loinc_code` column |
| HbA1c 6.5% vs threshold | Float comparison unreliable | NUMERIC exact comparison |
| `is_out_of_range` auto-flag | Manual `is_abnormal` only | Auto-computed from bounds |
| valueCodeableConcept (qualitative) | Silently dropped | Stored in `result_text` |
| Long condition display (>500 chars) | VARCHAR(500) truncation | TEXT, no limit |
| Long medication name (>500 chars) | VARCHAR(500) truncation | TEXT, no limit |
