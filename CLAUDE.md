# ambient-patient-companion
# Claude Code Context — Full Architecture

> **Read this file completely before writing any code or making any plan.**
> This is the single source of truth for the entire system.
> Update the session checklists at the bottom as you work.
> Repo: https://github.com/aliomraniH/ambient-patient-companion

---

## Visual Reference Cards

Read all five images before planning any session.
They contain build specs, file orders, physiological ranges, schema, and acceptance criteria.

```
cc_01_architecture.png  — Five-layer system + dual-track data + 3-session map
cc_02_session1_mcp.png  — Session 1: files in dependency order + OBT algorithm
cc_03_session2_data.png — Session 2: 21-table schema + ingestion pipeline + seed targets
cc_04_session3_ui.png   — Session 3: component specs + data-testid requirements
cc_05_acceptance.png    — 15-item acceptance criteria + daily pipeline + /compact strategy
```

---

## What We Are Building

**Ambient Patient Companion** — a multi-agent AI system that generates a
continuously derived patient health UX from Role x Context x Patient State x Time.

```
S = f(R, C, P, T)  →  optimal clinical surface
```

Seven specialized agents communicate through a shared MCP tool registry.
All agents read from a local PostgreSQL warehouse. No agent calls an
external API directly. The Data Ingestion Service handles all external sources.

Phase 1 (this build): Full system running on Synthea synthetic data.
HealthEx, device APIs, and multi-user auth are Phase 2+.

---

## Repository Structure

```
ambient-patient-companion/
│
├── CLAUDE.md                        ← you are here
├── cc_01_architecture.png           ← plan mode reference (READ FIRST)
├── cc_02_session1_mcp.png           ← plan mode reference
├── cc_03_session2_data.png          ← plan mode reference
├── cc_04_session3_ui.png            ← plan mode reference
├── cc_05_acceptance.png             ← plan mode reference
│
├── ingestion/                       ← NEW: Data Ingestion Service (Session 1)
│   ├── CLAUDE.md
│   ├── server.py                    ← FastMCP ingestion server entry point
│   ├── pipeline.py                  ← IngestionPipeline class (6 stages)
│   ├── conflict_resolver.py         ← Multi-source conflict resolution
│   ├── adapters/
│   │   ├── base.py                  ← PatientRecord dataclass + BaseAdapter ABC
│   │   ├── synthea.py               ← Track A: Synthea FHIR Bundle parser
│   │   ├── healthex.py              ← Track B: HealthEx MCP caller (Phase 2 only)
│   │   └── manual_entry.py          ← Patient check-in direct writes
│   └── tests/
│       ├── test_pipeline.py         ← P1-P8 ingestion pipeline tests
│       └── test_adapters.py         ← A1-A8 adapter tests
│
├── mcp-server/                      ← Sessions 1 + 2: Agent MCP servers
│   ├── CLAUDE.md
│   ├── .mcp.json                    ← Claude Code MCP connection config
│   ├── server.py                    ← FastMCP entry point (auto-discovers skills)
│   ├── config.py                    ← env vars, DATA_TRACK, adapter selection
│   ├── orchestrator.py              ← Daily pipeline sequencer
│   ├── seed.py                      ← argparse: --patients 10 --months 6
│   ├── db/
│   │   ├── connection.py            ← asyncpg pool + get_pool()
│   │   └── schema.sql               ← 21-table DDL (source of truth)
│   ├── generators/
│   │   ├── vitals_timeseries.py     ← BP, glucose, HRV, steps generators
│   │   ├── behavioral_model.py      ← mood, energy, adherence patterns
│   │   └── sdoh_profile.py          ← SDoH flag generation
│   ├── transforms/
│   │   └── fhir_to_schema.py        ← FHIR resources → DB table records
│   ├── skills/
│   │   ├── __init__.py              ← auto-discovery loader
│   │   ├── base.py                  ← BaseSkill abstract class
│   │   ├── generate_patient.py      ← import Synthea patient → DB
│   │   ├── generate_vitals.py       ← biometric readings generation
│   │   ├── generate_checkins.py     ← daily check-in + adherence
│   │   ├── compute_obt_score.py     ← One Big Thing algorithm
│   │   ├── sdoh_assessment.py       ← SDoH flags + interventions
│   │   ├── crisis_escalation.py     ← 7-signal crisis detection
│   │   ├── previsit_brief.py        ← pre-visit synthesis
│   │   ├── food_access_nudge.py     ← end-of-month food access trigger
│   │   ├── compute_provider_risk.py ← provider panel risk scores
│   │   └── ingestion_tools.py       ← check_freshness + run_ingestion + conflicts
│   └── tests/
│       ├── conftest.py
│       ├── test_generators.py       ← V1-V14 (14 tests)
│       ├── test_skills.py           ← S1-S18 (18 tests)
│       └── test_schema.py           ← D1-D12 (12 tests)
│
└── replit-app/                      ← Session 3: Next.js companion UX
    ├── CLAUDE.md
    ├── app/
    │   ├── page.tsx                 ← patient selector home
    │   ├── patient/[id]/page.tsx    ← ambient companion (3-tab)
    │   ├── provider/page.tsx        ← chase list + care gaps
    │   └── api/
    │       ├── obt/[id]/route.ts
    │       ├── vitals/[id]/route.ts
    │       ├── checkin/route.ts     ← direct write to daily_checkins
    │       ├── patients/route.ts
    │       └── sse/[id]/route.ts    ← SSE real-time updates
    ├── components/
    │   ├── OBTScoreCard.tsx
    │   ├── VitalsChart.tsx
    │   ├── CheckInFlow.tsx
    │   ├── SDoHFlags.tsx
    │   ├── ChaseList.tsx
    │   ├── CareGapTracker.tsx
    │   └── AgentMemoryLog.tsx
    ├── lib/
    │   └── db.ts                    ← pg Pool singleton + query<T>()
    └── tests/
        ├── components/
        │   ├── OBTScoreCard.test.tsx
        │   ├── VitalsChart.test.tsx
        │   ├── CheckInFlow.test.tsx
        │   └── provider.test.tsx
        └── api/
            └── routes.test.ts
```

