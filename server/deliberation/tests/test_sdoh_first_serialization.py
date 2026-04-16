"""Tests for SDOH-first serialization in PatientContextPackage.

Research basis: Liu et al. TACL 2024 (attention sink on first tokens);
Sun et al. Health Affairs 2022 (stigmatizing language 2.54× more common
for Black patients in EHR notes). Placing SDOH context ahead of clinical
data in the LLM context string mitigates primacy bias toward lab values.
"""
import json

import pytest

from server.deliberation.schemas import PatientContextPackage


def _make_ctx(**overrides) -> PatientContextPackage:
    """Build a minimally valid PatientContextPackage for serialization tests."""
    defaults = dict(
        patient_id="test-patient",
        patient_name="Test Patient",
        age=42,
        sex="F",
        mrn="T-001",
        primary_provider="Dr. Test",
        practice="Test Clinic",
        active_conditions=[{"code": "E11.9", "display": "Type 2 DM"}],
        current_medications=[{"code": "metformin", "display": "Metformin"}],
        recent_labs=[{"name": "HbA1c", "value": "8.2", "unit": "%"}],
        vital_trends=[{"name": "bp", "readings": []}],
        care_gaps=[{"gap_type": "eye_exam"}],
        sdoh_flags=["food_insecurity", "transportation_barrier"],
        prior_patient_knowledge=[],
        applicable_guidelines=[],
        upcoming_appointments=[],
        days_since_last_encounter=90,
        deliberation_trigger="unit_test",
    )
    defaults.update(overrides)
    return PatientContextPackage(**defaults)


def test_sdoh_appears_before_clinical_state():
    ctx = _make_ctx()
    serialized = ctx.serialize_for_llm()
    parsed = json.loads(serialized)
    keys = list(parsed.keys())

    sdoh_pos = keys.index("sdoh_flags")
    for clinical_key in ("active_conditions", "current_medications",
                         "recent_labs", "vital_trends", "care_gaps"):
        assert keys.index(clinical_key) > sdoh_pos, (
            f"sdoh_flags must precede {clinical_key}; got order: {keys}"
        )


def test_demographics_come_first():
    ctx = _make_ctx()
    parsed = json.loads(ctx.serialize_for_llm())
    assert list(parsed.keys())[0] == "patient_id"


def test_clinical_notes_are_last_section():
    ctx = _make_ctx(clinical_notes=[{"text": "note"}], available_media=[])
    parsed = json.loads(ctx.serialize_for_llm())
    keys = list(parsed.keys())
    # clinical_notes must come after sdoh_flags and after all clinical-state keys
    assert keys.index("clinical_notes") > keys.index("sdoh_flags")
    assert keys.index("clinical_notes") > keys.index("active_conditions")


def test_no_data_loss_vs_model_dump():
    ctx = _make_ctx()
    serialized_keys = set(json.loads(ctx.serialize_for_llm()).keys())
    dump_keys = set(ctx.model_dump().keys())
    assert serialized_keys == dump_keys, (
        f"serialize_for_llm lost keys: {dump_keys - serialized_keys}"
    )


def test_empty_sdoh_flags_still_present():
    ctx = _make_ctx(sdoh_flags=[])
    parsed = json.loads(ctx.serialize_for_llm())
    assert "sdoh_flags" in parsed
    assert parsed["sdoh_flags"] == []


def test_produces_valid_json():
    ctx = _make_ctx()
    # Round-trip: must be parseable
    parsed = json.loads(ctx.serialize_for_llm())
    assert parsed["patient_id"] == "test-patient"
    assert parsed["sdoh_flags"] == ["food_insecurity", "transportation_barrier"]
