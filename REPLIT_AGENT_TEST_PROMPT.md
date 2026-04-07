# Replit Agent — Test & Validate the Adaptive Schema-Inference Ingest Pipeline

## What Changed

We replaced the `ingest_from_healthex()` function in `server/mcp_server.py` with a 3-stage adaptive pipeline that handles all known HealthEx payload formats. Previously, `json.loads()` was called first, which crashed on 3 of the 4 formats HealthEx actually returns. Now the pipeline detects the format, routes to the correct parser, and falls back to LLM extraction if deterministic parsing fails.

We also fixed the CrossCritique JSON fence stripping bug in the deliberation engine (`server/deliberation/critic.py` and `analyst.py`).

### New Files to Test

```
ingestion/adapters/healthex/
├── __init__.py
├── format_detector.py       ← HealthExFormat enum + detect_format()
├── ingest.py                ← adaptive_parse() entry point
├── llm_fallback.py          ← Claude Sonnet fallback normaliser
└── parsers/
    ├── __init__.py
    ├── format_a_parser.py   ← Plain text summary parser
    ├── format_b_parser.py   ← Compressed dictionary table parser
    ├── format_c_parser.py   ← Flat FHIR text parser
    ├── format_d_parser.py   ← FHIR Bundle JSON parser
    └── json_dict_parser.py  ← Custom JSON dict-with-arrays parser

server/deliberation/json_utils.py  ← strip_markdown_fences()
```

### Modified Files

```
server/mcp_server.py                 ← ingest_from_healthex() rewritten
server/deliberation/critic.py        ← strip_markdown_fences() before model_validate_json()
server/deliberation/analyst.py       ← strip_markdown_fences() before model_validate_json()
```

### Existing Unit Tests (already passing, 54 total)

```
ingestion/tests/test_format_detector.py   ← 14 tests
ingestion/tests/test_parsers.py           ← 24 tests
ingestion/tests/test_adaptive_ingest.py   ← 9 tests
server/deliberation/tests/test_json_utils.py ← 8 tests
```

---

## Task 1: Verify Existing Tests Pass

First, install dependencies and run existing tests:

```bash
pip install pytest pytest-asyncio pydantic anthropic openai asyncpg faker numpy python-dateutil fastmcp
```

```bash
# New pipeline unit tests (no DB required)
pytest ingestion/tests/test_format_detector.py -v
pytest ingestion/tests/test_parsers.py -v
pytest ingestion/tests/test_adaptive_ingest.py -v
pytest server/deliberation/tests/test_json_utils.py -v
```

All 54 should pass. If any fail, investigate and fix before proceeding.

---

## Task 2: Create Integration Tests for the Adaptive Pipeline

Create `tests/phase1/test_adaptive_ingest_integration.py` — these tests call `ingest_from_healthex()` end-to-end against the live database, using the `healthex_patient` fixture from `tests/phase1/conftest.py`.

### Important: The existing integration tests in `tests/phase1/test_healthex_ingest_integration.py` need updating

The old tests expected `records_written == 0` for raw text and a `note` field. The new adaptive pipeline actually PARSES these formats now, so:
- A double-encoded compressed table string may now produce rows (Format B parsed)
- The response no longer has a `note` field — instead it has `format_detected` and `parser_used`

**Update the old tests** in `tests/phase1/test_healthex_ingest_integration.py`:
- `TestRawTextPayloadCaching` should be updated: a double-encoded compressed table is now unwrapped and parsed, so `records_written` may be > 0
- `TestRawTextPayloadCaching.test_raw_text_note_matches_spec` should check for `format_detected` and `parser_used` fields instead of `note`

### New Integration Tests to Write

Use these exact sample payloads — they are based on real HealthEx API responses observed in 4 live test runs.

#### Sample Payloads

