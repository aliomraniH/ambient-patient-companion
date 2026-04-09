# CLAUDE.md — Ambient Patient Companion
## Implementation Guide for Claude Code

> **Project**: Ambient Patient Companion  
> **Model**: Ambient Action Model — `S = f(R, C, P, T)`  
> **Stack**: Claude 4.6 + FastMCP + LangGraph + LangSmith + Replit  
> **Canonical Patient**: Maria Chen, 54F, MRN 4829341 · Dr. Rahul Patel · Patel Family Medicine  
> **GitHub**: https://github.com/aliomraniH/ambient-patient-companion

---

## 1. Project Overview

The Ambient Patient Companion transforms static patient dashboards into a continuously-derived, context-aware clinical intelligence surface. The system surface (`S`) is a function of:

| Variable | Meaning | Example |
|---|---|---|
| `R` | Role | Patient, Provider, Care Coordinator |
| `C` | Context | Pre-visit, In-encounter, Post-visit, Async |
| `P` | Patient State | HbA1c trend, BP readings, SDoH flags, med adherence |
| `T` | Time | Current timestamp, time-since-last-contact, care gap age |

**Core design principle**: Zero activation cost. The right action surfaces before the clinician thinks to look for it.

---

## 2. Architecture — Five Layers

```
┌─────────────────────────────────────────────────────────┐
│  Layer 1: Ambient UX                                     │
│  Pre-session dashboard · In-encounter workspace ·        │
│  Population panel · Message triage inbox                 │
└────────────────────┬────────────────────────────────────┘
                     │
┌────────────────────▼────────────────────────────────────┐
│  Layer 2: Agent Orchestration (LangGraph v1.0)           │
│  Orchestrator-worker pattern · Clinical priority queue   │
│  Event-driven triggers · Human-in-the-loop checkpoints   │
└────────────────────┬────────────────────────────────────┘
                     │
┌────────────────────▼────────────────────────────────────┐
│  Layer 3: Semantic Health Fabric                         │
│  Claude 4.6 + Extended Thinking · LangSmith tracing     │
│  Constitutional AI guardrails · MemPrompt corrections    │
│  Proactive suggestion engine (separate API call)         │
│  ★ Dual-LLM Deliberation Engine (Claude + GPT-4)        │
└────────────────────┬────────────────────────────────────┘
                     │
┌────────────────────▼────────────────────────────────────┐
│  Layer 4: MCP Tool Registry (FastMCP)                    │
│  Synthetic patient data · EHR integration               │
│  Lab result processing · Care gap analysis              │
└────────────────────┬────────────────────────────────────┘
                     │
┌────────────────────▼────────────────────────────────────┐
│  Layer 5: Sources of Truth                               │
│  Vector store (corrections + patient history)            │
│  FHIR R5 subscriptions · Event sourcing (audit log)     │
│  LangSmith datasets (few-shot retrieval)                 │
└─────────────────────────────────────────────────────────┘
```

---

## 3. Repository Structure

```
ambient-patient-companion/
├── CLAUDE.md                    ← This file
├── README.md                    ← Project overview, architecture, quick start
├── requirements.txt             ← Python deps (pytest-asyncio==0.21.2 pinned)
├── start.sh                     ← Production startup script
│
├── server/                      ← Server 1: ClinicalIntelligence (port 8001)
│   ├── mcp_server.py            ← FastMCP: 19 tools + REST wrappers
│   ├── guardrails/              ← input_validator · output_validator · clinical_rules
│   ├── guidelines/              ← ADA + USPSTF guideline JSON
│   ├── deliberation/            ← Dual-LLM Deliberation Engine
│   │   ├── engine.py            ← Orchestrator (run + run_progressive modes)
│   │   ├── context_compiler.py  ← Patient context assembly from warehouse
│   │   ├── tiered_context_loader.py ← Progressive 3-tier context loading (11K budget)
│   │   ├── analyst.py           ← Parallel Claude + GPT-4 analysis
│   │   ├── critic.py            ← Cross-critique rounds
│   │   ├── synthesizer.py       ← Unified synthesis → DeliberationResult
│   │   ├── flag_reviewer.py     ← LLM-powered flag lifecycle review (Haiku)
│   │   ├── flag_writer.py       ← Flag registry writes with data provenance
│   │   ├── data_request_parser.py ← Parse agent data requests between rounds
│   │   ├── knowledge_store.py   ← Patient knowledge persistence
│   │   ├── behavioral_adapter.py ← SMS/push nudge formatting
│   │   ├── json_utils.py        ← Markdown fence stripping
│   │   ├── schemas.py           ← Pydantic models
│   │   ├── prompts/             ← LLM prompt templates
│   │   ├── migrations/          ← 4 SQL migrations (001–004)
│   │   └── tests/               ← 109 deliberation unit tests
│   └── migrations/              ← 4 SQL migrations (002–005)
│
├── mcp-server/                  ← Server 2: PatientCompanion (port 8002)
│   ├── server.py                ← FastMCP: auto-discovers all skills (17 tools)
│   ├── skills/                  ← 10 skill modules, each with register(mcp)
│   │   ├── compute_obt_score.py
│   │   ├── generate_patient.py
│   │   ├── generate_vitals.py
│   │   ├── generate_checkins.py
│   │   ├── compute_provider_risk.py
│   │   ├── crisis_escalation.py
│   │   ├── sdoh_assessment.py
│   │   ├── previsit_brief.py
│   │   ├── food_access_nudge.py
│   │   └── ingestion_tools.py   ← 8 tools: freshness, ingestion, conflicts, data tracks
│   ├── db/schema.sql            ← 22-table PostgreSQL base schema (source of truth)
│   ├── transforms/              ← FHIR-to-schema transformers
│   ├── seed.py                  ← Seed: python mcp-server/seed.py --patients 10
│   └── tests/                   ← 92 backend tests
│
├── ingestion/                   ← Server 3: PatientIngestion (port 8003)
│   ├── server.py                ← FastMCP: trigger_ingestion tool
│   ├── pipeline.py              ← ETL pipeline orchestrator
│   ├── conflict_resolver.py     ← Multi-source conflict resolution
│   ├── adapters/
│   │   └── healthex/            ← Two-phase async ingestion adapter
│   │       ├── content_router.py    ← TEXT/STRUCT/REF content classification
│   │       ├── executor.py          ← Phase 2 worker: plan → parse → write
│   │       ├── format_detector.py   ← HealthEx format A/B/C/D detection
│   │       ├── planner.py           ← LLM-assisted extraction planning
│   │       ├── transfer_planner.py  ← Traceable transfer pipeline
│   │       ├── traced_writer.py     ← Audited warehouse writes + transfer log
│   │       ├── llm_fallback.py      ← LLM fallback for unparseable data
│   │       ├── ingest.py            ← Entry point
│   │       └── parsers/             ← 5 format-specific parsers (A/B/C/D + json_dict)
│   └── tests/                   ← 152 ingestion tests
│
├── replit-app/                  ← Next.js 16 frontend (port 5000)
│   ├── next.config.ts           ← Proxy rewrites → 3 MCP servers
│   ├── app/                     ← App Router pages + API routes
│   └── components/              ← React UI components
│
├── replit_dashboard/            ← Config Dashboard (port 8080)
│   ├── server.py                ← FastAPI: 18 env keys + Claude config download
│   ├── index.html               ← Single-page dashboard UI
│   └── tests/                   ← 30 dashboard tests
│
├── tests/
│   ├── phase1/                  ← 196 Phase 1 clinical intelligence tests
│   ├── phase2/                  ← 95 Phase 2 deliberation + flag lifecycle tests
│   ├── e2e/                     ← 28 end-to-end MCP use-case tests
│   └── test_mcp_smoke.py        ← 24 MCP smoke tests
│
├── docs/                        ← Deployment guides
│   ├── replit-deploy-flag-lifecycle.md
│   ├── replit-deploy-ingestion-plans.md
│   └── mcp_use_cases.md
│
├── submission/                  ← MCP marketplace submission package
├── config/system_prompts/       ← Role-based prompts (pcp · care_manager · patient)
├── shared/claude-client.js      ← Shared JS MCP client
└── prototypes/                  ← 4 HTML proof-of-concept prototypes
```