---

## Seven-Agent Architecture

Agents read ONLY from the PostgreSQL warehouse.
No agent calls HealthEx, Synthea, or any external API directly.

```
EXTERNAL SOURCES  →  DATA INGESTION SERVICE  →  PATIENT DATA WAREHOUSE
                                                         |
                                              MCP TOOL REGISTRY (Replit)
                                                         |
        Health Data | Synthesis | Crisis | Provider Brief | Nudge
                                         |
                                  Orchestrator Agent
                                         |
                                  UX Surface Agent
                                         |
                                  REPLIT NEXT.JS APP
```

### Agent to model mapping

| Agent           | Model            | Reason                           |
|-----------------|------------------|----------------------------------|
| Orchestrator    | claude-sonnet-4  | Routing decisions, low latency   |
| Health Data     | claude-haiku-4   | Structured retrieval, fast       |
| Synthesis       | claude-sonnet-4  | Pattern detection, OBT reasoning |
| UX Surface      | claude-sonnet-4  | Component selection, phase logic |
| Crisis          | claude-sonnet-4  | Always-on 15-min polling loop    |
| Provider Brief  | claude-opus-4    | Deep 6-month synthesis, complex  |
| Nudge           | claude-haiku-4   | Short-form generation, fast      |

---

## Stack

### Ingestion Service + MCP Server (Python)

```
Python          3.11+
FastMCP         3.x          pip install fastmcp
asyncpg         latest       pip install asyncpg
faker           latest       pip install faker
numpy           latest       pip install numpy
python-dateutil latest       pip install python-dateutil
pytest          latest       pip install pytest pytest-asyncio
```

### Replit App (Node.js)

```
Next.js         14  (App Router, TypeScript strict)
Tailwind CSS    3.x
shadcn/ui       card badge tabs progress button input
pg              node-postgres
recharts        time-series charts
Jest 29         + React Testing Library 14 + MSW
```

### Database

```
PostgreSQL 16   Replit Postgres (Neon-backed)
                Connect: DATABASE_URL env var (Replit Secret)
                Schema:  psql $DATABASE_URL < mcp-server/db/schema.sql
                Tables:  21 total
```

---

## Environment Variables

| Variable             | Value                            | Set in        |
|----------------------|----------------------------------|---------------|
| DATABASE_URL         | postgresql://... (Neon)          | Replit Secret |
| DATA_TRACK           | synthea                          | Replit Secret |
| SYNTHEA_OUTPUT_DIR   | /home/runner/synthea-output      | Replit Secret |
| ANTHROPIC_API_KEY    | sk-ant-...                       | Replit Secret |

Never hardcode credentials. Always read from os.environ.

---

## Dual-Track Data Strategy

