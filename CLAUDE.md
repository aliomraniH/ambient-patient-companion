# ambient-patient-companion
# Claude Code Context — Full Architecture (post-audit v2)

> **Read this file completely before writing any code or making any plan.**
> This is the single source of truth. Updated after first Replit build audit.
> Repo: https://github.com/aliomraniH/ambient-patient-companion

---

## Visual Reference Cards

```
cc_01_architecture.png  — Six-layer system + HealthEx session-bridge + session map
cc_02_session1_mcp.png  — Session 1: 22 files in dependency order
cc_03_session2_data.png — Session 2: 22-table schema + pipeline + seed targets
cc_04_session3_ui.png   — Session 3: component specs + data-testid requirements
cc_05_acceptance.png    — Acceptance criteria + HealthEx session flow + /compact
```

---

## Golden Rule — Synthetic Data First

**Synthetic data must work end-to-end before HealthEx is ever touched.**

The test order is fixed and cannot be reordered:

```
STEP 0  SYNTHEA_OUTPUT_DIR set + Synthea JAR run → 10 FHIR JSON files
STEP 1  numpy installed — generators load without ImportError
STEP 2  Schema deployed — 22 tables, system_config seeded
STEP 3  Seed runs clean — 10 patients × 6 months, >10K readings
STEP 4  Daily pipeline runs and is idempotent on synthetic data
STEP 5  52 backend tests pass against synthetic warehouse data
STEP 6  38 frontend tests pass (ts-node installed)
STEP 7  OBT scores computed for all 10 patients
STEP 8  Replit app renders correctly from synthetic warehouse
STEP 9  get_data_source_status() returns active_track='synthea'
──────  only after all 9 steps pass ─────────────────────────────
STEP 10 HealthEx session-bridge tested (ingest_from_healthex)
STEP 11 switch_data_track('healthex') and full re-run
```

Do not attempt STEP 10 or 11 if any of STEPS 0-9 have failures.
A broken synthetic pipeline produces misleading HealthEx errors.

---

## Known Issues (First Replit Audit — Fix These First)

```
BLOCKER 1  SYNTHEA_OUTPUT_DIR not set + no FHIR JSON files
           Fix: set Replit Secret + run Synthea CLI
           Downstream impact: B5, B6, B7, B8, B12 all fail when this fails

BLOCKER 2  numpy not installed
           Fix: add to requirements.txt + pip install
           Downstream impact: 3 of 9 skills fail to load (generate_vitals,
           generate_checkins, sdoh_assessment)

BLOCKER 3  ts-node not installed in replit-app/
           Fix: npm install -D ts-node
           Downstream impact: all 38 frontend Jest tests fail

BLOCKER 4  ingestion/pipeline.py stages 5-7 not implemented
           The code caches raw FHIR then defers transforms to MCP skills.
           Fix: implement fhir_to_schema.transform_by_type() call +
                ConflictResolver.apply() + warehouse write loop in pipeline.py

BLOCKER 5  ingestion/adapters/synthea.py parse_bundle() has placeholder stubs
           Calls no generators — returns empty wearable_data/behavioral_signals.
           Fix: call vitals_timeseries + behavioral_model generators directly

BLOCKER 6  ingestion/conflict_resolver.py uses resolve() not apply()
           Cards and pipeline both call apply(records, policy).
           Fix: add apply() as canonical wrapper around resolve()

BLOCKER 7  All 5 test files missing (only __init__.py exists in test dirs)
           Fix: create all 5 test files — 52 tests total

SCHEMA OK  22 tables confirmed, data_source on all required tables,
           system_config present with DATA_TRACK=synthea seed row.

TOOLS OK   All 5 tools in ingestion_tools.py correctly implemented.

is_stale   source_freshness.is_stale is BOOLEAN DEFAULT true (not GENERATED).
           This is correct for PostgreSQL — pipeline sets it manually.
           No fix needed.
```

---

## What We Are Building

**Ambient Patient Companion** — a multi-agent AI system deriving
a patient health UX continuously from Role × Context × Patient State × Time.

```
S = f(R, C, P, T)  →  optimal clinical surface
```

Seven specialized agents read exclusively from a local PostgreSQL warehouse.
No agent calls external APIs directly. The ingestion service handles all sources.

Phase 1: Full system on Synthea synthetic data.
Phase 2: HealthEx connects via Claude session-bridge pattern.

---

## Repository Structure