---

## 4. Environment Variables

Copy `.env.example` → `.env` and populate:

```bash
# ── Anthropic ──────────────────────────────────────────────
ANTHROPIC_API_KEY=sk-ant-...
CLAUDE_MODEL=claude-sonnet-4-6
CLAUDE_THINKING_MODE=adaptive          # or "enabled" for explicit budget
CLAUDE_THINKING_BUDGET=10000           # tokens (min 1024, only for explicit mode)
CLAUDE_MAX_TOKENS=16000

# ── LangSmith ──────────────────────────────────────────────
LANGSMITH_API_KEY=ls__...
LANGSMITH_TRACING=true
LANGSMITH_PROJECT=ambient-patient-companion
LANGSMITH_ENDPOINT=https://api.smith.langchain.com

# ── Vector Store (for MemPrompt corrections) ───────────────
VECTOR_STORE_URL=...                   # Pinecone / Qdrant / Weaviate
VECTOR_STORE_API_KEY=...
VECTOR_STORE_INDEX=clinical-corrections

# ── FHIR / EHR ─────────────────────────────────────────────
FHIR_BASE_URL=https://your-fhir-server/R4
FHIR_CLIENT_ID=...
FHIR_CLIENT_SECRET=...

# ── FastMCP Servers (Replit-hosted) ───────────────────────
MCP_SYNTHETIC_PATIENT_URL=https://synthetic-patient.repl.co/mcp
MCP_EHR_INTEGRATION_URL=https://ehr-integration.repl.co/mcp
MCP_CARE_GAP_ANALYZER_URL=https://care-gap-analyzer.repl.co/mcp
MCP_LAB_PROCESSOR_URL=https://lab-processor.repl.co/mcp
MCP_LANGSMITH_FEEDBACK_URL=https://langsmith-feedback.repl.co/mcp

# ── Event Sourcing ──────────────────────────────────────────
DATABASE_URL=postgresql://...          # For event store
KAFKA_BOOTSTRAP_SERVERS=...            # Optional: for production Kafka

# ── Compliance ─────────────────────────────────────────────
HIPAA_AUDIT_LOG=true
AI_DISCLOSURE_REQUIRED=true            # California AB 3030
```

---

## 5. Six Technical Pillars — Implementation

### Pillar 1: Claude Extended Thinking → LangSmith Traces

```python
# core/thinking_config.py

from anthropic import Anthropic
from langchain_anthropic import ChatAnthropic
import os

def get_claude_client():
    """Claude 4.6 with adaptive thinking, LangSmith tracing enabled."""
    return ChatAnthropic(
        model=os.getenv("CLAUDE_MODEL", "claude-sonnet-4-6"),
        thinking={"type": "adaptive"},          # Adaptive = let model decide
        output_config={"effort": "medium"},      # "low" | "medium" | "high"
        max_tokens=int(os.getenv("CLAUDE_MAX_TOKENS", "16000")),
        # LangSmith captures all reasoning blocks automatically
        # when LANGSMITH_TRACING=true
    )

def get_claude_client_explicit(budget_tokens: int = 10000):
    """Use when you need explicit budget control (e.g., complex diagnostics)."""
    return ChatAnthropic(
        model=os.getenv("CLAUDE_MODEL", "claude-sonnet-4-6"),
        thinking={"type": "enabled", "budget_tokens": budget_tokens},
        max_tokens=budget_tokens + 4000,  # max_tokens must exceed budget_tokens
    )

# IMPORTANT: LangGraph's SummarizationMiddleware strips thinking blocks.
# Use direct ChatAnthropic (not Bedrock routing) for thinking-block tracing.
# See: langsmith-sdk issue #2055 (resolved Oct 2025) — but summarization
# middleware issue remains active. Avoid it in multi-turn agent workflows.
```

### Pillar 2: LangSmith Trajectory Evaluation + Self-Learning Loop

