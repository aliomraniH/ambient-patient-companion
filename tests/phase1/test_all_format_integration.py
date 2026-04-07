"""Task 2 — Integration tests: all 5 HealthEx formats against a live DB.

Calls ingest_from_healthex() end-to-end with real HealthEx API sample payloads
for every format the adaptive pipeline handles.  Each class targets one format
and verifies:
  - status == 'ok'
  - format_detected matches the expected format enum value
  - total_written >= expected row count
  - records_written is a dict (adaptive pipeline shape)

Requires DATABASE_URL (skipped automatically without it).
"""

from __future__ import annotations

import json

import pytest

from server.mcp_server import ingest_from_healthex


# ── Format A: Plain Text Summary (get_health_summary output) ─────────────────

FORMAT_A_SUMMARY = (
    "PATIENT: Ali Omrani, DOB 1987-03-25\n"
    "PROVIDERS: Stanford Health Care\n"
    "CONDITIONS(4/10): Active: BMI 34.0-34.9,adult@Stanford Health Care 2019-01-11 | "
    "Active: Prediabetes@Stanford Health Care 2017-04-25 | "
    "Active: Fatty liver@Stanford Health Care 2017-01-01\n"
    "LABS(96): Hemoglobin A1c:4.8 %(ref:<5.7) 2025-07-11@Stanford Health Care"
    "[totalrecords:9] | "
    "LDL Cholesterol:104 mg/dL(ref:<100) 2025-07-11@Stanford Health Care"
    "[OutOfRange][totalrecords:8]\n"
    "MEDICATIONS(5): Metformin 500 mg 2x/day:active 2020-01-15@Stanford Health Care | "
    "Lisinopril 10 mg 1x/day:active 2019-06-01@Stanford Health Care\n"
    "IMMUNIZATIONS(17): Flu vaccine (IIV4) 2023-12-13@Stanford Health Care | "
    "COVID-19 mRNA 2022-10-15@Stanford Health Care\n"
    "CLINICAL VISITS(35): Office Visit:description:Internal Medicine,"
    "diagnoses:Fatty liver 2025-06-26@Stanford Health Care | "
    "Office Visit:description:Endocrinology,diagnoses:Prediabetes "
    "2023-12-13@Stanford Health Care"
)


class TestFormatAIntegration:
    """Format A (plain text summary) — ingest conditions and labs end-to-end."""

    @pytest.mark.asyncio
    async def test_format_a_conditions_detected_and_written(self, healthex_patient):
        raw = await ingest_from_healthex(
            patient_id=healthex_patient,
            resource_type="conditions",
            fhir_json=FORMAT_A_SUMMARY,
        )
        result = json.loads(raw)
        assert result["status"] == "ok", f"Expected ok: {result}"
        assert result["format_detected"] == "plain_text_summary", (
            f"Expected plain_text_summary, got {result['format_detected']!r}"
        )
        assert result["parser_used"] == "format_a_plain_text", (
            f"Expected format_a_plain_text, got {result['parser_used']!r}"
        )
        assert isinstance(result["records_written"], dict)
        assert result["total_written"] >= 3, (
            f"Expected ≥3 condition rows from Format A summary, "
            f"got total_written={result['total_written']}"
        )

    @pytest.mark.asyncio
    async def test_format_a_labs_detected_and_written(self, healthex_patient):
        raw = await ingest_from_healthex(
            patient_id=healthex_patient,
            resource_type="labs",
            fhir_json=FORMAT_A_SUMMARY,
        )
        result = json.loads(raw)
        assert result["status"] == "ok", f"Expected ok: {result}"
        assert result["format_detected"] == "plain_text_summary"
        assert result["total_written"] >= 2, (
            f"Expected ≥2 lab rows (HbA1c + LDL), got total_written={result['total_written']}"
        )

    @pytest.mark.asyncio
    async def test_format_a_encounters_detected_and_written(self, healthex_patient):
        raw = await ingest_from_healthex(
            patient_id=healthex_patient,
            resource_type="encounters",
            fhir_json=FORMAT_A_SUMMARY,
        )
        result = json.loads(raw)
        assert result["status"] == "ok", f"Expected ok: {result}"
        assert result["format_detected"] == "plain_text_summary"
        assert result["total_written"] >= 1, (
            f"Expected ≥1 encounter row from Format A, got total_written={result['total_written']}"
        )

    @pytest.mark.asyncio
    async def test_format_a_response_always_has_metadata_fields(self, healthex_patient):
        """All adaptive pipeline responses must include the full metadata shape."""
        raw = await ingest_from_healthex(
            patient_id=healthex_patient,
            resource_type="conditions",
            fhir_json=FORMAT_A_SUMMARY,
        )
        result = json.loads(raw)
        for field in ("status", "format_detected", "parser_used",
                      "records_written", "total_written", "duration_ms"):
            assert field in result, f"Missing field {field!r} in response"
        assert isinstance(result["duration_ms"], int)


