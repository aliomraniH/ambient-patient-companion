"""Task 3 — Deliberation fence-stripping: Pydantic model parsing with fenced JSON.

LLMs routinely wrap JSON responses in markdown code fences even when explicitly
told not to.  These tests verify that model_validate_json() succeeds on every
deliberation schema model when the raw LLM output is fenced, and that the parsed
values are identical to those produced from bare JSON.

Covers:
  - CrossCritique   (Phase 2 critic output)
  - RevisedAnalysis (Phase 2 revision output)
  - IndependentAnalysis (Phase 1 analyst output)

Each model is tested against:
  - ```json fences (most common)
  - ``` plain fences
  - No fences (passthrough — must still parse)
  - Leading/trailing whitespace around fences
  - Multi-line JSON with nested objects
"""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from server.deliberation.json_utils import strip_markdown_fences
from server.deliberation.schemas import (
    ClaimWithConfidence,
    CrossCritique,
    CritiqueItem,
    IndependentAnalysis,
    RevisedAnalysis,
)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _parse(model_cls, raw: str):
    """Strip fences then validate — the exact production call path."""
    return model_cls.model_validate_json(strip_markdown_fences(raw))


# ── CrossCritique ─────────────────────────────────────────────────────────────

_CROSS_CRITIQUE_DICT = {
    "critic_model": "claude-sonnet-4-20250514",
    "target_model": "gpt-4o",
    "round_number": 1,
    "critique_items": [
        {
            "target_claim": "HbA1c is within acceptable range",
            "critique_type": "factual_error",
            "critique_text": (
                "HbA1c of 7.2% exceeds the ADA target of <7.0% for most adults "
                "with T2DM. The claim is incorrect."
            ),
            "suggested_revision": "HbA1c is above the 7.0% ADA target — warrants action.",
            "severity": "blocking",
        },
        {
            "target_claim": "Blood pressure is controlled",
            "critique_type": "missed_consideration",
            "critique_text": "BP of 141/86 exceeds the 130/80 target for diabetic patients.",
            "suggested_revision": None,
            "severity": "moderate",
        },
    ],
    "areas_of_agreement": [
        "Both models agree that metformin adherence should be verified.",
        "Lifestyle modification counselling is warranted.",
    ],
    "raw_critique": "Full chain of thought for audit purposes.",
}


class TestCrossCritiqueFenceStripping:
    """CrossCritique parses correctly regardless of fence style."""

    def test_json_fence_parses_correctly(self):
        raw = f"```json\n{json.dumps(_CROSS_CRITIQUE_DICT, indent=2)}\n```"
        obj = _parse(CrossCritique, raw)
        assert obj.critic_model == "claude-sonnet-4-20250514"
        assert obj.target_model == "gpt-4o"
        assert len(obj.critique_items) == 2
        assert obj.critique_items[0].severity == "blocking"
        assert len(obj.areas_of_agreement) == 2

    def test_plain_fence_parses_correctly(self):
        raw = f"```\n{json.dumps(_CROSS_CRITIQUE_DICT)}\n```"
        obj = _parse(CrossCritique, raw)
        assert obj.critic_model == "claude-sonnet-4-20250514"
        assert obj.critique_items[1].critique_type == "missed_consideration"

    def test_no_fences_passthrough(self):
        raw = json.dumps(_CROSS_CRITIQUE_DICT)
        obj = _parse(CrossCritique, raw)
        assert obj.target_model == "gpt-4o"
        assert len(obj.critique_items) == 2

    def test_leading_trailing_whitespace_around_fences(self):
        raw = f"  \n  ```json\n{json.dumps(_CROSS_CRITIQUE_DICT)}\n```  \n  "
        obj = _parse(CrossCritique, raw)
        assert obj.round_number == 1

    def test_null_suggested_revision_accepted(self):
        """CritiqueItem.suggested_revision is Optional[str] — None must parse."""
        raw = json.dumps(_CROSS_CRITIQUE_DICT)
        obj = _parse(CrossCritique, raw)
        assert obj.critique_items[1].suggested_revision is None

    def test_server_defaults_before_assignment(self):
        """critic_model and target_model default to '' so parse succeeds without them."""
        minimal = {
            "critique_items": [],
            "areas_of_agreement": ["Something agreed"],
        }
        obj = CrossCritique.model_validate(minimal)
        assert obj.critic_model == ""
        assert obj.target_model == ""
        assert obj.round_number == 0

    def test_fenced_json_values_identical_to_bare(self):
        """Parsed values from fenced JSON must be identical to parsed bare JSON."""
        bare = CrossCritique.model_validate_json(json.dumps(_CROSS_CRITIQUE_DICT))
        fenced_raw = f"```json\n{json.dumps(_CROSS_CRITIQUE_DICT)}\n```"
        fenced = _parse(CrossCritique, fenced_raw)
        assert bare.model_dump() == fenced.model_dump()


