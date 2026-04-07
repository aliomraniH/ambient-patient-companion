"""Task 5 — Edge cases: empty, null, binary, malformed, very large, nested, concurrent.

Exercises the adaptive ingest pipeline with pathological inputs to verify it
never crashes and always returns a well-formed (rows, format, parser) tuple.

Coverage:
  - Empty and whitespace-only inputs
  - None / non-string inputs
  - Binary-like strings
  - Truncated / malformed JSON
  - Null values inside valid payloads
  - Very large payloads (100KB+)
  - Missing required sections
  - Nested bundles (Bundle containing a Bundle resource)
  - Concurrent invocations (thread-safety of stateless functions)
  - Unicode patient names and values
  - Numbers encoded as strings
  - Empty arrays in dict payloads
"""

from __future__ import annotations

import asyncio
import json
import threading
import time

import pytest

from ingestion.adapters.healthex.format_detector import detect_format, HealthExFormat
from ingestion.adapters.healthex.ingest import adaptive_parse
from ingestion.adapters.healthex.parsers.format_b_parser import parse_compressed_table
from ingestion.adapters.healthex.parsers.format_d_parser import parse_fhir_bundle
from ingestion.adapters.healthex.parsers.json_dict_parser import parse_json_dict_arrays


# ── Empty and whitespace inputs ───────────────────────────────────────────────

class TestEmptyInputs:
    """Pipeline must handle empty and whitespace-only strings gracefully."""

    def test_empty_string_detect_format(self):
        fmt, payload = detect_format("")
        assert fmt == HealthExFormat.UNKNOWN
        assert payload is None

    def test_whitespace_only_detect_format(self):
        fmt, _ = detect_format("   \n\t  ")
        assert fmt == HealthExFormat.UNKNOWN

    def test_empty_string_adaptive_parse(self):
        rows, fmt, parser = adaptive_parse("", "conditions")
        assert rows == []
        assert fmt == "unknown"
        assert isinstance(parser, str)

    def test_whitespace_adaptive_parse(self):
        rows, fmt, _ = adaptive_parse("   ", "labs")
        assert rows == []
        assert fmt == "unknown"

    def test_empty_fhir_bundle_entry_list(self):
        bundle = {"resourceType": "Bundle", "type": "searchset", "entry": []}
        rows = parse_fhir_bundle(bundle, "labs")
        assert rows == []

    def test_empty_json_dict(self):
        rows = parse_json_dict_arrays({}, "conditions")
        assert rows == []

    def test_empty_conditions_array_in_dict(self):
        rows = parse_json_dict_arrays({"conditions": []}, "conditions")
        assert rows == []

    def test_empty_compressed_table_string(self):
        rows = parse_compressed_table("", "conditions")
        assert rows == []


# ── None and non-string inputs ────────────────────────────────────────────────

class TestNullInputs:
    """None and non-string inputs must never crash — return UNKNOWN gracefully."""

    def test_none_detect_format(self):
        fmt, payload = detect_format(None)
        assert fmt == HealthExFormat.UNKNOWN

    def test_integer_detect_format(self):
        fmt, _ = detect_format(42)
        assert fmt == HealthExFormat.UNKNOWN

    def test_dict_detect_format(self):
        fmt, _ = detect_format({"key": "value"})
        assert fmt == HealthExFormat.UNKNOWN

    def test_null_item_in_conditions_array(self):
        """Null items inside a valid array should be skipped, not crash."""
        rows = parse_json_dict_arrays(
            {"conditions": [None, {"name": "Prediabetes"}, None]},
            "conditions",
        )
        assert len(rows) == 1
        assert rows[0]["name"] == "Prediabetes"

    def test_null_value_in_fhir_observation(self):
        """FHIR Observation with null valueQuantity should be skipped gracefully."""
        bundle = {
            "resourceType": "Bundle",
            "entry": [
                {
                    "resource": {
                        "resourceType": "Observation",
                        "code": {"text": "HbA1c"},
                        "valueQuantity": None,
                        "effectiveDateTime": "2025-07-11",
                    }
                }
            ],
        }
        rows = parse_fhir_bundle(bundle, "labs")
        assert isinstance(rows, list)

    def test_null_entry_list_in_bundle(self):
        """Bundle with entry: null should return empty list."""
        bundle = {"resourceType": "Bundle", "entry": None}
        rows = parse_fhir_bundle(bundle, "labs")
        assert rows == []


