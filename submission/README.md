# Ambient Patient Companion — Clinical Intelligence MCP

**Server type:** Remote MCP (FastMCP / Streamable HTTP)  
**Endpoint:** `https://a6097077-b5f6-4944-8b1b-fa48750483b9-00-gefgkuhumk6.janeway.replit.dev/mcp`  
**Protocol version:** MCP 2024-11-05  
**Transport:** Streamable HTTP  

---

## What This Integration Does

Ambient Patient Companion gives Claude a real-time clinical intelligence layer built for primary care and care management workflows. It connects Claude to a live patient health warehouse (PostgreSQL) and a 15-tool MCP server that enforces USPSTF clinical guidelines, runs drug interaction checks, applies a four-layer safety guardrail pipeline, and executes a full **Dual-LLM Deliberation Engine** — where Claude and GPT-4 independently analyze a patient's context, cross-critique each other across multiple rounds, and synthesize five structured clinical output categories.

The system is designed for three roles: **primary care physicians (PCP)**, **care managers**, and **patients**. Every tool response is role-aware, citation-backed, and filtered through clinical safety rules before returning to Claude.

---

## Capabilities

### Clinical Safety Guardrails (automatic on every query)
- **Input validation** — PHI detection, jailbreak blocking, out-of-scope query rejection, emotional tone flagging
- **Escalation rules** — life-threatening emergencies, controlled substance requests, pediatric edge cases, pregnancy
- **Output validation** — citation presence check, PHI leakage scan, diagnostic language detection, drug name grounding

### Evidence-Based Clinical Tools
- USPSTF screening recommendations by age, sex, and risk factors
- ADA and USPSTF guideline lookup by recommendation ID
- Drug–drug interaction checking with severity classification (high / moderate / low)

### Patient Data Pipeline (HealthEx + Synthea)
- Register and ingest real-patient HealthEx FHIR records into the warehouse
- Switch between live HealthEx data and synthetic Synthea demo data
- Demo patient available out of the box: Maria Chen, MRN `MC-2025-4829` (Type 2 DM, HTN, obesity)

### Dual-LLM Deliberation Engine (Phase 2)
A five-phase pre-computation pipeline that runs before an encounter:

| Phase | What happens |
|---|---|
| 0 — Context | Patient EHR compiled from warehouse (conditions, meds, labs, biometrics, SDOH) |
| 1 — Analysis | Claude and GPT-4 independently analyze in parallel |
| 2 — Critique | Each model critiques the other's reasoning across 1–3 rounds |
| 3 — Synthesis | Claude synthesizes into 5 structured output categories |
| 4 — Adaptation | Nudges formatted for SMS / push / portal |
| 5 — Commit | All outputs stored atomically in the warehouse |

**Five output categories:**
1. Anticipatory clinical scenarios (next 30 / 90 / 180 days, with probability + confidence)
2. Predicted patient questions (with plain-language suggested responses at 6th-grade reading level)
3. Missing data flags (prioritized: critical / high / medium / low)
4. Behavioral nudges (BCT taxonomy, COM-B model, multi-channel: SMS ≤160 chars, push, portal)
5. Knowledge updates (patient-specific inferences + core clinical knowledge reinforcements)

---

## Tool Reference (15 tools)

| Tool | Description |
|---|---|
| `clinical_query` | Send any clinical question through the 3-layer guardrail pipeline → Claude generates a role-appropriate response |
| `get_guideline` | Fetch a USPSTF or ADA guideline by recommendation ID |
| `check_screening_due` | Return all USPSTF screenings due for a patient given age, sex, and conditions |
| `flag_drug_interaction` | Check a medication list for known drug–drug interactions |
| `get_synthetic_patient` | Load the full Maria Chen demo patient record by MRN |
| `use_healthex` | Switch session to HealthEx real-patient data track |
| `use_demo_data` | Switch session to Synthea synthetic data track |
| `switch_data_track` | Switch to a named track: `synthea` / `healthex` / `auto` |
| `get_data_source_status` | Report the active data track and registered sources |
| `register_healthex_patient` | Create/upsert a HealthEx patient row and return their warehouse UUID |
| `ingest_from_healthex` | Write one resource type (labs / medications / conditions / encounters / summary) from HealthEx FHIR into the warehouse |
| `run_deliberation` | Trigger a full Dual-LLM deliberation session (all 5 phases) for a patient |
| `get_deliberation_results` | Retrieve structured outputs from the most recent deliberation |
| `get_patient_knowledge` | Fetch accumulated patient-specific inferences from past deliberations |
| `get_pending_nudges` | List undelivered nudges queued for a patient or care team |

---

## Setup

No installation required. The server runs as a hosted remote MCP — add it to Claude as a remote connector.

### Add to Claude Web

1. Open Claude → Settings → Integrations → Add integration
2. Enter the MCP URL:
   ```
   https://a6097077-b5f6-4944-8b1b-fa48750483b9-00-gefgkuhumk6.janeway.replit.dev/mcp
   ```
3. Name it: **Ambient Clinical Intelligence**
4. Click Connect — no API key required for the demo endpoint

### Self-hosting

```bash
# Clone the repository
git clone https://github.com/aliomraniH/ambient-patient-companion
cd ambient-patient-companion

# Install dependencies
pip install -r requirements.txt

# Set required secrets
export ANTHROPIC_API_KEY=sk-ant-...
export OPENAI_API_KEY=sk-...          # for deliberation (GPT-4 critic)
export DATABASE_URL=postgresql://...   # PostgreSQL 14+

# Run the Clinical MCP server
MCP_TRANSPORT=streamable-http MCP_PORT=8001 python -m server.mcp_server
```

