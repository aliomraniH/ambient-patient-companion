"""Tests for the Phase 2 executor (ingestion/adapters/healthex/executor.py).

Tests cover:
  EX-1: _normalize_to_fhir converts native conditions to FHIR
  EX-2: _normalize_to_fhir converts native labs to FHIR (including non-numeric)
  EX-3: _normalize_to_fhir passes through already-FHIR resources
  EX-4: _native_to_fhir_observations preserves non-numeric lab values
  EX-5: _native_to_fhir_observations handles empty/None values
  EX-6: _native_to_fhir_observations handles standard numeric values
"""

import pytest
from ingestion.adapters.healthex.executor import (
    _normalize_to_fhir,
    _native_to_fhir_observations,
    _native_to_fhir_conditions,
    _native_to_fhir_medications,
    _native_to_fhir_encounters,
)


# ── EX-1: Native conditions → FHIR ──────────────────────────────────────────

def test_native_conditions_to_fhir():
    native = [
        {"name": "Type 2 Diabetes", "icd10": "E11.9", "status": "active", "onset_date": "2019-01-11"},
        {"name": "Hypertension", "code": "I10", "status": "active", "onset_date": "2017-04-25"},
    ]
    fhir = _native_to_fhir_conditions(native)
    assert len(fhir) == 2
    assert fhir[0]["resourceType"] == "Condition"
    assert fhir[0]["code"]["coding"][0]["display"] == "Type 2 Diabetes"
    assert fhir[0]["onsetDateTime"] == "2019-01-11"
    assert fhir[1]["code"]["coding"][0]["code"] == "I10"


# ── EX-2: Native labs → FHIR (mixed numeric and non-numeric) ─────────────────

def test_native_labs_to_fhir_mixed():
    native = [
        {"test_name": "HbA1c", "value": "7.8", "unit": "%", "date": "2025-01-15"},
        {"test_name": "HIV Screen", "value": "Negative", "unit": "", "date": "2025-01-15"},
        {"test_name": "eGFR", "value": "68", "unit": "mL/min", "date": "2025-03-20"},
    ]
    fhir = _native_to_fhir_observations(native)
    # All 3 should be present — non-numeric NOT dropped
    assert len(fhir) == 3
    # HbA1c — numeric
    assert fhir[0]["valueQuantity"]["value"] == 7.8
    assert fhir[0]["valueQuantity"]["unit"] == "%"
    # HIV Screen — non-numeric, stored as 0.0 in value, text in _result_text
    assert fhir[1]["valueQuantity"]["value"] == 0.0
    assert fhir[1].get("_result_text") == "Negative"
    assert fhir[1]["valueQuantity"]["unit"] == ""
    # eGFR — numeric
    assert fhir[2]["valueQuantity"]["value"] == 68.0


# ── EX-3: Already-FHIR resources pass through ─────────────────────────────────

def test_passthrough_fhir_resources():
    fhir_resources = [
        {"resourceType": "Condition", "code": {"coding": [{"code": "E11.9"}]}},
        {"resourceType": "Condition", "code": {"coding": [{"code": "I10"}]}},
    ]
    result = _normalize_to_fhir("conditions", fhir_resources)
    assert result is fhir_resources  # exact same list, not a copy


# ── EX-4: Non-numeric lab values preserved ─────────────────────────────────────

def test_non_numeric_lab_preserved():
    native = [
        {"test_name": "Urinalysis", "value": "Positive", "unit": "qual", "date": "2025-01-15"},
        {"test_name": "Culture", "value": "No growth", "unit": "", "date": "2025-01-15"},
        {"test_name": "BP Range", "value": "120-130", "unit": "mmHg", "date": "2025-01-15"},
    ]
    fhir = _native_to_fhir_observations(native)
    assert len(fhir) == 3

    # "Positive" can't be float → stored as 0.0 in value, text in _result_text
    assert fhir[0]["valueQuantity"]["value"] == 0.0
    assert fhir[0].get("_result_text") == "Positive"
    assert fhir[0]["valueQuantity"]["unit"] == "qual"

    # "No growth" → same
    assert fhir[1]["valueQuantity"]["value"] == 0.0
    assert fhir[1].get("_result_text") == "No growth"
    assert fhir[1]["valueQuantity"]["unit"] == ""

    # "120-130" → float("120-130") fails (no whitespace), stored in _result_text
    assert fhir[2]["valueQuantity"]["value"] == 0.0
    assert fhir[2].get("_result_text") == "120-130"


# ── EX-5: Empty/None values handled ───────────────────────────────────────────

def test_empty_and_none_values():
    native = [
        {"test_name": "Test1", "value": None, "unit": "mg/dL", "date": "2025-01-15"},
        {"test_name": "Test2", "value": "", "unit": "mg/dL", "date": "2025-01-15"},
    ]
    fhir = _native_to_fhir_observations(native)
    # Both should still produce FHIR resources (not be dropped)
    assert len(fhir) == 2
    assert fhir[0]["valueQuantity"]["value"] == 0.0
    assert fhir[1]["valueQuantity"]["value"] == 0.0


# ── EX-6: Standard numeric values ─────────────────────────────────────────────

def test_standard_numeric_values():
    native = [
        {"test_name": "HbA1c", "value": "7.8", "unit": "%", "date": "2025-01-15"},
        {"test_name": "LDL", "value": 112, "unit": "mg/dL", "date": "2025-01-15"},
        {"test_name": "eGFR", "value": "68 mL/min", "unit": "mL/min/1.73m2", "date": "2025-03-20"},
    ]
    fhir = _native_to_fhir_observations(native)
    assert len(fhir) == 3
    assert fhir[0]["valueQuantity"]["value"] == 7.8
    assert fhir[1]["valueQuantity"]["value"] == 112.0
    # "68 mL/min".split()[0] = "68" → float(68) = 68.0
    assert fhir[2]["valueQuantity"]["value"] == 68.0


# ── EX-7: _normalize_to_fhir routes correctly ─────────────────────────────────

def test_normalize_routes_correctly():
    native_meds = [
        {"name": "Metformin", "status": "active", "start_date": "2020-01-01"},
    ]
    fhir = _normalize_to_fhir("medications", native_meds)
    assert len(fhir) == 1
    assert fhir[0]["resourceType"] == "MedicationRequest"
    assert fhir[0]["medicationCodeableConcept"]["coding"][0]["display"] == "Metformin"


# ── EX-8: Empty input returns empty ───────────────────────────────────────────

def test_normalize_empty():
    assert _normalize_to_fhir("conditions", []) == []
    assert _normalize_to_fhir("labs", []) == []