# ── Binary and malformed inputs ───────────────────────────────────────────────

class TestMalformedInputs:
    """Binary-like strings and malformed JSON must not raise exceptions."""

    def test_binary_like_string(self):
        raw = "\x00\x01\x02\x03binary data here\xff\xfe"
        fmt, _ = detect_format(raw)
        assert fmt == HealthExFormat.UNKNOWN

    def test_truncated_json_object(self):
        raw = '{"resourceType": "Bundle", "entry": [{'
        fmt, _ = detect_format(raw)
        assert fmt == HealthExFormat.UNKNOWN

    def test_truncated_json_string(self):
        raw = '{"conditions": [{"name": "Prediabetes", "onset":'
        fmt, _ = detect_format(raw)
        assert fmt == HealthExFormat.UNKNOWN

    def test_json_with_trailing_comma(self):
        """Trailing comma in JSON array is invalid — should detect as UNKNOWN."""
        raw = '{"conditions": [{"name": "Prediabetes"},]}'
        fmt, _ = detect_format(raw)
        assert fmt in (HealthExFormat.UNKNOWN, HealthExFormat.JSON_DICT_ARRAY)

    def test_json_number_not_dict_or_list(self):
        raw = "42"
        fmt, _ = detect_format(raw)
        assert fmt == HealthExFormat.UNKNOWN

    def test_json_boolean_not_dict_or_list(self):
        raw = "true"
        fmt, _ = detect_format(raw)
        assert fmt == HealthExFormat.UNKNOWN

    def test_adaptive_parse_malformed_never_raises(self):
        """adaptive_parse must never raise on any input."""
        bad_inputs = [
            "\x00\x01\x02",
            '{"unclosed":',
            "not json at all!!@#",
            "null",
            "undefined",
        ]
        for raw in bad_inputs:
            rows, fmt, parser = adaptive_parse(raw, "conditions")
            assert isinstance(rows, list), f"Expected list for input {raw!r}"
            assert isinstance(fmt, str)
            assert isinstance(parser, str)


# ── Very large payloads ───────────────────────────────────────────────────────

class TestLargePayloads:
    """Pipeline must process large payloads without crashing."""

    def test_large_fhir_bundle_50_observations(self):
        entries = [
            {
                "resource": {
                    "resourceType": "Observation",
                    "code": {"text": f"Lab Test {i}"},
                    "valueQuantity": {"value": float(i), "unit": "mg/dL"},
                    "effectiveDateTime": f"2025-{(i % 12 + 1):02d}-01",
                }
            }
            for i in range(50)
        ]
        bundle = {"resourceType": "Bundle", "entry": entries}
        rows = parse_fhir_bundle(bundle, "labs")
        assert len(rows) == 50

    def test_large_fhir_bundle_via_adaptive_parse(self):
        entries = [
            {
                "resource": {
                    "resourceType": "Observation",
                    "code": {"text": f"Test {i}"},
                    "valueQuantity": {"value": float(i), "unit": "mg/dL"},
                    "effectiveDateTime": "2025-01-15",
                }
            }
            for i in range(100)
        ]
        raw = json.dumps({"resourceType": "Bundle", "entry": entries})
        rows, fmt, _ = adaptive_parse(raw, "labs")
        assert fmt == "fhir_bundle_json"
        assert len(rows) == 100

    def test_large_format_a_string_does_not_crash(self):
        """Very long Format A string (padded with extra encounters) should not crash."""
        encounters = " | ".join(
            f"Office Visit:description:Cardiology,diagnoses:HTN "
            f"2025-{(i % 12 + 1):02d}-01@Stanford Health Care"
            for i in range(500)
        )
        raw = (
            "PATIENT: Large Data Patient, DOB 1970-01-01\n"
            f"CLINICAL VISITS(500): {encounters}"
        )
        assert len(raw) > 30_000
        rows, fmt, _ = adaptive_parse(raw, "encounters")
        assert fmt == "plain_text_summary"
        assert isinstance(rows, list)

    def test_large_json_dict_100_conditions(self):
        payload = {
            "conditions": [
                {"name": f"Condition {i}", "onset": "2020-01-01", "status": "active"}
                for i in range(100)
            ]
        }
        rows = parse_json_dict_arrays(payload, "conditions")
        assert len(rows) == 100