```
ambient-patient-companion/
│
├── CLAUDE.md
├── cc_01_architecture.png  through  cc_05_acceptance.png
├── requirements.txt               numpy · fastmcp · asyncpg · faker · python-dateutil
│
├── ingestion/
│   ├── adapters/
│   │   ├── base.py                PatientRecord dataclass + BaseAdapter ABC
│   │   ├── synthea.py             FIX: must call actual generators (not stubs)
│   │   └── manual_entry.py        direct daily_checkins writer
│   ├── conflict_resolver.py       FIX: add apply(records, policy) method
│   ├── pipeline.py                FIX: implement stages 5-7
│   ├── server.py                  FastMCP ingestion server
│   └── tests/
│       ├── test_pipeline.py       P1-P8  (8 tests)  MISSING — build this
│       └── test_adapters.py       A1-A8  (8 tests)  MISSING — build this
│
├── mcp-server/
│   ├── .mcp.json
│   ├── server.py
│   ├── config.py
│   ├── orchestrator.py            get_data_source_status() BEFORE skill pipeline
│   ├── seed.py                    --patients 10 --months 6
│   ├── db/
│   │   ├── connection.py
│   │   └── schema.sql             22 tables
│   ├── generators/
│   │   ├── vitals_timeseries.py   REQUIRES numpy
│   │   ├── behavioral_model.py    REQUIRES numpy
│   │   └── sdoh_profile.py
│   ├── transforms/
│   │   └── fhir_to_schema.py      + transform_by_type() at bottom
│   ├── skills/
│   │   ├── __init__.py  base.py  generate_patient.py  generate_vitals.py
│   │   ├── generate_checkins.py  compute_obt_score.py  sdoh_assessment.py
│   │   ├── crisis_escalation.py  previsit_brief.py  food_access_nudge.py
│   │   ├── compute_provider_risk.py
│   │   └── ingestion_tools.py    5 tools incl. 3 HealthEx bridge tools
│   └── tests/
│       ├── conftest.py            shared fixtures + test_patient + caregiver_stress
│       ├── test_generators.py     V1-V14 (14 tests)  MISSING — build this
│       ├── test_skills.py         S1-S18 (18 tests)  MISSING — build this
│       └── test_schema.py         D1-D12 (12 tests)  MISSING — build this
│
└── replit-app/
    ├── package.json               ts-node in devDependencies
    ├── jest.config.ts
    └── tests/  ...
```

---

## Environment Variables

| Variable             | Value                           | Set in        |
|----------------------|---------------------------------|---------------|
| DATABASE_URL         | postgresql://... (Neon)         | Replit Secret |
| DATA_TRACK           | synthea                         | Replit Secret |
| SYNTHEA_OUTPUT_DIR   | /home/runner/synthea-output     | Replit Secret |
| ANTHROPIC_API_KEY    | sk-ant-...                      | Replit Secret |

**SYNTHEA_OUTPUT_DIR must point to a directory containing FHIR JSON files.**
If unset or empty, the seed, pipeline, and all downstream tests fail.

---

## Synthea Setup

```bash
# Step 1 — Set Replit Secret
# Tools → Secrets → SYNTHEA_OUTPUT_DIR = /home/runner/synthea-output

# Step 2 — In Replit shell
mkdir -p ~/synthea ~/synthea-output/fhir
cd ~/synthea

# Step 3 — Download JAR
wget -q https://github.com/synthetichealth/synthea/releases/download/master-branch-latest/synthea-with-dependencies.jar

# Step 4 — Generate 10 patients
java -Xmx1g -jar synthea-with-dependencies.jar \
  -p 10 -s 42 \
  --exporter.fhir.export true \
  --exporter.baseDirectory ~/synthea-output \
  --exporter.years_of_history 6 \
  Massachusetts Boston

# Step 5 — Verify
ls ~/synthea-output/fhir/ | wc -l   # must be 10
```

### If Java is not available on Replit

Create minimal FHIR fixtures instead:

```bash
python mcp-server/scripts/create_minimal_fixtures.py --count 10
# Creates 10 minimal FHIR bundles in $SYNTHEA_OUTPUT_DIR/fhir/
# Each has: Patient + Condition (T2DM) resources
# Generators fill the rest (vitals, check-ins, SDoH)
```

The script `mcp-server/scripts/create_minimal_fixtures.py` must be
built if Synthea cannot run. It produces FHIR Bundle JSON files
sufficient for the adapter and seed pipeline.

