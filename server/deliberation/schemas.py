"""
schemas.py — Pydantic models for the Dual-LLM Deliberation Engine.
Every inter-phase data transfer uses these models.
"""
from __future__ import annotations
from datetime import datetime
from typing import Optional
from uuid import UUID
from pydantic import BaseModel, Field, field_validator


# ── INPUT ─────────────────────────────────────────────────────────────────────

class PatientContextPackage(BaseModel):
    """Phase 0 output / Phase 1 input. Complete patient context."""
    patient_id: str
    patient_name: str
    age: Optional[int] = None
    sex: str
    mrn: str
    primary_provider: str
    practice: str

    @field_validator("age", mode="before")
    @classmethod
    def _coerce_age(cls, v):
        """Coerce string/float age to int where possible. Pass None through
        unchanged so downstream prompts can render 'age unknown' explicitly
        rather than silently reasoning about a 0-year-old. Unparseable strings
        also fall back to None. Real DOB-based age computation happens
        upstream in context_compiler.py.
        """
        if v is None:
            return None
        try:
            return int(v)
        except (TypeError, ValueError):
            return None

    def age_display(self) -> str:
        """Render age for prompt consumption. 'age unknown' when None — never
        emits the string 'None' into a prompt.
        """
        return "age unknown" if self.age is None else str(self.age)
    # Clinical state
    active_conditions: list[dict]       # [{code, display, onset_date}]
    current_medications: list[dict]     # [{name, dose, frequency, start_date}]
    recent_labs: list[dict]             # [{name, value, unit, date, in_range}]
    vital_trends: list[dict]            # [{name, readings: [{value, date}]}]
    care_gaps: list[dict]               # [{gap_type, last_done, due_date}]
    sdoh_flags: list[str]               # ["food_insecurity", "transportation_barrier"]
    # Prior deliberation knowledge
    prior_patient_knowledge: list[dict] # from patient_knowledge table (is_current=true)
    # Relevant guidelines (pre-fetched from vector store)
    applicable_guidelines: list[dict]
    # Temporal context
    upcoming_appointments: list[dict]
    days_since_last_encounter: int
    deliberation_trigger: str
    # Ingestion plan summaries (from ingestion_plans table)
    data_inventory: list[dict] = []     # [{resource_type, summary, rows}]
    # Clinical notes extracted from Binary/Observation resources (clinical_notes table)
    clinical_notes: list[dict] = []     # [{type, text, date, author, source}]
    # Media inventory — URL references to non-text assets (media_references table)
    available_media: list[dict] = []    # [{type, url, date}]


class DeliberationRequest(BaseModel):
    """Input to engine.run()"""
    patient_id: str
    trigger_type: str
    max_rounds: int = Field(default=3, ge=1, le=5)
    force_round_count: Optional[int] = None   # override convergence detection


# ── PHASE 1 OUTPUTS ───────────────────────────────────────────────────────────

class ClaimWithConfidence(BaseModel):
    claim: str
    confidence: float = Field(ge=0.0, le=1.0)
    evidence_refs: list[str] = Field(default_factory=list)

class IndependentAnalysis(BaseModel):
    """Output of Phase 1 from each model."""
    # Set server-side after model_validate_json — must be Optional so parsing
    # succeeds before the caller assigns them (see analyst.py lines 112-116).
    model_id: str = ""                   # 'claude-sonnet-4-20250514' or 'gpt-4o'
    role_emphasis: str = ""             # 'diagnostic_reasoning' or 'treatment_optimization'
    key_findings: list[ClaimWithConfidence]
    risk_flags: list[ClaimWithConfidence]
    recommended_actions: list[ClaimWithConfidence]
    anticipated_trajectory: str          # narrative 3-6 month outlook
    missing_data_identified: list[str]
    raw_reasoning: str = ""              # full CoT, stored for audit


# ── PHASE 2 OUTPUTS ───────────────────────────────────────────────────────────

class CritiqueItem(BaseModel):
    target_claim: str
    critique_type: str  # 'factual_error'|'logical_gap'|'missed_consideration'|'overconfidence'
    critique_text: str
    suggested_revision: Optional[str] = None
    severity: str  # 'blocking'|'moderate'|'minor'

class CrossCritique(BaseModel):
    # critic_model, target_model set via prompt guidance; round_number set server-side.
    # All default to "" / 0 so model_validate_json succeeds before caller assigns.
    critic_model: str = ""
    target_model: str = ""
    round_number: int = 0
    critique_items: list[CritiqueItem]
    areas_of_agreement: list[str]
    raw_critique: str = ""

