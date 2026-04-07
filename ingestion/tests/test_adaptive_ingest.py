"""Tests for the adaptive ingest entry point."""
import json
import pytest
from unittest.mock import patch, MagicMock

from ingestion.adapters.healthex.ingest import adaptive_parse


class TestAdaptiveParse:
    """End-to-end tests: raw input → adaptive_parse → native dicts."""

    def test_plain_text_conditions(self):
        raw = (
            "PATIENT: Test User\n"
            "CONDITIONS(2/2): Active: Prediabetes@Stanford 2017-04-25 | "
            "Active: Fatty liver@Stanford 2017-01-01\n"
            "LABS(0):"
        )
        rows, fmt, parser = adaptive_parse(raw, "conditions")
        assert fmt == "plain_text_summary"
        assert "format_a" in parser
        assert len(rows) == 2

    def test_compressed_table_conditions(self):
        raw = (
            "#Conditions 5y|Total:2\n"
            "C:1=Prediabetes|2=Fatty liver|\n"
            "S:1=active|\n"
            "Date|Condition|ClinicalStatus|OnsetDate|AbatementDate|SNOMED|ICD10\n"
            "|@1|@1|2017-04-25||714628002|R73.03\n"
            "|@2|@1|2017-01-01||197321007|K76.0"
        )
        rows, fmt, parser = adaptive_parse(raw, "conditions")
        assert fmt == "compressed_table"
        assert "format_b" in parser
        assert len(rows) >= 1

    def test_flat_fhir_text_labs(self):
        raw = (
            "resourceType is Observation. code.text is Hemoglobin A1c. "
            "valueQuantity.value is 4.8. valueQuantity.unit is %. "
            "effectiveDateTime is 2025-07-11"
        )
        rows, fmt, parser = adaptive_parse(raw, "labs")
        assert fmt == "flat_fhir_text"
        assert "format_c" in parser
        assert len(rows) == 1
        assert rows[0]["test_name"] == "Hemoglobin A1c"

    def test_fhir_bundle_json(self):
        bundle = json.dumps({
            "resourceType": "Bundle",
            "entry": [
                {
                    "resource": {
                        "resourceType": "Observation",
                        "code": {"text": "HbA1c"},
                        "valueQuantity": {"value": 4.8, "unit": "%"},
                        "effectiveDateTime": "2025-07-11",
                    }
                },
                {
                    "resource": {
                        "resourceType": "Observation",
                        "code": {"text": "LDL"},
                        "valueQuantity": {"value": 104, "unit": "mg/dL"},
                        "effectiveDateTime": "2025-07-11",
                    }
                },
            ],
        })
        rows, fmt, parser = adaptive_parse(bundle, "labs")
        assert fmt == "fhir_bundle_json"
        assert "format_d" in parser
        assert len(rows) == 2

    def test_json_dict_array(self):
        raw = json.dumps({
            "conditions": [
                {"name": "Prediabetes", "onset": "2017-04-25", "status": "active"},
                {"name": "Fatty liver", "onset": "2017-01-01"},
            ]
        })
        rows, fmt, parser = adaptive_parse(raw, "conditions")
        assert fmt == "json_dict_array"
        assert "json_dict" in parser
        assert len(rows) == 2

    def test_unknown_format_short_input_no_llm(self):
        """Short unknown input should not trigger LLM fallback."""
        rows, fmt, parser = adaptive_parse("hello", "conditions")
        assert fmt == "unknown"
        assert len(rows) == 0
        assert "llm_fallback" not in parser

    def test_llm_fallback_triggered(self):
        """LLM fallback should be triggered when deterministic parsers fail
        on non-trivial input."""
        long_unknown = "x" * 200  # > 100 chars
        mock_rows = [{"name": "Prediabetes", "onset_date": "2017-04-25"}]

        with patch(
            "ingestion.adapters.healthex.llm_fallback.llm_normalise",
            return_value=mock_rows,
        ):
            rows, fmt, parser = adaptive_parse(long_unknown, "conditions")
            assert "llm_fallback" in parser
            assert len(rows) == 1

    def test_component_observation_flattening(self):
        """BP readings with components should produce multiple rows."""
        bundle = json.dumps({
            "resourceType": "Bundle",
            "entry": [
                {
                    "resource": {
                        "resourceType": "Observation",
                        "code": {"text": "Blood pressure"},
                        "component": [
                            {
                                "code": {"text": "Systolic"},
                                "valueQuantity": {"value": 120, "unit": "mmHg"},
                            },
                            {
                                "code": {"text": "Diastolic"},
                                "valueQuantity": {"value": 80, "unit": "mmHg"},
                            },
                        ],
                        "effectiveDateTime": "2025-07-11",
                    }
                }
            ],
        })
        rows, _, _ = adaptive_parse(bundle, "labs")
        assert len(rows) >= 2

    def test_return_format(self):
        """adaptive_parse should return a 3-tuple."""
        rows, fmt, parser = adaptive_parse("", "labs")
        assert isinstance(rows, list)
        assert isinstance(fmt, str)
        assert isinstance(parser, str)
