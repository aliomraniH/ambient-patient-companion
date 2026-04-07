"""Tests for HealthEx format detection."""
import json
import pytest
from ingestion.adapters.healthex.format_detector import detect_format, HealthExFormat


class TestFormatDetection:
    """Verify all 6 format detection cases."""

    def test_format_a_plain_text_summary(self):
        raw = "PATIENT: Ali Omrani, DOB 1987-03-25\nCONDITIONS(4/10): Active: BMI@Stanford 2019-01-11"
        fmt, payload = detect_format(raw)
        assert fmt == HealthExFormat.PLAIN_TEXT_SUMMARY
        assert "PATIENT:" in payload

    def test_format_a_lowercase_patient(self):
        raw = "Patient: Jane Doe, DOB 1990-01-01"
        fmt, _ = detect_format(raw)
        assert fmt == HealthExFormat.PLAIN_TEXT_SUMMARY

    def test_format_b_compressed_table(self):
        raw = "#Conditions 5y|Total:39\nD:1=2019-01-11|2=2017-04-25|\nC:1=BMI|2=Prediabetes|"
        fmt, payload = detect_format(raw)
        assert fmt == HealthExFormat.COMPRESSED_TABLE
        assert "#Conditions" in payload

    def test_format_c_flat_fhir_text(self):
        raw = "resourceType is Observation. id is fC2IoULh. status is final. code.coding[0].system is http://loinc.org"
        fmt, payload = detect_format(raw)
        assert fmt == HealthExFormat.FLAT_FHIR_TEXT
        assert "resourceType is " in payload

    def test_format_d_fhir_bundle(self):
        bundle = json.dumps({
            "resourceType": "Bundle",
            "type": "searchset",
            "entry": [{"resource": {"resourceType": "Observation"}}]
        })
        fmt, payload = detect_format(bundle)
        assert fmt == HealthExFormat.FHIR_BUNDLE_JSON
        assert payload["resourceType"] == "Bundle"

    def test_format_d_single_fhir_resource(self):
        """A single FHIR resource should be wrapped in a Bundle."""
        resource = json.dumps({
            "resourceType": "Observation",
            "code": {"text": "HbA1c"},
            "valueQuantity": {"value": 4.8, "unit": "%"},
        })
        fmt, payload = detect_format(resource)
        assert fmt == HealthExFormat.FHIR_BUNDLE_JSON
        assert payload["resourceType"] == "Bundle"
        assert len(payload["entry"]) == 1

    def test_json_dict_array(self):
        raw = json.dumps({"conditions": [{"name": "Prediabetes"}]})
        fmt, payload = detect_format(raw)
        assert fmt == HealthExFormat.JSON_DICT_ARRAY
        assert "conditions" in payload

    def test_json_dict_array_labs_key(self):
        raw = json.dumps({"labs": [{"test_name": "HbA1c", "value": "4.8"}]})
        fmt, payload = detect_format(raw)
        assert fmt == HealthExFormat.JSON_DICT_ARRAY

    def test_double_encoded_json_string(self):
        """Double-encoded JSON should unwrap and detect the inner format."""
        inner = json.dumps({"conditions": [{"name": "Diabetes"}]})
        double = json.dumps(inner)  # '"{\\"conditions\\":...}"'
        fmt, payload = detect_format(double)
        assert fmt == HealthExFormat.JSON_DICT_ARRAY

    def test_empty_string(self):
        fmt, payload = detect_format("")
        assert fmt == HealthExFormat.UNKNOWN
        assert payload is None

    def test_none_input(self):
        fmt, payload = detect_format(None)
        assert fmt == HealthExFormat.UNKNOWN

    def test_plain_string_unknown(self):
        fmt, _ = detect_format("just some random text without any markers")
        assert fmt == HealthExFormat.UNKNOWN

    def test_json_list_of_dicts(self):
        raw = json.dumps([{"name": "Prediabetes", "status": "active"}])
        fmt, payload = detect_format(raw)
        assert fmt == HealthExFormat.JSON_DICT_ARRAY
        assert "_items" in payload

    def test_whitespace_handling(self):
        raw = "  \n  PATIENT: Ali Omrani, DOB 1987-03-25  \n  "
        fmt, _ = detect_format(raw)
        assert fmt == HealthExFormat.PLAIN_TEXT_SUMMARY
