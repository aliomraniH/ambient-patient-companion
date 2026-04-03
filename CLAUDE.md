# Patient Companion — Claude Code Context

> **Read this file completely before writing any code.**
> This is the single source of truth for the entire Phase 1 build.
> Update the session checklists at the bottom as you work.

---

## Visual Reference Cards

Read these images before planning any session. They contain the complete
build spec, file order, physiological ranges, schema, and acceptance criteria.

```
cc_01_architecture.png  — System architecture + dual-track + 3-session map
cc_02_session1_mcp.png  — Session 1: 15 files in build order + OBT algorithm
cc_03_session2_data.png — Session 2: 17-table schema + pipeline + seed targets
cc_04_session3_ui.png   — Session 3: component specs + data-testid requirements
cc_05_acceptance.png    — 15-item acceptance criteria + daily pipeline + /compact
```

---

## Project Overview

**What we are building:** An ambient patient companion — a daily data pipeline
that generates synthetic health data for chronic disease patients and serves it
through a patient-facing UX (One Big Thing score, vitals charts, check-in flow)
and a provider-facing panel (risk scores, care gaps, chase list).

**Build approach:** Three Claude Code sessions. Each session is autonomous —
one opening prompt, Claude Code works to completion, commits, closes.

**This file lives at:** `patient-companion/CLAUDE.md` (project root)
**Subdirectory context files:**
- `mcp-server/CLAUDE.md` — MCP server session detail (Sessions 1 + 2)
- `replit-app/CLAUDE.md` — Next.js app session detail (Session 3)

---

## Repository Structure

```
patient-companion/
├── CLAUDE.md                        ← you are here
├── cc_01_architecture.png           ← plan mode reference
├── cc_02_session1_mcp.png           ← plan mode reference
├── cc_03_session2_data.png          ← plan mode reference
├── cc_04_session3_ui.png            ← plan mode reference
├── cc_05_acceptance.png             ← plan mode reference
│
├── mcp-server/                      ← Sessions 1 + 2
│   ├── CLAUDE.md
│   ├── .mcp.json                    ← Claude Code MCP connection
│   ├── server.py                    ← FastMCP entry point
│   ├── config.py                    ← env vars, DATA_TRACK selection
│   ├── orchestrator.py              ← daily pipeline sequencer
│   ├── seed.py                      ← argparse: --patients 10 --months 6
│   ├── db/
│   │   ├── connection.py            ← asyncpg pool + get_pool()
│   │   └── schema.sql               ← 17-table DDL (source of truth)
│   ├── adapters/
│   │   ├── base.py                  ← PatientRecord dataclass + BaseAdapter
│   │   ├── synthea.py               ← Track A: FHIR Bundle parser
│   │   └── healthex.py              ← Track B: HealthEx MCP (Phase 2 only)
│   ├── generators/
│   │   ├── vitals_timeseries.py     ← BP, glucose, HRV, steps generators
│   │   ├── behavioral_model.py      ← mood, energy, adherence patterns
│   │   └── sdoh_profile.py          ← SDoH flag generation
│   ├── transforms/
│   │   └── fhir_to_schema.py        ← FHIR resources → DB table records
│   ├── skills/
│   │   ├── __init__.py              ← auto-discovery loader
│   │   ├── base.py                  ← BaseSkill abstract class
│   │   ├── generate_patient.py
│   │   ├── generate_vitals.py
│   │   ├── generate_checkins.py
│   │   ├── compute_obt_score.py
│   │   ├── sdoh_assessment.py
│   │   ├── crisis_escalation.py
│   │   ├── previsit_brief.py
│   │   ├── food_access_nudge.py
│   │   └── compute_provider_risk.py
│   └── tests/
│       ├── conftest.py
│       ├── test_generators.py       ← V1-V14 (14 tests)
│       ├── test_adapter.py          ← A1-A8  (8 tests)
│       ├── test_skills.py           ← S1-S18 (18 tests)
│       └── test_schema.py           ← D1-D12 (12 tests)
│
└── replit-app/                      ← Session 3
    ├── CLAUDE.md
    ├── app/
    │   ├── page.tsx                 ← patient selector home
    │   ├── patient/[id]/page.tsx    ← ambient companion (3-tab)
    │   ├── provider/page.tsx        ← chase list + care gaps
    │   └── api/
    │       ├── obt/[id]/route.ts
    │       ├── vitals/[id]/route.ts
    │       ├── checkin/route.ts
    │       ├── patients/route.ts
    │       └── sse/[id]/route.ts
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
        │   ├── OBTScoreCard.test.tsx  ← F1-F8
        │   ├── VitalsChart.test.tsx   ← F9-F15
        │   ├── CheckInFlow.test.tsx   ← F16-F24
        │   └── provider.test.tsx      ← F32-F38
        └── api/
            └── routes.test.ts         ← F25-F31
```