```
DATA_TRACK=synthea   → ingestion/adapters/synthea.py   → PatientRecord
DATA_TRACK=healthex  → ingestion/adapters/healthex.py  → PatientRecord (Phase 2)
```

Both adapters produce identical PatientRecord output.
The warehouse schema is identical for both tracks.
Phase 1: DATA_TRACK=synthea only. Do not activate healthex adapter.

### What each source covers vs what custom generators must fill

| Data domain          | Synthea | HealthEx | Custom generator  |
|----------------------|---------|----------|-------------------|
| Patient demographics | YES     | YES      | No                |
| Conditions (ICD-10)  | YES     | YES      | No                |
| Medications (RxNorm) | YES     | YES      | No                |
| Lab results (LOINC)  | YES     | YES      | No                |
| In-clinic vitals     | YES     | YES      | No                |
| Home device vitals   | NO      | NO       | vitals_timeseries |
| Daily check-ins      | NO      | NO       | behavioral_model  |
| SDoH flags           | PARTIAL | PARTIAL  | sdoh_profile      |
| Medication adherence | NO      | NO       | behavioral_model  |

---

## 21-Table PostgreSQL Schema

Full DDL in mcp-server/db/schema.sql.
All tables have: data_source VARCHAR(50) NOT NULL DEFAULT 'synthea'

### Original 17 tables (+ data_source column added)

```
patients                UNIQUE: mrn
patient_conditions      FK → patients CASCADE
patient_medications     FK → patients CASCADE
patient_sdoh_flags      UNIQUE: patient_id + domain
biometric_readings      INDEX: patient_id + metric_type + measured_at DESC
daily_checkins          UNIQUE: patient_id + checkin_date
medication_adherence    UNIQUE: patient_id + medication_id + adherence_date
clinical_events         INDEX: patient_id + event_date DESC
care_gaps               INDEX: patient_id + status
obt_scores              UNIQUE: patient_id + score_date
clinical_facts          INDEX: ttl_expires_at (for expiry queries)
behavioral_correlations FK → patients
agent_interventions     INDEX: patient_id + delivered_at DESC
agent_memory_episodes   FK → patients
skill_executions        INDEX: execution_date + skill_name
provider_risk_scores    UNIQUE: patient_id + score_date
pipeline_runs           audit table (standalone)
```

### 4 new ingestion management tables

```
data_sources
  id UUID PK
  patient_id UUID FK → patients.id
  source_name VARCHAR(50)        -- synthea | healthex | withings | apple_health | manual
  is_active BOOLEAN
  auth_token_ref VARCHAR(200)    -- reference to OAuth token (not stored here)
  connected_at TIMESTAMPTZ
  UNIQUE(patient_id, source_name)

source_freshness
  id UUID PK
  patient_id UUID FK → patients.id
  source_name VARCHAR(50)
  last_ingested_at TIMESTAMPTZ
  records_count INT
  ttl_hours INT                  -- staleness threshold
  is_stale BOOL GENERATED        -- (last_ingested_at + ttl_hours * INTERVAL '1h' < NOW())
  UNIQUE(patient_id, source_name)

ingestion_log
  id UUID PK
  patient_id UUID FK → patients.id
  source_name VARCHAR(50)
  status VARCHAR(20)             -- completed | failed | skipped_fresh | partial
  records_upserted INT
  conflicts_detected INT
  duration_ms INT
  error_message TEXT
  retry_count INT DEFAULT 0
  triggered_by VARCHAR(50)       -- schedule | pre_visit | force_refresh
  started_at TIMESTAMPTZ DEFAULT NOW()

raw_fhir_cache
  id UUID PK
  patient_id UUID FK → patients.id
  source_name VARCHAR(50)
  resource_type VARCHAR(50)      -- Patient | Observation | Condition | etc
  raw_json JSONB
  fhir_resource_id VARCHAR(100)  -- source system ID
  retrieved_at TIMESTAMPTZ DEFAULT NOW()
  processed BOOL DEFAULT false
  UNIQUE(patient_id, source_name, fhir_resource_id)
```

---

## Data Ingestion Pipeline — Six Stages

The ingestion service runs BEFORE the agent skill pipeline each day.

```
Stage 1: Adapter selection    read DATA_TRACK → import adapter class
Stage 2: Freshness check      query source_freshness → skip if not stale
Stage 3: Raw retrieval        adapter.fetch(patient_id) → raw FHIR JSON
Stage 4: Cache raw bundle     INSERT INTO raw_fhir_cache (before transform)
Stage 5: Normalization        fhir_to_schema.py → flat DB records
Stage 6: Conflict resolution  patient-reported > device > HealthEx > Synthea
Stage 7: Warehouse write      INSERT ... ON CONFLICT DO UPDATE
Stage 8: Update freshness     UPDATE source_freshness SET last_ingested_at=NOW()
```