# ── Format B: Compressed Dictionary Table (#-prefixed) ───────────────────────

FORMAT_B_CONDITIONS = (
    "#Conditions 5y|Total:4\n"
    "D:1=2019-01-11|2=2017-04-25|3=2017-01-01|4=2020-03-15|\n"
    "C:1=BMI 34.0-34.9,adult|2=Prediabetes|3=Fatty liver|4=Hypertension|\n"
    "S:1=active|\n"
    "Date|Condition|ClinicalStatus|OnsetDate|AbatementDate|SNOMED|ICD10|"
    "PreferredCode|PreferredSystem|Recorder|Asserter|Encounter\n"
    "@1|@1|@1|2019-01-11||162864005|Z68.34|||||\n"
    "|@2|@1|2017-04-25||714628002|R73.03|||||\n"
    "|@3|@1|2017-01-01||197321007|K76.0|||||\n"
    "|@4|@1|2020-03-15||38341003|I10|||||"
)

FORMAT_B_LABS = (
    "#Labs 2y|Total:3\n"
    "T:1=Hemoglobin A1c|2=LDL Cholesterol|3=eGFR|\n"
    "V:1=4.8|2=104|3=78|\n"
    "U:1=%|2=mg/dL|3=mL/min|\n"
    "Dt:1=2025-07-11|2=2025-07-11|3=2025-06-01|\n"
    "Date|Test|Value|Unit|ReferenceRange|Status|Interpretation\n"
    "@1|@1|@1|@1|<5.7|final|normal\n"
    "|@2|@2|@2|<100|final|abnormal\n"
    "|@3|@3|@3|>60|final|normal"
)


class TestFormatBIntegration:
    """Format B (compressed dictionary table) — real HealthEx tool output."""

    @pytest.mark.asyncio
    async def test_format_b_conditions_4_rows(self, healthex_patient):
        raw = await ingest_from_healthex(
            patient_id=healthex_patient,
            resource_type="conditions",
            fhir_json=FORMAT_B_CONDITIONS,
        )
        result = json.loads(raw)
        assert result["status"] == "ok", f"Expected ok: {result}"
        assert result["format_detected"] == "compressed_table"
        assert result["parser_used"] == "format_b_compressed_table"
        assert result["total_written"] >= 1, (
            f"Expected ≥1 condition row from Format B, got total_written={result['total_written']}"
        )

    @pytest.mark.asyncio
    async def test_format_b_labs_detected_and_written(self, healthex_patient):
        raw = await ingest_from_healthex(
            patient_id=healthex_patient,
            resource_type="labs",
            fhir_json=FORMAT_B_LABS,
        )
        result = json.loads(raw)
        assert result["status"] == "ok", f"Expected ok: {result}"
        assert result["format_detected"] == "compressed_table"
        assert isinstance(result["records_written"], dict)

    @pytest.mark.asyncio
    async def test_format_b_double_encoded_string_also_detected(self, healthex_patient):
        """Format B payload wrapped in json.dumps() (old API pattern) still detected."""
        raw = await ingest_from_healthex(
            patient_id=healthex_patient,
            resource_type="conditions",
            fhir_json=json.dumps(FORMAT_B_CONDITIONS),
        )
        result = json.loads(raw)
        assert result["status"] == "ok"
        assert result["format_detected"] == "compressed_table"


# ── Format C: Flat FHIR Text (key is value sentences) ────────────────────────