```python
# core/feedback_loop.py

from langsmith import Client
from langsmith.evaluation import evaluate
from agentevals import TrajectoryEvaluator

ls_client = Client()

# ── Self-Learning Data Flywheel ─────────────────────────────────────────

async def capture_clinician_feedback(run_id: str, score: int, correction: str = None):
    """
    Attach clinician feedback to a specific LangSmith trace run.
    score: 1 = positive, 0 = negative
    correction: Optional text correction from clinician
    """
    ls_client.create_feedback(
        run_id=run_id,
        key="clinician_score",
        score=score,
        comment=correction,
    )
    if correction and score == 0:
        # Store correction in vector store for MemPrompt retrieval
        await store_correction(run_id, correction)

async def get_few_shot_corrections(query: str, k: int = 3):
    """
    Retrieve semantically similar past corrections for MemPrompt injection.
    Uses LangSmith's indexed dataset (BM25-like retrieval, open beta on paid plans).
    """
    examples = ls_client.similar_examples(
        inputs={"query": query},
        limit=k,
        dataset_name="clinical-corrections",
    )
    return examples

# ── Trajectory Evaluation ───────────────────────────────────────────────

def evaluate_agent_trajectory(run_id: str, expected_tools: list[str]):
    """
    Validate that a triage agent consulted the right clinical tools
    in the right order — not merely that its final answer was correct.
    """
    evaluator = TrajectoryEvaluator(
        mode="subset",   # "strict" | "unordered" | "subset" | "superset"
        expected_trajectory=expected_tools,
    )
    return evaluator.evaluate_run(run_id)
```

### Pillar 3: Proactive Suggestion Engine

```python
# agents/suggestion_agent.py
# Pattern: Separate hidden API call triggered by clinical events
# Informed by Google AMIE's uncertainty-directed questioning

import anthropic
from core.priority_queue import ClinicalEvent, Priority

async def generate_proactive_suggestions(
    patient_state: dict,
    encounter_context: str,
    n_suggestions: int = 3,
) -> list[str]:
    """
    Generates proactive follow-up suggestions via a SEPARATE API call.
    This is middleware-level — not intrinsic model capability.
    Runs after the primary response completes.
    
    Diagnostic uncertainty drives suggestion selection (AMIE pattern):
    - Maintain internal state tracking knowledge gaps
    - Generate questions that most reduce uncertainty
    """
    client = anthropic.Anthropic()
    
    suggestion_prompt = f"""You are analyzing a primary care encounter to generate
proactive clinical suggestions. Patient state: {patient_state}
Context: {encounter_context}

Generate exactly {n_suggestions} follow-up suggestions that would MOST REDUCE
DIAGNOSTIC UNCERTAINTY given what we don't yet know about this patient.

Format as JSON array: ["suggestion1", "suggestion2", "suggestion3"]
Prioritize: (1) gaps in history, (2) missing labs, (3) care guideline gaps.
NEVER suggest anything diagnostic — frame as "possible considerations" only.
Include CA AB 3030 disclaimer: this is AI-generated, contact provider for medical decisions."""

    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=500,
        messages=[{"role": "user", "content": suggestion_prompt}],
    )
    
    import json
    suggestions = json.loads(response.content[0].text)
    return suggestions

# ── Event-Triggered Suggestion Pattern ─────────────────────────────────

async def on_lab_result_received(lab_event: ClinicalEvent):
    """Lab results import → auto-trigger dynamic clinical questionnaire."""
    if lab_event.priority <= Priority.HIGH:
        suggestions = await generate_proactive_suggestions(
            patient_state=lab_event.patient_state,
            encounter_context=f"Lab result received: {lab_event.data}",
        )
        await deliver_suggestions_via_appropriate_channel(
            suggestions=suggestions,
            channel="provider_dashboard",   # NOT patient-facing (FDA Criterion 3)
            require_hcp_review=True,
        )
```

### Pillar 4: Lightweight Feedback Loop (No Retraining)

```python
# core/constitutional.py

HEALTHCARE_CONSTITUTION = """
PRIORITY HIERARCHY (in order of precedence):
1. SAFETY — Never output anything that could cause patient harm
2. ETHICS — Respect patient autonomy, privacy, dignity  
3. COMPLIANCE — FDA CDS criteria, HIPAA, California AB 3030, ONC HTI-1
4. HELPFULNESS — Be maximally useful within the above constraints

CLINICAL ACCURACY REQUIREMENTS:
- Never make diagnostic claims; use "possible considerations" language
- Always cite evidence basis for clinical suggestions (USPSTF, ADA, ACC)
- Scope-of-practice: surface information, do not replace clinical judgment
- Escalation triggers: flag any output that could influence treatment decisions

SCOPE-OF-PRACTICE BOUNDARIES:
- Patient-facing: general wellness only (FDA General Wellness exemption)
- Provider-facing: CDS exclusion criteria — must enable independent HCP review
- Never: diagnose, prescribe, or give specific dosing recommendations

DISCLOSURE REQUIREMENTS (California AB 3030):
- All AI-generated patient communications must include:
  "This message was generated with AI assistance. Please contact your
   healthcare provider for medical decisions."

PATIENT DATA HANDLING:
- PHI must never appear in: logs, error messages, embeddings, or examples
- Use MRN references only, never full patient names in system context

ESCALATION TRIGGERS — route to human review when:
- Confidence < 0.7 on any clinical recommendation
- Patient mentions: suicidal ideation, abuse, neglect, emergency symptoms
- Output involves: medication changes, test ordering, specialist referral
"""

def build_system_prompt(
    role: str,          # "provider" | "patient" | "coordinator"
    context: str,       # "pre_session" | "in_encounter" | "post_visit"
    few_shot_corrections: list = None,
) -> str:
    """Build constitutional system prompt with MemPrompt corrections injected."""
    
    base = f"""You are the Ambient Patient Companion AI assistant.
Current role: {role}
Current context: {context}

{HEALTHCARE_CONSTITUTION}"""

    if few_shot_corrections:
        corrections_xml = "\n".join([
            f"<example>\n  <query>{c['query']}</query>\n  <correction>{c['correction']}</correction>\n</example>"
            for c in few_shot_corrections[:5]  # Claude recommends 3-5 examples
        ])
        base += f"\n\n<corrections>\n{corrections_xml}\n</corrections>"
    
    return base
```