**SAMPLE_FORMAT_A** (Plain text summary from `get_health_summary`):
```python
SAMPLE_FORMAT_A = """PATIENT: Ali Omrani, DOB 1987-03-25
PROVIDERS: Stanford Health Care (Stanford Health Care and Stanford Medicine Partners)
CONDITIONS(4/10): Active: BMI 34.0-34.9,adult@Stanford Health Care 2019-01-11 | Active: Prediabetes@Stanford Health Care 2017-04-25 | Active: Fatty liver disease@Stanford Health Care 2017-01-01 | Inactive: GERD@Stanford Health Care 2015-06-15
LABS(96): Hemoglobin A1c:4.8 %(ref:<5.7) 2025-07-11@Stanford Health Care[totalrecords:9] | LDL Cholesterol:104 mg/dL(ref:<100) 2025-07-11@Stanford Health Care[OutOfRange][totalrecords:8] | Glucose:98 mg/dL(ref:74-106) 2025-07-11@Stanford Health Care[totalrecords:12] | eGFR:>90 mL/min/1.73m2(ref:>60) 2025-01-15@Stanford Health Care[totalrecords:5]
ALLERGIES(1): No Known Allergies 2015-07-21@Stanford Health Care
IMMUNIZATIONS(17): Flu vaccine (IIV4) 2023-12-13@Stanford Health Care | COVID-19 mRNA vaccine 2022-10-15@Stanford Health Care | Tdap 2020-08-20@Stanford Health Care
CLINICAL VISITS(35): Office Visit:description:Internal Medicine,diagnoses:Fatty liver disease,Family history of CAD 2025-06-26@Stanford Health Care | Office Visit:description:Endocrinology,diagnoses:Prediabetes followup 2023-12-13@Stanford Health Care | Annual Physical:description:Primary Care,diagnoses:Routine exam 2023-01-10@Stanford Health Care"""
```

**SAMPLE_FORMAT_B** (Compressed dictionary table from `get_conditions`):
```python
SAMPLE_FORMAT_B = """#Conditions 5y|Total:10
D:1=2019-01-11|2=2017-04-25|3=2017-01-01|4=2015-06-15|5=2024-03-10|
C:1=BMI 34.0-34.9,adult|2=Prediabetes|3=Fatty liver disease|4=Gastroesophageal reflux disease|5=Vitamin D deficiency|
S:1=active|2=resolved|
Sys:1=http://snomed.info/sct|
Date|Condition|ClinicalStatus|OnsetDate|AbatementDate|SNOMED|ICD10|PreferredCode|PreferredSystem|Recorder|Asserter|Encounter
@1|@1|@1|2019-01-11||162864005|Z68.34|Z68.34|http://hl7.org/fhir/sid/icd-10-cm|||enc_001
|@2|@1|2017-04-25||714628002|R73.03|R73.03|http://hl7.org/fhir/sid/icd-10-cm|||enc_002
|@3|@1|2017-01-01||197321007|K76.0|K76.0|http://hl7.org/fhir/sid/icd-10-cm|||enc_003
|@4|@2|2015-06-15|2020-03-01|235595009|K21.0|K21.0|http://hl7.org/fhir/sid/icd-10-cm|||enc_004
|@5|@1|2024-03-10||34713006|E55.9|E55.9|http://hl7.org/fhir/sid/icd-10-cm|||enc_005"""
```

**SAMPLE_FORMAT_C** (Flat FHIR text from `search`):
```python
SAMPLE_FORMAT_C = """resourceType is Observation. id is obs_hba1c_001. status is final. code.coding[0].system is http://loinc.org. code.coding[0].code is 4548-4. code.coding[0].display is Hemoglobin A1c/Hemoglobin.total in Blood. code.text is Hemoglobin A1c. valueQuantity.value is 4.8. valueQuantity.unit is %. valueQuantity.system is http://unitsofmeasure.org. effectiveDateTime is 2025-07-11T10:30:00Z. referenceRange[0].text is <5.7%. resourceType is Observation. id is obs_ldl_001. status is final. code.coding[0].system is http://loinc.org. code.coding[0].code is 2089-1. code.text is LDL Cholesterol. valueQuantity.value is 104. valueQuantity.unit is mg/dL. effectiveDateTime is 2025-07-11T10:30:00Z. interpretation[0].coding[0].code is H"""
```