---

## Stack

### MCP Server (mcp-server/)
```
Python         3.11+
FastMCP        3.x          pip install fastmcp
asyncpg        latest       pip install asyncpg
faker          latest       pip install faker
numpy          latest       pip install numpy
python-dateutil latest      pip install python-dateutil
pytest         latest       pip install pytest pytest-asyncio
```

### Replit App (replit-app/)
```
Next.js        14 (App Router)
TypeScript     strict
Tailwind CSS   3.x
shadcn/ui      card badge tabs progress button input
pg             node-postgres
recharts       time-series charts
Jest 29        + React Testing Library 14
MSW            mock service worker for API tests
```

### Database
```
PostgreSQL     16  (Replit Postgres / Neon-backed)
               Connect via: DATABASE_URL env var (Replit Secret)
               Deploy schema: psql $DATABASE_URL < mcp-server/db/schema.sql
```

---

## Environment Variables

| Variable            | Value                          | Where set       |
|---------------------|-------------------------------|-----------------|
| `DATABASE_URL`      | postgresql://...               | Replit Secret   |
| `DATA_TRACK`        | `synthea`                      | Replit Secret   |
| `SYNTHEA_OUTPUT_DIR`| `/home/runner/synthea-output`  | Replit Secret   |

**Never hardcode credentials. Always read from os.environ.**

---

## Data Track Strategy

This project uses a dual-track data architecture. The adapter layer abstracts
the data source — all downstream code (skills, schema, UX) is identical.

```
DATA_TRACK=synthea   → adapters/synthea.py  → PatientRecord
DATA_TRACK=healthex  → adapters/healthex.py → PatientRecord (Phase 2)
```

**Phase 1 (this build): DATA_TRACK=synthea only.**
Do not build or activate adapters/healthex.py in Phase 1.

### Track A — Synthetic (Phase 1)
- Source: Synthea CLI generates FHIR R4 Bundle JSON files
- Location: `$SYNTHEA_OUTPUT_DIR/fhir/*.json`
- Auth required: None
- Gaps vs HealthEx: Synthea has no wearable data, no behavioral check-ins,
  no SDoH flags → custom generators fill these three gaps

