"""Tests for FHIR validator (ingestion/validators/fhir_validator.py).

Tests cover:
  FV-1: Valid resources pass
  FV-2: Missing required fields flagged
  FV-3: Invalid date formats flagged
  FV-4: Unknown resource types pass without validation
  FV-5: Native format types validated (labs, conditions, medications)
"""

import pytest
from ingestion.validators.fhir_validator import validate_fhir_resource


class TestFHIRValidation:
    """Test FHIR R4 structural validation."""

    def test_valid_observation_passes(self):
        resource = {
            "resourceType": "Observation",
            "code": {"coding": [{"code": "4548-4", "system": "LOINC"}]},
            "effectiveDateTime": "2026-01-15",
            "valueQuantity": {"value": 7.4, "unit": "%"},
        }
        is_valid, issues = validate_fhir_resource(resource, "Observation")
        assert is_valid is True
        assert len(issues) == 0

    def test_observation_missing_code_flagged(self):
        resource = {
            "resourceType": "Observation",
            "effectiveDateTime": "2026-01-15",
            "valueQuantity": {"value": 7.4},
        }
        is_valid, issues = validate_fhir_resource(resource, "Observation")
        assert is_valid is False
        assert any("code" in i for i in issues)

    def test_valid_native_lab(self):
        resource = {
            "test_name": "HbA1c",
            "value": "7.4",
            "unit": "%",
            "date": "2026-01-15",
            "code": "4548-4",
        }
        is_valid, issues = validate_fhir_resource(resource, "labs")
        assert is_valid is True

    def test_native_lab_missing_name_flagged(self):
        resource = {
            "value": "7.4",
            "unit": "%",
            "date": "2026-01-15",
        }
        is_valid, issues = validate_fhir_resource(resource, "labs")
        assert is_valid is False
        assert any("test_name" in i for i in issues)

    def test_valid_native_condition(self):
        resource = {
            "name": "Type 2 Diabetes",
            "code": "E11.9",
            "status": "active",
            "onset_date": "2019-01-11",
        }
        is_valid, issues = validate_fhir_resource(resource, "conditions")
        assert is_valid is True

    def test_condition_missing_name_flagged(self):
        resource = {
            "code": "E11.9",
            "status": "active",
        }
        is_valid, issues = validate_fhir_resource(resource, "conditions")
        assert is_valid is False
        assert any("name" in i for i in issues)

    def test_valid_native_medication(self):
        resource = {
            "name": "Metformin",
            "display": "Metformin 1000mg",
            "status": "active",
            "start_date": "2020-03-15",
        }
        is_valid, issues = validate_fhir_resource(resource, "medications")
        assert is_valid is True

    def test_invalid_date_format_flagged(self):
        resource = {
            "test_name": "HbA1c",
            "value": "7.4",
            "date": "not-a-date",
        }
        is_valid, issues = validate_fhir_resource(resource, "labs")
        assert is_valid is False
        assert any("date" in i.lower() for i in issues)

    def test_valid_iso_datetime(self):
        resource = {
            "test_name": "HbA1c",
            "value": "7.4",
            "date": "2026-01-15T10:30:00Z",
        }
        is_valid, issues = validate_fhir_resource(resource, "labs")
        assert is_valid is True

    def test_unknown_resource_type_passes(self):
        resource = {"foo": "bar"}
        is_valid, issues = validate_fhir_resource(resource, "unknown_type")
        assert is_valid is True

    def test_empty_resource_fails(self):
        is_valid, issues = validate_fhir_resource({}, "labs")
        assert is_valid is False

    def test_none_resource_fails(self):
        is_valid, issues = validate_fhir_resource(None, "labs")
        assert is_valid is False