### Pillar 5: Event-Driven Architecture + Clinical Priority Queue

```python
# core/priority_queue.py

import asyncio
from dataclasses import dataclass, field
from enum import IntEnum
from itertools import count

_seq = count()   # Monotonic sequence for FIFO tie-breaking within priority

class Priority(IntEnum):
    CRITICAL  = 0   # Life-threatening: SpO2 < 90%, cardiac arrest alerts → Immediate
    URGENT    = 1   # Critical labs, sepsis indicators → < 1 minute
    HIGH      = 2   # Provider messages requiring clinical action → < 5 minutes
    MODERATE  = 3   # Routine labs, appointment notifications → < 30 minutes
    LOW       = 4   # Patient messages, scheduling, refills → < 2 hours
    DEFERRED  = 5   # Analytics, background sync, reporting → Best effort

@dataclass(order=True)
class ClinicalEvent:
    priority: int              # Priority enum value (lower = more urgent)
    seq: int = field(default_factory=lambda: next(_seq))  # FIFO tie-break
    event_type: str = field(compare=False, default="")
    patient_mrn: str = field(compare=False, default="")
    patient_state: dict = field(compare=False, default_factory=dict)
    data: dict = field(compare=False, default_factory=dict)
    
    # Event sourcing — immutable audit trail
    event_id: str = field(compare=False, default="")
    timestamp: str = field(compare=False, default="")

class ClinicalPriorityQueue:
    def __init__(self, maxsize: int = 1000):
        self._queue = asyncio.PriorityQueue(maxsize=maxsize)
        self._event_log = []   # Immutable event sourcing log
    
    async def put(self, event: ClinicalEvent):
        # Event sourcing: append to immutable log before queuing
        self._event_log.append({
            "event_id": event.event_id,
            "priority": event.priority,
            "type": event.event_type,
            "mrn": event.patient_mrn,
            "timestamp": event.timestamp,
        })
        await self._queue.put(event)
    
    async def get(self) -> ClinicalEvent:
        return await self._queue.get()
    
    def replay_events(self, from_timestamp: str = None) -> list:
        """HIPAA audit support: replay all events from a given point in time."""
        if from_timestamp:
            return [e for e in self._event_log if e["timestamp"] >= from_timestamp]
        return self._event_log.copy()
```

### Pillar 6: FDA Compliance + Regulatory Guardrails

```python
# core/compliance.py

from enum import Enum

class DeliveryChannel(Enum):
    PROVIDER_DASHBOARD = "provider"    # CDS exclusion eligible (Criterion 3)
    PATIENT_PORTAL = "patient"         # FDA regulation applies
    GENERAL_WELLNESS = "wellness"      # General Wellness exemption
    HCP_REVIEWED = "hcp_reviewed"      # CA AB 3030 exception: reviewed before send

def validate_output_compliance(
    content: str,
    channel: DeliveryChannel,
    requires_hcp_review: bool = True,
) -> dict:
    """
    Validate output against FDA CDS four-criteria test and state law.
    Returns: { "compliant": bool, "issues": list, "required_additions": list }
    """
    issues = []
    additions = []
    
    # FDA Criterion 3: patient-facing AI fails CDS non-device exclusion
    if channel == DeliveryChannel.PATIENT_PORTAL and not requires_hcp_review:
        issues.append("Patient-facing AI suggestions require HCP review (FDA Criterion 3)")
    
    # California AB 3030 (effective Jan 2025)
    if channel in [DeliveryChannel.PATIENT_PORTAL, DeliveryChannel.GENERAL_WELLNESS]:
        ab3030_disclaimer = (
            "This message was generated with AI assistance. "
            "Please contact your healthcare provider for medical decisions."
        )
        if ab3030_disclaimer not in content:
            additions.append(("disclaimer", ab3030_disclaimer))
    
    # ONC HTI-1: no predictive DSI without source attribution
    if "recommend" in content.lower() or "suggest" in content.lower():
        if "USPSTF" not in content and "ADA" not in content and "ACC" not in content:
            additions.append(("source_required", "Add evidence basis citation (USPSTF/ADA/ACC)"))
    
    # Language check: no diagnostic terminology in patient-facing content
    diagnostic_terms = ["diagnosis", "diagnose", "you have", "you are suffering from"]
    for term in diagnostic_terms:
        if term in content.lower() and channel == DeliveryChannel.PATIENT_PORTAL:
            issues.append(f"Diagnostic language detected: '{term}' — use 'possible considerations'")
    
    return {
        "compliant": len(issues) == 0,
        "issues": issues,
        "required_additions": additions,
    }
```

---

## 6. FastMCP Synthetic Data Server

> **CRITICAL**: HealthEx MCP cannot generate synthetic data and is incompatible with Claude Code.  
> Use FastMCP (Python) for all synthetic patient data generation.

```python
# mcp_servers/synthetic_patient/server.py

from fastmcp import FastMCP
from datetime import datetime, timedelta
import random

mcp = FastMCP("synthetic-patient-data")

@mcp.tool()
async def get_patient_record(mrn: str = "4829341") -> dict:
    """
    Get synthetic patient record. Default: Maria Chen (canonical demo patient).
    Returns FHIR-compatible patient bundle.
    """
    if mrn == "4829341":
        return MARIA_CHEN_RECORD
    return generate_synthetic_patient(mrn)

@mcp.tool()
async def get_lab_results(mrn: str, days_back: int = 180) -> list:
    """Get synthetic lab results for the past N days."""
    return generate_lab_trend(mrn, days_back)

@mcp.tool()
async def get_care_gaps(mrn: str) -> list:
    """Return USPSTF/ADA care gaps for a patient."""
    return analyze_care_gaps(mrn)

@mcp.tool()
async def get_schedule(provider_id: str = "patel_rahul", date: str = None) -> list:
    """Get provider's day schedule with patient state summaries."""
    if date is None:
        date = datetime.today().strftime("%Y-%m-%d")
    return generate_schedule(provider_id, date)

# Canonical demo patient
MARIA_CHEN_RECORD = {
    "mrn": "4829341",
    "name": "Maria Chen",
    "age": 54,
    "gender": "F",
    "provider": "Dr. Rahul Patel",
    "practice": "Patel Family Medicine",
    "conditions": ["Type 2 Diabetes (E11.9)", "Hypertension (I10)", "Hyperlipidemia (E78.5)"],
    "medications": [
        {"name": "Metformin", "dose": "1000mg", "frequency": "BID", "adherence": 0.78},
        {"name": "Lisinopril", "dose": "10mg", "frequency": "QD", "adherence": 0.91},
        {"name": "Atorvastatin", "dose": "40mg", "frequency": "QD", "adherence": 0.85},
    ],
    "vitals": {"bp": "141/86", "weight": "168 lbs", "bmi": 27.4},
    "labs": {"hba1c": 7.8, "ldl": 112, "egfr": 68, "last_labs": "2025-11-14"},
    "care_gaps": [
        {"gap": "Retinal exam", "due_since": "2024-12-01", "priority": "HIGH"},
        {"gap": "Foot exam", "due_since": "2025-03-01", "priority": "MODERATE"},
        {"gap": "UACR", "due_since": "2025-01-01", "priority": "HIGH"},
    ],
    "sdoh_flags": ["Transportation barrier (NLP-detected)", "Food insecurity risk"],
}

if __name__ == "__main__":
    mcp.run(transport="streamable-http", port=8001)
```

