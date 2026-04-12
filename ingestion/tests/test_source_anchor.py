"""Tests for source anchor validator (ingestion/validators/source_anchor.py).

Tests cover:
  SA-1: Anchored values pass through unchanged
  SA-2: Hallucinated values are nulled with proper flags
  SA-3: Numeric equivalence matching (1.50 == 1.5)
  SA-4: Exempt fields bypass anchoring
  SA-5: Anchor rate computation and warning
  SA-6: Maria Chen scenario — fabricated creatinine when source says unavailable
"""

import logging
import pytest
from ingestion.validators.source_anchor import (
    verify_extracted_numerics,
    assert_anchor_rate,
)


class TestSourceAnchoring:
    """Verify source anchoring catches hallucinated values."""

    def test_anchored_value_passes(self):
        """Real value in source blob passes through unchanged."""
        source = "Patient creatinine 1.2 mg/dL measured 2026-03-15"
        extracted = {"value_quantity": 1.2, "unit": "mg/dL"}
        result = verify_extracted_numerics(source, extracted)
        assert result["verified"]["value_quantity"] == 1.2
        assert result["anchor_rate"] == 1.0
        assert len(result["nulled"]) == 0

    def test_hallucinated_value_nulled(self):
        """Fabricated value not in source must be nulled."""
        source = "Renal function not available at this visit"
        extracted = {"value_quantity": 1.5}
        result = verify_extracted_numerics(source, extracted)
        assert result["verified"]["value_quantity"] is None
        assert len(result["flags"]) == 1
        assert result["flags"][0]["status"] == "unanchored_hallucination_risk"
        assert result["anchor_rate"] == 0.0

    def test_numeric_equivalence_matching(self):
        """1.50 in source matches extracted 1.5."""
        source = "HbA1c level is 7.40 percent"
        extracted = {"value_quantity": 7.4}
        result = verify_extracted_numerics(source, extracted)
        assert result["verified"]["value_quantity"] == 7.4
        assert result["anchor_rate"] == 1.0

    def test_integer_in_source_matches_float(self):
        """Integer 68 in source matches extracted 68.0."""
        source = "eGFR 68 mL/min calculated"
        extracted = {"value_quantity": 68.0}
        result = verify_extracted_numerics(source, extracted)
        assert result["verified"]["value_quantity"] == 68.0
        assert result["anchor_rate"] == 1.0

    def test_exempt_fields_bypass_anchoring(self):
        """Patient MRN and LOINC codes are never nulled."""
        source = "No numbers here"
        extracted = {"patient_mrn": "4829341", "loinc_code": "4548-4", "value_quantity": 7.4}
        result = verify_extracted_numerics(source, extracted)
        assert result["verified"]["patient_mrn"] == "4829341"
        assert result["verified"]["loinc_code"] == "4548-4"
        # value_quantity should fail (7.4 not in source)
        assert result["verified"]["value_quantity"] is None

    def test_none_values_pass_through(self):
        """None values in extracted dict pass through without anchoring."""
        source = "Some text"
        extracted = {"value_quantity": None, "unit": "mg/dL"}
        result = verify_extracted_numerics(source, extracted)
        assert result["verified"]["value_quantity"] is None
        assert result["anchor_rate"] == 1.0  # No numeric fields to check

    def test_empty_source_blob(self):
        """Empty source blob returns extracted as-is."""
        result = verify_extracted_numerics("", {"value_quantity": 1.2})
        assert result["verified"]["value_quantity"] == 1.2
        assert result["anchor_rate"] == 1.0

    def test_multiple_values_mixed_anchoring(self):
        """Mix of real and fabricated values."""
        source = "HbA1c 7.4%, BP 141/86 mmHg, metformin 1000mg"
        extracted = {
            "value_quantity": 7.4,     # Real
            "systolic": 141,           # Real
            "diastolic": 86,           # Real
            "heart_rate": 78,          # Fabricated
        }
        result = verify_extracted_numerics(source, extracted)
        assert result["verified"]["value_quantity"] == 7.4
        assert result["verified"]["systolic"] == 141
        assert result["verified"]["diastolic"] == 86
        assert result["verified"]["heart_rate"] is None  # Not in source
        assert result["anchor_rate"] == 0.75  # 3 of 4

    def test_maria_chen_creatinine_scenario(self):
        """Exact Maria Chen test: fabricated creatinine when source says unavailable."""
        source = (
            "PATIENT: Maria Chen MRN 4829341\n"
            "LABS: HbA1c 7.4% (2026-01-15)\n"
            "NOTE: Renal function panel not collected at this visit.\n"
            "MEDICATIONS: Metformin 1000mg BID, Lisinopril 10mg daily"
        )
        extracted = {
            "value_quantity": 7.4,     # Real — in source
            "result_numeric": 1.5,     # Hallucinated creatinine
            "patient_mrn": "4829341",  # Exempt
        }
        result = verify_extracted_numerics(source, extracted)
        assert result["verified"]["value_quantity"] == 7.4
        assert result["verified"]["result_numeric"] is None  # Hallucination nulled
        assert result["verified"]["patient_mrn"] == "4829341"
        assert result["anchor_rate"] == 0.5  # 1 of 2 numeric fields anchored


class TestAnchorRateWarning:
    """Verify low anchor rate triggers warning."""

    def test_low_anchor_rate_logs_warning(self, caplog):
        result = {"anchor_rate": 0.60, "nulled": {"creatinine": None}, "numeric_fields_checked": 2}
        with caplog.at_level(logging.WARNING):
            assert_anchor_rate(result, patient_mrn="4829341")
        assert "Low anchor rate" in caplog.text
        assert "4829341" in caplog.text

    def test_high_anchor_rate_no_warning(self, caplog):
        result = {"anchor_rate": 0.98, "nulled": {}, "numeric_fields_checked": 5}
        with caplog.at_level(logging.WARNING):
            assert_anchor_rate(result, patient_mrn="4829341")
        assert "Low anchor rate" not in caplog.text

    def test_no_numeric_fields_no_warning(self, caplog):
        result = {"anchor_rate": 1.0, "nulled": {}, "numeric_fields_checked": 0}
        with caplog.at_level(logging.WARNING):
            assert_anchor_rate(result, patient_mrn="4829341")
        assert "Low anchor rate" not in caplog.text