---

## Python Dependencies

`requirements.txt` must contain all of these:

```
fastmcp
asyncpg
faker
numpy
python-dateutil
pytest
pytest-asyncio
```

Install and verify:
```bash
pip install -r requirements.txt
python -c "import numpy; print('numpy', numpy.__version__)"
# If this fails: 3 skills fail to load (generate_vitals, generate_checkins, sdoh_assessment)
```

---

## Node.js Dependencies

`ts-node` is required by `jest.config.ts`. It must be in devDependencies.

```bash
cd replit-app
npm install -D ts-node
npm run build     # verify 0 TypeScript errors first
npm test          # then run tests
```

---

## 22-Table PostgreSQL Schema

Full DDL in `mcp-server/db/schema.sql`.
Every table has: `data_source VARCHAR(50) NOT NULL DEFAULT 'synthea'`

### is_stale implementation note

`source_freshness.is_stale` is `BOOLEAN DEFAULT true`.
PostgreSQL GENERATED columns cannot reference NOW() with volatile functions
in a way that re-evaluates at query time. The correct implementation:
- Set `is_stale = false` after successful ingestion (pipeline stage 6)
- Set `is_stale = true` when orchestrator detects TTL has expired
This is functionally equivalent to the GENERATED spec and is correct.

### Table inventory

```
Ingestion management (4):
  data_sources · source_freshness · ingestion_log · raw_fhir_cache

System config (1):
  system_config
  Seed row: DATA_TRACK = synthea

Patient profile (4):
  patients · patient_conditions · patient_medications · patient_sdoh_flags

Behavioral + time-series (4):
  biometric_readings · daily_checkins · medication_adherence · clinical_events

Clinical + AI outputs (5):
  care_gaps · obt_scores · clinical_facts · behavioral_correlations
  agent_interventions

Agent memory + pipeline (4):
  agent_memory_episodes · skill_executions · provider_risk_scores · pipeline_runs
```

---

## Ingestion Pipeline — All 6 Stages (stages 5-7 are the gap)

```python
class IngestionPipeline:
    async def run(self, patient_id: str, force_refresh: bool = False):

        # Stage 1: adapter selection
        adapter = AdapterRegistry.get(os.environ["DATA_TRACK"])

        # Stage 2: freshness check
        if not force_refresh and not await self._is_stale(patient_id, adapter.source_name):
            return IngestionResult(status="skipped_fresh", records_upserted=0)

        # Stage 3: raw retrieval
        raw_bundle = await adapter.fetch(patient_id)

        # Stage 4: cache raw FHIR before any transformation
        await self._cache_raw(patient_id, raw_bundle, adapter.source_name)

        # Stage 5: normalization — MUST call transform_by_type, not defer to skills
        from transforms.fhir_to_schema import transform_by_type
        records = []
        for resource_type, resources in raw_bundle.items():
            try:
                records.extend(
                    transform_by_type(resource_type, resources,
                                      patient_id, source=adapter.source_name)
                )
            except ValueError:
                pass  # unknown resource type — skip

        # Stage 6: conflict resolution
        from ingestion.conflict_resolver import ConflictResolver
        resolved = ConflictResolver.apply(records, policy="patient_first")

        # Stage 7: warehouse write + freshness update
        records_written = await self._write_to_warehouse(resolved)
        await self._update_freshness(patient_id, adapter.source_name, records_written)

        return IngestionResult(status="completed", records_upserted=records_written)
```

---

## ConflictResolver — Canonical Method

```python
class ConflictResolver:
    @staticmethod
    def apply(records: list, policy: str = "patient_first") -> list:
        """Canonical name. Called by pipeline.run() and ingest_from_healthex().
        patient_first: patient-reported > device > healthex > synthea
        """
        # implementation here

    @staticmethod
    def resolve(records, key_field, conflict_field):
        """Legacy signature — keep for backward compatibility."""
        # existing implementation
```

Both methods must exist. `apply()` is what all new code calls.

---

## SyntheaAdapter — parse_bundle() Requirements

