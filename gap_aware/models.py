"""Pydantic models for all gap-aware MCP tools."""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field


# ── Shared type aliases ───────────────────────────────────────────────────────

GapType = Literal[
    "missing_data", "stale_data", "conflicting_evidence",
    "ambiguous_context", "guideline_uncertainty",
    "drug_interaction_unknown", "patient_preference_unknown",
    "social_determinant_unknown",
]

Severity = Literal["critical", "high", "medium", "low"]

AgentId = Literal["ARIA", "MIRA", "THEO"]

Recipient = Literal["provider", "patient", "peer_agent", "synthesis"]

ProceedRecommendation = Literal[
    "proceed", "proceed_with_caveats", "pause_and_resolve", "escalate_to_provider",
]


# ── Tool 1: assess_reasoning_confidence ───────────────────────────────────────

class ConfidenceGap(BaseModel):
    gap_id: str = Field(default_factory=lambda: f"gap_{uuid.uuid4().hex[:8]}")
    gap_type: GapType
    severity: Severity
    description: str
    affected_reasoning_step: str
    data_elements_needed: List[str] = []
    staleness_hours: Optional[float] = None
    resolvable_by: List[Literal[
        "provider_clarification", "patient_query",
        "external_search", "lab_order", "peer_agent",
    ]] = []


class AssessReasoningConfidenceInput(BaseModel):
    agent_id: AgentId
    deliberation_id: str
    patient_mrn: str
    reasoning_draft: str
    context_snapshot: Optional[Dict[str, Any]] = None
    clinical_domain: Literal[
        "risk_assessment", "pharmacotherapy",
        "patient_education", "care_planning",
    ]
    confidence_threshold: float = 0.7


class AssessReasoningConfidenceOutput(BaseModel):
    overall_confidence: float
    threshold_met: bool
    gaps: List[ConfidenceGap] = []
    proceed_recommendation: ProceedRecommendation


# ── Tool 2: detect_context_staleness ─────────────────────────────────────────

class ContextElement(BaseModel):
    element_type: Literal[
        "lab_result", "vital_sign", "medication_list",
        "problem_list", "imaging", "encounter_note",
    ]
    loinc_code: Optional[str] = None
    last_updated: datetime
    source_system: str


class StaleElement(BaseModel):
    element_type: str
    loinc_code: Optional[str]
    age_hours: float
    max_acceptable_age_hours: float
    clinical_rationale: str
    guideline_source: str


class DetectContextStalenessInput(BaseModel):
    patient_mrn: str
    context_elements: List[ContextElement]
    clinical_scenario: Literal[
        "pre_encounter", "acute_event", "chronic_management",
        "medication_change", "discharge_planning",
    ]


class DetectContextStalenessOutput(BaseModel):
    stale_elements: List[StaleElement] = []
    freshness_score: float
    recommended_refreshes: List[str] = []


# ── Tool 3: search_clinical_knowledge ─────────────────────────────────────────

class KnowledgeSearchResult(BaseModel):
    source: str
    source_url: Optional[str] = None
    finding: str
    evidence_level: Literal[
        "any", "case_report", "observational",
        "rct", "systematic_review", "guideline",
    ]
    relevance_score: float
    clinical_applicability: str
    last_updated: Optional[datetime] = None


class SearchClinicalKnowledgeInput(BaseModel):
    query: str
    query_type: Literal[
        "drug_interaction", "guideline_recommendation", "lab_reference_range",
        "contraindication", "dosing_adjustment", "differential_diagnosis",
        "screening_schedule",
    ]
    patient_context: Optional[Dict[str, Any]] = None
    sources_to_search: List[Literal[
        "openfda", "rxnorm", "dailymed", "pubmed",
        "clinicaltrials", "snomed", "loinc",
    ]] = ["openfda", "rxnorm", "pubmed"]
    evidence_level_minimum: str = "any"
    max_results_per_source: int = 5
    gap_id: Optional[str] = None


class SearchClinicalKnowledgeOutput(BaseModel):
    results: List[KnowledgeSearchResult] = []
    gap_resolved: bool
    synthesis_summary: str
    confidence_after_search: float
    caveats: List[str] = []


# ── Tool 4: request_clarification ─────────────────────────────────────────────

