"""Task 6 — Performance benchmarks: format detection < 1ms, parsing < 5ms per call.

These are timing assertions, not functional tests.  They verify that the
adaptive pipeline meets the latency SLA required for real-time ingestion:

  - detect_format()  : < 1 ms per call  (1 000 calls in < 1.0 s)
  - adaptive_parse() : < 5 ms per call  (200 calls in < 1.0 s)
  - Format B parse   : < 5 ms per 50-row table
  - Format D parse   : < 5 ms per 50-entry bundle
  - Format A parse   : < 10 ms per full summary (heavier regex)

Timings are measured with time.perf_counter() and asserted against soft limits
that leave 3× headroom over the spec to avoid flakiness on shared runners.
"""

from __future__ import annotations

import json
import time

import pytest

from ingestion.adapters.healthex.format_detector import detect_format, HealthExFormat
from ingestion.adapters.healthex.ingest import adaptive_parse
from ingestion.adapters.healthex.parsers.format_a_parser import parse_plain_text_summary
from ingestion.adapters.healthex.parsers.format_b_parser import parse_compressed_table
from ingestion.adapters.healthex.parsers.format_d_parser import parse_fhir_bundle
from ingestion.adapters.healthex.parsers.json_dict_parser import parse_json_dict_arrays


# ── Fixtures / sample payloads ────────────────────────────────────────────────

_FORMAT_A_FULL = (
    "PATIENT: Ali Omrani, DOB 1987-03-25\n"
    "PROVIDERS: Stanford Health Care\n"
    "CONDITIONS(4/10): Active: BMI 34.0-34.9,adult@Stanford Health Care 2019-01-11 | "
    "Active: Prediabetes@Stanford Health Care 2017-04-25 | "
    "Active: Fatty liver@Stanford Health Care 2017-01-01\n"
    "LABS(96): Hemoglobin A1c:4.8 %(ref:<5.7) 2025-07-11@Stanford Health Care"
    "[totalrecords:9] | LDL Cholesterol:104 mg/dL(ref:<100) 2025-07-11@Stanford"
    " Health Care[OutOfRange][totalrecords:8] | eGFR:78 mL/min 2025-06-01@Stanford"
    "\nMEDICATIONS(5): Metformin 500 mg 2x/day:active 2020-01-15@Stanford | "
    "Lisinopril 10 mg 1x/day:active 2019-06-01@Stanford\n"
    "IMMUNIZATIONS(17): Flu vaccine (IIV4) 2023-12-13@Stanford | COVID-19 mRNA 2022-10-15\n"
    "CLINICAL VISITS(35): Office Visit:description:Internal Medicine,"
    "diagnoses:Fatty liver 2025-06-26@Stanford | Office Visit:description:Endocrinology,"
    "diagnoses:Prediabetes 2023-12-13@Stanford"
)

_FORMAT_B_50_ROWS = (
    "#Conditions 5y|Total:50\n"
    + "C:" + "|".join(f"{i}=Condition {i}" for i in range(1, 51)) + "|\n"
    + "S:1=active|\n"
    + "Date|Condition|ClinicalStatus|OnsetDate|AbatementDate|SNOMED|ICD10\n"
    + "\n".join(f"|@{i}|@1|2020-01-{(i % 28) + 1:02d}||00000{i}|X{i:02d}.0"
               for i in range(1, 51))
)

_FORMAT_D_50_ENTRIES = {
    "resourceType": "Bundle",
    "entry": [
        {
            "resource": {
                "resourceType": "Observation",
                "code": {"text": f"Lab Test {i}"},
                "valueQuantity": {"value": float(i) * 0.5, "unit": "mg/dL"},
                "effectiveDateTime": f"2025-{(i % 12 + 1):02d}-01",
            }
        }
        for i in range(50)
    ],
}
_FORMAT_D_50_JSON = json.dumps(_FORMAT_D_50_ENTRIES)

