"""Tests for the LLM Planner module (ingestion/adapters/healthex/planner.py).

Tests cover:
  PL-1: Deterministic planner detects Format B compressed table
  PL-2: Deterministic planner detects Format D FHIR bundle
  PL-3: Deterministic planner detects Format A plain text summary
  PL-4: Deterministic planner handles empty input gracefully
  PL-5: Deterministic planner produces valid plan structure
  PL-6: Row estimation for Format B (counts pipe+@ lines)
  PL-7: Row estimation for JSON dict arrays
  PL-8: Fallback plan on unknown format
"""

import pytest
from ingestion.adapters.healthex.planner import (
    plan_extraction_deterministic,
    _estimate_rows,
    _fallback_plan,
)


# ── Sample payloads ──────────────────────────────────────────────────────────

FORMAT_B_LABS = """#Labs 6m|Total:5
D:1=2025-01-15|2=2025-03-20|3=2025-06-01|
C:1=HbA1c|2=LDL Cholesterol|3=eGFR|4=Creatinine|5=BUN|
S:1=final|
Date|TestName|Value|Unit|ReferenceRange|Status|LOINC|EffectiveDate
@1|@1|7.8|%|4.0-5.6|@1|4548-4|@1
@1|@2|112|mg/dL|<100|@1|2089-1|@1
@2|@3|68|mL/min/1.73m2|>60|@1|33914-3|@2
@2|@4|1.1|mg/dL|0.7-1.3|@1|2160-0|@2
@3|@5|18|mg/dL|7-20|@1|3094-0|@3"""

FORMAT_D_BUNDLE = """{
  "resourceType": "Bundle",
  "type": "searchset",
  "entry": [
    {"resource": {"resourceType": "Condition", "code": {"coding": [{"code": "E11.9", "display": "Type 2 Diabetes"}]}}},
    {"resource": {"resourceType": "Condition", "code": {"coding": [{"code": "I10", "display": "Hypertension"}]}}}
  ]
}"""

FORMAT_A_SUMMARY = """PATIENT: Maria Chen, 54F, MRN 4829341
CONDITIONS(3): Type 2 Diabetes, Hypertension, Hyperlipidemia
MEDICATIONS(3): Metformin 1000mg BID, Lisinopril 10mg QD, Atorvastatin 40mg QD
LABS: HbA1c 7.8%, LDL 112 mg/dL, eGFR 68"""

FORMAT_E_JSON = '{"conditions": [{"name": "Diabetes", "code": "E11.9"}, {"name": "HTN", "code": "I10"}]}'


# ── PL-1: Detect Format B ─────────────────────────────────────────────────────

def test_detect_format_b():
    plan = plan_extraction_deterministic(FORMAT_B_LABS, "labs", "test-patient-id")
    assert plan["detected_format"] == "compressed_table"
    assert plan["extraction_strategy"] == "at_ref_dict_lookup"


# ── PL-2: Detect Format D ─────────────────────────────────────────────────────

def test_detect_format_d():
    plan = plan_extraction_deterministic(FORMAT_D_BUNDLE, "conditions", "test-patient-id")
    assert plan["detected_format"] == "fhir_bundle_json"
    assert plan["extraction_strategy"] == "fhir_bundle_entry_array"


# ── PL-3: Detect Format A ─────────────────────────────────────────────────────

def test_detect_format_a():
    plan = plan_extraction_deterministic(FORMAT_A_SUMMARY, "summary", "test-patient-id")
    assert plan["detected_format"] == "plain_text_summary"
    assert plan["extraction_strategy"] == "plain_text_section_parse"


# ── PL-4: Handle empty input ──────────────────────────────────────────────────

def test_empty_input():
    plan = plan_extraction_deterministic("", "labs", "test-patient-id")
    assert plan["detected_format"] in ("unknown", "plain_text_summary", "compressed_table")
    assert plan["estimated_rows"] >= 0
    assert "insights_summary" in plan


# ── PL-5: Valid plan structure ─────────────────────────────────────────────────

def test_plan_structure():
    plan = plan_extraction_deterministic(FORMAT_B_LABS, "labs", "test-patient-id")
    required_keys = {
        "detected_format", "extraction_strategy", "estimated_rows",
        "column_map", "sample_rows", "insights_summary",
        "planner_confidence", "patient_id", "planner_model",
    }
    assert required_keys.issubset(set(plan.keys()))
    assert plan["patient_id"] == "test-patient-id"
    assert plan["planner_model"] == "deterministic"
    assert 0.0 <= plan["planner_confidence"] <= 1.0


# ── PL-6: Row estimation for Format B ─────────────────────────────────────────

def test_row_estimation_format_b():
    count = _estimate_rows(FORMAT_B_LABS, "compressed_table", "labs")
    assert count == 5  # 5 data rows with @ references


# ── PL-7: Row estimation for JSON dict array ──────────────────────────────────

def test_row_estimation_json_dict():
    count = _estimate_rows(FORMAT_E_JSON, "json_dict_array", "conditions")
    assert count == 2


# ── PL-8: Fallback plan ───────────────────────────────────────────────────────

def test_fallback_plan():
    plan = _fallback_plan("labs", "patient-123", "test error")
    assert plan["detected_format"] == "unknown"
    assert plan["extraction_strategy"] == "llm_fallback"
    assert plan["planner_confidence"] == 0.0
    assert "test error" in plan["insights_summary"]
