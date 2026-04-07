"""Tests for all deterministic HealthEx parsers."""
import json
import pytest

from ingestion.adapters.healthex.parsers.format_a_parser import parse_plain_text_summary
from ingestion.adapters.healthex.parsers.format_b_parser import parse_compressed_table
from ingestion.adapters.healthex.parsers.format_c_parser import parse_flat_fhir_text
from ingestion.adapters.healthex.parsers.format_d_parser import parse_fhir_bundle
from ingestion.adapters.healthex.parsers.json_dict_parser import parse_json_dict_arrays


# ── Format A: Plain Text Summary ────────────────────────────────────────────

SAMPLE_PLAIN_TEXT = """PATIENT: Ali Omrani, DOB 1987-03-25
PROVIDERS: Stanford Health Care
CONDITIONS(4/10): Active: BMI 34.0-34.9,adult@Stanford Health Care 2019-01-11 | Active: Prediabetes@Stanford Health Care 2017-04-25 | Active: Fatty liver@Stanford Health Care 2017-01-01
LABS(96): Hemoglobin A1c:4.8 %(ref:<5.7) 2025-07-11@Stanford Health Care[totalrecords:9] | LDL Cholesterol:104 mg/dL(ref:<100) 2025-07-11@Stanford Health Care[OutOfRange][totalrecords:8]
ALLERGIES(1): No Known Allergies 2015-07-21@Stanford
IMMUNIZATIONS(17): Flu vaccine (IIV4) 2023-12-13@Stanford Health Care | COVID-19 mRNA 2022-10-15@Stanford Health Care
CLINICAL VISITS(35): Office Visit:description:Internal Medicine,diagnoses:Fatty liver 2025-06-26@Stanford Health Care | Office Visit:description:Endocrinology,diagnoses:Prediabetes 2023-12-13@Stanford Health Care"""


class TestFormatAParser:
    def test_conditions_extraction(self):
        rows = parse_plain_text_summary(SAMPLE_PLAIN_TEXT, "conditions")
        assert len(rows) >= 3
        names = [r["name"] for r in rows]
        assert "BMI 34.0-34.9,adult" in names
        assert "Prediabetes" in names
        assert all(r.get("status") == "active" for r in rows)

    def test_labs_extraction(self):
        rows = parse_plain_text_summary(SAMPLE_PLAIN_TEXT, "labs")
        assert len(rows) >= 2
        hba1c = [r for r in rows if "A1c" in r.get("name", "")]
        assert len(hba1c) == 1
        assert hba1c[0]["value"] == "4.8"
        assert hba1c[0]["date"] == "2025-07-11"

    def test_encounters_extraction(self):
        rows = parse_plain_text_summary(SAMPLE_PLAIN_TEXT, "encounters")
        assert len(rows) >= 2
        assert rows[0]["encounter_date"] == "2025-06-26"

    def test_immunizations_extraction(self):
        rows = parse_plain_text_summary(SAMPLE_PLAIN_TEXT, "immunizations")
        assert len(rows) >= 2
        names = [r["name"] for r in rows]
        assert any("Flu" in n for n in names)

    def test_unknown_resource_type(self):
        rows = parse_plain_text_summary(SAMPLE_PLAIN_TEXT, "allergies")
        assert rows == []


# ── Format B: Compressed Dictionary Table ────────────────────────────────────

SAMPLE_COMPRESSED = """#Conditions 5y|Total:2
D:1=2019-01-11|2=2017-04-25|
C:1=BMI 34.0-34.9,adult|2=Prediabetes|
S:1=active|
Date|Condition|ClinicalStatus|OnsetDate|AbatementDate|SNOMED|ICD10|PreferredCode|PreferredSystem|Recorder|Asserter|Encounter
@1|@1|@1|2019-01-11||162864005|Z68.34|||||
|@2|@1|2017-04-25||714628002|R73.03|||||"""


class TestFormatBParser:
    def test_conditions_from_compressed_table(self):
        rows = parse_compressed_table(SAMPLE_COMPRESSED, "conditions")
        assert len(rows) >= 1
        names = [r["name"] for r in rows]
        assert "BMI 34.0-34.9,adult" in names or "Prediabetes" in names

    def test_icd10_extracted(self):
        rows = parse_compressed_table(SAMPLE_COMPRESSED, "conditions")
        icd_codes = [r.get("icd10", "") for r in rows]
        assert any(c in ("Z68.34", "R73.03") for c in icd_codes)

    def test_no_crash_on_empty_input(self):
        rows = parse_compressed_table("", "conditions")
        assert rows == []


# ── Format C: Flat FHIR Text ────────────────────────────────────────────────

SAMPLE_FLAT_FHIR = """resourceType is Observation. id is fC2IoULh. status is final. code.coding[0].system is http://loinc.org. code.coding[0].code is 4548-4. code.text is Hemoglobin A1c. valueQuantity.value is 4.8. valueQuantity.unit is %. effectiveDateTime is 2025-07-11"""