class ClarificationQuestion(BaseModel):
    text: str
    clinical_rationale: str
    response_schema: Optional[Dict[str, Any]] = None
    suggested_options: Optional[List[str]] = None
    default_if_unanswered: Optional[str] = None


class RequestClarificationInput(BaseModel):
    deliberation_id: str
    requesting_agent: AgentId
    recipient: Recipient
    recipient_agent_id: Optional[str] = None
    urgency: Literal["blocking", "preferred", "optional"]
    question: ClarificationQuestion
    gap_id: str
    timeout_minutes: int = 60
    fallback_behavior: Literal[
        "proceed_with_default", "skip_reasoning_step", "escalate_to_synthesis",
    ] = "escalate_to_synthesis"


class RequestClarificationOutput(BaseModel):
    clarification_id: str
    status: Literal["answered", "pending", "timeout", "declined"]
    response: Optional[Dict[str, Any]] = None
    respondent: Optional[str] = None
    response_timestamp: Optional[datetime] = None
    resolution_action: Literal["gap_resolved", "fallback_applied", "escalated"]


# ── Tool 5: search_patient_data_extended ──────────────────────────────────────

class DataElementQuery(BaseModel):
    element_type: str
    loinc_code: Optional[str] = None
    rxnorm_code: Optional[str] = None
    snomed_code: Optional[str] = None
    lookback_days: int = 365


class FoundDataElement(BaseModel):
    element_type: str
    value: str
    unit: Optional[str] = None
    effective_date: datetime
    source_system: str
    provenance: str
    normalized: bool


class SearchPatientDataExtendedInput(BaseModel):
    patient_mrn: str
    search_scope: List[Literal[
        "warehouse_full_history", "pharmacy_claims",
        "hie_network", "external_labs", "patient_reported", "wearable_telemetry",
    ]]
    data_elements: List[DataElementQuery]
    gap_id: Optional[str] = None
    fhir_query_override: Optional[str] = None


class SearchPatientDataExtendedOutput(BaseModel):
    found_elements: List[FoundDataElement] = []
    not_found: List[str] = []
    gap_resolved: bool


# ── Tool 6: emit_reasoning_gap_artifact ───────────────────────────────────────

class AttemptedResolution(BaseModel):
    method: str
    result: Literal["resolved", "partially_resolved", "unresolved", "timeout"]


class GapArtifact(BaseModel):
    gap_type: GapType
    severity: Severity
    description: str
    impact_statement: str
    confidence_without_resolution: float
    confidence_with_resolution: float
    attempted_resolutions: List[AttemptedResolution] = []
    recommended_action_for_synthesis: Literal[
        "include_caveat_in_output", "defer_to_provider",
        "flag_for_next_encounter", "add_to_care_gap_list",
        "trigger_order_recommendation",
    ]
    caveat_text: Optional[str] = None
    expires_at: Optional[datetime] = None


class EmitReasoningGapArtifactInput(BaseModel):
    deliberation_id: str
    emitting_agent: AgentId
    gap_id: str
    artifact: GapArtifact


class EmitReasoningGapArtifactOutput(BaseModel):
    artifact_id: str
    stored: bool
    synthesis_notified: bool
    downstream_actions_triggered: List[str] = []


# ── Tool 7: register_gap_trigger ─────────────────────────────────────────────

class TriggerCondition(BaseModel):
    watch_for: Literal[
        "lab_result", "medication_change", "encounter_note",
        "screening_score", "vital_sign", "patient_response",
    ]
    loinc_code: Optional[str] = None
    snomed_code: Optional[str] = None
    custom_condition: Optional[str] = None


class RegisterGapTriggerInput(BaseModel):
    patient_mrn: str
    gap_id: str
    trigger_condition: TriggerCondition
    trigger_type: str = "gap_resolution_received"
    expires_at: datetime
    on_fire_action: Literal[
        "re_run_deliberation", "update_gap_artifact",
        "notify_synthesis", "notify_provider",
    ]
    deliberation_scope: List[Literal[
        "ARIA", "MIRA", "THEO", "full_council",
    ]] = ["full_council"]


class RegisterGapTriggerOutput(BaseModel):
    trigger_id: str
    registered: bool
    expires_at: datetime
    estimated_resolution_probability: float