---

## 7. LangGraph Agent Orchestration

```python
# agents/orchestrator.py

from langgraph.graph import StateGraph, END
from langgraph.checkpoint.memory import MemorySaver
from typing import TypedDict, Annotated
import operator

class ClinicalAgentState(TypedDict):
    messages: Annotated[list, operator.add]
    patient_mrn: str
    priority: int
    suggestions: list[str]
    safety_approved: bool
    requires_human_review: bool
    audit_trail: Annotated[list, operator.add]

def build_clinical_graph():
    graph = StateGraph(ClinicalAgentState)
    
    # Nodes
    graph.add_node("triage", triage_agent)
    graph.add_node("generate", suggestion_agent)
    graph.add_node("safety_review", safety_reviewer)     # Constitutional AI critic
    graph.add_node("refine", correction_refiner)         # MemPrompt refinement
    graph.add_node("human_checkpoint", human_review)     # Human-in-the-loop
    graph.add_node("deliver", delivery_agent)
    
    # Edges
    graph.set_entry_point("triage")
    graph.add_edge("triage", "generate")
    graph.add_edge("generate", "safety_review")
    graph.add_conditional_edges("safety_review", route_after_safety, {
        "approved": "deliver",
        "needs_refinement": "refine",
        "human_required": "human_checkpoint",
    })
    graph.add_edge("refine", "safety_review")        # Critic-refiner loop
    graph.add_edge("human_checkpoint", "deliver")
    graph.add_edge("deliver", END)
    
    # Durable checkpointing for HIPAA audit trail
    checkpointer = MemorySaver()
    return graph.compile(checkpointer=checkpointer)

def route_after_safety(state: ClinicalAgentState) -> str:
    if state["requires_human_review"]:
        return "human_required"
    if not state["safety_approved"]:
        return "needs_refinement"
    return "approved"
```

---

## 8. MCP Tools for Claude Desktop/Code