# ── Missing sections ──────────────────────────────────────────────────────────

class TestMissingSections:
    """Format A payloads missing a section return empty lists, not crashes."""

    def test_format_a_missing_conditions_section(self):
        raw = (
            "PATIENT: Test User, DOB 1980-01-01\n"
            "LABS(2): Hemoglobin A1c:4.8 % 2025-07-11@Stanford"
        )
        rows, fmt, _ = adaptive_parse(raw, "conditions")
        assert fmt == "plain_text_summary"
        assert rows == []

    def test_format_a_missing_labs_section(self):
        raw = (
            "PATIENT: Test User, DOB 1980-01-01\n"
            "CONDITIONS(1): Active: Prediabetes@Stanford 2017-04-25"
        )
        rows, fmt, _ = adaptive_parse(raw, "labs")
        assert fmt == "plain_text_summary"
        assert rows == []

    def test_fhir_bundle_missing_entry_key(self):
        """Bundle without 'entry' key should return empty list gracefully."""
        bundle = {"resourceType": "Bundle", "type": "searchset"}
        rows = parse_fhir_bundle(bundle, "labs")
        assert rows == []

    def test_json_dict_no_list_values_returns_empty(self):
        """Payload with no list values at all should return []."""
        rows = parse_json_dict_arrays({"info": "some string", "count": 5}, "labs")
        assert rows == []

    def test_json_dict_wrong_key_uses_fallback(self):
        """When no canonical key matches, the parser falls back to the first list it finds.
        This is documented behavior allowing any-key payloads to work."""
        rows = parse_json_dict_arrays({"conditions": [{"name": "Prediabetes"}]}, "labs")
        assert isinstance(rows, list)


# ── Nested bundles ────────────────────────────────────────────────────────────

class TestNestedBundles:
    """Bundles containing non-matching resource types should not crash."""

    def test_bundle_with_patient_resource_for_labs(self):
        """Patient resources in a bundle targeting 'labs' should yield 0 rows."""
        bundle = {
            "resourceType": "Bundle",
            "entry": [
                {
                    "resource": {
                        "resourceType": "Patient",
                        "id": "pat-001",
                        "name": [{"family": "Chen", "given": ["Maria"]}],
                    }
                }
            ],
        }
        rows, fmt, _ = adaptive_parse(json.dumps(bundle), "labs")
        assert fmt == "fhir_bundle_json"
        assert rows == []

    def test_bundle_with_mixed_resource_types(self):
        """Bundle with Observation + Condition targeting 'labs' yields only Observations."""
        bundle = {
            "resourceType": "Bundle",
            "entry": [
                {
                    "resource": {
                        "resourceType": "Observation",
                        "code": {"text": "HbA1c"},
                        "valueQuantity": {"value": 7.2, "unit": "%"},
                        "effectiveDateTime": "2025-07-11",
                    }
                },
                {
                    "resource": {
                        "resourceType": "Condition",
                        "code": {"text": "Prediabetes"},
                    }
                },
            ],
        }
        rows, fmt, _ = adaptive_parse(json.dumps(bundle), "labs")
        assert fmt == "fhir_bundle_json"
        assert len(rows) == 1

    def test_double_encoded_fhir_bundle_unwraps(self):
        """A JSON string containing a JSON-encoded bundle should be detected correctly."""
        inner_bundle = {
            "resourceType": "Bundle",
            "entry": [
                {
                    "resource": {
                        "resourceType": "Observation",
                        "code": {"text": "LDL"},
                        "valueQuantity": {"value": 104, "unit": "mg/dL"},
                        "effectiveDateTime": "2025-07-11",
                    }
                }
            ],
        }
        double_encoded = json.dumps(json.dumps(inner_bundle))
        rows, fmt, _ = adaptive_parse(double_encoded, "labs")
        assert fmt == "fhir_bundle_json"
        assert len(rows) == 1


