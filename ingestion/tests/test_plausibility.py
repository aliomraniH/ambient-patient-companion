"""Tests for clinical plausibility validator (ingestion/validators/plausibility.py).

Tests cover:
  PV-1: Valid values pass without flags
  PV-2: Implausible values flagged (not discarded)
  PV-3: Decimal error scenario (HbA1c 0.74 vs 7.4)
  PV-4: Name-based fallback when no LOINC code
  PV-5: Future dates flagged
  PV-6: Maria Chen scenario
"""

import pytest
from ingestion.validators.plausibility import validate_plausibility


class TestLOINCPlausibility:
    """LOINC-code-keyed plausibility validation."""

    def test_valid_hba1c_passes(self):
        record = {"loinc_code": "4548-4", "value": 7.4, "unit": "%"}
        result = validate_plausibility(record, patient_mrn="4829341")
        assert result["quality_status"] == "passed"
        assert result["quality_flags"] == []

    def test_hba1c_decimal_error_caught(self):
        """HbA1c of 0.74 (should be 7.4%) caught as implausible."""
        record = {"loinc_code": "4548-4", "value": 0.74, "unit": "%"}
        result = validate_plausibility(record, patient_mrn="4829341")
        assert result["quality_status"] == "flagged"
        assert any("4548-4" in str(f) for f in result["quality_flags"])
        assert any("decimal" in f.get("note", "").lower() for f in result["quality_flags"])

    def test_valid_creatinine_passes(self):
        record = {"loinc_code": "2160-0", "value": 1.2, "unit": "mg/dL"}
        result = validate_plausibility(record)
        assert result["quality_status"] == "passed"

    def test_impossible_creatinine_flagged(self):
        """Creatinine of 0.001 is physiologically impossible."""
        record = {"loinc_code": "2160-0", "value": 0.001}
        result = validate_plausibility(record)
        assert result["quality_status"] == "flagged"

    def test_valid_systolic_bp_passes(self):
        record = {"loinc_code": "55284-4", "value": 141}
        result = validate_plausibility(record)
        assert result["quality_status"] == "passed"

    def test_impossible_bp_flagged(self):
        """BP of 500 is physiologically impossible."""
        record = {"loinc_code": "55284-4", "value": 500}
        result = validate_plausibility(record)
        assert result["quality_status"] == "flagged"

    def test_valid_glucose_passes(self):
        record = {"loinc_code": "2345-7", "value": 210}
        result = validate_plausibility(record)
        assert result["quality_status"] == "passed"

    def test_zero_heart_rate_flagged(self):
        """Heart rate of 0 should be flagged."""
        record = {"loinc_code": "8867-4", "value": 0}
        result = validate_plausibility(record)
        assert result["quality_status"] == "flagged"

    def test_valid_egfr_passes(self):
        record = {"loinc_code": "33914-3", "value": 68}
        result = validate_plausibility(record)
        assert result["quality_status"] == "passed"

    def test_valid_potassium_passes(self):
        record = {"loinc_code": "2823-3", "value": 4.2}
        result = validate_plausibility(record)
        assert result["quality_status"] == "passed"

    def test_extreme_potassium_flagged(self):
        record = {"loinc_code": "2823-3", "value": 15.0}
        result = validate_plausibility(record)
        assert result["quality_status"] == "flagged"


class TestNameFallback:
    """Name-based fallback when LOINC code is absent."""

    def test_hba1c_by_name_valid(self):
        record = {"metric_type": "HbA1c", "value": 7.4}
        result = validate_plausibility(record)
        assert result["quality_status"] == "passed"

    def test_hba1c_by_name_implausible(self):
        record = {"metric_type": "HbA1c", "value": 0.74}
        result = validate_plausibility(record)
        assert result["quality_status"] == "flagged"

    def test_glucose_by_name(self):
        record = {"metric_type": "glucose_fasting", "value": 210}
        result = validate_plausibility(record)
        assert result["quality_status"] == "passed"

    def test_creatinine_by_test_name(self):
        record = {"test_name": "Creatinine", "value": 1.2}
        result = validate_plausibility(record)
        assert result["quality_status"] == "passed"


class TestTemporalValidation:
    """Future date detection."""

    def test_future_date_flagged(self):
        record = {
            "loinc_code": "4548-4",
            "value": 7.4,
            "date": "2099-01-01",
        }
        result = validate_plausibility(record)
        assert result["quality_status"] == "flagged"
        assert any(f["type"] == "future_date" for f in result["quality_flags"])

    def test_past_date_passes(self):
        record = {
            "loinc_code": "4548-4",
            "value": 7.4,
            "date": "2025-06-15",
        }
        result = validate_plausibility(record)
        assert result["quality_status"] == "passed"


class TestEdgeCases:
    """Edge cases and special scenarios."""

    def test_no_numeric_value_passes(self):
        """Non-numeric lab results (e.g., 'Positive') should pass."""
        record = {"loinc_code": "4548-4", "value": "Positive"}
        result = validate_plausibility(record)
        # No numeric to validate → passes
        assert result["quality_status"] == "passed"

    def test_none_value_passes(self):
        record = {"loinc_code": "4548-4", "value": None}
        result = validate_plausibility(record)
        assert result["quality_status"] == "passed"

    def test_unknown_loinc_passes(self):
        """LOINC codes not in our table pass without validation."""
        record = {"loinc_code": "99999-9", "value": 42.0}
        result = validate_plausibility(record)
        assert result["quality_status"] == "passed"

    def test_record_always_gets_validated_at(self):
        record = {"value": 7.4}
        result = validate_plausibility(record)
        assert "validated_at" in result

    def test_maria_chen_hba1c_valid(self):
        """Maria Chen's HbA1c 7.4% — a real value that must pass."""
        record = {
            "loinc_code": "4548-4",
            "value": 7.4,
            "unit": "%",
            "metric_type": "HbA1c",
        }
        result = validate_plausibility(record, patient_mrn="4829341")
        assert result["quality_status"] == "passed"
        assert result["quality_flags"] == []

    def test_nested_fhir_value_quantity(self):
        """FHIR-style valueQuantity dict."""
        record = {
            "loinc_code": "4548-4",
            "valueQuantity": {"value": 7.4, "unit": "%"},
        }
        result = validate_plausibility(record)
        assert result["quality_status"] == "passed"