### Freshness TTL per source

| Source         | TTL       | Trigger                                  |
|----------------|-----------|------------------------------------------|
| Synthea        | Never     | One-time seed + daily increments         |
| HealthEx       | 24 hours  | Nightly 3AM + pre-visit force-refresh    |
| Device APIs    | 1 hour    | Webhooks (Phase 3) or polling fallback   |
| Patient input  | Real-time | Direct write on check-in submit          |
| OBT scores     | 24 hours  | Computed daily by Synthesis Agent        |
| Clinical facts | 30-72h-1yr| TTL set per fact type by skills          |

---

## MCP Server Absolute Rules

```
RULE 1  NEVER print() in tool handlers or generators
        FastMCP uses stdout for JSON-RPC. print() corrupts the stream.
        All logging: logging.basicConfig(level=logging.INFO, stream=sys.stderr)
        VERIFY: grep -r "print(" mcp-server/skills/ mcp-server/generators/ → EMPTY

RULE 2  ALL SQL parameterized (no f-strings)
        CORRECT:   await conn.execute("... WHERE id=$1", patient_id)
        WRONG:     await conn.execute(f"... WHERE id='{patient_id}'")
        VERIFY: grep -rn 'f".*SELECT\|f".*INSERT\|f".*UPDATE' mcp-server/ → EMPTY

RULE 3  Every tool returns a string
        FastMCP requirement. Never return dict, list, or None.
        Success: return f"OK Generated {N} readings for {patient_id}"
        Error:   return f"Error: {str(e)}"

RULE 4  Every tool catches all exceptions
        try/except wraps the entire tool body.
        On exception: log to skill_executions status='failed', return error string.

RULE 5  Every tool logs to skill_executions
        Both on success (status='completed') and failure (status='failed').
        This is the pipeline audit trail.

RULE 6  Idempotent writes only
        All INSERTs use ON CONFLICT DO NOTHING or ON CONFLICT DO UPDATE.
        Running the same tool twice for the same date must produce the same state.

RULE 7  data_source column on every INSERT
        Every row written to any table must include data_source.
        Phase 1 default: 'synthea'
```

---

## Vital Sign Physiological Ranges

```
Metric           Unit    Min    Max    Behavioral model notes
bp_systolic      mmHg     90    180    Baseline ~141. Morning +8, evening -5.
                                       EOM (days 25-31): +11 avg. StdDev >= 8.
bp_diastolic     mmHg     55    115    Correlated with systolic (r > 0.7).
                                       Pulse pressure always 20-80 mmHg.
glucose_fasting  mg/dL    70    300    EOM spike +25 avg. Stress days +20.
                                       Postprandial = fasting + 30-80.
hrv_rmssd        ms       12    100    Lower = more stress. Correlates with mood.
spo2             %        88    100    Stable 95-99 baseline.
steps_daily      count   800  14000    Weekday vs weekend +/-20%.
                                       Crisis month: -40% from baseline.
sleep_hours      hours   4.0    9.5    Normal avg 7.2. Crisis avg 5.8.
stress_level     int       1     10    Normal avg 4. Caregiver stress avg 7.5.
check-in rate              65%   90%   Normal. Crisis: 55-75%.
```

---

## OBT Score Algorithm

Implement exactly. Weights are non-negotiable.

```python
# Domain scores 0-100, based on patient's OWN 30-day baseline
bp_score       = deviation_score(avg_systolic, patient_baseline_systolic,
                                 good_threshold=5mmHg, bad_threshold=30mmHg)
glucose_score  = deviation_score(avg_fasting, patient_baseline_fasting,
                                 good_threshold=10mg_dL, bad_threshold=60mg_dL)
behavioral     = normalize(mood_avg, energy_avg)  # great=5,good=4,okay=3,low=2,bad=1
adherence      = (taken_count / total_due) * 100
sleep          = 100 if 7.0 <= avg_sleep <= 9.0 else linear_decay

score = (bp_score * 0.30 + glucose_score * 0.25 + behavioral * 0.20
         + adherence * 0.15 + sleep * 0.10)

primary_driver  = domain with LOWEST individual score
trend_direction = this-week avg vs last-week avg
confidence      = 1.0 (>=14d) | 0.7 (7-13d) | 0.4 (<7d)

# DB writes
INSERT INTO obt_scores ON CONFLICT (patient_id, score_date) DO UPDATE
INSERT INTO clinical_facts ttl_expires_at = NOW() + INTERVAL '30 days'
                           source_skill = 'compute_obt_score'
```