**SAMPLE_FORMAT_D** (Proper FHIR R4 Bundle JSON):
```python
import json
SAMPLE_FORMAT_D = json.dumps({
    "resourceType": "Bundle",
    "type": "searchset",
    "total": 5,
    "entry": [
        {
            "resource": {
                "resourceType": "Observation",
                "id": "obs-hba1c-2025",
                "code": {
                    "text": "Hemoglobin A1c",
                    "coding": [{"system": "http://loinc.org", "code": "4548-4", "display": "Hemoglobin A1c/Hemoglobin.total in Blood"}]
                },
                "valueQuantity": {"value": 4.8, "unit": "%", "system": "http://unitsofmeasure.org"},
                "effectiveDateTime": "2025-07-11",
                "referenceRange": [{"text": "<5.7%"}]
            }
        },
        {
            "resource": {
                "resourceType": "Observation",
                "id": "obs-ldl-2025",
                "code": {
                    "text": "LDL Cholesterol",
                    "coding": [{"system": "http://loinc.org", "code": "2089-1"}]
                },
                "valueQuantity": {"value": 104, "unit": "mg/dL"},
                "effectiveDateTime": "2025-07-11",
                "interpretation": [{"coding": [{"code": "H"}]}]
            }
        },
        {
            "resource": {
                "resourceType": "Observation",
                "id": "obs-glucose-2025",
                "code": {"text": "Glucose"},
                "valueQuantity": {"value": 98, "unit": "mg/dL"},
                "effectiveDateTime": "2025-07-11"
            }
        },
        {
            "resource": {
                "resourceType": "Observation",
                "id": "obs-bp-2025",
                "code": {"text": "Blood pressure panel"},
                "component": [
                    {
                        "code": {"text": "Systolic blood pressure"},
                        "valueQuantity": {"value": 128, "unit": "mmHg"}
                    },
                    {
                        "code": {"text": "Diastolic blood pressure"},
                        "valueQuantity": {"value": 82, "unit": "mmHg"}
                    }
                ],
                "effectiveDateTime": "2025-07-11"
            }
        },
        {
            "resource": {
                "resourceType": "Condition",
                "id": "cond-prediabetes",
                "code": {
                    "text": "Prediabetes",
                    "coding": [
                        {"system": "http://snomed.info/sct", "code": "714628002"},
                        {"system": "http://hl7.org/fhir/sid/icd-10-cm", "code": "R73.03"}
                    ]
                },
                "clinicalStatus": {"coding": [{"code": "active"}]},
                "onsetDateTime": "2017-04-25"
            }
        }
    ]
})
```

**SAMPLE_JSON_DICT** (Custom JSON dict-with-arrays):
```python
SAMPLE_JSON_DICT = json.dumps({
    "conditions": [
        {"name": "Prediabetes", "onset": "2017-04-25", "status": "active", "icd10": "R73.03", "snomed": "714628002"},
        {"name": "Fatty liver disease", "onset": "2017-01-01", "status": "active", "icd10": "K76.0"},
        {"name": "BMI 34.0-34.9, adult", "onset": "2019-01-11", "status": "active", "icd10": "Z68.34"},
    ],
    "medications": [
        {"name": "Pantoprazole 40mg", "status": "active", "start_date": "2017-03-31"},
        {"name": "Vitamin D3 2000 IU", "status": "active", "start_date": "2024-03-15"},
    ],
    "labs": [
        {"test_name": "Hemoglobin A1c", "value": "4.8", "unit": "%", "date": "2025-07-11", "loinc": "4548-4"},
        {"test_name": "LDL Cholesterol", "value": "104", "unit": "mg/dL", "date": "2025-07-11", "loinc": "2089-1"},
        {"test_name": "Glucose", "value": "98", "unit": "mg/dL", "date": "2025-07-11"},
    ],
    "encounters": [
        {"date": "2025-06-26", "type": "Office Visit"},
        {"date": "2023-12-13", "type": "Office Visit"},
    ]
})
```

**SAMPLE_DOUBLE_ENCODED** (Double-encoded string — the old bug trigger):
```python
SAMPLE_DOUBLE_ENCODED = json.dumps(
    "#Conditions 5y|Total:2\nC:1=Prediabetes|2=Fatty liver|\nS:1=active|\n"
    "Date|Condition|ClinicalStatus|OnsetDate\n|@1|@1|2017-04-25\n|@2|@1|2017-01-01"
)
```