FORMAT_C_LABS = (
    "resourceType is Observation. id is fC2IoULh. status is final. "
    "code.coding[0].system is http://loinc.org. code.coding[0].code is 4548-4. "
    "code.text is Hemoglobin A1c. valueQuantity.value is 4.8. "
    "valueQuantity.unit is %. effectiveDateTime is 2025-07-11. "
    "resourceType is Observation. id is mK8pQrTe. status is final. "
    "code.coding[0].system is http://loinc.org. code.coding[0].code is 2089-1. "
    "code.text is LDL Cholesterol. valueQuantity.value is 104. "
    "valueQuantity.unit is mg/dL. effectiveDateTime is 2025-07-11"
)

FORMAT_C_CONDITIONS = (
    "resourceType is Condition. id is cond123. "
    "clinicalStatus.coding[0].code is active. "
    "code.coding[0].code is 714628002. code.text is Prediabetes. "
    "onsetDateTime is 2017-04-25. recordedDate is 2017-04-25"
)


class TestFormatCIntegration:
    """Format C (flat FHIR key=value text) — end-to-end ingest."""

    @pytest.mark.asyncio
    async def test_format_c_labs_two_observations(self, healthex_patient):
        raw = await ingest_from_healthex(
            patient_id=healthex_patient,
            resource_type="labs",
            fhir_json=FORMAT_C_LABS,
        )
        result = json.loads(raw)
        assert result["status"] == "ok", f"Expected ok: {result}"
        assert result["format_detected"] == "flat_fhir_text", (
            f"Expected flat_fhir_text, got {result['format_detected']!r}"
        )
        assert result["parser_used"] == "format_c_flat_fhir_text"
        assert result["total_written"] >= 1, (
            f"Expected ≥1 lab row from Format C, got total_written={result['total_written']}"
        )

    @pytest.mark.asyncio
    async def test_format_c_condition_detected(self, healthex_patient):
        raw = await ingest_from_healthex(
            patient_id=healthex_patient,
            resource_type="conditions",
            fhir_json=FORMAT_C_CONDITIONS,
        )
        result = json.loads(raw)
        assert result["status"] == "ok", f"Expected ok: {result}"
        assert result["format_detected"] == "flat_fhir_text"

    @pytest.mark.asyncio
    async def test_format_c_wrong_resource_type_writes_zero(self, healthex_patient):
        """Passing a Condition payload as labs resource_type should write 0 rows."""
        raw = await ingest_from_healthex(
            patient_id=healthex_patient,
            resource_type="labs",
            fhir_json=FORMAT_C_CONDITIONS,
        )
        result = json.loads(raw)
        assert result["status"] == "ok"
        assert result["format_detected"] == "flat_fhir_text"
        assert result["total_written"] == 0, (
            f"Condition payload with labs resource_type should write 0 rows, "
            f"got {result['total_written']}"
        )


# ── Format D: FHIR Bundle JSON ────────────────────────────────────────────────

FORMAT_D_LAB_BUNDLE = {
    "resourceType": "Bundle",
    "type": "searchset",
    "total": 2,
    "entry": [
        {
            "resource": {
                "resourceType": "Observation",
                "id": "obs-hba1c-001",
                "status": "final",
                "code": {
                    "coding": [{"system": "http://loinc.org", "code": "4548-4",
                                "display": "Hemoglobin A1c"}],
                    "text": "Hemoglobin A1c",
                },
                "valueQuantity": {"value": 7.2, "unit": "%",
                                  "system": "http://unitsofmeasure.org", "code": "%"},
                "referenceRange": [{"high": {"value": 5.7, "unit": "%"}}],
                "effectiveDateTime": "2025-07-11T10:00:00Z",
            }
        },
        {
            "resource": {
                "resourceType": "Observation",
                "id": "obs-ldl-002",
                "status": "final",
                "code": {
                    "coding": [{"system": "http://loinc.org", "code": "2089-1",
                                "display": "LDL Cholesterol"}],
                    "text": "LDL Cholesterol",
                },
                "valueQuantity": {"value": 104, "unit": "mg/dL"},
                "referenceRange": [{"high": {"value": 100, "unit": "mg/dL"}}],
                "effectiveDateTime": "2025-07-11T10:00:00Z",
                "interpretation": [{"coding": [{"code": "H", "display": "High"}]}],
            }
        },
    ],
}