---

## Skill Registry Pattern

```python
# skills/__init__.py — auto-discovery
import importlib, pkgutil, logging, sys

def load_skills(mcp):
    import skills
    for _, modname, _ in pkgutil.iter_modules(skills.__path__):
        if modname in ("base",) or modname.startswith("_"):
            continue
        module = importlib.import_module(f"skills.{modname}")
        if hasattr(module, "register"):
            module.register(mcp)
            logging.info(f"Loaded skill: {modname}")  # stderr only

# skills/my_skill.py — canonical template for every skill file
from fastmcp import FastMCP
from fastmcp.dependencies import Depends
from db.connection import get_pool
import logging, json, sys

def register(mcp: FastMCP):
    @mcp.tool
    async def tool_name(patient_id: str, pool=Depends(get_pool)) -> str:
        """Brief description under 100 chars.
        Args:
            patient_id: UUID of patient in the database
        """
        try:
            async with pool.acquire() as conn:
                await conn.execute(
                    "INSERT INTO skill_executions "
                    "(skill_name, patient_id, status, output_data) "
                    "VALUES ($1, $2, $3, $4)",
                    "tool_name", patient_id, "completed", json.dumps({"n": 0})
                )
            return f"OK Done for {patient_id}"
        except Exception as e:
            logging.error(f"tool_name failed: {e}")
            async with pool.acquire() as conn:
                await conn.execute(
                    "INSERT INTO skill_executions "
                    "(skill_name, patient_id, status, error_message) "
                    "VALUES ($1, $2, $3, $4)",
                    "tool_name", patient_id, "failed", str(e)
                )
            return f"Error: {str(e)}"
```

---

## .mcp.json

Place in mcp-server/ directory:

```json
{
  "mcpServers": {
    "patient-companion": {
      "command": "python",
      "args": ["server.py"],
      "env": {
        "DATABASE_URL": "${DATABASE_URL}",
        "DATA_TRACK": "synthea",
        "SYNTHEA_OUTPUT_DIR": "${SYNTHEA_OUTPUT_DIR}",
        "ANTHROPIC_API_KEY": "${ANTHROPIC_API_KEY}"
      }
    }
  }
}
```

---

## Frontend Component Requirements

data-testid attributes are tested by Jest. Missing = failing test.

### OBTScoreCard.tsx (F1-F8)
```
"use client"                    required
data-testid="score-skeleton"    when data is null
data-color="green"              score >= 70
data-color="amber"              score 40-69
data-color="red"                score < 40
data-testid="trend-arrow"       always present
data-direction="up"             trend_direction==="improving"
data-direction="down"           trend_direction==="declining"
data-direction="right"          trend_direction==="stable"
"Limited data — score may vary" when confidence < 0.6 (exact text match)
```

### VitalsChart.tsx (F9-F15)
```
data-testid="vitals-chart-container"
Tab buttons: "BP" | "Glucose" | "HRV"
Time range buttons: "7d" | "30d" | "90d"
data-count attribute on chart element   updates on range change
"No data yet"                           when readings prop is empty
```

### CheckInFlow.tsx (F16-F24)
```
5 steps: mood → energy → stress → sleep → medications
Validation: cannot advance without selecting (Next disabled)
data-testid="submit-checkin"    on submit button (step 5)
"Check-in complete"             on successful POST
"Try again"                     on API 500 error
```

### Provider components (F32-F38)
```
data-testid="patient-name"      on each row in ChaseList
data-testid="sdoh-{domain}"     e.g. "sdoh-food_access"
role="progressbar"              on each care gap bar
data-testid="memory-episode"    on each AgentMemoryLog row
```

---

## Test Suite Reference

```
Total: 90 tests

Backend — pytest mcp-server/tests/
  V1-V14  Vital sign generators  14 tests
  A1-A8   Synthea FHIR adapter    8 tests
  S1-S18  MCP skill tools + DB   18 tests
  D1-D12  Schema integrity       12 tests
  Total:  52 backend tests

Frontend — npm test (replit-app/)
  F1-F8   OBTScoreCard            8 tests
  F9-F15  VitalsChart             7 tests
  F16-F24 CheckInFlow             9 tests
  F25-F31 API routes              7 tests
  F32-F38 Provider + SDoH         7 tests
  Total:  38 frontend tests

Run backend:   cd mcp-server && pytest tests/ -v --tb=short
Run frontend:  cd replit-app && npm test -- --coverage
```

