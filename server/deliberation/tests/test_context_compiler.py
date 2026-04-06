"""Tests for the context compiler module."""
import json
from pathlib import Path
from server.deliberation.schemas import PatientContextPackage

FIXTURES = Path(__file__).parent / "fixtures"


def test_maria_chen_fixture_loads():
    """The canonical Maria Chen fixture validates as a PatientContextPackage."""
    data = json.loads((FIXTURES / "maria_chen_context.json").read_text())
    ctx = PatientContextPackage(**data)
    assert ctx.patient_id == "4829341"
    assert ctx.patient_name == "Maria Chen"
    assert ctx.sex == "F"


def test_fixture_conditions_are_coded():
    data = json.loads((FIXTURES / "maria_chen_context.json").read_text())
    ctx = PatientContextPackage(**data)
    codes = [c["code"] for c in ctx.active_conditions]
    assert "E11.9" in codes  # T2DM
    assert "I10" in codes    # HTN


def test_fixture_has_vital_trends():
    data = json.loads((FIXTURES / "maria_chen_context.json").read_text())
    ctx = PatientContextPackage(**data)
    bp_trend = [t for t in ctx.vital_trends if t["name"] == "systolic_bp"]
    assert len(bp_trend) == 1
    assert len(bp_trend[0]["readings"]) == 3


def test_fixture_has_applicable_guidelines():
    data = json.loads((FIXTURES / "maria_chen_context.json").read_text())
    ctx = PatientContextPackage(**data)
    assert len(ctx.applicable_guidelines) >= 2
    sources = [g["source"] for g in ctx.applicable_guidelines]
    assert "ADA" in sources