FORMAT_D_CONDITION_BUNDLE = {
    "resourceType": "Bundle",
    "type": "searchset",
    "entry": [
        {
            "resource": {
                "resourceType": "Condition",
                "id": "cond-prediabetes",
                "clinicalStatus": {
                    "coding": [{"system": "http://terminology.hl7.org/CodeSystem/condition-clinical",
                                "code": "active"}]
                },
                "code": {
                    "coding": [{"system": "http://snomed.info/sct", "code": "714628002",
                                "display": "Prediabetes"}],
                    "text": "Prediabetes",
                },
                "onsetDateTime": "2017-04-25",
                "recordedDate": "2017-04-25",
            }
        },
        {
            "resource": {
                "resourceType": "Condition",
                "id": "cond-htn",
                "clinicalStatus": {
                    "coding": [{"system": "http://terminology.hl7.org/CodeSystem/condition-clinical",
                                "code": "active"}]
                },
                "code": {
                    "coding": [{"system": "http://snomed.info/sct", "code": "38341003",
                                "display": "Hypertension"}],
                    "text": "Hypertension",
                },
                "onsetDateTime": "2019-06-15",
            }
        },
    ],
}


class TestFormatDIntegration:
    """Format D (FHIR R4 Bundle JSON) — real HealthEx FHIR server payloads."""

    @pytest.mark.asyncio
    async def test_format_d_labs_bundle_two_observations(self, healthex_patient):
        raw = await ingest_from_healthex(
            patient_id=healthex_patient,
            resource_type="labs",
            fhir_json=json.dumps(FORMAT_D_LAB_BUNDLE),
        )
        result = json.loads(raw)
        assert result["status"] == "ok", f"Expected ok: {result}"
        assert result["format_detected"] == "fhir_bundle_json", (
            f"Expected fhir_bundle_json, got {result['format_detected']!r}"
        )
        assert "fhir_bundle" in result["parser_used"], (
            f"Expected parser_used to reference fhir_bundle, got {result['parser_used']!r}"
        )
        assert result["total_written"] >= 2, (
            f"Expected ≥2 lab rows from FHIR Bundle, got total_written={result['total_written']}"
        )

    @pytest.mark.asyncio
    async def test_format_d_conditions_bundle_two_rows(self, healthex_patient):
        raw = await ingest_from_healthex(
            patient_id=healthex_patient,
            resource_type="conditions",
            fhir_json=json.dumps(FORMAT_D_CONDITION_BUNDLE),
        )
        result = json.loads(raw)
        assert result["status"] == "ok", f"Expected ok: {result}"
        assert result["format_detected"] == "fhir_bundle_json"
        assert result["total_written"] >= 2, (
            f"Expected ≥2 condition rows from FHIR Bundle, got total_written={result['total_written']}"
        )

    @pytest.mark.asyncio
    async def test_format_d_single_resource_wrapped_as_bundle(self, healthex_patient):
        """Single FHIR resource (not a Bundle) is auto-wrapped and processed."""
        single_obs = json.dumps({
            "resourceType": "Observation",
            "code": {"text": "eGFR", "coding": [{"code": "33914-3"}]},
            "valueQuantity": {"value": 78, "unit": "mL/min"},
            "effectiveDateTime": "2025-06-01",
        })
        raw = await ingest_from_healthex(
            patient_id=healthex_patient,
            resource_type="labs",
            fhir_json=single_obs,
        )
        result = json.loads(raw)
        assert result["status"] == "ok"
        assert result["format_detected"] == "fhir_bundle_json", (
            "Single FHIR resource should be auto-wrapped as a Bundle"
        )
        assert result["total_written"] >= 1

    @pytest.mark.asyncio
    async def test_format_d_empty_bundle_writes_zero(self, healthex_patient):
        """Empty FHIR Bundle should write 0 rows and return ok."""
        empty_bundle = json.dumps({
            "resourceType": "Bundle",
            "type": "searchset",
            "entry": [],
        })
        raw = await ingest_from_healthex(
            patient_id=healthex_patient,
            resource_type="labs",
            fhir_json=empty_bundle,
        )
        result = json.loads(raw)
        assert result["status"] == "ok"
        assert result["format_detected"] == "fhir_bundle_json"
        assert result["total_written"] == 0