# ── RevisedAnalysis ───────────────────────────────────────────────────────────

_REVISED_ANALYSIS_DICT = {
    "model_id": "claude-sonnet-4-20250514",
    "round_number": 1,
    "revised_findings": [
        {
            "claim": "HbA1c of 7.2% is above the 7.0% ADA target",
            "confidence": 0.95,
            "evidence_refs": ["ADA-9.1a", "USPSTF-DM-2021"],
        },
        {
            "claim": "Blood pressure 141/86 exceeds 130/80 target for T2DM patients",
            "confidence": 0.90,
            "evidence_refs": ["JNC8-2014"],
        },
    ],
    "revisions_made": [
        "Corrected HbA1c assessment: critique correctly identified it is above target.",
        "Added BP finding after critique highlighted omission.",
    ],
    "maintained_positions": [
        "Metformin adherence is the highest priority intervention — maintained despite "
        "critique suggesting dose escalation first.",
    ],
    "raw_revision": (
        "My full chain of thought: the peer critique correctly identified two errors. "
        "I accept the HbA1c critique. For BP I accept the factual correction but maintain "
        "that metformin adherence checking must come before dose escalation."
    ),
}


class TestRevisedAnalysisFenceStripping:
    """RevisedAnalysis parses correctly with all fence styles."""

    def test_json_fence_parses_correctly(self):
        raw = f"```json\n{json.dumps(_REVISED_ANALYSIS_DICT, indent=2)}\n```"
        obj = _parse(RevisedAnalysis, raw)
        assert len(obj.revised_findings) == 2
        assert obj.revised_findings[0].confidence == 0.95
        assert "ADA-9.1a" in obj.revised_findings[0].evidence_refs
        assert len(obj.revisions_made) == 2
        assert len(obj.maintained_positions) == 1

    def test_plain_fence_parses_correctly(self):
        raw = f"```\n{json.dumps(_REVISED_ANALYSIS_DICT)}\n```"
        obj = _parse(RevisedAnalysis, raw)
        assert obj.round_number == 1

    def test_no_fences_passthrough(self):
        raw = json.dumps(_REVISED_ANALYSIS_DICT)
        obj = _parse(RevisedAnalysis, raw)
        assert obj.model_id == "claude-sonnet-4-20250514"

    def test_server_defaults_before_assignment(self):
        """model_id and round_number default so parse succeeds without them."""
        minimal = {
            "revised_findings": [],
            "revisions_made": ["One revision"],
            "maintained_positions": [],
        }
        obj = RevisedAnalysis.model_validate(minimal)
        assert obj.model_id == ""
        assert obj.round_number == 0

    def test_confidence_bounds_validated(self):
        """ClaimWithConfidence.confidence must be in [0.0, 1.0]."""
        invalid = {**_REVISED_ANALYSIS_DICT,
                   "revised_findings": [{"claim": "x", "confidence": 1.5, "evidence_refs": []}]}
        with pytest.raises(ValidationError):
            RevisedAnalysis.model_validate_json(json.dumps(invalid))

    def test_fenced_values_identical_to_bare(self):
        bare = RevisedAnalysis.model_validate_json(json.dumps(_REVISED_ANALYSIS_DICT))
        fenced_raw = f"```json\n{json.dumps(_REVISED_ANALYSIS_DICT)}\n```"
        fenced = _parse(RevisedAnalysis, fenced_raw)
        assert bare.model_dump() == fenced.model_dump()

    def test_multiline_raw_revision_preserved(self):
        """raw_revision is a free-form string — must survive round-trip."""
        raw = f"```json\n{json.dumps(_REVISED_ANALYSIS_DICT)}\n```"
        obj = _parse(RevisedAnalysis, raw)
        assert "chain of thought" in obj.raw_revision
        assert "metformin" in obj.raw_revision.lower()


# ── IndependentAnalysis ───────────────────────────────────────────────────────