class RevisedAnalysis(BaseModel):
    # model_id and round_number set server-side (critic.py lines 101-102).
    # raw_revision defaults to "" if LLM omits it.
    model_id: str = ""
    round_number: int = 0
    revised_findings: list[ClaimWithConfidence]
    revisions_made: list[str]       # what changed and why
    maintained_positions: list[str] # what was defended and why
    raw_revision: str = ""


# ── PHASE 3 OUTPUTS (Final Synthesis) ─────────────────────────────────────────

class AnticipatoryScenario(BaseModel):
    scenario_id: str
    timeframe: str              # 'next_30_days'|'next_90_days'|'next_6_months'
    title: str
    description: str
    probability: float = Field(ge=0.0, le=1.0)
    confidence: float = Field(ge=0.0, le=1.0)
    clinical_implications: str
    evidence_basis: list[str]
    dissenting_view: Optional[str] = None  # preserved if models did not converge

class PredictedPatientQuestion(BaseModel):
    question: str
    likelihood: float = Field(ge=0.0, le=1.0)
    category: str   # 'medication_understanding'|'risk_awareness'|'lifestyle'|'logistics'
    suggested_response: str  # plain language, 6th grade reading level
    reading_level: str
    behavioral_framing: str  # 'facilitator'|'spark'|'sustainer'

class MissingDataFlag(BaseModel):
    flag_id: str
    priority: str   # 'critical'|'high'|'medium'|'low'
    data_type: str  # 'lab_result'|'social_determinant'|'screening'|'medication_history'
    description: str
    clinical_relevance: str
    recommended_action: str
    confidence: float = Field(ge=0.0, le=1.0)
    both_models_agreed: bool = False

class NudgeContent(BaseModel):
    nudge_id: str
    target: str         # 'patient'|'care_team'
    trigger_condition: str
    behavioral_technique: str   # BCT taxonomy code, e.g. 'BCT_1.4_action_planning'
    com_b_target: str           # COM-B model component targeted
    channels: dict              # {'sms': str, 'push_notification': dict, 'portal': str}
    reading_level: str
    personalization_factors: list[str]
    decay_schedule: Optional[str] = None

class KnowledgeUpdate(BaseModel):
    update_type: str        # 'reinforcement'|'revision'|'new_inference'
    scope: str              # 'core'|'patient_specific'
    entry_text: str
    confidence: float = Field(ge=0.0, le=1.0)
    valid_from: datetime
    valid_until: Optional[datetime] = None
    supersedes: Optional[str] = None
    evidence: list[str] = Field(default_factory=list)

class DeliberationResult(BaseModel):
    """Complete output of the engine. Maps directly to DB tables."""
    deliberation_id: str
    patient_id: str
    timestamp: datetime
    trigger: str
    # Server-assigned after synthesis — defaults allow synthesizer to validate first
    models: dict = Field(default_factory=dict)
    rounds_completed: int = 0
    convergence_score: float = 0.0
    total_tokens: int = 0
    total_latency_ms: int = 0
    # Five output categories
    anticipatory_scenarios: list[AnticipatoryScenario] = Field(default_factory=list)
    predicted_patient_questions: list[PredictedPatientQuestion] = Field(default_factory=list)
    missing_data_flags: list[MissingDataFlag] = Field(default_factory=list)
    nudge_content: list[NudgeContent] = Field(default_factory=list)
    knowledge_updates: list[KnowledgeUpdate] = Field(default_factory=list)
    # Preserved disagreements requiring clinician attention
    unresolved_disagreements: list[dict] = Field(default_factory=list)
    # Full audit trail — set by engine after synthesis
    transcript: dict = Field(default_factory=dict)
    # Gap-aware validation metadata (set by engine after deliberation)
    gap_artifacts: list[dict] = Field(default_factory=list)
    gap_summary: str = ""
    context_validation: dict = Field(default_factory=dict)
    # ATOM-first behavioral detection — set by engine after synthesis via
    # behavioral_section_builder. A role-filtered list of structured
    # cards (screening_gap, positive_screen, critical_flag, sdoh_need,
    # behavioral_routing). Empty list when no phenotype/cards exist.
    behavioral_section: list[dict] = Field(default_factory=list)