---

## Daily Pipeline Sequence

```
STEP 1 — Freshness check (run before agents)
  For each patient:
    check_data_freshness(patient_id)
    If stale: run_ingestion(patient_id, force_refresh=False)
    Wait for completion before running agent skills.

STEP 2 — Agent skill pipeline
  For each patient:
    1. generate_daily_vitals(patient_id, days_back=1)
    2. generate_daily_checkins(patient_id, days_back=1)
    3. compute_obt_score(patient_id, score_date=today)
    4. run_sdoh_assessment(patient_id)
    5. run_crisis_escalation(patient_id, lookback_days=7)
    6. run_food_access_nudge(patient_id, current_date=today)
    7. compute_provider_risk(patient_id)

  Error handling: per-skill try/except
  Failed skill → log skill_executions status=failed → continue

STEP 3 — Summary
  Report: patients, records upserted, escalations, stale sources
```

---

## Session Plans

### Pre-session setup (manual, done once)

```bash
git clone https://github.com/aliomraniH/ambient-patient-companion.git
cd ambient-patient-companion

# Synthea install and patient generation
mkdir -p ~/synthea && cd ~/synthea
wget -q https://github.com/synthetichealth/synthea/releases/download/master-branch-latest/synthea-with-dependencies.jar
java -Xmx1g -jar synthea-with-dependencies.jar \
  -p 10 -s 42 \
  --exporter.fhir.export true \
  --exporter.baseDirectory ~/synthea-output \
  --exporter.years_of_history 6 \
  Massachusetts Boston
# Verify: ls ~/synthea-output/fhir/ | wc -l → 10

# Python deps
pip install fastmcp asyncpg faker numpy python-dateutil pytest pytest-asyncio

# Replit Secrets (Tools → Secrets in sidebar)
# DATABASE_URL, DATA_TRACK=synthea, SYNTHEA_OUTPUT_DIR, ANTHROPIC_API_KEY
```

---

### Session 1 — Ingestion Service + MCP Server (4-5 hrs)

Build order (strict dependency order):

```
ingestion/adapters/base.py
ingestion/adapters/synthea.py
ingestion/adapters/manual_entry.py
ingestion/conflict_resolver.py
ingestion/pipeline.py
ingestion/server.py

mcp-server/db/connection.py
mcp-server/generators/vitals_timeseries.py
mcp-server/generators/behavioral_model.py
mcp-server/generators/sdoh_profile.py
mcp-server/transforms/fhir_to_schema.py
mcp-server/skills/__init__.py
mcp-server/skills/base.py
mcp-server/skills/generate_patient.py
mcp-server/skills/generate_vitals.py
mcp-server/skills/generate_checkins.py
mcp-server/skills/compute_obt_score.py
mcp-server/skills/sdoh_assessment.py
mcp-server/skills/crisis_escalation.py
mcp-server/skills/ingestion_tools.py
mcp-server/server.py
mcp-server/.mcp.json
```

Verify:
```bash
python mcp-server/server.py 2>&1 | grep "Loaded skill"  # 9+ lines
grep -r "print(" mcp-server/skills/ mcp-server/generators/  # EMPTY
grep -rn 'f".*SELECT\|f".*INSERT' mcp-server/               # EMPTY
```

Commit message:
```
Session 1: Ingestion service + FastMCP server + 9 skills

- ingestion/: pipeline (6 stages), adapters, conflict resolver
- mcp-server/: asyncpg, generators, transforms, 9 skills, .mcp.json
- Verified: 9+ skills loaded, no stdout, no f-string SQL
```

---

### Session 2 — Schema + Seeding (2-3 hrs)

Pre-session manual step:
```bash
psql $DATABASE_URL < mcp-server/db/schema.sql
psql $DATABASE_URL -c "\dt" | wc -l  # must be 21+
```

Build:
```
mcp-server/orchestrator.py
mcp-server/seed.py
mcp-server/skills/previsit_brief.py
mcp-server/skills/food_access_nudge.py
mcp-server/skills/compute_provider_risk.py
```