_JSON_DICT_50_CONDITIONS = {
    "conditions": [
        {"name": f"Condition {i}", "onset": "2020-01-01", "status": "active"}
        for i in range(50)
    ]
}

_DETECT_INPUTS = [
    ("PATIENT: Test, DOB 1980-01-01", HealthExFormat.PLAIN_TEXT_SUMMARY),
    (
        "#Conditions 5y|Total:1\nC:1=Prediabetes|\nDate|Condition\n|@1",
        HealthExFormat.COMPRESSED_TABLE,
    ),
    (
        "resourceType is Observation. code.text is HbA1c. valueQuantity.value is 7.2",
        HealthExFormat.FLAT_FHIR_TEXT,
    ),
    (
        json.dumps({
            "resourceType": "Bundle",
            "entry": [{"resource": {"resourceType": "Observation"}}],
        }),
        HealthExFormat.FHIR_BUNDLE_JSON,
    ),
    (
        json.dumps({"conditions": [{"name": "Prediabetes"}]}),
        HealthExFormat.JSON_DICT_ARRAY,
    ),
]


# ── detect_format() benchmarks ────────────────────────────────────────────────

class TestDetectFormatPerformance:
    """detect_format() must complete in < 1 ms per call."""

    def test_detect_format_1000_calls_under_1_second(self):
        """1 000 detect_format calls across all 5 formats must finish in < 1.0 s."""
        n = 1000
        start = time.perf_counter()
        for i in range(n):
            raw, _ = _DETECT_INPUTS[i % len(_DETECT_INPUTS)]
            detect_format(raw)
        elapsed = time.perf_counter() - start
        assert elapsed < 1.0, (
            f"detect_format: {n} calls took {elapsed:.3f}s "
            f"(> 1.0s limit; {elapsed / n * 1000:.3f} ms/call)"
        )

    def test_detect_format_format_a_under_1ms(self):
        """Single Format A detection must complete in < 1 ms."""
        n, limit_ms = 500, 1.0
        start = time.perf_counter()
        for _ in range(n):
            detect_format("PATIENT: Test, DOB 1980-01-01\nCONDITIONS(1): Active: X 2020-01-01")
        elapsed_ms = (time.perf_counter() - start) * 1000
        per_call = elapsed_ms / n
        assert per_call < limit_ms, (
            f"Format A detect_format: {per_call:.3f} ms/call (limit {limit_ms} ms)"
        )

    def test_detect_format_format_d_under_1ms(self):
        """Format D (FHIR Bundle JSON parse + detect) must complete in < 1 ms per call."""
        n, limit_ms = 200, 1.0
        raw = json.dumps({
            "resourceType": "Bundle",
            "entry": [{"resource": {"resourceType": "Observation"}}],
        })
        start = time.perf_counter()
        for _ in range(n):
            detect_format(raw)
        elapsed_ms = (time.perf_counter() - start) * 1000
        per_call = elapsed_ms / n
        assert per_call < limit_ms, (
            f"Format D detect_format: {per_call:.3f} ms/call (limit {limit_ms} ms)"
        )

    def test_detect_format_all_formats_return_correct_enum(self):
        """Performance regression guard: correct enum returned at speed."""
        for raw, expected_fmt in _DETECT_INPUTS:
            fmt, _ = detect_format(raw)
            assert fmt == expected_fmt, (
                f"detect_format returned {fmt} but expected {expected_fmt} for {raw[:40]!r}"
            )


# ── Parser benchmarks ─────────────────────────────────────────────────────────

