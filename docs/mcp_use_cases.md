# MCP Tool Use Cases — Story Line & Action Plan

## Patient Story: Maria Chen

**Patient**: Maria Chen, 54 F, Taiwanese-American  
**MRN**: MC-2025-4829  
**Conditions**: Type 2 Diabetes (HbA1c 8.1%), Stage 1 Hypertension, Obesity (BMI 31)  
**Medications**: Metformin 1000mg BID, Lisinopril 10mg QD, Atorvastatin 40mg QD  
**Provider**: Dr. Elena Martinez, PCP — Central Valley Health System  
**Enrolled in Ambient Patient Companion**: October 1, 2025  

---

## Six-Month Timeline

| Period | Phase | Key Events |
|--------|-------|-----------|
| Oct–Nov 2025 | **Stable** | Daily check-ins, normal vitals, 78% medication adherence |
| Dec 2025 | **Crisis** | Mother hospitalized — caregiver stress. BP spikes to 172, sleep < 5h, mood = 1 for 4 consecutive days, stress ≥ 8 |
| Jan 2026 | **Recovery** | Care manager outreach; mood & sleep slowly recovering; glucose still elevated |
| Feb 2026 | **Pre-visit** | Appointment with Dr. Martinez on Feb 14, 2026 |
| Mar–Apr 2026 | **Improvement** | OBT trending up; SDOH food-access flag triggers end-of-month nudge |

---

## MCP Tool Inventory (15 Tools)

### Group A — mcp-server Skills (10 tools)

| # | Tool | Trigger |
|---|------|---------|
| 1 | `generate_patient` | Onboard Maria Chen from manual fields |
| 2 | `generate_daily_vitals` | Simulate one day of device-uploaded biometrics |
| 3 | `generate_daily_checkins` | Simulate daily symptom/mood self-report |
| 4 | `compute_obt_score` | Calculate wellness score from 30+ days of data |
| 5 | `run_crisis_escalation` | Detect Dec 2025 BP spike + sustained low mood |
| 6 | `run_sdoh_assessment` | Screen for social risk factors (food access flagged) |
| 7 | `check_data_freshness` | Confirm all data sources are within TTL |
| 8 | `generate_previsit_brief` | Prepare 6-month summary for Feb 14 appointment |
| 9 | `run_food_access_nudge` | Trigger Mar 28 end-of-month food resource nudge |
| 10 | `compute_provider_risk` | Rank Maria on Dr. Martinez's chase list |

### Group B — Phase 1 Clinical Intelligence Server (5 tools)

| # | Tool | Trigger |
|---|------|---------|
| 11 | `get_synthetic_patient` | Retrieve full clinical record for chart review |
| 12 | `check_screening_due` | Identify overdue USPSTF screenings for 54F with DM |
| 13 | `flag_drug_interaction` | Check metformin + lisinopril + atorvastatin |
| 14 | `get_guideline` | Fetch ADA guideline 9.1a — HbA1c targets |
| 15 | `clinical_query` | Guardrail-filtered clinical question about her DM mgmt |

---

## Data Entry Agent Design

The **PatientDataEntryAgent** (`tests/e2e/data_entry_agent.py`) autonomously simulates
Maria Chen's full 6-month history of data entry.

### Phase Logic

```
Normal months (Oct–Nov 2025, Jan–Apr 2026):
  BP systolic: 128–148 mmHg
  Glucose fasting: 115–145 mg/dL
  Mood: okay(3) – good(4)
  Sleep: 6.5–8.0h
  Stress: 3–5
  Medication adherence: 78–95%

Crisis month (Dec 2025 — caregiver_stress scenario):
  BP systolic: 155–175 mmHg  ← triggers crisis (>170)
  Glucose fasting: 160–220 mg/dL
  Mood: bad(1) – low(2)      ← 4 consecutive days mood=1
  Sleep: 3.5–5.0h            ← triggers crisis (<5 for 3 days)
  Stress: 8–10               ← triggers crisis (≥8 for 3 days)
  Medication adherence: 30–55% ← missed doses
```

### What the Agent Seeds

1. **Patient record** — `patients` table with conditions + medications  
2. **Source freshness rows** — `source_freshness` for `wearable`, `ehr`, `manual`  
3. **Batch vitals** — `biometric_readings` (180 days × 6 metrics = ~1,080 rows)  
4. **Batch check-ins** — `daily_checkins` + `medication_adherence` (180 days)  
5. **SDOH flags** — `patient_sdoh_flags` including `food_access` severity `moderate`  

---

## Action Plan — Steps to Execute

### Step 1: Scaffold test infrastructure
- Create `tests/e2e/` directory  
- Write `conftest.py` with session-scoped DB pool + `maria_chen` fixture  

### Step 2: Implement PatientDataEntryAgent
- `setup_patient()` — upsert Maria Chen into DB, return deterministic UUID  
- `seed_historical_vitals()` — batch-insert 6 months of biometrics using generators  
- `seed_historical_checkins()` — batch-insert 6 months of check-ins (crisis month uses `caregiver_stress` scenario)  
- `seed_sdoh_flags()` — insert food_access + housing_insecurity flags  
- `seed_source_freshness()` — insert freshness records so `check_data_freshness` has data  

### Step 3: Write 15 use-case tests
- Each test follows the pattern: **Arrange → Act → Assert**  
- mcp-server tools are called by importing and awaiting the skill functions directly  
- Phase 1 tools are called via `GET/POST http://localhost:8000/tools/<name>`  

### Step 4: Run the suite
```bash
cd mcp-server
python -m pytest ../tests/e2e/ -v --tb=short -x
```

### Step 5: Validate results
- All 15 tests green  
- OBT score computed with `confidence ≥ 0.7` (14+ data days)  
- Crisis escalation fires on Dec 2025 data  
- Provider risk score > 50 (due to crisis events)  
- Food nudge fires on Mar 28 date (day_of_month ≥ 25 + food_access flag)  

---

## Expected Test Outcomes

| Test | Assertion |
|------|-----------|
| UC-01 generate_patient | patient_id returned, row present in `patients` |
| UC-02 generate_daily_vitals | "OK Generated 6 vital readings" |
| UC-03 generate_daily_checkins | check-in + adherence records inserted |
| UC-04 compute_obt_score | score 0–100, confidence ≥ 0.7, primary_driver present |
| UC-05 run_crisis_escalation | triggers list non-empty for Dec 2025 data |
| UC-06 run_sdoh_assessment | ≥ 1 flag inserted, food_access present |
| UC-07 check_data_freshness | sources list contains wearable/ehr/manual |
| UC-08 generate_previsit_brief | brief contains patient name + OBT + vitals trend |
| UC-09 run_food_access_nudge | nudge triggered on day ≥ 25 with food_access flag |
| UC-10 compute_provider_risk | risk_score > 0, composite_score present |
| UC-11 get_synthetic_patient | returns Maria Chen demographics + conditions |
| UC-12 check_screening_due | returns mammogram, eye exam, foot exam due |
| UC-13 flag_drug_interaction | no critical interactions (safe combo) |
| UC-14 get_guideline | returns ADA/USPSTF guideline text |
| UC-15 clinical_query | returns guardrail-filtered clinical guidance |