Verify:
```bash
python mcp-server/seed.py --patients 2 --months 1  # quick test
psql $DATABASE_URL -c "SELECT COUNT(*) FROM patients;"           # 2
psql $DATABASE_URL -c "SELECT COUNT(*) FROM biometric_readings;" # 300+

python mcp-server/seed.py --patients 10 --months 6  # full seed
psql $DATABASE_URL -c "SELECT COUNT(*) FROM patients;"           # 10
psql $DATABASE_URL -c "SELECT COUNT(*) FROM biometric_readings;" # >10000
psql $DATABASE_URL -c "SELECT COUNT(*) FROM source_freshness;"   # 10
python mcp-server/orchestrator.py --daily  # run twice, same counts
```

Commit message:
```
Session 2: 21-table schema deployed, 10 patients seeded, orchestrator complete

- 21-table schema deployed to Replit Postgres
- seed.py: 10 patients x 6 months (~18K biometric readings)
- orchestrator.py: freshness-first daily pipeline
- 3 new skills: previsit_brief, food_access_nudge, compute_provider_risk
- source_freshness populated for all 10 patients
```

---

### Session 3 — Replit App UI (3-4 hrs)

Pre-session manual step:
```bash
cd replit-app
npx create-next-app@latest . --typescript --tailwind --app --src-dir=false --yes
npm install pg recharts
npx shadcn@latest init --yes
npx shadcn@latest add card badge tabs progress button input
npm run dev  # verify no errors
```

Build order:
```
lib/db.ts
app/api/obt/[id]/route.ts
app/api/vitals/[id]/route.ts
app/api/checkin/route.ts
app/api/patients/route.ts
app/api/sse/[id]/route.ts
components/OBTScoreCard.tsx
components/VitalsChart.tsx
components/CheckInFlow.tsx
components/SDoHFlags.tsx
components/ChaseList.tsx
components/CareGapTracker.tsx
components/AgentMemoryLog.tsx
app/patient/[id]/page.tsx
app/provider/page.tsx
app/page.tsx
```

Session 3 rules:
```
Server Components fetch data. "use client" only for interactive elements.
SSE not WebSockets — Replit WebSocket connections are unreliable.
All SQL parameterized (no string interpolation).
Mobile-first Tailwind.
ALL data-testid attributes from Frontend Requirements section above.
Run npm run build after every 3-4 files to catch TypeScript errors early.
```

Verify:
```bash
npm run build   # must exit 0
npm test        # must pass F1-F38 (38 tests)
psql $DATABASE_URL -c "SELECT id FROM patients LIMIT 1;"
# Navigate to /patient/[uuid] — verify OBT score renders
```

Commit message:
```
Session 3: Full Replit app — patient companion + provider panel

- lib/db.ts, 5 API routes, 7 components, 3 pages
- Patient dashboard: 3-tab UX (Today, Vitals, My Health)
- Provider panel: chase list + care gap tracker
- npm run build: 0 errors. npm test: 38/38 passing.
- Deployed to Replit Reserved VM.
```

---

## /compact Strategy

| Session   | Trigger point                                  |
|-----------|------------------------------------------------|
| Session 1 | After ingestion/ complete, before mcp-server/skills/ |
| Session 2 | After orchestrator.py, before seed run         |
| Session 3 | After API routes, before UI components         |

Always commit before /compact. Git is the safety net.
After /compact: CLAUDE.md content survives. Conversation details do not.

---

## Phase 1 Acceptance Criteria

All 15 must be true:

```
□  pytest tests/ -v exits 0  — 52 backend tests pass
□  npm test exits 0, coverage > 80%  — 38 frontend tests pass
□  SELECT COUNT(*) FROM patients = 10
□  SELECT COUNT(*) FROM biometric_readings > 10000
□  SELECT COUNT(DISTINCT patient_id) FROM obt_scores = 10
□  SELECT COUNT(*) FROM source_freshness = 10
□  Daily pipeline idempotent (run twice, same row counts)
□  MCP Inspector shows 9+ tools registered
□  Replit public URL returns 200
□  /patient/[uuid] renders OBT score card with correct data-color
□  Vitals chart shows 30-day data points
□  Check-in flow completes 5 steps, writes to daily_checkins
□  Provider panel shows patients sorted by risk_score DESC
□  grep -r "print(" mcp-server/skills/ → EMPTY
□  grep -rn 'f".*SELECT' mcp-server/ → EMPTY
```

---

## What Comes After Phase 1