_INDEPENDENT_ANALYSIS_DICT = {
    "model_id": "gpt-4o",
    "role_emphasis": "treatment_optimization",
    "key_findings": [
        {
            "claim": "HbA1c of 7.2% is 0.2 points above the ADA <7.0% target",
            "confidence": 0.95,
            "evidence_refs": ["ADA Standards 2024, Section 9"],
        },
        {
            "claim": "LDL of 104 mg/dL exceeds the <100 mg/dL target for T2DM",
            "confidence": 0.88,
            "evidence_refs": ["ACC/AHA 2019 Lipid Guidelines"],
        },
    ],
    "risk_flags": [
        {
            "claim": "Elevated cardiovascular risk due to combined HbA1c + LDL elevation",
            "confidence": 0.80,
            "evidence_refs": [],
        }
    ],
    "recommended_actions": [
        {
            "claim": "Consider intensifying metformin or adding GLP-1 RA",
            "confidence": 0.85,
            "evidence_refs": ["ADA-9.5"],
        }
    ],
    "anticipated_trajectory": (
        "Without intervention, HbA1c will likely continue to rise over the next "
        "3-6 months. Combined cardiovascular risk from uncontrolled HbA1c and LDL "
        "will increase. Lifestyle changes alone are unlikely to achieve target HbA1c."
    ),
    "missing_data_identified": [
        "UACR (urine albumin-to-creatinine ratio) — needed for CKD staging",
        "Recent blood pressure readings — last 3 months",
        "Medication adherence self-report",
    ],
    "raw_reasoning": (
        "Step 1: Review labs. HbA1c 7.2% — above target. LDL 104 — above target. "
        "Step 2: Consider medication optimisation. Metformin is first-line but may "
        "need dose increase or addition of GLP-1 RA given cardiovascular benefit."
    ),
}


class TestIndependentAnalysisFenceStripping:
    """IndependentAnalysis parses correctly with all fence styles."""

    def test_json_fence_parses_correctly(self):
        raw = f"```json\n{json.dumps(_INDEPENDENT_ANALYSIS_DICT, indent=2)}\n```"
        obj = _parse(IndependentAnalysis, raw)
        assert len(obj.key_findings) == 2
        assert len(obj.risk_flags) == 1
        assert len(obj.recommended_actions) == 1
        assert len(obj.missing_data_identified) == 3
        assert obj.anticipated_trajectory.startswith("Without intervention")

    def test_plain_fence_parses_correctly(self):
        raw = f"```\n{json.dumps(_INDEPENDENT_ANALYSIS_DICT)}\n```"
        obj = _parse(IndependentAnalysis, raw)
        assert obj.role_emphasis == "treatment_optimization"

    def test_no_fences_passthrough(self):
        raw = json.dumps(_INDEPENDENT_ANALYSIS_DICT)
        obj = _parse(IndependentAnalysis, raw)
        assert obj.model_id == "gpt-4o"
        assert len(obj.key_findings) == 2

    def test_server_assigns_model_id_and_role_after_parse(self):
        """model_id and role_emphasis default to '' — server sets them post-parse."""
        minimal = {
            "key_findings": [],
            "risk_flags": [],
            "recommended_actions": [],
            "anticipated_trajectory": "Stable",
            "missing_data_identified": [],
        }
        obj = IndependentAnalysis.model_validate(minimal)
        assert obj.model_id == ""
        assert obj.role_emphasis == ""
        # Simulate server-side assignment
        obj.model_id = "claude-sonnet-4-20250514"
        obj.role_emphasis = "diagnostic_reasoning"
        assert obj.model_id == "claude-sonnet-4-20250514"

    def test_missing_data_identified_is_plain_string_list(self):
        """missing_data_identified must be list[str], not list[dict]."""
        raw = f"```json\n{json.dumps(_INDEPENDENT_ANALYSIS_DICT)}\n```"
        obj = _parse(IndependentAnalysis, raw)
        assert all(isinstance(s, str) for s in obj.missing_data_identified)

    def test_anticipated_trajectory_is_plain_string(self):
        """anticipated_trajectory must be a plain string, not a dict."""
        raw = f"```json\n{json.dumps(_INDEPENDENT_ANALYSIS_DICT)}\n```"
        obj = _parse(IndependentAnalysis, raw)
        assert isinstance(obj.anticipated_trajectory, str)
        assert len(obj.anticipated_trajectory) > 10

    def test_fenced_values_identical_to_bare(self):
        bare = IndependentAnalysis.model_validate_json(
            json.dumps(_INDEPENDENT_ANALYSIS_DICT)
        )
        fenced_raw = f"```json\n{json.dumps(_INDEPENDENT_ANALYSIS_DICT)}\n```"
        fenced = _parse(IndependentAnalysis, fenced_raw)
        assert bare.model_dump() == fenced.model_dump()

    def test_evidence_refs_is_list_of_strings(self):
        raw = f"```json\n{json.dumps(_INDEPENDENT_ANALYSIS_DICT)}\n```"
        obj = _parse(IndependentAnalysis, raw)
        for finding in obj.key_findings:
            assert isinstance(finding.evidence_refs, list)
            assert all(isinstance(r, str) for r in finding.evidence_refs)