### Track B — HealthEx (Phase 2, not this build)
- Source: HealthEx MCP at api.healthex.io/mcp
- Auth: CLEAR biometric identity + OAuth 2.0 (Claude.ai sessions only)
- NOT compatible with Claude Code (GitHub issue #25171, closed)
- Same FHIR R4 format → same transformer pipeline as Track A

---

## Synthea Data

```bash
# Generated with:
java -Xmx1g -jar synthea-with-dependencies.jar \
  -p 10 -s 42 \
  --exporter.fhir.export true \
  --exporter.baseDirectory ~/synthea-output \
  --exporter.years_of_history 6 \
  Massachusetts Boston

# Output: ~/synthea-output/fhir/*.json (10 files)
# Each file: FHIR R4 Bundle with Patient, Condition, MedicationRequest,
#            Observation, Encounter, AllergyIntolerance, Immunization
```

---

## MCP Server — Absolute Rules

Violating any of these will cause silent failures or corrupted output.
Check every file before committing.

```
RULE 1 — NEVER stdout in tool handlers
  FastMCP uses stdout for JSON-RPC protocol.
  Any print() call corrupts the JSON-RPC stream.
  ALL logging: logging.basicConfig(level=logging.INFO, stream=sys.stderr)
  Verify: grep -r "print(" skills/ generators/ adapters/ server.py → MUST BE EMPTY

RULE 2 — ALL SQL parameterized
  Correct:   await conn.execute("SELECT * FROM patients WHERE id=$1", patient_id)
  Incorrect: await conn.execute(f"SELECT * FROM patients WHERE id='{patient_id}'")
  Verify: grep -rn "f\".*SELECT\|f\".*INSERT\|f\".*UPDATE\|f\".*DELETE" . → MUST BE EMPTY

RULE 3 — Every tool returns a string
  FastMCP requirement. Return a summary string, never dict, list, or None.
  On success: return f"✓ Generated {N} readings for patient {patient_id}"
  On error:   return f"Error: {str(e)}"  (never raise from a tool handler)

RULE 4 — Every tool catches exceptions
  Wrap the entire tool body in try/except.
  On exception: log to skill_executions (status='failed'), return error string.

RULE 5 — Every tool logs to skill_executions
  INSERT INTO skill_executions (skill_name, patient_id, status, output_data, ...)
  Do this on both success and failure. This is the pipeline audit trail.

RULE 6 — Idempotent writes only
  All INSERTs use ON CONFLICT DO NOTHING or ON CONFLICT (...) DO UPDATE.
  Running the same tool twice for the same date must produce the same DB state.
```

---

## Vital Sign Physiological Ranges

All generators MUST produce values within these bounds.
Flat/constant output is a bug — require realistic variance.

```
Metric           Unit    Min    Max    Behavioral model requirements
─────────────────────────────────────────────────────────────────────────────
bp_systolic      mmHg     90    180    Baseline ~141. Morning +8, evening -5.
                                       EOM (days 25-31): +11 avg. StdDev ≥ 8.
bp_diastolic     mmHg     55    115    Correlated with systolic (r > 0.7).
                                       Pulse pressure always 20-80 mmHg.
glucose_fasting  mg/dL    70    300    EOM spike +25 avg vs mid-month.
                                       Stress days (score ≥ 8): +20 avg.
                                       Postprandial = fasting + 30-80.
glucose_post     mg/dL    70    380    Always ≥ fasting (glucose rises post-meal).
hrv_rmssd        ms       12    100    Lower = more stress. Correlates with mood.
                                       7-day rolling average computed for OBT.
spo2             %        88    100    Stable 95-99 baseline. Dips only in COPD.
steps_daily      count   800  14000    Weekday vs weekend ±20%.
                                       Crisis month: -40% from baseline.
weight           kg       45    180    Weekly only. Drift ±0.3 kg/week.

Check-in stress_level   int   1    10    Correlates with BP and glucose.
Check-in sleep_hours  float  4.0   9.5   Normal avg 7.2. Crisis avg 5.8.
Check-in adherence rate:                 Normal 65-90%. Crisis 55-75%.
```

---

## OBT Score Algorithm

Implement exactly as specified. Do not substitute a simpler calculation.

```python
# Domain scores (each 0-100)
# Based on deviation from patient's OWN 30-day baseline — not population norms

bp_score       = deviation_to_score(avg_systolic, patient_baseline_systolic,
                                    good_threshold=5, bad_threshold=30)
glucose_score  = deviation_to_score(avg_fasting, patient_baseline_fasting,
                                    good_threshold=10, bad_threshold=60)
behavioral_score = normalize(mood_numeric_avg, energy_numeric_avg)
                 # mood: great=5, good=4, okay=3, low=2, bad=1
adherence_score  = pct_taken_in_30d * 100
sleep_score      = 100 if 7.0 <= avg_sleep_hours <= 9.0 else degraded_linearly

# Final score
score = (bp_score       * 0.30 +
         glucose_score  * 0.25 +
         behavioral_score * 0.20 +
         adherence_score * 0.15 +
         sleep_score    * 0.10)

# Metadata
primary_driver  = domain with LOWEST score (most problematic today)
trend_direction = "improving" | "stable" | "declining"
                  (compare this-week avg to last-week avg)
confidence      = 1.0 if data_days >= 14
                  0.7 if 7 <= data_days < 14
                  0.4 if data_days < 7

# Database writes
INSERT INTO obt_scores (patient_id, score_date, score, primary_driver,
                         trend_direction, confidence)
ON CONFLICT (patient_id, score_date) DO UPDATE ...

INSERT INTO clinical_facts (patient_id, fact_type, category, summary,
                              ttl_expires_at, source_skill)
VALUES (..., NOW() + INTERVAL '30 days', 'compute_obt_score')
```

---

## 17-Table Schema Reference

Full DDL is in `mcp-server/db/schema.sql`. Key relationships:

```
patients                    (id UUID PK, mrn UNIQUE, is_synthetic BOOL)
  ├── patient_conditions    (patient_id FK → patients.id ON DELETE CASCADE)
  ├── patient_medications   (patient_id FK)
  ├── patient_sdoh_flags    (patient_id FK, UNIQUE patient_id+domain)
  ├── biometric_readings    (patient_id FK, INDEX patient_id+metric_type+measured_at)
  ├── daily_checkins        (patient_id FK, UNIQUE patient_id+checkin_date)
  ├── medication_adherence  (patient_id FK, UNIQUE patient_id+medication_id+date)
  ├── clinical_events       (patient_id FK, INDEX patient_id+event_date DESC)
  ├── care_gaps             (patient_id FK, INDEX patient_id+status)
  ├── obt_scores            (patient_id FK, UNIQUE patient_id+score_date)
  ├── clinical_facts        (patient_id FK, INDEX ttl_expires_at for expiry)
  ├── behavioral_correlations (patient_id FK)
  ├── agent_interventions   (patient_id FK, INDEX patient_id+delivered_at DESC)
  ├── agent_memory_episodes (patient_id FK)
  ├── skill_executions      (patient_id FK nullable, INDEX execution_date+skill_name)
  ├── provider_risk_scores  (patient_id FK, UNIQUE patient_id+score_date)
  ├── care_gap_progress     (patient_id FK, care_gap_id FK)
  └── pipeline_runs         (standalone audit table)

Key constraints:
  - obt_scores: UNIQUE (patient_id, score_date) — one score per patient per day
  - daily_checkins: UNIQUE (patient_id, checkin_date)
  - TTL pattern: clinical_facts where ttl_expires_at < NOW() are considered stale
  - All timestamps: TIMESTAMPTZ (UTC always)
  - All PKs: UUID DEFAULT uuid_generate_v4()
```

---

## Skill Registry Pattern

```python
# skills/__init__.py — auto-discovery
import importlib, pkgutil, logging

def load_skills(mcp):
    import skills
    for _, modname, _ in pkgutil.iter_modules(skills.__path__):
        if modname in ("base",) or modname.startswith("_"):
            continue
        module = importlib.import_module(f"skills.{modname}")
        if hasattr(module, "register"):
            module.register(mcp)
            logging.info(f"Loaded skill: {modname}")  # stderr only

# skills/my_skill.py — template
from fastmcp import FastMCP
from fastmcp.dependencies import Depends
from db.connection import get_pool
import logging, json

def register(mcp: FastMCP):
    @mcp.tool
    async def my_tool_name(patient_id: str, pool=Depends(get_pool)) -> str:
        """One-line description under 100 chars. Args listed below.

        Args:
            patient_id: UUID of the patient in the database
        """
        try:
            async with pool.acquire() as conn:
                # ... parameterized SQL only ...
                await conn.execute(
                    "INSERT INTO skill_executions (skill_name, patient_id, status)"
                    " VALUES ($1, $2, $3)",
                    "my_tool_name", patient_id, "completed"
                )
            return f"✓ Done for patient {patient_id}"
        except Exception as e:
            logging.error(f"my_tool_name failed: {e}")
            async with pool.acquire() as conn:
                await conn.execute(
                    "INSERT INTO skill_executions (skill_name, patient_id, status, error_message)"
                    " VALUES ($1, $2, $3, $4)",
                    "my_tool_name", patient_id, "failed", str(e)
                )
            return f"Error: {str(e)}"
```

---

## MCP Server Connection (.mcp.json)

Place this in `mcp-server/` for Claude Code to discover the server:

```json
{
  "mcpServers": {
    "patient-companion": {
      "command": "python",
      "args": ["server.py"],
      "env": {
        "DATABASE_URL": "${DATABASE_URL}",
        "DATA_TRACK": "synthea",
        "SYNTHEA_OUTPUT_DIR": "${SYNTHEA_OUTPUT_DIR}"
      }
    }
  }
}
```

Verify connection after Session 1:
```bash
python server.py 2>&1 | grep "Loaded skill"
# Expect: INFO: Loaded skill: generate_patient
#         INFO: Loaded skill: generate_vitals
#         ... (6+ lines, no errors)
```

---

## Daily Pipeline Order

Skills must run in this order — each depends on the previous:

```
1. generate_daily_vitals        → biometric_readings
2. generate_daily_checkins      → daily_checkins + medication_adherence
3. compute_obt_score            → obt_scores + clinical_facts (TTL=30d)
4. run_sdoh_assessment          → patient_sdoh_flags + agent_interventions
5. run_crisis_escalation        → agent_interventions + agent_memory_episodes
6. run_food_access_nudge        → agent_interventions (if day ≥ 25 + food_access flag)
7. compute_provider_risk        → provider_risk_scores

Error handling: per-skill try/except. Failed skill logs to skill_executions
with status='failed'. Pipeline continues to next skill regardless.
```

Daily prompt for Claude Code (after Phase 1 complete):
```
Run the daily data pipeline for all patients using the patient-companion
MCP tools. For each patient: generate_daily_vitals, generate_daily_checkins,
compute_obt_score, run_sdoh_assessment, run_crisis_escalation,
run_food_access_nudge. Show pipeline summary: patients processed, readings
generated, any escalations triggered.
```

---

## Frontend Component Requirements

Critical: these attributes are tested by the Jest test suite.
If they are missing or wrong, tests fail.

### OBTScoreCard.tsx (tests F1-F8)
```
Required: "use client" directive
data-testid="score-skeleton"    when data prop is null
data-color="green"              score >= 70
data-color="amber"              score 40-69
data-color="red"                score < 40
data-testid="trend-arrow"       always present
data-direction="up"             trend_direction === "improving"
data-direction="down"           trend_direction === "declining"
data-direction="right"          trend_direction === "stable"
Text "Limited data — score may vary"   when confidence < 0.6 (exact match)
```

### VitalsChart.tsx (tests F9-F15)
```
data-testid="vitals-chart-container"   outer wrapper
Tab buttons: "BP" | "Glucose" | "HRV"
Time range buttons: "7d" | "30d" | "90d"
data-count attribute on chart element  (updates on range change)
Text "No data yet"                     when readings prop is empty array
```

### CheckInFlow.tsx (tests F16-F24)
```
5 steps: mood → energy → stress → sleep → medications
Cannot advance without selecting a value (validation on Next click)
data-testid="submit-checkin"           on submit button (step 5)
Text "Check-in complete"               on successful submit
Button "Try again"                     on API 500 error
POST to /api/checkin with complete payload
```

### ChaseList.tsx (tests F32-F35)
```
data-testid="patient-name"            on each row name element
Sorted: risk_score DESC (highest risk first)
data-testid="sdoh-{domain}"           e.g. data-testid="sdoh-food_access"
role="progressbar"                    on each care gap bar
data-testid="memory-episode"          on each episode row
```

---

## Test Suite Reference

```
Backend — pytest mcp-server/tests/
  V1-V14  Vital sign generators    (14 tests)
  A1-A8   Synthea FHIR adapter     (8 tests)
  S1-S18  MCP skill tools + DB     (18 tests)
  D1-D12  Schema integrity         (12 tests)
  Total:  52 backend tests

Frontend — npm test (replit-app/)
  F1-F8   OBTScoreCard             (8 tests)
  F9-F15  VitalsChart              (7 tests)
  F16-F24 CheckInFlow              (9 tests)
  F25-F31 API routes               (7 tests)
  F32-F38 Provider + SDoH          (7 tests)
  Total:  38 frontend tests

Run all backend:   cd mcp-server && pytest tests/ -v --tb=short
Run all frontend:  cd replit-app && npm test -- --coverage
```

---

## Pre-Session Setup (Manual — Done Once)

These steps are done before any Claude Code session:

```bash
# 1. Install Synthea and generate patients
mkdir -p ~/synthea
cd ~/synthea
wget -q https://github.com/synthetichealth/synthea/releases/download/master-branch-latest/synthea-with-dependencies.jar
mkdir -p ~/synthea-output
java -Xmx1g -jar synthea-with-dependencies.jar \
  -p 10 -s 42 \
  --exporter.fhir.export true \
  --exporter.baseDirectory ~/synthea-output \
  --exporter.years_of_history 6 \
  Massachusetts Boston
# Verify: ls ~/synthea-output/fhir/ | wc -l  → 10

# 2. Install Python dependencies
pip install fastmcp asyncpg faker numpy python-dateutil pytest pytest-asyncio

# 3. Create directory skeleton
mkdir -p mcp-server/{skills,generators,adapters,db,transforms,tests}
mkdir -p replit-app/{app/{api,patient,provider},components,lib,tests}

# 4. Set Replit Secrets (Tools → Secrets in sidebar)
#    DATABASE_URL      = (from Tools → Database after creating Postgres)
#    DATA_TRACK        = synthea
#    SYNTHEA_OUTPUT_DIR = /home/runner/synthea-output

# 5. Deploy schema (after Session 1 builds db/schema.sql)
psql $DATABASE_URL < mcp-server/db/schema.sql
```

---

## Session Plans

### Session 1 — MCP Server (3-4 hrs · 120K tokens)

**Opening prompt:**
```
Read CLAUDE.md and the reference card images before planning.
Build the complete FastMCP MCP server for the Ambient Patient Companion.

File build order (dependency-first):
1.  db/connection.py
2.  adapters/base.py
3.  generators/vitals_timeseries.py
4.  generators/behavioral_model.py
5.  generators/sdoh_profile.py
6.  adapters/synthea.py
7.  skills/__init__.py
8.  skills/base.py
9.  skills/generate_patient.py
10. skills/generate_vitals.py
11. skills/generate_checkins.py
12. skills/compute_obt_score.py
13. skills/sdoh_assessment.py
14. skills/crisis_escalation.py
15. server.py

After each file: run python -c "from [module] import [key]; print('OK')"
After all files: run python server.py 2>&1 | head -30
Enforce all rules from CLAUDE.md. Update session checklist. Commit.
```

**Commit message:**
```
Session 1: FastMCP server + 6 skills + Synthea adapter

- FastMCP 3.x with stdio transport
- asyncpg pool with Depends() injection
- Auto-discovery skill loader (skills/__init__.py)
- Synthea FHIR Bundle parser (adapters/synthea.py)
- Generators: vitals_timeseries, behavioral_model, sdoh_profile
- Skills: generate_patient, generate_vitals, generate_checkins,
  compute_obt_score, sdoh_assessment, crisis_escalation
- Verified: 6 skills loaded, no stdout, no f-string SQL
```

---

### Session 2 — Data + Pipeline (2-3 hrs · 110K tokens)

**Pre-session (manual):**
```bash
# Paste 17-table DDL into mcp-server/db/schema.sql, then:
psql $DATABASE_URL < mcp-server/db/schema.sql
psql $DATABASE_URL -c "\dt" | wc -l  # must be 17+
```

**Opening prompt:**
```
Read CLAUDE.md. Session 1 is complete and committed. The 17-table
schema is deployed to the database.

Build in this order:
1. transforms/fhir_to_schema.py — 5 pure transform functions, no DB calls
2. orchestrator.py — run_daily_pipeline() + run_seed_pipeline()
3. seed.py — argparse --patients 10 --months 6
4. skills/previsit_brief.py
5. skills/food_access_nudge.py
6. skills/compute_provider_risk.py

Then run the seed:
  python seed.py --patients 2 --months 1    (quick test)
  psql $DATABASE_URL -c "SELECT COUNT(*) FROM biometric_readings;"

If quick test passes, run full seed:
  python seed.py --patients 10 --months 6

Verify idempotency:
  Run orchestrator twice for same patient, confirm same row counts.

Update session checklist. Commit.
```

**Commit message:**
```
Session 2: Schema deployed, transformers, 3 new skills, 10 patients seeded

- transforms/fhir_to_schema.py (5 transform functions)
- orchestrator.py with idempotent daily pipeline
- seed.py: 10 patients × 6 months seeded
- 3 new skills: previsit_brief, food_access_nudge, compute_provider_risk
- DB: 10 patients, ~18K biometric readings, ~1800 OBT scores
- Pipeline idempotency verified
```

---

### Session 3 — Replit App UI (3-4 hrs · 150K tokens)

**Pre-session (manual):**
```bash
cd replit-app
npx create-next-app@latest . --typescript --tailwind --app --src-dir=false --yes
npm install pg recharts
npx shadcn@latest init --yes
npx shadcn@latest add card badge tabs progress button input
npm run dev  # verify starts cleanly before opening Claude Code
```

**Opening prompt:**
```
Read CLAUDE.md and cc_04_session3_ui.png. Sessions 1 and 2 are complete.
The database has 10 synthetic patients with 6 months of health data.

Build the Replit app in this order:
1.  lib/db.ts
2.  app/api/obt/[id]/route.ts
3.  app/api/vitals/[id]/route.ts
4.  app/api/checkin/route.ts
5.  app/api/patients/route.ts
6.  app/api/sse/[id]/route.ts
7.  components/OBTScoreCard.tsx
8.  components/VitalsChart.tsx
9.  components/CheckInFlow.tsx
10. components/SDoHFlags.tsx
11. components/ChaseList.tsx
12. components/CareGapTracker.tsx
13. components/AgentMemoryLog.tsx
14. app/patient/[id]/page.tsx
15. app/provider/page.tsx
16. app/page.tsx

After every 3-4 files: npm run build — fix TypeScript errors before continuing.
Enforce all data-testid requirements from CLAUDE.md.
Use SSE not WebSockets for real-time updates.

After all files:
  npm run build   # must exit 0
  npm test        # must pass F1-F38

Get a real patient UUID: psql $DATABASE_URL -c "SELECT id FROM patients LIMIT 1;"
Navigate /patient/[uuid] and verify OBT score renders.

Update session checklist. Commit.
```

**Commit message:**
```
Session 3: Full Replit app — patient companion + provider panel

- Next.js 14 App Router, Tailwind, shadcn/ui, Recharts
- Components: OBTScoreCard, VitalsChart, CheckInFlow, SDoHFlags,
  ChaseList, CareGapTracker, AgentMemoryLog
- API routes: obt, vitals, checkin, patients, sse
- Patient dashboard: 3-tab UX (Today, Vitals, My Health)
- Provider panel: chase list + care gap tracker
- npm run build: 0 errors. npm test: 38/38 passing
```

---

## Context Management (/compact)

Use `/compact` at ~60% context window usage to prevent context exhaustion.

| Session   | /compact trigger point                        |
|-----------|-----------------------------------------------|
| Session 1 | After generators/ built, before skills/       |
| Session 2 | After orchestrator.py, before seed run        |
| Session 3 | After API routes, before UI components        |

**Before /compact:** commit all working code. Git is the safety net.
**After /compact:** CLAUDE.md content is preserved. Conversation details are not.

---

## Phase 1 Acceptance Criteria

All 15 must be true before Phase 1 is complete:

```
□  pytest tests/ -v exits 0 (52 backend tests pass)
□  npm test exits 0, coverage > 80% (38 frontend tests pass)
□  SELECT COUNT(*) FROM patients = 10
□  SELECT COUNT(*) FROM biometric_readings > 10,000
□  SELECT COUNT(DISTINCT patient_id) FROM obt_scores = 10
□  Daily pipeline idempotent (run twice → same row counts)
□  MCP Inspector shows 9 tools registered
□  Replit public URL returns 200
□  /patient/[uuid] renders OBT score card with correct data-color
□  Vitals chart shows 30-day data points
□  Check-in flow completes 5 steps, writes to daily_checkins
□  Provider panel shows patients sorted by risk_score DESC
□  grep -r "print(" skills/ generators/ adapters/ → EMPTY
□  grep -rn "f\".*SELECT\|f\".*INSERT" . → EMPTY
□  All CLAUDE.md session checklists fully checked off
```

---

## Session Checklists

Update these as you build. They persist across sessions via CLAUDE.md.

### Session 1 — MCP Server
```
- [x] db/connection.py
- [x] adapters/base.py
- [x] generators/vitals_timeseries.py (4 generator functions)
- [x] generators/behavioral_model.py
- [x] generators/sdoh_profile.py
- [x] adapters/synthea.py
- [x] skills/__init__.py (auto-discovery loader)
- [x] skills/base.py (BaseSkill ABC)
- [x] skills/generate_patient.py
- [x] skills/generate_vitals.py
- [x] skills/generate_checkins.py
- [x] skills/compute_obt_score.py
- [x] skills/sdoh_assessment.py
- [x] skills/crisis_escalation.py
- [x] server.py
- [x] .mcp.json
- [x] Verified: python server.py shows 6+ skills loaded, no errors
- [x] Verified: grep -r "print(" skills/ → EMPTY
- [x] Verified: grep -rn "f\".*SELECT" . → EMPTY
- [x] Committed: "Session 1: FastMCP server + 6 skills + Synthea adapter"
```

### Session 2 — Data + Pipeline
```
- [ ] db/schema.sql deployed (psql $DATABASE_URL < db/schema.sql)
- [ ] transforms/fhir_to_schema.py (5 transform functions)
- [ ] orchestrator.py (run_daily_pipeline + run_seed_pipeline)
- [ ] seed.py (argparse --patients --months)
- [ ] skills/previsit_brief.py
- [ ] skills/food_access_nudge.py
- [ ] skills/compute_provider_risk.py
- [ ] Quick test passed: 2 patients, 1 month, counts verified
- [ ] Full seed complete: 10 patients × 6 months
- [ ] Verified: SELECT COUNT(*) FROM patients = 10
- [ ] Verified: SELECT COUNT(*) FROM biometric_readings > 10000
- [ ] Verified: Daily pipeline idempotent (run twice, same counts)
- [ ] Committed: "Session 2: Schema deployed, transformers, 3 new skills, 10 patients seeded"
```

### Session 3 — Replit App UI
```
- [ ] lib/db.ts (pg Pool singleton + query<T>)
- [ ] app/api/obt/[id]/route.ts
- [ ] app/api/vitals/[id]/route.ts
- [ ] app/api/checkin/route.ts
- [ ] app/api/patients/route.ts
- [ ] app/api/sse/[id]/route.ts
- [ ] components/OBTScoreCard.tsx (data-testid attrs verified)
- [ ] components/VitalsChart.tsx (tab + time range filters)
- [ ] components/CheckInFlow.tsx (5-step wizard + validation)
- [ ] components/SDoHFlags.tsx
- [ ] components/ChaseList.tsx
- [ ] components/CareGapTracker.tsx
- [ ] components/AgentMemoryLog.tsx
- [ ] app/patient/[id]/page.tsx (3-tab layout)
- [ ] app/provider/page.tsx
- [ ] app/page.tsx (patient selector)
- [ ] npm run build exits 0 (zero TypeScript errors)
- [ ] npm test: 38/38 frontend tests passing
- [ ] Manual verify: /patient/[uuid] renders OBT score card
- [ ] Manual verify: Check-in flow completes and writes to DB
- [ ] Deployed to Replit Reserved VM (public URL working)
- [ ] Committed: "Session 3: Full Replit app — patient companion + provider panel"
```

---

## What Comes After Phase 1

Phase 2 (not this build):
```
adapters/healthex.py  — HealthEx MCP tool caller + FHIR-to-schema transformer
DATA_TRACK=healthex   — switch via Replit Secret
Personal records      — requires Claude.ai session for CLEAR OAuth consent
Phase 2 scope         — ~1 Claude Code session once Phase 1 acceptance criteria met
```

---

*Last updated: Phase 1 start — update this file as sessions complete*