**SAMPLE_FENCED_CROSSCRITIQUE** (Bug 3 — markdown fences around JSON):
```python
SAMPLE_FENCED_CROSSCRITIQUE = '```json\n{\n  "critique_items": [\n    {\n      "target_claim": "HbA1c trend is stable",\n      "critique_type": "missed_consideration",\n      "critique_text": "Does not account for recent dietary changes",\n      "suggested_revision": "Add note about incomplete diet history",\n      "severity": "moderate"\n    }\n  ],\n  "areas_of_agreement": [\n    "Both models agree HbA1c is within normal range",\n    "Fatty liver requires ongoing monitoring"\n  ],\n  "raw_critique": "Full analysis of partner findings."\n}\n```'
```

---

### Test Classes to Implement

#### `tests/phase1/test_adaptive_ingest_integration.py`

```python
"""Integration tests for the adaptive schema-inference ingest pipeline.

Tests the full end-to-end flow: raw HealthEx payload → format detection →
parsing → FHIR normalisation → DB write → verification.
Requires DATABASE_URL to be set and the schema to be applied.
"""
```

**Class: TestFormatAIngestion**
- `test_conditions_from_plain_text_summary`: Pass SAMPLE_FORMAT_A with resource_type="conditions" → verify `total_written >= 3` (BMI, Prediabetes, Fatty liver, GERD)
- `test_labs_from_plain_text_summary`: Pass SAMPLE_FORMAT_A with resource_type="labs" → verify `total_written >= 3` (HbA1c, LDL, Glucose, eGFR)
- `test_encounters_from_plain_text_summary`: Pass SAMPLE_FORMAT_A with resource_type="encounters" → verify `total_written >= 2`
- `test_immunizations_ingest`: Pass SAMPLE_FORMAT_A with resource_type="immunizations" — note: immunizations aren't in valid_types for ingest_from_healthex, so this should return an error. Verify the error message.
- `test_format_detected_field`: Verify response JSON includes `"format_detected": "plain_text_summary"` and `"parser_used"` contains `"format_a"`

**Class: TestFormatBIngestion**
- `test_conditions_from_compressed_table`: Pass SAMPLE_FORMAT_B with resource_type="conditions" → verify `total_written >= 3`
- `test_icd10_codes_preserved`: After ingestion, query `patient_conditions` table and verify ICD-10 codes (R73.03, K76.0, Z68.34) are stored (check the `code` column)
- `test_format_detected_field`: Verify `"format_detected": "compressed_table"`

**Class: TestFormatCIngestion**
- `test_labs_from_flat_fhir_text`: Pass SAMPLE_FORMAT_C with resource_type="labs" → verify `total_written >= 2` (HbA1c + LDL)
- `test_format_detected_field`: Verify `"format_detected": "flat_fhir_text"`

**Class: TestFormatDIngestion**
- `test_labs_from_fhir_bundle`: Pass SAMPLE_FORMAT_D with resource_type="labs" → verify `total_written >= 4` (HbA1c, LDL, Glucose, BP systolic, BP diastolic)
- `test_conditions_from_fhir_bundle`: Pass SAMPLE_FORMAT_D with resource_type="conditions" → verify `total_written >= 1` (Prediabetes)
- `test_component_observations_exploded`: Verify that the BP panel produces separate Systolic and Diastolic rows in `biometric_readings`
- `test_verified_counts_in_response`: Verify the response includes `verified_counts` dict with actual DB counts

**Class: TestJsonDictIngestion**
- `test_conditions_from_json_dict`: Pass SAMPLE_JSON_DICT with resource_type="conditions" → verify `total_written == 3`
- `test_medications_from_json_dict`: Pass SAMPLE_JSON_DICT with resource_type="medications" → verify `total_written == 2`
- `test_labs_from_json_dict`: Pass SAMPLE_JSON_DICT with resource_type="labs" → verify `total_written == 3`
- `test_summary_fan_out`: Pass SAMPLE_JSON_DICT with resource_type="summary" → verify conditions + medications + labs + encounters all written

**Class: TestDoubleEncodedPayload**
- `test_double_encoded_string_parsed`: Pass SAMPLE_DOUBLE_ENCODED with resource_type="conditions" → the old code returned `records_written: 0`, new code should parse and write rows
- `test_parser_used_shows_format_detected`: Verify `parser_used` and `format_detected` indicate the format was identified