After deploying FastMCP servers to Replit, add these to your Claude settings (`~/Library/Application Support/Claude/claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "synthetic-patient": {
      "command": "npx",
      "args": ["mcp-remote", "https://synthetic-patient.YOUR-REPLIT-URL.repl.co/mcp"]
    },
    "ehr-integration": {
      "command": "npx",
      "args": ["mcp-remote", "https://ehr-integration.YOUR-REPLIT-URL.repl.co/mcp"]
    },
    "care-gap-analyzer": {
      "command": "npx",
      "args": ["mcp-remote", "https://care-gap-analyzer.YOUR-REPLIT-URL.repl.co/mcp"]
    },
    "lab-processor": {
      "command": "npx",
      "args": ["mcp-remote", "https://lab-processor.YOUR-REPLIT-URL.repl.co/mcp"]
    },
    "langsmith-feedback": {
      "command": "npx",
      "args": ["mcp-remote", "https://langsmith-feedback.YOUR-REPLIT-URL.repl.co/mcp"]
    }
  }
}
```

> **Generate this config automatically**: Use the Replit Dashboard at `/replit_dashboard/index.html`
> to enter your Replit URLs → it generates the exact JSON to paste into Claude settings.

---

## 8b. Dual-LLM Deliberation Engine

> **Implemented**: April 2026
> **Status**: Active — both modes operational
> **Location**: `server/deliberation/`

The Deliberation Engine is an async pre-computation layer where Claude (Anthropic) and GPT-4 (OpenAI) independently analyze a patient's clinical context, cross-critique each other's findings through structured debate rounds, then synthesize their combined reasoning into five structured output categories.

### Two Execution Modes

| Mode | Entry Point | Description |
|---|---|---|
| **Progressive** (default) | `engine.run_progressive()` | Tiered context loading — starts with minimal data, fetches more on demand. Prevents context overflow crashes. |
| **Full** | `engine.run()` | Original dual-LLM pipeline — loads all context upfront, runs parallel Claude + GPT-4 analysis. |

### Five Output Categories

| Output | Description | Surfaces In |
|---|---|---|
| **Anticipatory Scenarios** | Clinical scenarios likely in next 30/90/180 days with probability | Provider dashboard (Deliberation tab) |
| **Predicted Patient Questions** | Questions patient likely to ask at next encounter | Pre-encounter brief |
| **Missing Data Flags** | Data gaps identified (marked if both models agreed) | Provider dashboard, care manager alert |
| **Patient Nudges** | BCT-formatted behavioral nudges (SMS, push, portal) | Notification scheduler (pending review) |
| **Knowledge Updates** | Patient-specific and core clinical knowledge | Feeds back into future deliberations |

### Pipeline Phases

```
Phase 0: Context Compilation → PatientContextPackage
Phase 1: Parallel Independent Analysis (Claude + GPT-4)
Phase 2: Cross-Critique Rounds (up to 3, early stop on convergence)
Phase 3: Unified Synthesis → DeliberationResult
Phase 4: Behavioral Adaptation (SMS truncation, reading level)
Phase 5: Knowledge Commit (atomic write to DB tables)
Phase 6: Flag Lifecycle Review (post-deliberation flag correction)
```

### Progressive Context Loading

File: `server/deliberation/tiered_context_loader.py`

The tiered loader builds deliberation context in priority order with a hard character budget:

| Tier | Budget | Content | When Loaded |
|---|---|---|---|
| Tier 1 | 2,000 chars (~500 tokens) | Demographics, active conditions, current meds | Always |
| Tier 2 | 6,000 chars (~1,500 tokens) | Lab trends, biometric history, encounters | On demand (agent requests) |
| Tier 3 | 4,000 chars (~1,000 tokens) | Clinical notes, specific documents | On demand (specific queries) |
| **Total** | **11,000 chars (~2,750 tokens)** | | Well below crash zone at 16,190 |

The `data_request_parser.py` inspects each deliberation round's output for signals that more data is needed (explicit `data_requests` fields or mentions of specific lab tests), then triggers the appropriate tier fetch.

### Flag Lifecycle & Retroactive Correction System

Files: `server/deliberation/flag_reviewer.py`, `server/deliberation/flag_writer.py`
Migration: `server/deliberation/migrations/004_flag_lifecycle.sql`

After each deliberation or data ingestion, the flag reviewer (Claude Haiku — fast, cheap) examines all open flags and determines corrections:

**Flag States:**
```
open → retracted          (data that caused it is now correct)
     → superseded         (replaced by a newer flag on same topic)
     → human_verified     (clinician confirmed still valid)
     → human_dismissed    (clinician confirmed false alarm)
     → resolved           (underlying clinical issue addressed)
```

**Flag Basis Types:** `data_corrupt` | `data_missing` | `data_stale` | `data_conflict` | `clinical_finding` | `derived_inference`

**Correction Actions:** `auto_retract` | `auto_supersede` | `escalate_human` | `confirm_valid` | `upgrade_priority` | `downgrade_priority`

**Safety Rule:** Never auto-retract a flag that:
- Was linked to a nudge sent to a patient or care team
- Has priority `critical` or `high` unless evidence is overwhelming
- Involves medication safety, allergy, or acute symptoms

### Database Tables (8 new)

- `deliberations` — session record with convergence score, mode, and transcript
- `deliberation_outputs` — five output categories per deliberation
- `patient_knowledge` — accumulated patient-specific knowledge with temporal validity
- `core_knowledge_updates` — shared clinical knowledge reinforcements
- `deliberation_data_requests` — tracked data requests between progressive rounds
- `deliberation_flags` — flag registry with lifecycle state, basis, priority, provenance
- `flag_review_runs` — audit trail of flag review sessions
- `flag_corrections` — individual corrections applied to flags

### MCP Tools (6 on Server 1)

- `run_deliberation` — trigger deliberation (progressive or full mode)
- `get_deliberation_results` — retrieve structured outputs per patient
- `get_flag_review_status` — current flag lifecycle status (open, retracted, needs review)
- `get_patient_knowledge` — query accumulated patient knowledge
- `get_pending_nudges` — fetch undelivered nudges for scheduling

### Cost Estimate

~15,000-30,000 tokens per full deliberation run (both models combined across all phases). Progressive mode uses ~40% fewer tokens by loading context on demand. At current pricing, approximately $0.15-0.30 per full session, $0.08-0.18 per progressive session.

### Known Limitations

- Convergence detection uses Jaccard similarity (word overlap), not semantic similarity
- Next improvement: replace `_compute_convergence` with MedCPT sentence embeddings
- Vector store for guideline retrieval is currently a placeholder
- Nudges queue for delivery but auto-send is disabled by default

---

## 8c. Two-Phase Async Ingestion Architecture

> **Implemented**: April 2026
> **Status**: Active
> **Location**: `ingestion/adapters/healthex/`

The ingestion pipeline uses a two-phase architecture to handle the variety of HealthEx data formats (FHIR Bundles, native summaries, Binary/DocumentReference resources, raw text):

### Phase 1 — Planner

The LLM planner (`planner.py`) analyzes raw data blobs and creates extraction plans stored in `ingestion_plans`. Each plan specifies what resource types are present, what fields to extract, and how many rows to expect.

### Phase 2 — Executor

The executor (`executor.py`) reads pending plans from the `ingestion_plans` table, fetches raw blobs from `raw_fhir_cache`, routes through the appropriate parser via adaptive parsing, and writes structured rows through the normalize → transform → write pipeline.

### Content Router

File: `content_router.py`

Classifies HealthEx `Binary`, `DocumentReference`, and `Observation` resources by content type:

| Route | Content Types | Destination |
|---|---|---|
| **TEXT** | text/html, text/rtf, text/plain, text/xml | `clinical_notes` table |
| **STRUCT** | application/json, application/fhir+json | Existing warehouse tables (conditions, labs, etc.) |
| **REF** | image/*, audio/*, video/*, application/pdf | `media_references` table (URL + metadata only) |

### Format Detection & Parsers

The format detector (`format_detector.py`) identifies four HealthEx data formats. Each has a dedicated parser:

| Format | Parser | Description |
|---|---|---|
| A | `format_a_parser.py` | Standard FHIR Bundles |
| B | `format_b_parser.py` | HealthEx native summary format |
| C | `format_c_parser.py` | Binary/DocumentReference resources |
| D | `format_d_parser.py` | Mixed format with embedded resources |
| — | `json_dict_parser.py` | Generic JSON dict fallback |

When all parsers fail, `llm_fallback.py` uses Claude to extract structured data from unrecognized formats.

### Traced Writer & Transfer Log

All warehouse writes go through `traced_writer.py`, which:
1. Writes structured rows to the appropriate table
2. Records every write in `transfer_log` with provenance (source, resource type, row count, duration)
3. Returns canonical `patient_id` UUID for downstream use

### Database Tables (4 new)

- `ingestion_plans` — extraction plans with status, resource type, insights summary
- `transfer_log` — audited record of every warehouse write
- `clinical_notes` — extracted clinical note text (note type, content, date, author)
- `media_references` — non-text assets (content type, doc type, reference URL)

### MCP Tools (on Server 1)

- `execute_pending_plans` — execute pending two-phase ingestion plans
- `get_ingestion_plans` — list ingestion plans for a patient with status
- `get_transfer_audit` — audit trail for data transfers

### Migration 005 — Clinical Data Storage Fix

`server/migrations/005_clinical_data_storage.sql` addresses silent data truncation:
- Widens `VARCHAR(20)` → `TEXT` for `biometric_readings.unit`, `metric_type`, `patient_conditions.display`, `patient_medications.display`, `patient_sdoh_flags.flag_code`
- Adds structured lab columns: `result_text` (qualitative), `reference_range_text`, `value_precise` (NUMERIC for exact threshold comparisons)

---

## 9. Implementation Priority Sequence

### Week 1 — Foundation
- [ ] Constitutional system prompt live (`core/constitutional.py`)
- [ ] FastMCP synthetic data server deployed on Replit (Maria Chen data)
- [ ] Claude 4.6 + adaptive thinking configured with LangSmith tracing
- [ ] Clinical priority queue running (`core/priority_queue.py`)

### Week 2 — Feedback Infrastructure
- [ ] LangSmith feedback capture wired to provider UI (thumbs up/down)
- [ ] Vector store deployed for MemPrompt corrections
- [ ] Correction retrieval injected into system prompt on every call
- [ ] LangSmith annotation queue for clinical reviewer workflow

### Month 1 — Agent Orchestration
- [ ] LangGraph multi-agent graph: triage → generate → safety → deliver
- [ ] Critic-refiner loop with iteration cap (max 3 refinements)
- [ ] Proactive suggestion engine (separate API call post-response)
- [ ] FHIR R5 subscription events feeding priority queue

### Month 2 — Continuous Improvement
- [ ] Automated system prompt updates from feedback pattern analysis
- [ ] Trajectory evaluation in LangSmith (agentevals, subset mode)
- [ ] Multi-turn online evaluation for patient-facing interactions
- [ ] AI Model Registry + Applied Model Cards (CHAI standard)

### Quarter 2 — Scale + Compliance
- [ ] ONC HTI-1 FAVES methodology documentation for predictive DSI
- [ ] Joint Commission / CHAI certification preparation
- [ ] Optional: DPO fine-tuning on accumulated preference data
- [ ] Population health panel with 11-patient schedule (full day view)

---

## 10. Key Constraints & Hard Rules

| Rule | Detail |
|---|---|
| **HealthEx MCP** | Cannot generate synthetic data. Incompatible with Claude Code. Use FastMCP. |
| **Thinking blocks** | Do NOT use LangGraph SummarizationMiddleware — strips thinking blocks. |
| **Bedrock routing** | Use direct ChatAnthropic, not Bedrock, for thinking-block tracing. |
| **Few-shot examples** | Always 3–5 examples. Use XML tags: `<corrections>`, `<example>`, `<clinician_guidance>`. |
| **Patient-facing AI** | Must route through HCP review OR carry AB 3030 disclaimer. Cannot be diagnostic. |
| **Token billing** | Thinking tokens billed at output rates. Claude 4.6 Sonnet: 64k output max. |
| **Priority queue backpressure** | Set `maxsize=1000` on asyncio.PriorityQueue to handle surges. |
| **Event sourcing** | All state changes stored as immutable events. Never mutate event log. |
| **PHI in logs** | Never. Use MRN references only. Validate before any logging call. |
| **Two-phase ingestion** | Plans cached in `ingestion_plans`; executor reads raw blobs from `raw_fhir_cache`. Never write without a plan. |
| **Flag lifecycle safety** | Never auto-retract flags linked to sent nudges or with priority critical/high. See `flag_reviewer.py`. |
| **Context budget** | Tiered loader enforces 11K char total (Tier 1: 2K, Tier 2: 6K, Tier 3: 4K). Exceeding this crashes deliberation. |
| **VARCHAR columns** | Use TEXT not VARCHAR for clinical data columns. Migration 005 fixed silent truncation of UCUM codes and reference ranges. |

---

## 11. Testing & Evaluation

~695 test functions across 9 test suites.

```bash
# Phase 1 — Clinical intelligence tools (196 tests)
python -m pytest tests/phase1/ -v

# Phase 2 — Deliberation + flag lifecycle (95 tests)
python -m pytest tests/phase2/ -v

# End-to-end MCP tool tests (28 tests)
python -m pytest tests/e2e/ -v

# MCP smoke tests (24 tests)
python -m pytest tests/test_mcp_smoke.py -v

# Ingestion pipeline tests (152 tests)
python -m pytest ingestion/tests/ -v

# Backend MCP skills (92 tests)
cd mcp-server && python -m pytest tests/ -v

# Deliberation engine unit tests (109 tests)
python -m pytest server/deliberation/tests/ -v

# Frontend (37 tests)
cd replit-app && npm test

# Config dashboard (30 tests)
cd replit_dashboard && python -m pytest

# Start MCP servers
MCP_TRANSPORT=streamable-http MCP_PORT=8001 python -m server.mcp_server &
cd mcp-server && MCP_TRANSPORT=streamable-http MCP_PORT=8002 python server.py &
MCP_TRANSPORT=streamable-http MCP_PORT=8003 python -m ingestion.server &
```

---

## 12. Demo Personas (for prototype testing)

| MRN | Name | Age | Key Conditions | Demo Purpose |
|---|---|---|---|---|
| 4829341 | Maria Chen | 54F | T2DM, HTN, HLD | Primary canonical patient |
| 4829342 | James Rivera | 67M | CHF, CKD Stage 3 | High-acuity complexity |
| 4829343 | Aisha Okonkwo | 41F | Prenatal, anxiety | Care gap & SDoH focus |
| 4829344 | Robert Kim | 72M | COPD, depression | Polypharmacy + mental health |
| 4829345 | Sarah Patel | 29F | Hypothyroidism | Low-acuity, preventive care |

---

---

## 13. HealthEx Session-Bridge Protocol

Real-patient data enters the warehouse through an 8-step session-bridge because HealthEx keeps records in the authenticated Claude session, not the warehouse. All 8 steps are mandatory for `run_deliberation` to function.

> **Note**: `run_deliberation` now defaults to `mode="progressive"` (tiered context loading). Pass `mode="full"` for the original dual-LLM pipeline.

### Why it matters

`run_deliberation` compiles patient context by querying the warehouse PostgreSQL tables (`patient_conditions`, `patient_medications`, `biometric_readings`, `clinical_events`). If no patient row exists in `patients`, the context compiler raises a 404 and the deliberation pipeline never fires.

Synthetic patients (Maria Chen, `MC-2025-4829`) work end-to-end because they have fixed MRNs pre-seeded in the warehouse. Real patients connected via HealthEx do not — hence this protocol.

### The 8-Step Workflow

| Step | Tool | Purpose |
|---|---|---|
| 1 | `get_data_source_status` | Confirm `DATA_TRACK` and existing patients |
| **2** | **`register_healthex_patient`** | **Mandatory bootstrapper — upserts `patients` row (`is_synthetic=False`), initialises `data_sources` + `source_freshness`, sets `DATA_TRACK=healthex`, returns canonical UUID** |
| 3 | `use_healthex` | Switch session to HealthEx connector |
| 4a | `get_health_summary` (HealthEx) | Pull patient demographics |
| 4b | `get_lab_results` (HealthEx) | Pull labs as FHIR Observations |
| 4c | `get_medications` (HealthEx) | Pull meds as FHIR MedicationRequests |
| 4d | `get_conditions` (HealthEx) | Pull diagnoses as FHIR Conditions |
| 5 | `ingest_from_healthex` (×4) | Write each resource type to warehouse (returns JSON with canonical `patient_id`) |
| 6 | `check_data_freshness` | Verify records landed in warehouse |
| 7 | `run_deliberation` | Fire 5-phase Claude + GPT-4 critique loop |
| 8 | `get_deliberation_results` | Retrieve anticipatory scenarios + nudges |

### Step 2 Detail: `register_healthex_patient`

```
Input:  health_summary_json  (raw JSON from HealthEx get_health_summary)
        mrn_override         (optional — override MRN when summary omits it)

Accepts:
  • Bare FHIR Patient resource   {"resourceType": "Patient", ...}
  • FHIR Bundle                  {"resourceType": "Bundle", "entry": [...]}
  • HealthEx summary dict        {"name": "Ali Omrani", "mrn": "HX-...", ...}

Output: {"status": "registered", "patient_id": "<UUID>", "mrn": "<MRN>",
          "is_synthetic": false, "data_track": "healthex", "next_step": "..."}
```

The returned `patient_id` UUID must be passed to all subsequent `ingest_from_healthex` calls.

### `ingest_from_healthex` return format (Patch 3)

Previously returned a plain string. Now returns structured JSON:

```json
{
  "status": "ok",
  "resource_type": "labs",
  "records_written": 12,
  "duration_ms": 340,
  "patient_id": "<canonical-UUID>"
}
```

The `patient_id` in the response is the canonical UUID recovered from `ON CONFLICT (mrn)` — useful when `register_healthex_patient` was accidentally skipped.

### Files Changed (Patches 1–4)

| File | Change |
|---|---|
| `mcp-server/skills/ingestion_tools.py` | `register_healthex_patient` at module level + `mcp.tool()` registration; `ingest_from_healthex` captures canonical UUID + returns JSON |
| `mcp-server/transforms/fhir_to_schema.py` | `transform_patient(is_synthetic=True)` — callers can pass `False` for real patients |
| `ingestion/tests/test_healthex_registration.py` | HR-1→HR-7 tests (mock-only, no live DB required) |
| `mcp-server/tests/fixtures/fhir/` | Minimal Synthea FHIR bundles for `generate_patient` skill tests |

### Test Commands

```bash
pytest ingestion/tests/test_healthex_registration.py -v   # HR-1→HR-7
pytest ingestion/tests/test_pipeline.py -v                # P1→P8
pytest mcp-server/tests/ -v                               # schema + skills + generators
pytest tests/e2e/test_deliberation_tools.py -v            # UC-16→UC-20b
```

### IMPORTANT — Routing Patch (April 2026)

`register_healthex_patient` and `ingest_from_healthex` were **moved from port 8002 (`/mcp-skills`) to port 8001 (`/mcp`)** — the Clinical Intelligence server — because Claude Web's external connections cannot reach the `/mcp-skills` Next.js proxy (SSE streaming incompatibility). The "No approval received" error from Claude Web is its own HITL gate triggered when the server is unreachable, not a bug in FastMCP.

**All 19 tools now run on the single `/mcp` endpoint (port 8001).** Key HealthEx pipeline tools:

| Tool | Canonical location |
|---|---|
| `use_healthex` | `server/mcp_server.py` (port 8001) |
| `register_healthex_patient` | `server/mcp_server.py` (port 8001) — NOT `mcp-server/skills/ingestion_tools.py` |
| `ingest_from_healthex` | `server/mcp_server.py` (port 8001) — NOT `mcp-server/skills/ingestion_tools.py` |
| `execute_pending_plans` | `server/mcp_server.py` (port 8001) — two-phase ingestion executor |
| `get_ingestion_plans` | `server/mcp_server.py` (port 8001) — plan status + insights |
| `get_transfer_audit` | `server/mcp_server.py` (port 8001) — transfer provenance log |
| `run_deliberation` | `server/mcp_server.py` (port 8001) — progressive (default) or full mode |
| `get_deliberation_results` | `server/mcp_server.py` (port 8001) |
| `get_flag_review_status` | `server/mcp_server.py` (port 8001) — flag lifecycle dashboard |
| `get_patient_knowledge` | `server/mcp_server.py` (port 8001) |
| `get_pending_nudges` | `server/mcp_server.py` (port 8001) |

All tools also have REST wrappers at `http://localhost:8001/tools/<name>` for use by the JS client (`shared/claude-client.js`).

*Last updated: April 2026 — Ambient Action Model v2.2*
*Architecture source: "Healthcare AI Architecture: Six Technical Pillars" (2026)*
