"""Tests for gap_aware.models — Pydantic model validation."""
import pytest
from datetime import datetime, timezone


# ── ConfidenceGap ─────────────────────────────────────────────────────────────

def test_confidence_gap_defaults():
    from gap_aware.models import ConfidenceGap

    gap = ConfidenceGap(
        gap_type="missing_data",
        severity="high",
        description="Missing HbA1c",
        affected_reasoning_step="Risk assessment",
    )
    assert gap.gap_id.startswith("gap_")
    assert gap.data_elements_needed == []
    assert gap.staleness_hours is None
    assert gap.resolvable_by == []


def test_confidence_gap_all_gap_types():
    from gap_aware.models import ConfidenceGap

    valid_types = [
        "missing_data", "stale_data", "conflicting_evidence",
        "ambiguous_context", "guideline_uncertainty",
        "drug_interaction_unknown", "patient_preference_unknown",
        "social_determinant_unknown",
    ]
    for gt in valid_types:
        gap = ConfidenceGap(
            gap_type=gt, severity="low",
            description="test", affected_reasoning_step="test",
        )
        assert gap.gap_type == gt


def test_confidence_gap_invalid_type():
    from gap_aware.models import ConfidenceGap
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        ConfidenceGap(
            gap_type="invented_type", severity="high",
            description="test", affected_reasoning_step="test",
        )


def test_confidence_gap_invalid_severity():
    from gap_aware.models import ConfidenceGap
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        ConfidenceGap(
            gap_type="missing_data", severity="extreme",
            description="test", affected_reasoning_step="test",
        )


# ── AssessReasoningConfidenceOutput ───────────────────────────────────────────

def test_assess_output_valid():
    from gap_aware.models import AssessReasoningConfidenceOutput

    out = AssessReasoningConfidenceOutput(
        overall_confidence=0.65,
        threshold_met=False,
        gaps=[],
        proceed_recommendation="proceed_with_caveats",
    )
    assert out.overall_confidence == 0.65
    assert out.threshold_met is False


def test_assess_output_invalid_recommendation():
    from gap_aware.models import AssessReasoningConfidenceOutput
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        AssessReasoningConfidenceOutput(
            overall_confidence=0.5, threshold_met=False,
            proceed_recommendation="unknown_action",
        )


# ── StaleElement ──────────────────────────────────────────────────────────────

def test_stale_element():
    from gap_aware.models import StaleElement

    se = StaleElement(
        element_type="lab_result",
        loinc_code="4548-4",
        age_hours=2400.0,
        max_acceptable_age_hours=2160.0,
        clinical_rationale="HbA1c 90-day max",
        guideline_source="ADA 2024",
    )
    assert se.age_hours > se.max_acceptable_age_hours


# ── KnowledgeSearchResult ────────────────────────────────────────────────────

def test_knowledge_search_result():
    from gap_aware.models import KnowledgeSearchResult

    r = KnowledgeSearchResult(
        source="rxnorm",
        finding="No interaction found",
        evidence_level="guideline",
        relevance_score=0.85,
        clinical_applicability="Safe combination",
    )
    assert r.source_url is None
    assert r.relevance_score == 0.85


def test_knowledge_search_result_invalid_evidence_level():
    from gap_aware.models import KnowledgeSearchResult
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        KnowledgeSearchResult(
            source="rxnorm", finding="test",
            evidence_level="triple_blind_magic",
            relevance_score=0.5, clinical_applicability="test",
        )


# ── RequestClarificationOutput ────────────────────────────────────────────────

def test_clarification_output():
    from gap_aware.models import RequestClarificationOutput

    out = RequestClarificationOutput(
        clarification_id="clar_abc123",
        status="pending",
        resolution_action="escalated",
    )
    assert out.response is None
    assert out.respondent is None


# ── GapArtifact ───────────────────────────────────────────────────────────────

def test_gap_artifact_valid():
    from gap_aware.models import GapArtifact

    a = GapArtifact(
        gap_type="stale_data",
        severity="high",
        description="HbA1c > 90 days old",
        impact_statement="Cannot assess glycemic control trajectory",
        confidence_without_resolution=0.45,
        confidence_with_resolution=0.85,
        recommended_action_for_synthesis="include_caveat_in_output",
    )
    assert a.attempted_resolutions == []
    assert a.caveat_text is None


def test_gap_artifact_with_resolutions():
    from gap_aware.models import GapArtifact, AttemptedResolution

    a = GapArtifact(
        gap_type="drug_interaction_unknown",
        severity="critical",
        description="Buspirone + metformin interaction unknown",
        impact_statement="Cannot confirm safety of combination",
        confidence_without_resolution=0.3,
        confidence_with_resolution=0.9,
        recommended_action_for_synthesis="defer_to_provider",
        attempted_resolutions=[
            AttemptedResolution(method="rxnorm_search", result="unresolved"),
            AttemptedResolution(method="openfda_search", result="partially_resolved"),
        ],
    )
    assert len(a.attempted_resolutions) == 2


# ── RegisterGapTriggerOutput ──────────────────────────────────────────────────

def test_trigger_output():
    from gap_aware.models import RegisterGapTriggerOutput

    out = RegisterGapTriggerOutput(
        trigger_id="trig_abc123",
        registered=True,
        expires_at=datetime.now(timezone.utc),
        estimated_resolution_probability=0.75,
    )
    assert out.registered is True


# ── SearchPatientDataExtendedOutput ───────────────────────────────────────────

def test_extended_search_output():
    from gap_aware.models import SearchPatientDataExtendedOutput

    out = SearchPatientDataExtendedOutput(
        found_elements=[], not_found=["HbA1c (LOINC 4548-4)"],
        gap_resolved=False,
    )
    assert len(out.not_found) == 1
    assert not out.gap_resolved