---

## Authentication

| Context | Requirement |
|---|---|
| Hosted demo endpoint | None — open for evaluation |
| Self-hosted deployment | `ANTHROPIC_API_KEY` (Anthropic API) + `OPENAI_API_KEY` (deliberation engine) |
| Database | `DATABASE_URL` pointing to PostgreSQL 14+ |
| HealthEx real-patient data | HealthEx session must be authenticated in your Claude session before calling `use_healthex` |

The server itself does not require bearer tokens or OAuth — connection is by URL. All LLM API calls happen server-side; credentials never pass through Claude's context.

---

## Working Examples

### Example 1 — Run a Clinical Query with Guardrails

> "Check if a 58-year-old female patient with Type 2 diabetes and hypertension has any overdue USPSTF screenings, then flag any interactions in her current medications: Metformin 500mg, Lisinopril 10mg, and low-dose aspirin."

Claude will:
1. Call `check_screening_due` → returns breast cancer, colorectal cancer, and diabetes screening recommendations
2. Call `flag_drug_interaction` → returns aspirin–lisinopril moderate interaction with recommended action
3. Call `clinical_query` to summarize findings through the guardrail pipeline with citations

**Expected response shape:**
```json
{
  "status": "success",
  "recommendation": "Based on USPSTF guidelines (Grade B)...",
  "citations": [{"source": "USPSTF", "recommendation_id": "breast-cancer-2024"}],
  "escalation_flags": []
}
```

---

### Example 2 — HealthEx Patient Registration + Deliberation

> "I have a new HealthEx patient — Ali Omrani, MRN HX-ALI-001, 52-year-old male with Type 2 diabetes. Register him, load his FHIR data, and run a pre-encounter deliberation."

Claude will:
1. Call `register_healthex_patient` → returns `patient_id` UUID
2. Call `ingest_from_healthex` (resource_type: `summary`) → writes to warehouse
3. Call `run_deliberation` → runs all 5 phases, returns convergence score + output counts
4. Call `get_deliberation_results` → returns anticipatory scenarios and nudges

**Expected deliberation summary:**
```json
{
  "deliberation_id": "del-abc123",
  "status": "complete",
  "convergence_score": 0.87,
  "summary": {
    "anticipatory_scenarios": 3,
    "predicted_questions": 4,
    "missing_data_flags": 5,
    "nudges_generated": 3,
    "knowledge_updates": 2
  },
  "top_scenario": "HbA1c deterioration risk in next 90 days"
}
```

---

### Example 3 — Load Demo Patient and Query Guidelines

> "Load Maria Chen's patient record and tell me what the latest ADA guideline says about HbA1c targets for her."

Claude will:
1. Call `get_synthetic_patient` (mrn: `MC-2025-4829`) → full patient record (conditions, meds, labs, SDOH)
2. Call `get_guideline` (recommendation_id: `ada-hba1c-2024`) → guideline text + evidence grade
3. Call `clinical_query` to synthesize a care-manager–appropriate response

Maria Chen's profile includes: Type 2 DM, hypertension, obesity (BMI 31), food insecurity flag, 14-month gap since last HbA1c.

---

### Example 4 — Retrieve Pending Nudges for Care Manager Dashboard

> "Show me all undelivered patient nudges for Maria Chen."

Claude will:
1. Call `get_pending_nudges` (patient_id: `MC-2025-4829`, target: `patient`)
2. Return SMS-ready text, push notification title/body, and portal long-form content for each nudge

```json
{
  "pending_count": 2,
  "nudges": [
    {
      "trigger_condition": "HbA1c overdue > 90 days",
      "channels": {
        "sms": "Hi Maria — your A1C test is overdue. Please call your clinic. Discuss with your provider.",
        "push_notification": {"title": "Lab checkup reminder", "body": "Your HbA1c is overdue..."},
        "portal": "Dear Maria, our records show your last HbA1c was..."
      }
    }
  ]
}
```

---

## Privacy and Data Handling

- **No PHI stored in Claude context.** All patient data lives in the server-side PostgreSQL warehouse. Claude only sees tool call results.
- **PHI detection runs on every output** before it returns to Claude — SSN, email, and direct identifiers are redacted.
- **Synthetic data by default.** The demo endpoint uses Synthea-generated, fully de-identified patients. Real patient data requires explicit `use_healthex` activation.
- **Audit trail.** Every deliberation, nudge, and knowledge update is logged in the warehouse with timestamps and model provenance.

**Privacy Policy:** https://github.com/aliomraniH/ambient-patient-companion/blob/main/PRIVACY.md  
*(Note: a full HIPAA-compliant privacy policy will be in place before production deployment.)*

---

## Support and Contact

- **GitHub:** https://github.com/aliomraniH/ambient-patient-companion
- **Issues:** https://github.com/aliomraniH/ambient-patient-companion/issues
- **MCP server health check:** `GET /health` → `{"ok": true, "server": "ambient-clinical-intelligence", "version": "1.0.0"}`

---

## Technical Specs

| Property | Value |
|---|---|
| Framework | FastMCP 3.2.0 |
| Language | Python 3.12 |
| Transport | Streamable HTTP (MCP 2024-11-05) |
| Database | PostgreSQL 14+ (26 tables) |
| LLMs used server-side | Claude Sonnet (analysis + synthesis), GPT-4o (deliberation critic) |
| Test coverage | 215 tests (100 phase1 + 48 skills + 37 Jest + 30 dashboard) |
| Endpoint | `/mcp` |
| Health check | `GET /health` |
| REST adapter | `POST /tools/<name>` for all 15 tools |
