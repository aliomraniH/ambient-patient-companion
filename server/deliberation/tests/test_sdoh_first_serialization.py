"""Phase 1 — SDOH-first serialization tests."""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent))

from server.deliberation.schemas import PatientContextPackage

_SAMPLE = dict(
    patient_id="p1",
    patient_name="Test Patient",
    age=45,
    sex="F",
    mrn="MRN-001",
    primary_provider="Dr. A",
    practice="Test Clinic",
    active_conditions=[{"code": "E11", "display": "Type 2 diabetes", "onset_date": "2020-01-01"}],
    current_medications=[{"name": "Metformin", "dose": "500mg", "frequency": "BID", "start_date": "2020-01-15"}],
    recent_labs=[{"name": "HbA1c", "value": 8.2, "unit": "%", "date": "2025-01-01", "in_range": False}],
    vital_trends=[{"name": "BP", "readings": [{"value": 135, "date": "2025-01-01"}]}],
    care_gaps=[{"gap_type": "eye_exam", "last_done": None, "due_date": "2025-03-01"}],
    sdoh_flags=["food_insecurity", "transportation_barrier"],
    prior_patient_knowledge=[],
    applicable_guidelines=[],
    upcoming_appointments=[],
    days_since_last_encounter=30,
    deliberation_trigger="scheduled_review",
)


def _make_ctx(**overrides) -> PatientContextPackage:
    data = {**_SAMPLE, **overrides}
    return PatientContextPackage(**data)


def test_sdoh_before_conditions():
    """sdoh_flags must appear before active_conditions in serialized output."""
    ctx = _make_ctx()
    serialized = ctx.serialize_for_llm()
    parsed = json.loads(serialized)
    keys = list(parsed.keys())
    assert "sdoh_flags" in keys, "sdoh_flags missing from serialized output"
    assert "active_conditions" in keys, "active_conditions missing"
    assert keys.index("sdoh_flags") < keys.index("active_conditions"), (
        f"sdoh_flags (pos {keys.index('sdoh_flags')}) must come before "
        f"active_conditions (pos {keys.index('active_conditions')})"
    )


def test_sdoh_before_recent_labs():
    """sdoh_flags must appear before recent_labs."""
    ctx = _make_ctx()
    parsed = json.loads(ctx.serialize_for_llm())
    keys = list(parsed.keys())
    assert keys.index("sdoh_flags") < keys.index("recent_labs")


def test_sdoh_before_vital_trends():
    """sdoh_flags must appear before vital_trends."""
    ctx = _make_ctx()
    parsed = json.loads(ctx.serialize_for_llm())
    keys = list(parsed.keys())
    assert keys.index("sdoh_flags") < keys.index("vital_trends")


def test_no_data_loss():
    """All fields from model_dump() must be present in serialized output."""
    ctx = _make_ctx()
    raw_keys = set(ctx.model_dump().keys())
    serialized_keys = set(json.loads(ctx.serialize_for_llm()).keys())
    missing = raw_keys - serialized_keys
    assert not missing, f"Fields missing from serialize_for_llm(): {missing}"


def test_empty_sdoh():
    """Works correctly when sdoh_flags is an empty list."""
    ctx = _make_ctx(sdoh_flags=[])
    parsed = json.loads(ctx.serialize_for_llm())
    assert "sdoh_flags" in parsed
    assert parsed["sdoh_flags"] == []
    assert "active_conditions" in parsed