# ── JSON Dict Array format ────────────────────────────────────────────────────

FORMAT_JSON_DICT_CONDITIONS = {
    "conditions": [
        {"name": "Prediabetes", "onset": "2017-04-25", "status": "active",
         "icd10": "R73.03", "snomed": "714628002"},
        {"name": "Hypertension", "onset": "2019-06-15", "status": "active",
         "icd10": "I10", "snomed": "38341003"},
        {"name": "Fatty liver", "onset_date": "2017-01-01", "status": "active",
         "icd10": "K76.0"},
    ]
}

FORMAT_JSON_DICT_LABS = {
    "labs": [
        {"test_name": "Hemoglobin A1c", "value": "7.2", "unit": "%",
         "date": "2025-07-11", "reference_range": "<5.7"},
        {"test_name": "LDL Cholesterol", "value": "104", "unit": "mg/dL",
         "date": "2025-07-11", "in_range": False},
    ],
    "observations": [
        {"test_name": "eGFR", "value": "78", "unit": "mL/min", "date": "2025-06-01"},
    ],
}

FORMAT_JSON_DICT_MEDICATIONS = {
    "medications": [
        {"name": "Metformin", "dose": "500 mg", "frequency": "twice daily",
         "status": "active", "start_date": "2020-01-15"},
        {"name": "Lisinopril", "dose": "10 mg", "frequency": "once daily",
         "status": "active", "start_date": "2019-06-01"},
    ]
}


class TestJsonDictIntegration:
    """JSON Dict Array format — Claude-constructed payloads and tool variations."""

    @pytest.mark.asyncio
    async def test_json_dict_conditions_three_rows(self, healthex_patient):
        raw = await ingest_from_healthex(
            patient_id=healthex_patient,
            resource_type="conditions",
            fhir_json=json.dumps(FORMAT_JSON_DICT_CONDITIONS),
        )
        result = json.loads(raw)
        assert result["status"] == "ok", f"Expected ok: {result}"
        assert result["format_detected"] == "json_dict_array", (
            f"Expected json_dict_array, got {result['format_detected']!r}"
        )
        assert "json_dict" in result["parser_used"], (
            f"Expected parser_used to reference json_dict, got {result['parser_used']!r}"
        )
        assert result["total_written"] >= 2, (
            f"Expected ≥2 conditions, got total_written={result['total_written']}"
        )

    @pytest.mark.asyncio
    async def test_json_dict_labs_with_mixed_keys(self, healthex_patient):
        """Payload uses both 'labs' and 'observations' keys — only 'labs' key is used."""
        raw = await ingest_from_healthex(
            patient_id=healthex_patient,
            resource_type="labs",
            fhir_json=json.dumps(FORMAT_JSON_DICT_LABS),
        )
        result = json.loads(raw)
        assert result["status"] == "ok"
        assert result["format_detected"] == "json_dict_array"
        assert result["total_written"] >= 1, (
            f"Expected ≥1 lab row from JSON dict, got total_written={result['total_written']}"
        )

    @pytest.mark.asyncio
    async def test_json_dict_medications_two_rows(self, healthex_patient):
        raw = await ingest_from_healthex(
            patient_id=healthex_patient,
            resource_type="medications",
            fhir_json=json.dumps(FORMAT_JSON_DICT_MEDICATIONS),
        )
        result = json.loads(raw)
        assert result["status"] == "ok"
        assert result["format_detected"] == "json_dict_array"
        assert result["total_written"] >= 2, (
            f"Expected ≥2 medication rows, got total_written={result['total_written']}"
        )

    @pytest.mark.asyncio
    async def test_json_dict_onset_alias_key_normalised(self, healthex_patient):
        """'onset' key (not 'onset_date') must be aliased correctly by the parser."""
        payload = json.dumps({
            "conditions": [
                {"name": "Type 2 Diabetes", "onset": "2020-06-01", "status": "active"},
            ]
        })
        raw = await ingest_from_healthex(
            patient_id=healthex_patient,
            resource_type="conditions",
            fhir_json=payload,
        )
        result = json.loads(raw)
        assert result["status"] == "ok"
        assert result["total_written"] >= 1, (
            "onset key alias should produce 1 condition row"
        )