```
Phase 2 — HealthEx adapter (1 Claude Code session)
  ingestion/adapters/healthex.py
  Connect HealthEx MCP to Claude Project
  DATA_TRACK=healthex
  Requires Claude.ai session (not Claude Code) for CLEAR OAuth consent

Phase 3 — Device APIs + LangSmith
  ingestion/adapters/withings.py, apple_health.py, dexcom.py
  LangSmith tracing around all agent calls
  Webhook-based continuous device data streams

Phase 4 — Full Orchestrator + LangGraph
  Multi-user authentication for real patient pilots
  LangGraph state machine for complex agent routing
  Clinical validation of OBT scoring algorithm
```

---

## Session Checklists

### Session 1 — Ingestion Service + MCP Server
```
Ingestion service:
- [ ] ingestion/adapters/base.py
- [ ] ingestion/adapters/synthea.py
- [ ] ingestion/adapters/manual_entry.py
- [ ] ingestion/conflict_resolver.py
- [ ] ingestion/pipeline.py (6-stage IngestionPipeline)
- [ ] ingestion/server.py

MCP server:
- [ ] mcp-server/db/connection.py
- [ ] mcp-server/generators/vitals_timeseries.py (4 generator functions)
- [ ] mcp-server/generators/behavioral_model.py
- [ ] mcp-server/generators/sdoh_profile.py
- [ ] mcp-server/transforms/fhir_to_schema.py
- [ ] mcp-server/skills/__init__.py
- [ ] mcp-server/skills/base.py
- [ ] mcp-server/skills/generate_patient.py
- [ ] mcp-server/skills/generate_vitals.py
- [ ] mcp-server/skills/generate_checkins.py
- [ ] mcp-server/skills/compute_obt_score.py
- [ ] mcp-server/skills/sdoh_assessment.py
- [ ] mcp-server/skills/crisis_escalation.py
- [ ] mcp-server/skills/ingestion_tools.py
- [ ] mcp-server/server.py
- [ ] mcp-server/.mcp.json

Verified:
- [ ] python server.py 2>&1 | grep "Loaded skill" shows 9+ lines
- [ ] grep -r "print(" mcp-server/skills/ → EMPTY
- [ ] grep -rn 'f".*SELECT' mcp-server/ → EMPTY
- [ ] git commit: "Session 1: Ingestion service + FastMCP server + 9 skills"
```

### Session 2 — Schema + Data + Pipeline
```
- [ ] Schema deployed: psql $DATABASE_URL < mcp-server/db/schema.sql
- [ ] 21 tables confirmed: psql -c "\dt" | wc -l
- [ ] mcp-server/orchestrator.py
- [ ] mcp-server/seed.py
- [ ] mcp-server/skills/previsit_brief.py
- [ ] mcp-server/skills/food_access_nudge.py
- [ ] mcp-server/skills/compute_provider_risk.py
- [ ] Quick seed: 2 patients, 1 month verified
- [ ] Full seed: 10 patients x 6 months complete
- [ ] COUNT(*) FROM patients = 10
- [ ] COUNT(*) FROM biometric_readings > 10000
- [ ] COUNT(*) FROM source_freshness = 10
- [ ] Pipeline idempotent (run twice, same counts)
- [ ] git commit: "Session 2: 21-table schema deployed, 10 patients seeded"
```

### Session 3 — Replit App UI
```
- [ ] lib/db.ts
- [ ] app/api/obt/[id]/route.ts
- [ ] app/api/vitals/[id]/route.ts
- [ ] app/api/checkin/route.ts
- [ ] app/api/patients/route.ts
- [ ] app/api/sse/[id]/route.ts
- [ ] components/OBTScoreCard.tsx (data-testid attrs verified)
- [ ] components/VitalsChart.tsx
- [ ] components/CheckInFlow.tsx (5 steps + validation)
- [ ] components/SDoHFlags.tsx
- [ ] components/ChaseList.tsx
- [ ] components/CareGapTracker.tsx
- [ ] components/AgentMemoryLog.tsx
- [ ] app/patient/[id]/page.tsx (3-tab layout)
- [ ] app/provider/page.tsx
- [ ] app/page.tsx (patient selector)
- [ ] npm run build exits 0
- [ ] npm test: 38/38 passing
- [ ] Manual: /patient/[uuid] renders OBT score card
- [ ] Manual: check-in flow writes to daily_checkins
- [ ] Deployed to Replit Reserved VM (public URL working)
- [ ] git commit: "Session 3: Full Replit app — patient companion + provider panel"
```

---

*Last updated: Architecture reset — all three sessions pending*
*Repo: https://github.com/aliomraniH/ambient-patient-companion*