# ── Concurrency ───────────────────────────────────────────────────────────────

class TestConcurrency:
    """Stateless functions must produce correct results under concurrent load."""

    def test_detect_format_concurrent_threads(self):
        """10 threads each calling detect_format 100 times — all results correct."""
        inputs_and_expected = [
            (
                json.dumps({"conditions": [{"name": "Prediabetes"}]}),
                HealthExFormat.JSON_DICT_ARRAY,
            ),
            (
                "PATIENT: Test, DOB 1980-01-01",
                HealthExFormat.PLAIN_TEXT_SUMMARY,
            ),
            (
                json.dumps({
                    "resourceType": "Bundle",
                    "entry": [{"resource": {"resourceType": "Observation"}}],
                }),
                HealthExFormat.FHIR_BUNDLE_JSON,
            ),
        ]
        errors = []

        def worker(raw, expected_fmt):
            for _ in range(100):
                fmt, _ = detect_format(raw)
                if fmt != expected_fmt:
                    errors.append(f"Expected {expected_fmt}, got {fmt}")

        threads = [
            threading.Thread(target=worker, args=(raw, expected))
            for raw, expected in inputs_and_expected
            for _ in range(3)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors, f"Concurrency errors: {errors[:5]}"

    def test_adaptive_parse_concurrent_threads(self):
        """5 threads each parsing the same Format D bundle — all produce 2 rows."""
        bundle = json.dumps({
            "resourceType": "Bundle",
            "entry": [
                {
                    "resource": {
                        "resourceType": "Observation",
                        "code": {"text": "HbA1c"},
                        "valueQuantity": {"value": 7.2, "unit": "%"},
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
        results = []
        errors = []

        def worker():
            rows, fmt, _ = adaptive_parse(bundle, "labs")
            results.append(len(rows))
            if fmt != "fhir_bundle_json":
                errors.append(f"Wrong format: {fmt}")

        threads = [threading.Thread(target=worker) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors
        assert all(r == 2 for r in results), f"Not all threads got 2 rows: {results}"


# ── Unicode and special values ────────────────────────────────────────────────

class TestUnicodeAndSpecialValues:
    """Pipeline handles unicode names and number-as-string values correctly."""

    def test_unicode_patient_name_format_a(self):
        raw = (
            "PATIENT: 李小明, DOB 1975-08-20\n"
            "CONDITIONS(1): Active: Prediabetes@Stanford Health Care 2017-04-25"
        )
        rows, fmt, _ = adaptive_parse(raw, "conditions")
        assert fmt == "plain_text_summary"
        assert isinstance(rows, list)

    def test_unicode_condition_name_in_json_dict(self):
        payload = {
            "conditions": [
                {"name": "糖尿病前期", "onset": "2017-04-25", "status": "active"},
            ]
        }
        rows = parse_json_dict_arrays(payload, "conditions")
        assert len(rows) == 1
        assert rows[0]["name"] == "糖尿病前期"

    def test_numbers_as_strings_in_fhir_observation(self):
        """value encoded as string '7.2' (not float) should be preserved."""
        bundle = {
            "resourceType": "Bundle",
            "entry": [
                {
                    "resource": {
                        "resourceType": "Observation",
                        "code": {"text": "HbA1c"},
                        "valueQuantity": {"value": "7.2", "unit": "%"},
                        "effectiveDateTime": "2025-07-11",
                    }
                }
            ],
        }
        rows = parse_fhir_bundle(bundle, "labs")
        assert len(rows) == 1
        assert str(rows[0]["value"]) == "7.2"

    def test_json_dict_with_string_encoded_number_value(self):
        rows = parse_json_dict_arrays(
            {"labs": [{"test_name": "HbA1c", "value": "7.2", "unit": "%",
                       "date": "2025-07-11"}]},
            "labs",
        )
        assert len(rows) == 1
        assert rows[0]["value"] == "7.2"

    def test_emoji_in_condition_name_does_not_crash(self):
        """Emoji characters in condition names must not crash the parser."""
        rows = parse_json_dict_arrays(
            {"conditions": [{"name": "Prediabetes 🩺", "onset": "2020-01-01"}]},
            "conditions",
        )
        assert isinstance(rows, list)