```python
async def parse_bundle(self, fhir_bundle: dict,
                        augment_wearables: bool = True,
                        augment_behavioral: bool = True) -> PatientRecord:

    # Extract FHIR resources by type
    resources = {}
    for entry in fhir_bundle.get("entry", []):
        rt = entry["resource"]["resourceType"]
        resources.setdefault(rt, []).append(entry["resource"])

    # MUST call actual generators — not stubs or empty lists
    wearable_data = []
    behavioral_signals = {}

    if augment_wearables:
        from generators.vitals_timeseries import (
            generate_bp_series, generate_glucose_series,
            generate_hrv_series, generate_steps_series
        )
        wearable_data = (
            generate_bp_series(days=180) +
            generate_glucose_series(days=180) +
            generate_hrv_series(days=180) +
            generate_steps_series(days=180)
        )

    if augment_behavioral:
        from generators.behavioral_model import (
            generate_checkin_series, generate_adherence_series
        )
        behavioral_signals = {
            "checkins":   generate_checkin_series(days=180),
            "adherence":  generate_adherence_series(days=180),
        }

    return PatientRecord(
        fhir_bundle=resources,
        wearable_data=wearable_data,
        behavioral_signals=behavioral_signals,
        source_track="synthea",
        patient_ref_id=self._extract_patient_id(resources)
    )
```

---

## MCP Server Absolute Rules

```
RULE 1  NEVER print() in tool handlers or generators
        VERIFY: grep -r "print(" mcp-server/skills/ mcp-server/generators/ ingestion/

RULE 2  ALL SQL parameterized ($1, $2)
        VERIFY: grep -rn 'f".*SELECT\|f".*INSERT' mcp-server/ ingestion/

RULE 3  Every tool returns a string

RULE 4  Every tool catches all exceptions → log + return error string

RULE 5  Every tool logs to skill_executions (success AND failure)

RULE 6  Idempotent writes — ON CONFLICT DO NOTHING or DO UPDATE

RULE 7  data_source on every INSERT (default 'synthea')
```

---

## OBT Score Algorithm

```
score = bp×0.30 + glucose×0.25 + behavioral×0.20 + adherence×0.15 + sleep×0.10
Baseline: patient's OWN 30-day baseline — not population norms
primary_driver = domain with LOWEST score
confidence: 1.0 (>=14d) | 0.7 (7-13d) | 0.4 (<7d)
```

---

## Vital Sign Ranges

```
bp_systolic      90-180 mmHg   baseline ~141 · EOM +11 · StdDev>=8
bp_diastolic     55-115 mmHg   pulse pressure 20-80 · r>0.7 with systolic
glucose_fasting  70-300 mg/dL  EOM +25 · stress +20 · post = fast+30-80
hrv_rmssd        12-100 ms     lower=stress · 7-day rolling avg
steps_daily      800-14000     weekday/weekend ±20% · crisis -40%
sleep_hours      4.0-9.5 hrs   normal avg 7.2 · caregiver stress 5.8
```

---

## Test Suite

### Backend (52 tests total)