class TestFormatCParser:
    def test_observation_extraction(self):
        rows = parse_flat_fhir_text(SAMPLE_FLAT_FHIR, "labs")
        assert len(rows) == 1
        assert rows[0]["test_name"] == "Hemoglobin A1c"
        assert rows[0]["value"] == "4.8"
        assert rows[0]["unit"] == "%"
        assert rows[0]["date"] == "2025-07-11"

    def test_multiple_resources(self):
        multi = (
            "resourceType is Observation. code.text is HbA1c. "
            "valueQuantity.value is 4.8. effectiveDateTime is 2025-07-11. "
            "resourceType is Observation. code.text is LDL. "
            "valueQuantity.value is 104. effectiveDateTime is 2025-07-11"
        )
        rows = parse_flat_fhir_text(multi, "labs")
        assert len(rows) == 2

    def test_wrong_resource_type(self):
        rows = parse_flat_fhir_text(SAMPLE_FLAT_FHIR, "conditions")
        assert rows == []


# ── Format D: FHIR Bundle JSON ──────────────────────────────────────────────

class TestFormatDParser:
    def test_labs_from_bundle(self):
        bundle = {
            "resourceType": "Bundle",
            "entry": [
                {
                    "resource": {
                        "resourceType": "Observation",
                        "code": {"text": "HbA1c", "coding": [{"code": "4548-4"}]},
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
        }
        rows = parse_fhir_bundle(bundle, "labs")
        assert len(rows) == 2
        names = {r["test_name"] for r in rows}
        assert "HbA1c" in names
        assert "LDL" in names

    def test_conditions_from_bundle(self):
        bundle = {
            "resourceType": "Bundle",
            "entry": [
                {
                    "resource": {
                        "resourceType": "Condition",
                        "code": {"text": "Prediabetes"},
                        "clinicalStatus": {"coding": [{"code": "active"}]},
                        "onsetDateTime": "2017-04-25",
                    }
                }
            ],
        }
        rows = parse_fhir_bundle(bundle, "conditions")
        assert len(rows) == 1
        assert rows[0]["name"] == "Prediabetes"
        assert rows[0]["status"] == "active"
        assert rows[0]["onset_date"] == "2017-04-25"

    def test_component_observations_bp(self):
        """Blood pressure observations have component-based values.
        parse_fhir_bundle returns 1 row with _extra_rows for components;
        flattening happens in adaptive_parse()."""
        bundle = {
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
        }
        rows = parse_fhir_bundle(bundle, "labs")
        assert len(rows) == 1
        assert "Systolic" in rows[0]["test_name"]
        # Second component is in _extra_rows
        extra = rows[0].get("_extra_rows", [])
        assert len(extra) == 1
        assert "Diastolic" in extra[0]["test_name"]

    def test_empty_bundle(self):
        bundle = {"resourceType": "Bundle", "entry": []}
        rows = parse_fhir_bundle(bundle, "labs")
        assert rows == []

    def test_medications_from_bundle(self):
        bundle = {
            "resourceType": "Bundle",
            "entry": [
                {
                    "resource": {
                        "resourceType": "MedicationRequest",
                        "medicationCodeableConcept": {"text": "Metformin"},
                        "status": "active",
                        "authoredOn": "2020-01-15",
                    }
                }
            ],
        }
        rows = parse_fhir_bundle(bundle, "medications")
        assert len(rows) == 1
        assert rows[0]["name"] == "Metformin"


# ── JSON Dict Array Parser ──────────────────────────────────────────────────

class TestJsonDictParser:
    def test_conditions_array(self):
        payload = {
            "conditions": [
                {"name": "Prediabetes", "onset": "2017-04-25", "status": "active"},
                {"name": "Fatty liver", "onset": "2017-01-01", "status": "active"},
            ]
        }
        rows = parse_json_dict_arrays(payload, "conditions")
        assert len(rows) == 2
        assert rows[0]["name"] == "Prediabetes"
        assert rows[0]["onset_date"] == "2017-04-25"

    def test_labs_array(self):
        payload = {
            "labs": [
                {"test_name": "HbA1c", "value": "4.8", "unit": "%", "date": "2025-07-11"},
            ]
        }
        rows = parse_json_dict_arrays(payload, "labs")
        assert len(rows) == 1
        assert rows[0]["test_name"] == "HbA1c"

    def test_medications_array(self):
        payload = {
            "medications": [
                {"name": "Metformin", "status": "active", "start_date": "2020-01-15"},
            ]
        }
        rows = parse_json_dict_arrays(payload, "medications")
        assert len(rows) == 1
        assert rows[0]["name"] == "Metformin"

    def test_encounters_with_visits_key(self):
        payload = {
            "visits": [
                {"date": "2025-06-26", "type": "Office Visit"},
            ]
        }
        rows = parse_json_dict_arrays(payload, "encounters")
        assert len(rows) == 1
        assert rows[0]["encounter_date"] == "2025-06-26"

    def test_items_key_from_bare_array(self):
        """When detect_format wraps a bare array in {"_items": [...]}"""
        payload = {
            "_items": [
                {"name": "Prediabetes", "status": "active"},
            ]
        }
        rows = parse_json_dict_arrays(payload, "conditions")
        assert len(rows) == 1

    def test_flexible_key_names(self):
        """Should handle alternative key names like 'onset' vs 'onset_date'."""
        payload = {
            "conditions": [
                {"name": "Diabetes", "onset": "2020-01-01"},
            ]
        }
        rows = parse_json_dict_arrays(payload, "conditions")
        assert rows[0]["onset_date"] == "2020-01-01"

    def test_empty_dict(self):
        rows = parse_json_dict_arrays({}, "conditions")
        assert rows == []