**Class: TestPatientIdStability**
- `test_patient_id_never_overridden`: For every format (A through JSON dict), verify the response `patient_id` always equals the input `patient_id` from the fixture — never a UUID derived from the payload
- `test_raw_cache_always_written`: For every format, verify a row exists in `raw_fhir_cache` with the correct `patient_id` and `resource_type`

**Class: TestWriteVerification**
- `test_verified_counts_match_written`: After ingesting labs from SAMPLE_FORMAT_D, verify `verified_counts["labs"] >= records_written["labs"]`
- `test_zero_write_zero_verify`: Pass an empty Bundle → verify `total_written == 0`

---

## Task 3: Create Deliberation Engine Fence-Stripping Integration Test

Create `tests/phase1/test_deliberation_fence_strip.py`:

```python
"""Integration test for markdown fence stripping in the deliberation engine.

Verifies that CrossCritique, RevisedAnalysis, and IndependentAnalysis
all parse correctly when the LLM wraps the JSON in ```json ... ``` fences.
"""
```

**Test with Pydantic models directly** (no LLM call needed):

```python
from server.deliberation.json_utils import strip_markdown_fences
from server.deliberation.schemas import CrossCritique, RevisedAnalysis, IndependentAnalysis
```

**Class: TestCrossCritiqueFenceStrip**
- `test_fenced_crosscritique_parses`: Use SAMPLE_FENCED_CROSSCRITIQUE → `CrossCritique.model_validate_json(strip_markdown_fences(raw))` → verify `len(critique.critique_items) == 1` and `critique.critique_items[0].severity == "moderate"`
- `test_unfenced_crosscritique_still_works`: Same JSON without fences → verify it still parses
- `test_fenced_revised_analysis_parses`: Create a fenced RevisedAnalysis JSON → verify it parses after stripping

**Class: TestIndependentAnalysisFenceStrip**
- `test_fenced_independent_analysis`: Create a fenced IndependentAnalysis JSON with key_findings, risk_flags, etc. → verify `strip_markdown_fences()` + `model_validate_json()` succeeds

Use these sample payloads:

```python
FENCED_REVISED = '```json\n{\n  "revised_findings": [{"claim": "HbA1c stable at 4.8%", "confidence": 0.92, "evidence_refs": ["lab_2025-07-11"]}],\n  "revisions_made": ["Upgraded confidence based on 9 historical readings"],\n  "maintained_positions": ["Prediabetes diagnosis remains appropriate"],\n  "raw_revision": "Full revision chain of thought."\n}\n```'

FENCED_ANALYSIS = '```json\n{\n  "key_findings": [{"claim": "HbA1c within normal range", "confidence": 0.95, "evidence_refs": ["lab_hba1c_2025"]}],\n  "risk_flags": [{"claim": "LDL borderline high at 104", "confidence": 0.80, "evidence_refs": ["lab_ldl_2025"]}],\n  "recommended_actions": [{"claim": "Repeat lipid panel in 6 months", "confidence": 0.75, "evidence_refs": ["ADA_2024"]}],\n  "anticipated_trajectory": "Stable metabolic profile with minor lipid concern",\n  "missing_data_identified": ["No recent retinal exam", "UACR not done in 12 months"],\n  "raw_reasoning": "Full diagnostic reasoning chain."\n}\n```'
```

---

## Task 4: Create MCP Tool-Level Smoke Tests

Create `tests/e2e/test_adaptive_ingest_mcp.py` — these test the MCP tool registration and the REST wrapper:

**Class: TestMCPToolRegistration**
- `test_ingest_from_healthex_is_registered`: Import the MCP app and verify `ingest_from_healthex` is in the tool list
- `test_rest_wrapper_exists`: Verify the `/tools/ingest_from_healthex` route exists

**Class: TestRESTEndpoint** (if httpx is available):
- `test_post_format_a_via_rest`: POST to `/tools/ingest_from_healthex` with Format A payload → verify 200 response with `format_detected`
- `test_post_invalid_resource_type`: POST with `resource_type="invalid"` → verify error response

---

## Task 5: Edge Case Tests

Create `ingestion/tests/test_edge_cases.py`:

**Class: TestEdgeCases**
- `test_empty_string_input`: `adaptive_parse("", "labs")` → 0 rows, no crash
- `test_none_coerced_to_string`: Handle case where fhir_json might be "None" or "null"
- `test_very_large_payload_truncated`: 100KB+ string → verify LLM fallback truncates to 8000 chars (mock the anthropic call)
- `test_malformed_json_no_crash`: `adaptive_parse("{broken json", "labs")` → unknown format, 0 rows
- `test_binary_garbage_no_crash`: `adaptive_parse("\x00\x01\xff", "labs")` → unknown format, 0 rows
- `test_nested_fhir_bundle_in_bundle`: A Bundle containing another Bundle entry → should not infinite loop
- `test_format_b_with_missing_dict_definitions`: Compressed table with `@N` refs but no `D:` dictionary → should gracefully degrade
- `test_format_a_missing_section`: Plain text with PATIENT header but no CONDITIONS section → 0 rows for conditions, no crash
- `test_format_c_single_key_value`: `"resourceType is Observation"` with no other fields → should not crash
- `test_medications_not_in_valid_types_but_handled`: resource_type="medications" is valid — verify it works for all formats
- `test_summary_with_mixed_formats`: resource_type="summary" where the raw payload is Format A (plain text) → should extract all sub-types

**Class: TestConcurrency** (if pytest-asyncio available):
- `test_parallel_ingest_different_resource_types`: Ingest conditions, labs, medications concurrently for the same patient → all succeed, no race condition on source_freshness update

---

## Task 6: Performance Benchmark (Optional)

Create `ingestion/tests/test_performance.py`:

```python
import time

def test_format_detection_is_fast():
    """Format detection should be < 1ms per call."""
    from ingestion.adapters.healthex.format_detector import detect_format
    start = time.perf_counter()
    for _ in range(1000):
        detect_format(SAMPLE_FORMAT_A)
    elapsed = time.perf_counter() - start
    assert elapsed < 1.0, f"1000 detections took {elapsed:.3f}s (> 1s)"

def test_parser_a_is_fast():
    """Format A parsing should be < 5ms per call."""
    from ingestion.adapters.healthex.parsers.format_a_parser import parse_plain_text_summary
    start = time.perf_counter()
    for _ in range(1000):
        parse_plain_text_summary(SAMPLE_FORMAT_A, "conditions")
    elapsed = time.perf_counter() - start
    assert elapsed < 5.0, f"1000 parses took {elapsed:.3f}s (> 5s)"
```

---

## Run Commands

```bash
# All new unit tests (no DB required)
pytest ingestion/tests/test_format_detector.py ingestion/tests/test_parsers.py ingestion/tests/test_adaptive_ingest.py ingestion/tests/test_edge_cases.py server/deliberation/tests/test_json_utils.py -v

# Integration tests (requires DATABASE_URL)
pytest tests/phase1/test_adaptive_ingest_integration.py -v
pytest tests/phase1/test_deliberation_fence_strip.py -v

# Existing tests still pass
pytest tests/phase1/test_healthex_ingest_integration.py -v
pytest ingestion/tests/test_healthex_registration.py -v

# Full suite
pytest ingestion/tests/ server/deliberation/tests/ tests/phase1/ -v --tb=short
```

---

## Success Criteria

1. **All 54 existing new tests pass** (format_detector, parsers, adaptive_ingest, json_utils)
2. **All new integration tests pass** against the live database
3. **Old integration tests updated** to reflect new pipeline behavior (no more `note` field, `records_written` may be > 0 for formats that are now parsed)
4. **Format A (plain text)**: Conditions, labs, encounters extracted from `get_health_summary` output
5. **Format B (compressed table)**: Conditions with ICD-10/SNOMED codes extracted from `get_conditions` output
6. **Format C (flat FHIR text)**: Lab observations extracted from `search` output
7. **Format D (FHIR Bundle)**: All resource types extracted, component observations (BP) exploded to separate rows
8. **JSON dict arrays**: All resource types handled with flexible key aliasing
9. **Double-encoded strings**: Unwrapped and parsed instead of silently cached with 0 records
10. **CrossCritique fence stripping**: Fenced JSON parses correctly for all 3 Pydantic models
11. **patient_id stability**: Never overridden by payload content
12. **Write verification**: `verified_counts` in response match actual DB state
13. **No crashes** on any edge case input (empty, null, binary, malformed)