```
mcp-server/tests/test_generators.py  V1-V14 (14 tests)
  V1   bp_systolic always in 90-180 mmHg across 180 days
  V2   bp_diastolic always in 55-115 mmHg
  V3   pulse pressure (systolic-diastolic) always 20-80
  V4   bp_systolic StdDev >= 8 (not flat — real variance)
  V5   EOM days 25-31 systolic avg >= mid-month avg + 8
  V6   glucose_fasting always in 70-300 mg/dL
  V7   EOM glucose avg >= mid-month avg + 15
  V8   postprandial always >= fasting for same date
  V9   hrv_rmssd always in 12-100 ms
  V10  steps_daily always in 800-14000
  V11  sleep_hours always in 4.0-9.5
  V12  checkin mood values from set (great/good/okay/low/bad)
  V13  normal scenario adherence rate 65-90%
  V14  caregiver_stress scenario avg mood score < 3.0

mcp-server/tests/test_skills.py  S1-S18 (18 tests)
  S1   generate_patient: inserts row, returns OK string
  S2   generate_patient: inserts correct condition count
  S3   generate_patient: data_source='synthea' on all rows
  S4   generate_vitals: inserts biometric_readings rows
  S5   generate_vitals: idempotent (ON CONFLICT DO NOTHING)
  S6   generate_checkins: inserts daily_checkins rows
  S7   generate_checkins: data_source='manual' on check-in rows
  S8   compute_obt_score: returns JSON with score + primary_driver
  S9   compute_obt_score: score in 0-100 range
  S10  compute_obt_score: writes to obt_scores table
  S11  compute_obt_score: writes clinical_facts with TTL=30d
  S12  run_sdoh_assessment: inserts patient_sdoh_flags rows
  S13  run_crisis_escalation: returns JSON with escalation_triggered bool
  S14  run_crisis_escalation: logs to skill_executions
  S15  check_data_freshness: returns valid JSON string
  S16  get_data_source_status: JSON contains active_track field
  S17  ingest_from_healthex: returns Error on invalid resource_type
  S18  switch_data_track: rejects values other than synthea/healthex

mcp-server/tests/test_schema.py  D1-D12 (12 tests)
  D1   All 22 tables exist in the database
  D2   system_config has DATA_TRACK=synthea seed row
  D3   source_freshness UNIQUE on (patient_id, source_name)
  D4   raw_fhir_cache UNIQUE on (patient_id, source_name, fhir_resource_id)
  D5   obt_scores UNIQUE on (patient_id, score_date)
  D6   daily_checkins UNIQUE on (patient_id, checkin_date)
  D7   patients.mrn has UNIQUE constraint
  D8   data_source column exists on patients
  D9   data_source column exists on biometric_readings
  D10  data_source column exists on daily_checkins
  D11  data_source column exists on obt_scores
  D12  All FK columns reference patients.id

ingestion/tests/test_pipeline.py  P1-P8 (8 tests)
  P1   IngestionPipeline.run() returns IngestionResult with status field
  P2   force_refresh=False skips when source_freshness not stale
  P3   force_refresh=True always fetches regardless of freshness
  P4   Stage 4: raw FHIR written to raw_fhir_cache before transform
  P5   Stage 5: transform_by_type called with correct resource_type
  P6   Stage 6: ConflictResolver.apply called with policy='patient_first'
  P7   Warehouse write uses ON CONFLICT DO UPDATE (idempotent)
  P8   source_freshness updated after successful run

ingestion/tests/test_adapters.py  A1-A8 (8 tests)
  A1   SyntheaAdapter.parse_bundle() returns PatientRecord instance
  A2   parse_bundle(augment_wearables=True) calls vitals generators
  A3   parse_bundle(augment_behavioral=True) calls behavioral generators
  A4   parse_bundle() raises ValueError on empty entry list
  A5   PatientRecord.source_track == 'synthea'
  A6   load_all_patients() returns list of PatientRecord
  A7   ConflictResolver.apply() exists and is callable
  A8   ConflictResolver.apply() patient-reported beats synthea for same field
```

### Frontend (38 tests total)

```
replit-app/tests/components/OBTScoreCard.test.tsx   F1-F8
replit-app/tests/components/VitalsChart.test.tsx    F9-F15
replit-app/tests/components/CheckInFlow.test.tsx    F16-F24
replit-app/tests/api/routes.test.ts                 F25-F31
replit-app/tests/components/provider.test.tsx       F32-F38
```

---

## conftest.py (mcp-server/tests/conftest.py)

```python
import pytest, asyncio, asyncpg, os, uuid
from datetime import date, timedelta

@pytest.fixture(scope="session")
def event_loop():
    loop = asyncio.get_event_loop_policy().new_event_loop()
    yield loop
    loop.close()

@pytest.fixture(scope="session")
async def db_pool():
    pool = await asyncpg.create_pool(os.environ["DATABASE_URL"])
    yield pool
    await pool.close()

@pytest.fixture
async def test_patient(db_pool):
    pid = str(uuid.uuid4())
    async with db_pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO patients
            (id, mrn, first_name, last_name, date_of_birth, sex,
             is_synthetic, data_source)
            VALUES ($1, $2, 'Test', 'Patient', '1970-01-01', 'female',
                    true, 'synthea')
        """, pid, f"MRN-TEST-{pid[:8]}")
    yield pid
    async with db_pool.acquire() as conn:
        await conn.execute("DELETE FROM patients WHERE id=$1", pid)

@pytest.fixture
async def caregiver_stress_patient(db_pool):
    """Patient with 7 days of deteriorating signals for crisis tests (S13)."""
    pid = str(uuid.uuid4())
    async with db_pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO patients
            (id, mrn, first_name, last_name, date_of_birth, sex,
             is_synthetic, data_source)
            VALUES ($1, $2, 'Crisis', 'Patient', '1970-01-01', 'female',
                    true, 'synthea')
        """, pid, f"MRN-CRISIS-{pid[:8]}")
        for i in range(7):
            day = date.today() - timedelta(days=i)
            await conn.execute("""
                INSERT INTO daily_checkins
                (patient_id, checkin_date, mood, energy, stress_level,
                 sleep_hours, data_source)
                VALUES ($1, $2, 'bad', 'very_low', 9, 5.0, 'manual')
                ON CONFLICT DO NOTHING
            """, pid, day)
            await conn.execute("""
                INSERT INTO biometric_readings
                (patient_id, metric_type, value, unit, measured_at, data_source)
                VALUES ($1, 'glucose_fasting', 220, 'mg/dL', NOW(), 'synthea')
            """, pid)
    yield pid
    async with db_pool.acquire() as conn:
        await conn.execute("DELETE FROM patients WHERE id=$1", pid)
```

