# Ambient Patient Companion

> **The interface is not designed. It is derived.**
> `S = f(R, C, P, T)`

A production multi-agent AI health system that continuously generates the optimal clinical interface as a mathematical function of four dynamic variables — Role, Context, Patient State, and Time. Built on Next.js 16, FastMCP Python servers, PostgreSQL, and the Anthropic Claude API.

---

## Get Started in 3 Steps

Connect Claude (or any MCP-compatible LLM) to a live clinical intelligence layer and explore insights from your medical reports — imported via HealthEx, your own FHIR data, or the built-in demo patient.

```
  ┌──────────────────┐     ┌──────────────────┐     ┌──────────────────┐
  │   Step 1         │     │   Step 2         │     │   Step 3         │
  │                  │     │                  │     │                  │
  │  Import this     │────▶│  Get your MCP    │────▶│  Add to Claude   │
  │  repo to Replit  │     │  server URLs     │     │  and explore     │
  │                  │     │                  │     │  your data       │
  └──────────────────┘     └──────────────────┘     └──────────────────┘
```

---

### Step 1 — Import this repository to Replit

[![Run on Replit](https://replit.com/badge/github/aliomraniH/ambient-patient-companion)](https://replit.com/new/github/aliomraniH/ambient-patient-companion)

Click the badge above, or do it manually:

1. Go to **[replit.com](https://replit.com)** → click **Create Repl**
2. Choose **Import from GitHub**
3. Paste `https://github.com/aliomraniH/ambient-patient-companion`
4. Click **Import from GitHub** — the environment configures automatically

Once imported, open the **Secrets** tab (lock icon in the sidebar) and add:

| Secret name | What to put there |
|---|---|
| `ANTHROPIC_API_KEY` | Your Claude API key (`sk-ant-...`) |
| `OPENAI_API_KEY` | Your OpenAI key — used by the deliberation engine's GPT-4o critic |

Then start all services with one command in the Shell tab:

```bash
./start.sh
```

This regenerates your server URLs, then starts all 5 services in parallel:

```
Port 5000  — Next.js frontend + OAuth layer
Port 8001  — Clinical MCP server  (44 tools · guardrails · deliberation engine)
Port 8002  — Skills MCP server    (41 tools · behavioral stack · AgentRuntime)
Port 8003  — Ingestion MCP server (HealthEx + FHIR pipeline)
Port 8080  — Config Dashboard     (watcher health · environment monitor)
```

---

### Step 2 — Get your MCP server URLs

After `start.sh` runs, your permanent server URLs are written to `.mcp.json`. See them with:

```bash
cat .mcp.json
```

They follow this pattern — replace `<your-replit-domain>` with the domain shown in your Replit preview pane:

```
Clinical Intelligence:  https://<your-replit-domain>/mcp
Skills Companion:       https://<your-replit-domain>/mcp-skills
Ingestion Pipeline:     https://<your-replit-domain>/mcp-ingestion
```

**Verify all three servers are up:**

```bash
curl https://<your-domain>/health
# → {"ok": true, "server": "ambient-clinical-intelligence", "version": "1.0.0"}

curl https://<your-domain>/mcp-skills/health
# → {"ok": true, "server": "ambient-skills-companion", "version": "1.0.0"}
```

---

### Step 3 — Add to Claude and explore your medical data

#### Connect to Claude

1. Open **Claude** → **Settings** → **Integrations** → **Add custom integration**
2. Paste your server URL(s) — start with the Clinical server, add the others when ready:

```
https://<your-domain>/mcp           ← Clinical Intelligence (start here)
https://<your-domain>/mcp-skills    ← Skills + behavioral + audit tools
https://<your-domain>/mcp-ingestion ← HealthEx / FHIR ingestion
```

3. Name it **Ambient Clinical Intelligence** → click **Connect**. OAuth completes automatically — no login screen appears.

---

#### Option A — Import a medical report from HealthEx

If you have **HealthEx MCP** connected to Claude alongside Ambient, use this sequence:

**Prompt 1 — Register the patient:**
```
Register this patient in Ambient: [Full name], MRN [HX-XXXXX],
[age]-year-old [sex] with [primary condition].
```

**Prompt 2 — Ingest their data:**
```
Now ingest this patient's HealthEx data into the Ambient warehouse —
pull their labs, conditions, medications, and encounters summary.
```

**Prompt 3 — Run clinical deliberation:**
```
Run a full deliberation for this patient and show me:
- The top anticipatory clinical scenarios for the next 30/90/180 days
- Any critical missing data flags
- The behavioral nudges queued for delivery
```

> **Important — why the order matters:** Ambient enforces a ground-truth gate. If you skip ingestion and try to deliberate directly, you'll get `"status": "ingestion_required"` — by design. Deliberating on an un-ingested patient produces confidently wrong outputs (phantom BMI, missing labs, stale care gaps). The gate exists precisely because *the AI doesn't know what it doesn't know*.

---

#### Option B — Import your own FHIR data

Ambient's ingestion pipeline accepts 5 formats automatically — FHIR R4 JSON bundles, QuestionnaireResponse, flat FHIR text, plain clinical summaries, or compressed table format.

```
I have FHIR data for a new patient. Please:
1. Register them with register_healthex_patient (name: [name], MRN: [id])
2. Ingest their data with ingest_from_healthex — I'll paste the FHIR below
3. Then run deliberation and summarize the key clinical findings

[paste your FHIR R4 bundle or clinical summary here]
```

---

#### Option C — Try the built-in demo patient (no HealthEx required)

```
Load the demo patient Maria Chen (MRN MC-2025-4829) and:
1. Show me what USPSTF screenings she's overdue for
2. Check for any drug interactions in her current medications
3. Run a deliberation and show me her top anticipatory scenarios
```

Maria Chen: 54F, Taiwanese-American, Type 2 DM + HTN + obesity (BMI 31), food insecurity flag, 14-month gap since last HbA1c.

---

#### What happens under the hood

```mermaid
sequenceDiagram
    participant You
    participant Claude
    participant Ambient as Ambient MCP Servers
    participant DB as PostgreSQL Warehouse

    You->>Claude: "Register and deliberate on [patient]"
    Claude->>Ambient: register_healthex_patient()
    Ambient->>DB: upsert patient + source_freshness row
    Claude->>Ambient: ingest_from_healthex(resource_type="summary")
    Ambient->>Ambient: 5-format adaptive parser + ETL
    Ambient->>DB: write labs · conditions · meds · encounters
    Claude->>Ambient: run_deliberation(mode="full")
    Ambient->>Ambient: Phase 0 context compile (11K token budget)
    Ambient->>Ambient: Phase 1 Claude + GPT-4o analyze in parallel
    Ambient->>Ambient: Phase 2 cross-critique across 1–3 rounds
    Ambient->>Ambient: Phase 3 synthesis → 5 output categories
    Ambient->>Ambient: Phase 3.5 guardrail safety wrapper
    Ambient->>DB: atomic commit of all deliberation outputs
    Claude->>Ambient: get_deliberation_results()
    Ambient-->>Claude: scenarios · flags · nudges · knowledge
    Claude-->>You: Structured clinical insights
```

---

## The Core Premise

Traditional healthcare software forces clinicians and patients to navigate static dashboards designed for a generic "average" user. The Ambient Patient Companion inverts this entirely.

```
Traditional approach:   DESIGNER → fixed UI → user adapts to it
Ambient approach:       S = f(R, C, P, T) → UI derives itself → right surface for this exact moment
```

```mermaid
graph LR
    R["R — Role<br/>(PCP / Nurse / Patient / Lab)"]
    C["C — Context<br/>(Pre-visit / Crisis / Routine)"]
    P["P — Patient State<br/>(Labs / Vitals / Behavior / SDoH)"]
    T["T — Time<br/>(Day 0 / Week 3 / Month 6)"]
    S["S — Optimal<br/>Clinical Surface"]

    R --> S
    C --> S
    P --> S
    T --> S

    style S fill:#3E6B5C,color:#fff,stroke:#2a4f43
    style R fill:#6B5EA8,color:#fff,stroke:#5a4d97
    style C fill:#C9864A,color:#fff,stroke:#b8753a
    style P fill:#4A8C72,color:#fff,stroke:#3a7b61
    style T fill:#6B5EA8,color:#fff,stroke:#5a4d97
```

---

## Research Foundation

This project operationalizes three peer-reviewed streams of research:

| Paper | Key Finding | Applied As |
|-------|-------------|------------|
| *AI Healthcare UX — Ambient Action Model* | LLM interfaces should emerge from context like a coding assistant, not be statically designed | The `S=f(R,C,P,T)` formula drives every UI surface |
| *AI for Holistic Primary Care* | AI-driven "chase lists" cut acute medical events by **22.9%** and hospitalizations by **48.3%** | `compute_provider_risk` + `run_crisis_escalation` skills |
| *EAGLE Trial (22,000 patients)* | AI on routine ECGs increased low-ejection-fraction diagnosis by **32%**, reducing mortality | Biometric monitoring + screening gap detection |
| *JMIR Alert Fatigue Review* | Clinicians get **56 alerts/day**, spend **49 min** on async notifications; overrides increase with volume | Action-first notification architecture, no accumulating badges |
| *Lumeris "Tom" Agent* | 50 AI touches over 6 months reduces required physician visits from 5/year to 2/year | Continuous check-in + behavioral nudge pipeline |

---

## System Architecture

```mermaid
graph TB
    subgraph "Claude Web / API"
        CW["OAuth PKCE handshake<br/>auto-handled by Next.js"]
    end

    subgraph "Next.js 16 — Port 5000"
        NX["Proxy Rewrites"]
        OA["OAuth Layer<br/>/.well-known/* · /register<br/>/authorize · /token"]
    end

    subgraph "MCP Server 1 — Port 8001"
        S1["ambient-clinical-intelligence<br/>FastMCP 3.2"]
        G1["3-Layer Guardrail Pipeline"]
        DE["Dual-LLM Deliberation Engine<br/>6 phases · Claude + GPT-4o"]
        T1["23 Tools · AuditMiddleware"]
    end

    subgraph "MCP Server 2 — Port 8002"
        S2["ambient-skills-companion<br/>FastMCP 3.2"]
        SK["26 skill modules<br/>auto-discovered"]
        AR["AgentRuntime<br/>3 autonomous watchers"]
        T2["22+ Tools · AuditMiddleware"]
    end

    subgraph "MCP Server 3 — Port 8003"
        S3["ambient-ingestion<br/>FastMCP 3.2"]
        T3["4 Tools · AuditMiddleware<br/>5 format parsers"]
    end

    subgraph "PostgreSQL — 35 Tables"
        DB["patients · biometrics · conditions<br/>deliberations · flags · nudges<br/>ingestion_plans · transfer_log<br/>clinical_notes · system_config · …"]
    end

    subgraph "LLMs"
        AN["claude-sonnet-4-20250514<br/>clinical + synthesis"]
        GP["gpt-4o<br/>deliberation critic"]
        HA["claude-haiku-4-5-20251001<br/>flag review · planner · reviewer"]
    end

    CW -->|"OAuth PKCE"| OA
    CW -->|"/mcp"| NX
    NX -->|"localhost:8001"| S1
    NX -->|"localhost:8002"| S2
    NX -->|"localhost:8003"| S3

    S1 --> G1 --> AN
    S1 --> DE --> AN
    DE --> GP
    DE --> HA
    S1 --> DB
    S2 --> SK --> DB
    S3 --> DB

    style S1 fill:#4A8C72,color:#fff
    style S2 fill:#6B5EA8,color:#fff
    style S3 fill:#C9864A,color:#fff
    style DB fill:#2d4a6b,color:#fff
    style AN fill:#c9655c,color:#fff
    style OA fill:#3E6B5C,color:#fff
```

---

## Three MCP Servers

### Server 1 — `ambient-clinical-intelligence` · `server/mcp_server.py`

The primary clinical intelligence layer. Every AI call passes through a three-layer guardrail pipeline. Also hosts the HealthEx ingestion pipeline, Dual-LLM Deliberation Engine, and Flag Lifecycle system.

```
Public URL: https://[your-replit-domain]/mcp

Guardrail Pipeline:
  Layer 1 — Input:      PHI detection · jailbreak blocking · scope check · emotional tone flag
  Layer 2 — Escalation: life-threatening · controlled substances · pediatric · pregnancy
  Layer 3 — Output:     citation check · PHI leakage scan · diagnostic language flags · drug grounding
```

| Tool | Description | REST |
|------|-------------|------|
| `clinical_query` | 3-layer guardrail → Claude Sonnet → validated response | `POST /tools/clinical_query` |
| `get_guideline` | Fetch ADA/USPSTF guideline by ID (e.g., `9.1a`) | `GET /tools/get_guideline` |
| `check_screening_due` | Overdue USPSTF screenings for patient profile | `POST /tools/check_screening_due` |
| `flag_drug_interaction` | Known drug interactions from clinical rules | `POST /tools/flag_drug_interaction` |
| `get_synthetic_patient` | Demo patient from live DB (MRN 4829341) | `GET /tools/get_synthetic_patient` |
| `use_healthex` | Switch data track to HealthEx real records | `POST /tools/use_healthex` |
| `use_demo_data` | Switch data track to Synthea demo data | `POST /tools/use_demo_data` |
| `switch_data_track` | Switch to named track (synthea/healthex/auto) | `POST /tools/switch_data_track` |
| `get_data_source_status` | Report active track + available sources | `GET /tools/get_data_source_status` |
| `register_healthex_patient` | Create/upsert HealthEx patient row, return UUID | `POST /tools/register_healthex_patient` |
| `ingest_from_healthex` | Two-phase ingest: plan (fast) + execute (write rows) | `POST /tools/ingest_from_healthex` |
| `execute_pending_plans` | Re-execute failed/pending ingestion plans | `POST /tools/execute_pending_plans` |
| `get_ingestion_plans` | Read plan summaries + insights_summary | `POST /tools/get_ingestion_plans` |
| `get_transfer_audit` | Per-record transfer_log audit trail | `POST /tools/get_transfer_audit` |
| `run_deliberation` | Dual-LLM deliberation (progressive or full mode) | `POST /tools/run_deliberation` |
| `get_deliberation_results` | Retrieve stored deliberation outputs | `POST /tools/get_deliberation_results` |
| `get_flag_review_status` | Flag lifecycle status (open/retracted/pending human review) | `POST /tools/get_flag_review_status` |
| `get_patient_knowledge` | Accumulated patient-specific knowledge | `POST /tools/get_patient_knowledge` |
| `get_pending_nudges` | Queued nudges for delivery scheduling | `POST /tools/get_pending_nudges` |

### Server 2 — `ambient-skills-companion` · `mcp-server/server.py`

22+ tools auto-discovered from `mcp-server/skills/` via a `register(mcp)` convention. 26 skill modules loaded. Every tool call is logged to `mcp_call_log` via `AuditMiddleware`.

**AgentRuntime** — in addition to call-driven MCP tools, the Skills server runs an embedded `AgentRuntime` (`mcp-server/runtime/agent_runtime.py`) that starts three autonomous background watchers on server boot. Each skill file that wants proactive execution exports a `register_watchers(runtime)` hook; `load_skills()` calls it automatically. Watcher run state is persisted to `system_config` (key `watcher_state:<name>`) after every execution and restored on restart.

| Watcher | Interval | Purpose |
|---------|----------|---------|
| `checkin_atom_watcher` | 5 min | Extract behavioral atoms from new check-ins, run gap detection |
| `crisis_scan_watcher` | 60 min | Crisis escalation scan for patients with recent activity |
| `care_gap_watcher` | 24 h | Flag overdue open care gaps, insert agent interventions |

Live watcher health is available at `GET /api/agent-runtime/status` on the Skills server and proxied through the Config Dashboard watcher health panel.

```
Public URL: https://[your-replit-domain]/mcp-skills
```

```mermaid
graph LR
    subgraph "ambient-skills-companion — 22+ tools"
        A["compute_obt_score<br/>Optimal Being Trajectory"]
        B["compute_provider_risk<br/>Chase list score"]
        C["run_crisis_escalation<br/>Behavioral crisis detection"]
        D["run_food_access_nudge<br/>End-of-month SDoH trigger"]
        E["generate_daily_checkins<br/>Idempotent check-in seed"]
        F["generate_patient<br/>FHIR bundle → PostgreSQL"]
        G["generate_daily_vitals<br/>Biometric reading seed"]
        H["generate_previsit_brief<br/>Pre-encounter synthesis"]
        I["run_sdoh_assessment<br/>Social determinants"]
        J["Data track tools (5)<br/>freshness · ingestion · conflicts<br/>source status · orchestrate_refresh"]
        K["Audit query tools (4)<br/>get_current_session · list_sessions<br/>get_session_transcript · search_tool_calls"]
        L["Behavioral + vector stack<br/>search_similar_atoms · atom_cohort<br/>behavioral pressure + cards"]
    end

    style A fill:#6B5EA8,color:#fff
    style B fill:#6B5EA8,color:#fff
    style C fill:#c9655c,color:#fff
    style D fill:#4A8C72,color:#fff
    style H fill:#C9864A,color:#fff
    style J fill:#2d4a6b,color:#fff
    style K fill:#3E6B5C,color:#fff
    style L fill:#4A8C72,color:#fff
```

### Server 3 — `ambient-ingestion` · `ingestion/server.py`

```
Public URL: https://[your-replit-domain]/mcp-ingestion

trigger_ingestion(patient_id, source, force_refresh)
  Full ETL pipeline: FHIR parse → conflict detection → upsert → freshness log
  Adapters: synthea (demo) | healthex (real records)
  Format parsers: A (plain text) · B (compressed table) · C (flat FHIR text) · D (FHIR JSON) · JSON-dict

detect_context_staleness(patient_id, clinical_scenario)
  LOINC-keyed clinical freshness per evidence-based thresholds.
  Returns freshness_score + recommended_refreshes.

search_patient_data_extended(patient_id, query, ...)
  Extended data search across all patient records.

verify_output_provenance(payload, ...)
  Shared provenance gate (source_server=ingestion).
```

---

## Connecting Claude

All three servers require OAuth PKCE before connecting. The flow completes automatically — no login screen because this is a public server. Claude handles the handshake invisibly.

**To add to Claude:** Settings → Integrations → Add custom integration → paste URL below → done.

| Server | URL |
|--------|-----|
| `ambient-clinical-intelligence` | `https://[your-replit-domain]/mcp` |
| `ambient-skills-companion` | `https://[your-replit-domain]/mcp-skills` |
| `ambient-ingestion` | `https://[your-replit-domain]/mcp-ingestion` |

The OAuth discovery endpoints are served by Next.js:

| Endpoint | RFC | Purpose |
|---|---|---|
| `GET /.well-known/oauth-protected-resource` | RFC 9728 | Declares auth server — prevents "server sleeping" error |
| `GET /.well-known/oauth-authorization-server` | RFC 8414 | Lists token/register/authorize endpoints |
| `POST /register` | RFC 7591 | Issues `client_id` to Claude |
| `GET /authorize` | RFC 6749 | Auto-issues authorization code |
| `POST /token` | RFC 6749 | Returns Bearer token |

---

## Dual-LLM Deliberation Engine

An async pre-computation pipeline where Claude Sonnet and GPT-4o independently analyze a patient's clinical context, cross-critique each other, then synthesize into 5 structured output categories.

```mermaid
graph LR
    subgraph "6-Phase Deliberation Pipeline"
        P05["Phase 0.5<br/>planner.py<br/>Pre-deliberation agenda (Haiku)"]
        P0["Phase 0<br/>context_compiler.py<br/>EHR context assembly (11K budget)"]
        P1["Phase 1<br/>analyst.py<br/>Claude + GPT-4o in parallel"]
        P2["Phase 2<br/>critic.py<br/>Cross-critique + convergence"]
        P3["Phase 3<br/>synthesizer.py<br/>Unified synthesis"]
        P325["Phase 3.25<br/>synthesis_reviewer.py<br/>Domain review (Haiku)"]
        P35["Phase 3.5<br/>output_safety.py<br/>Guardrail wrapper"]
        P4["Phase 4<br/>behavioral_adapter.py<br/>SMS/nudge formatting"]
        P5["Phase 5<br/>knowledge_store.py<br/>Atomic DB commit"]
    end

    P05 --> P0 --> P1 --> P2 --> P3 --> P325 --> P35 --> P4 --> P5
```

**5 output categories** (from synthesis): clinical_findings · medication_review · care_gaps · behavioral_insights · care_coordination_actions

**Flag Lifecycle**: deliberation results are screened by `flag_reviewer.py` (Haiku). Flags with `had_zero_values=True` or `requires_human=True` are held for human review before activation.

---

## MCP Audit Log System

Every tool call made by Claude (or any external MCP client) is automatically recorded — across all three servers — with no action required from callers.

**How it works:** `AuditMiddleware` (a `fastmcp.Middleware` subclass in `shared/audit_middleware.py`) is wired to all three servers. It fires on every `on_call_tool` event, capturing inputs, output, timing, and outcome in a fire-and-forget async write to the `mcp_call_log` table.

**Session tracking:** `shared/call_recorder.py` maintains a session UUID per server instance. 30 minutes of inactivity triggers a new session UUID automatically. The `seq` column is the call number within the session.

**Query from Claude** using four tools on the Skills server:

| Tool | Description |
|------|-------------|
| `get_current_session` | Live session IDs + call counts for every running server |
| `list_sessions(limit, server_name)` | Recent sessions ordered by last activity |
| `get_session_transcript(session_id)` | Full chronological call log for a session |
| `search_tool_calls(tool_name, server_name, outcome, from_minutes_ago)` | Flexible filter |

---

## Database Schema — 35 Tables

```mermaid
erDiagram
    patients ||--o{ patient_conditions : has
    patients ||--o{ patient_medications : takes
    patients ||--o{ biometric_readings : generates
    patients ||--o{ daily_checkins : submits
    patients ||--o{ obt_scores : receives
    patients ||--o{ deliberations : analyzed_by
    patients ||--o{ deliberation_flags : flagged_in
    patients ||--o{ ingestion_plans : planned_for

    biometric_readings {
        uuid patient_id
        string metric_type
        float value
        timestamp measured_at
    }

    deliberations {
        uuid patient_id
        float convergence_score
        int rounds_completed
        string mode
    }

    deliberation_flags {
        uuid patient_id
        string title
        enum lifecycle_state
        enum priority
        bool requires_human
    }
```

**Table groups:**
- **Base schema** (22 tables): `patients`, `patient_conditions`, `patient_medications`, `biometric_readings`, `daily_checkins`, `obt_scores`, `provider_risk_scores`, `sdoh_assessments`, `care_gaps`, `ingestion_log`, `source_freshness`, `system_config` (also stores `watcher_state:<name>` rows for AgentRuntime persistence) + 10 more
- **Deliberation** (4 tables): `deliberations`, `deliberation_outputs`, `patient_knowledge`, `core_knowledge_updates`
- **Flag lifecycle** (3 tables): `deliberation_flags`, `flag_review_runs`, `flag_corrections`
- **Ingestion** (4 tables): `ingestion_plans`, `transfer_log`, `clinical_notes`, `media_references`
- **Audit** (1 table): `mcp_call_log` — MCP tool call audit log: `id`, `session_id`, `server_name`, `tool_name`, `called_at`, `duration_ms`, `input_params` (JSONB), `output_text`, `output_data` (JSONB), `outcome`, `error_message`, `seq`
- **System**: `system_config` (data track, model, dashboard state)

---

## Four Interaction Contracts

The `S=f(R,C,P,T)` formula produces four distinct interaction patterns.
Three (PCP, Care Manager, Patient) are live today — their role-specific
system prompts live in `config/system_prompts/`. The Lab Technician
contract is on the roadmap but **not yet implemented**: there is no
`lab_tech.xml` system prompt, and `clinical_query(role='lab_tech')` will
raise `ValueError` until one is added.

```mermaid
graph TD
    subgraph PCP["Primary Care Provider"]
        Q1["What do I need to know<br/>in the next 15 minutes?"]
        A1["Encounter-anchored surface:<br/>backward synthesis of interval events<br/>forward pre-staged reflex orders"]
    end

    subgraph CM["Care Manager"]
        Q2["Who needs me today,<br/>and in what order?"]
        A2["Queue-based surface:<br/>continuously prioritized work list<br/>AI eliminates manual triage entirely"]
    end

    subgraph PT["Patient"]
        Q3["What should I do today,<br/>and why does it matter?"]
        A3["Relationship-based surface:<br/>plain language, warm tone<br/>no dashboards, no streak mechanics"]
    end

    subgraph LB["Lab Technician"]
        Q4["Is this value critical?<br/>Who needs to know?"]
        A4["Critical-value surface:<br/>binary routing decisions<br/>approve + note for release"]
    end

    style Q1 fill:#3E6B5C,color:#fff
    style Q2 fill:#6B5EA8,color:#fff
    style Q3 fill:#C9864A,color:#fff
    style Q4 fill:#4A8C72,color:#fff
```

---

## Alert Fatigue — The Clinical Research Problem

```
📖 JMIR Systematic Review, 2021
   → 56 alerts/day per clinician
   → 49 minutes spent on async notifications
   → Override rates increase as volume increases

📖 AMIA Conference, 2019 (4 health systems)
   → 1/3 of medication alerts are repeats from same patient, same year

📖 BMC Medical Informatics, 2017
   → Two distinct fatigue mechanisms:
     (A) Cognitive overload from volume
     (B) Desensitization from repetition
```

**Design response — Action-First Architecture:**

```
Every card must result in action or be dismissed.
No "read" state.   No history.   No accumulating badge.
The feed empties as you work. When empty: "All caught up."
```

---

## AI Escalation Design

A key demonstration: **the AI's value is sometimes in what it refuses to answer.**

```
Normal flow:
  Patient asks about stress → BP relationship     ← AI answers
  Patient asks about Tuesday's reading (148/91)   ← AI answers

Escalation trigger:
  Patient: "My head hurts — adjust my pill?"

  Instead of answering:
  ┌─────────────────────────────────────────────────────┐
  │  This question needs your care team.                │
  │                                                     │
  │  ✶ AI stopped here · Handing off                   │
  │                                                     │
  │  Questions about adjusting medication — especially  │
  │  with a headache — aren't something I can advise   │
  │  on safely.                                         │
  │                                                     │
  │  [Alert care team now]   [Save for next visit]     │
  └─────────────────────────────────────────────────────┘
```

The escalation is not a failure state. It is the system working exactly as intended.

---

## Workflows (5 active)

```mermaid
graph LR
    subgraph "Replit Workflows"
        W1["Start application<br/>cd replit-app && npm run dev<br/>Port 5000"]
        W2["Config Dashboard<br/>cd replit_dashboard && python server.py<br/>Port 8080"]
        W3["Clinical MCP Server<br/>python -m server.mcp_server<br/>Port 8001"]
        W4["Skills MCP Server<br/>cd mcp-server && python server.py<br/>Port 8002"]
        W5["Ingestion MCP Server<br/>python -m ingestion.server<br/>Port 8003"]
    end

    style W1 fill:#3E6B5C,color:#fff
    style W2 fill:#C9864A,color:#fff
    style W3 fill:#4A8C72,color:#fff
    style W4 fill:#6B5EA8,color:#fff
    style W5 fill:#2d4a6b,color:#fff
```

`start.sh` is the production entry point. It calls `scripts/generate_mcp_json.py` first to regenerate `.mcp.json` with the correct public HTTPS URLs from `$REPLIT_DEV_DOMAIN`, then starts all 5 services.

---

## Test Coverage — ~1,300 tests

```
┌────────────────────────────────────────┬────────┬───────────┐
│ Suite                                  │ Tests  │ Framework │
├────────────────────────────────────────┼────────┼───────────┤
│ Phase 1 Clinical Intelligence          │  255   │ pytest    │
│ Phase 2 Deliberation + Flags           │  156   │ pytest    │
│ Deliberation Engine Unit               │  258   │ pytest    │
│ Ingestion Pipeline                     │  269   │ pytest    │
│ Skills MCP Backend + AgentRuntime      │  181   │ pytest    │
│   ↳ mcp-server/tests/ (170)           │        │           │
│   ↳ tests/test_agent_runtime.py (11)  │        │           │
│ Shared Utilities (coercion+datetime)   │   24   │ pytest    │
│ End-to-End MCP Use-Cases               │   28   │ pytest    │
│ MCP Smoke + Discovery + OAuth          │   50   │ pytest    │
│ Frontend (Next.js)                     │   37   │ Jest      │
│ Config Dashboard                       │   37   │ anyio     │
└────────────────────────────────────────┴────────┴───────────┘
```

```bash
python -m pytest tests/phase1/ -v
python -m pytest tests/phase2/ -v
python -m pytest server/deliberation/tests/ -v
python -m pytest ingestion/tests/ -v
python -m pytest shared/tests/ -v                          # coerce_confidence + ensure_aware
python -m pytest tests/e2e/ -v
python -m pytest tests/test_mcp_discovery.py tests/test_mcp_smoke.py -v
python -m pytest tests/test_agent_runtime.py -v           # RT1-RT10 AgentRuntime
PYTHONPATH=mcp-server python -m pytest mcp-server/tests/ -v
cd replit-app && npm test
cd replit_dashboard && python -m pytest tests/ -v
```

---

## Project Structure

```
ambient-patient-companion/
│
├── replit-app/                  Next.js 16 frontend (port 5000)
│   ├── next.config.ts           Proxy rewrites → 3 MCP servers
│   ├── lib/oauth-store.ts       In-memory OAuth client/code/token store
│   ├── app/
│   │   ├── .well-known/         OAuth discovery (RFC 9728 + RFC 8414)
│   │   ├── authorize/           Authorization code grant (auto-issues)
│   │   ├── token/               Token exchange
│   │   ├── register/            Dynamic client registration (RFC 7591)
│   │   └── api/                 patients · vitals · checkin · obt · mcp · sse
│   └── components/
│       └── PatientManager.tsx   Patient CRUD (search · add · edit · delete)
│
├── server/                      Server 1: ambient-clinical-intelligence (port 8001)
│   ├── mcp_server.py            FastMCP: 23 tools + REST wrappers + /health
│   │                            + AuditMiddleware("clinical", _get_db_pool)
│   ├── guardrails/              input_validator · output_validator · clinical_rules
│   └── deliberation/            Dual-LLM Deliberation Engine (6 phases)
│       ├── engine.py            Phase orchestrator
│       ├── planner.py           Phase 0.5: agenda builder (Haiku)
│       ├── context_compiler.py  Phase 0: EHR context assembly
│       ├── analyst.py           Phase 1: parallel Claude + GPT-4o
│       ├── critic.py            Phase 2: cross-critique
│       ├── synthesizer.py       Phase 3: unified synthesis
│       ├── synthesis_reviewer.py Phase 3.25: domain review (Haiku)
│       ├── output_safety.py     Phase 3.5: guardrail wrapper
│       ├── behavioral_adapter.py Phase 4: nudge formatting
│       ├── knowledge_store.py   Phase 5: DB commit (uses coerce_confidence)
│       ├── flag_reviewer.py     LLM flag lifecycle review (Haiku)
│       └── flag_writer.py       Flag registry writes
│
├── mcp-server/                  Server 2: ambient-skills-companion (port 8002)
│   ├── server.py                FastMCP: auto-discovers skills (22+ tools) + AgentRuntime lifespan
│   │                            + GET /api/agent-runtime/status + AuditMiddleware("skills", get_pool)
│   ├── runtime/                 Autonomous background-task engine
│   │   ├── agent_runtime.py     AgentRuntime singleton: watch/start/lifespan/status
│   │   │                        Persist state to system_config · prune stale rows at boot
│   │   └── watchers.py          Migration-notice shell (watchers live in skill files)
│   ├── skills/                  26 skill modules (register(mcp) + register_watchers(runtime) hooks)
│   │   ├── behavioral_atoms.py  Behavioral atom tools + checkin_atom_watcher (every 5 min)
│   │   ├── care_gap.py          Care gap skill + care_gap_watcher (every 24 h)
│   │   ├── crisis_escalation.py Crisis escalation tools + crisis_scan_watcher (every 60 min)
│   │   ├── call_history.py      4 audit query tools
│   │   └── …                    compute_obt_score · behavioral stack · ingestion_tools · …
│   ├── tests/                   170 tests (skills + AgentRuntime + watcher persistence)
│   │   ├── test_agent_runtime.py   15 tests (load_skills hook, watch, duplicate guard)
│   │   └── test_watcher_persistence.py  31 tests (persist/restore/stale-prune, 9 integration)
│   ├── db/schema.sql            22-table base schema (source of truth)
│   └── transforms/              FHIR-to-schema transformers
│
├── ingestion/                   Server 3: ambient-ingestion (port 8003)
│   ├── server.py                FastMCP: 4 tools + AuditMiddleware("ingestion", pool)
│   ├── pipeline.py              ETL orchestrator (uses ensure_aware)
│   └── adapters/healthex/       5-format adaptive parser + audit trail
│
├── shared/                      Cross-server Python utilities (on sys.path in all servers)
│   ├── coercion.py              coerce_confidence(): float→clamp; int>1→÷100; str→map
│   ├── datetime_utils.py        ensure_aware(): naive→UTC-aware for DB datetime arithmetic
│   ├── call_recorder.py         CallRecorder: session UUID tracking + asyncpg audit writes
│   ├── audit_middleware.py      AuditMiddleware(Middleware): FastMCP on_call_tool hook
│   ├── claude-client.js         Shared JS MCP client
│   ├── provenance/              Universal provenance gate (all 3 MCP servers)
│   └── tests/                   34 unit tests for coercion + datetime utils
│
├── replit_dashboard/            Config Dashboard (port 8080)
├── scripts/
│   ├── generate_mcp_json.py     Regenerates .mcp.json from $REPLIT_DEV_DOMAIN
│   └── post-merge.sh            Post-merge setup: pip install + npm install + schema apply
├── tests/
│   ├── phase1/                  255 Phase 1 tests
│   ├── phase2/                  156 Phase 2 tests
│   ├── e2e/                     28 end-to-end tests
│   ├── test_mcp_smoke.py        MCP smoke tests
│   ├── test_mcp_discovery.py    Discovery + OAuth tests (DN-1–DN-26)
│   └── test_agent_runtime.py    11 AgentRuntime tests (RT1-RT10)
├── .mcp.json                    MCP client discovery (auto-regenerated at startup)
├── start.sh                     Production startup script
├── config/system_prompts/       Role-based prompts (pcp · care_manager · patient)
├── docs/images/                 Architecture and session screenshots
└── submission/README.md         MCP marketplace submission
```