class TestParserPerformance:
    """Each parser must complete in < 5 ms per call."""

    def test_format_b_50_row_table_under_5ms(self):
        """Format B parser on a 50-row table must finish in < 5 ms."""
        n, limit_ms = 100, 5.0
        start = time.perf_counter()
        for _ in range(n):
            parse_compressed_table(_FORMAT_B_50_ROWS, "conditions")
        elapsed_ms = (time.perf_counter() - start) * 1000
        per_call = elapsed_ms / n
        assert per_call < limit_ms, (
            f"Format B (50 rows): {per_call:.3f} ms/call (limit {limit_ms} ms)"
        )

    def test_format_d_50_entry_bundle_under_5ms(self):
        """Format D parser on a 50-entry bundle must finish in < 5 ms."""
        n, limit_ms = 100, 5.0
        start = time.perf_counter()
        for _ in range(n):
            parse_fhir_bundle(_FORMAT_D_50_ENTRIES, "labs")
        elapsed_ms = (time.perf_counter() - start) * 1000
        per_call = elapsed_ms / n
        assert per_call < limit_ms, (
            f"Format D (50 entries): {per_call:.3f} ms/call (limit {limit_ms} ms)"
        )

    def test_json_dict_50_conditions_under_5ms(self):
        """JSON Dict parser on a 50-condition dict must finish in < 5 ms."""
        n, limit_ms = 200, 5.0
        start = time.perf_counter()
        for _ in range(n):
            parse_json_dict_arrays(_JSON_DICT_50_CONDITIONS, "conditions")
        elapsed_ms = (time.perf_counter() - start) * 1000
        per_call = elapsed_ms / n
        assert per_call < limit_ms, (
            f"JSON dict (50 items): {per_call:.3f} ms/call (limit {limit_ms} ms)"
        )

    def test_format_a_full_summary_under_10ms(self):
        """Format A (full summary with regex) must finish in < 10 ms per call."""
        n, limit_ms = 100, 10.0
        start = time.perf_counter()
        for _ in range(n):
            parse_plain_text_summary(_FORMAT_A_FULL, "conditions")
        elapsed_ms = (time.perf_counter() - start) * 1000
        per_call = elapsed_ms / n
        assert per_call < limit_ms, (
            f"Format A (full summary): {per_call:.3f} ms/call (limit {limit_ms} ms)"
        )


# ── adaptive_parse() end-to-end benchmarks ───────────────────────────────────

class TestAdaptiveParsePerformance:
    """adaptive_parse() end-to-end must complete in < 5 ms per call."""

    def test_adaptive_parse_format_d_200_calls_under_1_second(self):
        """200 adaptive_parse calls on a 2-entry FHIR Bundle must finish in < 1.0 s."""
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
        n = 200
        start = time.perf_counter()
        for _ in range(n):
            rows, _, _ = adaptive_parse(bundle, "labs")
        elapsed = time.perf_counter() - start
        assert elapsed < 1.0, (
            f"adaptive_parse (Format D, 2 entries): {n} calls took {elapsed:.3f}s "
            f"({elapsed / n * 1000:.3f} ms/call; limit 5.0 ms)"
        )
        assert len(rows) == 2

    def test_adaptive_parse_format_json_dict_200_calls_under_1_second(self):
        """200 adaptive_parse calls on a JSON dict payload must finish in < 1.0 s."""
        raw = json.dumps({"conditions": [
            {"name": "Prediabetes", "onset": "2017-04-25", "status": "active"},
            {"name": "Hypertension", "onset": "2019-06-15", "status": "active"},
        ]})
        n = 200
        start = time.perf_counter()
        for _ in range(n):
            adaptive_parse(raw, "conditions")
        elapsed = time.perf_counter() - start
        assert elapsed < 1.0, (
            f"adaptive_parse (JSON dict): {n} calls took {elapsed:.3f}s "
            f"({elapsed / n * 1000:.3f} ms/call; limit 5.0 ms)"
        )

    def test_adaptive_parse_50_entry_bundle_under_5ms_per_call(self):
        """adaptive_parse on a 50-entry bundle: < 5 ms per call."""
        n, limit_ms = 50, 5.0
        start = time.perf_counter()
        for _ in range(n):
            adaptive_parse(_FORMAT_D_50_JSON, "labs")
        elapsed_ms = (time.perf_counter() - start) * 1000
        per_call = elapsed_ms / n
        assert per_call < limit_ms, (
            f"adaptive_parse (50-entry bundle): {per_call:.3f} ms/call (limit {limit_ms} ms)"
        )