---

## Daily Pipeline Sequence

```
STEP 0  get_data_source_status()           active track + freshness check
STEP 1  run_ingestion() if stale           freshness → adapter → 6 pipeline stages
STEP 2  generate_daily_vitals()            → biometric_readings
STEP 3  generate_daily_checkins()          → daily_checkins + medication_adherence
STEP 4  compute_obt_score()               → obt_scores + clinical_facts (TTL=30d)
STEP 5  run_sdoh_assessment()             → sdoh_flags + agent_interventions
STEP 6  run_crisis_escalation()           → interventions + memory_episodes
STEP 7  run_food_access_nudge()           → interventions (day>=25 + food flag)
STEP 8  compute_provider_risk()           → provider_risk_scores

Error handling: per-skill catch → log status=failed → continue
```

---

## Session Plans

### Pre-session setup (run before any Claude Code session)

```bash
# 1. Replit Secrets (Tools → Secrets):
#    DATABASE_URL, DATA_TRACK=synthea, SYNTHEA_OUTPUT_DIR, ANTHROPIC_API_KEY

# 2. Python deps
pip install -r requirements.txt
python -c "import numpy; print('numpy OK')"

# 3. Synthea (or minimal fixtures if Java unavailable)
mkdir -p ~/synthea && cd ~/synthea
wget -q https://github.com/synthetichealth/synthea/releases/download/master-branch-latest/synthea-with-dependencies.jar
java -Xmx1g -jar synthea-with-dependencies.jar \
  -p 10 -s 42 \
  --exporter.fhir.export true \
  --exporter.baseDirectory ~/synthea-output \
  --exporter.years_of_history 6 \
  Massachusetts Boston
ls ~/synthea-output/fhir/ | wc -l   # must be 10

# 4. Schema
psql $DATABASE_URL < mcp-server/db/schema.sql
psql $DATABASE_URL -c "\dt" | grep -c "public"   # must be 22

# 5. Node deps
cd replit-app
npm install && npm install -D ts-node
npm run build   # must exit 0
```

---

### Session 1 — Fix Pass (current target)

Fix blockers first, then add missing test files. Build order:

```
Environment first:
  requirements.txt (add numpy)
  scripts/create_minimal_fixtures.py (if Java unavailable)

Fix blockers:
  ingestion/adapters/synthea.py      — call actual generators in parse_bundle()
  ingestion/conflict_resolver.py     — add apply(records, policy) method
  ingestion/pipeline.py              — implement stages 5-7

Then verify server loads:
  python mcp-server/server.py 2>&1 | grep "Loaded skill"   # 9+ skills

Add test files (all missing):
  mcp-server/tests/conftest.py
  mcp-server/tests/test_generators.py   V1-V14
  mcp-server/tests/test_skills.py       S1-S18
  mcp-server/tests/test_schema.py       D1-D12
  ingestion/tests/test_pipeline.py      P1-P8
  ingestion/tests/test_adapters.py      A1-A8

Run full verification:
  python -c "import numpy; print('OK')"
  cd mcp-server && pytest tests/ -v --tb=short   # 52 passed, 0 failed
  cd replit-app && npm test                       # 38 passed, 0 failed
```

Commit message:
```
Fix pass: synthetic pipeline complete + all 52 backend tests

- requirements.txt: numpy + all deps explicit
- ingestion/adapters/synthea.py: parse_bundle() calls real generators
- ingestion/conflict_resolver.py: apply() canonical method added
- ingestion/pipeline.py: stages 5-7 fully implemented
- mcp-server/tests/conftest.py: shared fixtures incl. caregiver_stress
- 5 test files: V1-V14 · S1-S18 · D1-D12 · P1-P8 · A1-A8 = 52 tests
- 52/52 backend tests passing on synthetic data
```

---

### Session 2 — Schema + Seeding

```bash
# Pre-session: confirm 22 tables
psql $DATABASE_URL -c "\dt" | grep -c "public"   # 22
```

Build: orchestrator.py · seed.py · previsit_brief.py · food_access_nudge.py · compute_provider_risk.py

Verify:
```bash
python mcp-server/seed.py --patients 2 --months 1    # quick test
python mcp-server/seed.py --patients 10 --months 6   # full seed

psql $DATABASE_URL -c "SELECT COUNT(*) FROM patients;"            # 10
psql $DATABASE_URL -c "SELECT COUNT(*) FROM biometric_readings;"  # >10000
psql $DATABASE_URL -c "SELECT COUNT(*) FROM source_freshness;"    # 10
psql $DATABASE_URL -c "SELECT * FROM system_config;"              # DATA_TRACK|synthea

python mcp-server/orchestrator.py --daily   # run twice — same counts
```

---

### Session 3 — Replit App UI

Pre-session: npm run build exits 0 · npm test 38/38 already passing

16 files: lib/db.ts → 5 API routes → 7 components → 3 pages

---

## /compact Strategy

| Session   | Trigger point                                       |
|-----------|-----------------------------------------------------|
| Session 1 | After ingestion/ fixed, before mcp-server/skills/  |
| Session 1 | After generators + transforms, before skills/       |
| Session 2 | After orchestrator.py, before seed run              |
| Session 3 | After API routes, before UI components              |

Always commit before /compact.

---

## Phase 1 Acceptance Criteria

```
□  pytest tests/ -v exits 0 — 52 backend tests pass (synthetic data)
□  npm test exits 0, coverage >80% — 38 frontend tests pass
□  SELECT COUNT(*) FROM patients = 10
□  SELECT COUNT(*) FROM biometric_readings > 10000
□  SELECT COUNT(DISTINCT patient_id) FROM obt_scores = 10
□  SELECT COUNT(*) FROM source_freshness = 10
□  SELECT * FROM system_config shows DATA_TRACK=synthea
□  get_data_source_status() returns active_track='synthea'
□  Daily pipeline idempotent (run twice, same counts)
□  MCP Inspector shows 9+ tools
□  Replit public URL returns 200
□  /patient/[uuid] OBT card renders with correct data-color
□  Check-in flow writes to daily_checkins (data_source='manual')
□  grep -r "print(" mcp-server/skills/ → EMPTY
□  grep -rn 'f".*SELECT' mcp-server/ → EMPTY
```

---

## Session 1 Checklist (Fix Pass)

```
Environment:
- [ ] requirements.txt has numpy listed
- [ ] pip install -r requirements.txt succeeds
- [ ] python -c "import numpy" prints version
- [ ] SYNTHEA_OUTPUT_DIR set in Replit Secrets
- [ ] Synthea run OR minimal fixtures script created
- [ ] ls $SYNTHEA_OUTPUT_DIR/fhir/ | wc -l >= 10
- [ ] ts-node installed: npm install -D ts-node in replit-app/

Core fixes:
- [ ] ingestion/adapters/synthea.py — parse_bundle() calls generators
- [ ] ingestion/conflict_resolver.py — apply() method present
- [ ] ingestion/pipeline.py — stages 5-7 implemented

Server verification:
- [ ] python mcp-server/server.py 2>&1 | grep "Loaded skill" shows 9+
- [ ] grep -r "print(" → EMPTY
- [ ] grep f-string SQL → EMPTY

Test files (all must be built):
- [ ] mcp-server/tests/conftest.py
- [ ] mcp-server/tests/test_generators.py (V1-V14, 14 tests)
- [ ] mcp-server/tests/test_skills.py (S1-S18, 18 tests)
- [ ] mcp-server/tests/test_schema.py (D1-D12, 12 tests)
- [ ] ingestion/tests/test_pipeline.py (P1-P8, 8 tests)
- [ ] ingestion/tests/test_adapters.py (A1-A8, 8 tests)

Test results:
- [ ] pytest mcp-server/tests/ -v: 52 passed, 0 failed
- [ ] npm test: 38 passed, 0 failed
- [ ] git commit: "Fix pass: synthetic pipeline complete + 52 tests"
```

---

*Last updated: post-audit v2 — three blockers fixed, five test files added*
*Repo: https://github.com/aliomraniH/ambient-patient-companion*
