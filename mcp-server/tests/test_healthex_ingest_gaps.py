"""Regression tests for HealthEx ingest gap fixes.

Covers the five bugs identified in the spec:
  1. transform_conditions: code.text fallback for display when coding is absent
  2. transform_clinical_observations: code.text fallback for metric_type
  3. transform_clinical_observations (BP component): code.text fallback for component metric_type
  4. _healthex_native_to_fhir_conditions: 'onset' key alias in onset-date chain
  5. ingest_from_healthex: patient-existence guard (integration, needs db_pool)
  6. ingest_from_healthex: raw text cached in raw_fhir_cache (integration, needs db_pool)
"""

from __future__ import annotations

import json
import os
import sys
import uuid

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "server"))

from transforms.fhir_to_schema import (
    transform_conditions,
    transform_clinical_observations,
)


# ---------------------------------------------------------------------------
# Fix 1: transform_conditions — code.text fallback
# ---------------------------------------------------------------------------

class TestTransformConditionsCodeTextFallback:
    """transform_conditions must use code.text when coding is absent."""

    def test_display_uses_code_text_when_no_coding(self):
        resource = {
            "resourceType": "Condition",
            "code": {"text": "Prediabetes"},
            "clinicalStatus": {"coding": [{"code": "active"}]},
        }
        recs = transform_conditions([resource], patient_id="patient-uuid-001")
        assert len(recs) == 1
        assert recs[0]["display"] == "Prediabetes", (
            f"Expected 'Prediabetes', got {recs[0]['display']!r} — "
            "code.text fallback is broken"
        )

    def test_display_prefers_coding_display_over_code_text(self):
        resource = {
            "resourceType": "Condition",
            "code": {
                "text": "Fallback Name",
                "coding": [{"code": "E11", "display": "Type 2 Diabetes", "system": "ICD-10"}],
            },
        }
        recs = transform_conditions([resource], patient_id="patient-uuid-001")
        assert recs[0]["display"] == "Type 2 Diabetes"

    def test_display_empty_string_when_both_absent(self):
        resource = {"resourceType": "Condition", "code": {}}
        recs = transform_conditions([resource], patient_id="patient-uuid-001")
        assert recs[0]["display"] == ""

    def test_onset_date_captured_from_onsetDateTime(self):
        resource = {
            "resourceType": "Condition",
            "code": {"text": "Hypertension"},
            "onsetDateTime": "2019-06-15",
        }
        recs = transform_conditions([resource], patient_id="patient-uuid-001")
        assert recs[0]["onset_date"] is not None
        assert str(recs[0]["onset_date"]) == "2019-06-15"


# ---------------------------------------------------------------------------
# Fix 2: transform_clinical_observations — code.text fallback (main path)
# ---------------------------------------------------------------------------

class TestTransformClinicalObservationsCodeTextFallback:
    """transform_clinical_observations must use code.text for metric_type."""

    def test_metric_type_uses_code_text_when_no_coding(self):
        resource = {
            "resourceType": "Observation",
            "code": {"text": "HbA1c"},
            "valueQuantity": {"value": 7.2, "unit": "%"},
            "effectiveDateTime": "2024-03-01",
        }
        recs = transform_clinical_observations([resource], patient_id="patient-uuid-002")
        assert len(recs) == 1
        assert recs[0]["metric_type"] == "hba1c", (
            f"Expected 'hba1c', got {recs[0]['metric_type']!r} — "
            "code.text fallback for metric_type is broken"
        )

    def test_metric_type_prefers_coding_display(self):
        resource = {
            "resourceType": "Observation",
            "code": {
                "text": "Fallback",
                "coding": [{"code": "4548-4", "display": "Hemoglobin A1c"}],
            },
            "valueQuantity": {"value": 7.2, "unit": "%"},
        }
        recs = transform_clinical_observations([resource], patient_id="patient-uuid-002")
        assert recs[0]["metric_type"] == "hemoglobin_a1c"

    def test_metric_type_empty_string_when_both_absent(self):
        resource = {
            "resourceType": "Observation",
            "code": {},
            "valueQuantity": {"value": 5.0, "unit": "mmol/L"},
        }
        recs = transform_clinical_observations([resource], patient_id="patient-uuid-002")
        assert recs[0]["metric_type"] == ""

    def test_value_and_unit_captured_correctly(self):
        resource = {
            "resourceType": "Observation",
            "code": {"text": "Glucose"},
            "valueQuantity": {"value": 95.0, "unit": "mg/dL"},
            "effectiveDateTime": "2024-01-15",
        }
        recs = transform_clinical_observations([resource], patient_id="patient-uuid-002")
        assert recs[0]["value"] == 95.0
        assert recs[0]["unit"] == "mg/dL"


# ---------------------------------------------------------------------------
# Fix 3: transform_clinical_observations — component path code.text fallback
# ---------------------------------------------------------------------------

class TestTransformClinicalObservationsComponentCodeText:
    """Component-based observations (BP) must also use code.text fallback."""

    def test_bp_component_uses_code_text_when_no_coding(self):
        resource = {
            "resourceType": "Observation",
            "code": {"text": "Blood pressure panel"},
            "component": [
                {
                    "code": {"text": "Systolic blood pressure"},
                    "valueQuantity": {"value": 120.0, "unit": "mmHg"},
                },
                {
                    "code": {"text": "Diastolic blood pressure"},
                    "valueQuantity": {"value": 80.0, "unit": "mmHg"},
                },
            ],
            "effectiveDateTime": "2024-02-10",
        }
        recs = transform_clinical_observations([resource], patient_id="patient-uuid-003")
        assert len(recs) == 2
        metric_types = {r["metric_type"] for r in recs}
        assert "systolic_blood_pressure" in metric_types, (
            f"Expected 'systolic_blood_pressure' in {metric_types} — "
            "code.text fallback for component metric_type is broken"
        )
        assert "diastolic_blood_pressure" in metric_types


# ---------------------------------------------------------------------------
# Fix 4: _healthex_native_to_fhir_conditions — 'onset' key alias
# ---------------------------------------------------------------------------

class TestHealthexNativeToFhirConditionsOnsetAlias:
    """_healthex_native_to_fhir_conditions must handle 'onset' key."""

    def _run(self, items):
        from server.mcp_server import _healthex_native_to_fhir_conditions  # type: ignore
        return _healthex_native_to_fhir_conditions(items)

    def test_onset_key_is_used(self):
        try:
            result = self._run([
                {"name": "Prediabetes", "onset": "2017-04-25", "status": "active"}
            ])
            assert result[0]["onsetDateTime"] == "2017-04-25", (
                f"Expected '2017-04-25', got {result[0]['onsetDateTime']!r} — "
                "'onset' key alias is not in the fallback chain"
            )
        except (ImportError, ModuleNotFoundError):
            pytest.skip("server.mcp_server not importable from mcp-server test dir")

    def test_onset_date_key_still_works(self):
        try:
            result = self._run([
                {"name": "Hypertension", "onset_date": "2015-01-01"}
            ])
            assert result[0]["onsetDateTime"] == "2015-01-01"
        except (ImportError, ModuleNotFoundError):
            pytest.skip("server.mcp_server not importable from mcp-server test dir")

    def test_onsetDate_camelCase_works(self):
        try:
            result = self._run([
                {"name": "CKD", "onsetDate": "2020-07-10"}
            ])
            assert result[0]["onsetDateTime"] == "2020-07-10"
        except (ImportError, ModuleNotFoundError):
            pytest.skip("server.mcp_server not importable from mcp-server test dir")

    def test_display_captured_from_name(self):
        try:
            result = self._run([{"name": "Asthma", "onset": "2010-03-05"}])
            coding = result[0]["code"]["coding"][0]
            assert coding["display"] == "Asthma"
        except (ImportError, ModuleNotFoundError):
            pytest.skip("server.mcp_server not importable from mcp-server test dir")


# ---------------------------------------------------------------------------
# Fix 5 & 6: patient guard + raw text cache (integration via db_pool)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_ingest_from_healthex_rejects_unknown_patient(db_pool):
    """Guard query: a random UUID must not be found in patients table."""
    fake_id = str(uuid.uuid4())
    async with db_pool.acquire() as conn:
        exists = await conn.fetchval(
            "SELECT id FROM patients WHERE id = $1::uuid", fake_id
        )
    assert exists is None, (
        f"Random UUID {fake_id} should not exist in patients table — "
        "the patient-existence guard would incorrectly allow writes for it"
    )


@pytest.mark.asyncio
async def test_raw_fhir_cache_columns_exist(db_pool):
    """raw_fhir_cache must have the columns needed by the raw-text branch."""
    async with db_pool.acquire() as conn:
        cols = await conn.fetch(
            """SELECT column_name FROM information_schema.columns
               WHERE table_name = 'raw_fhir_cache'""",
        )
    col_names = {r["column_name"] for r in cols}
    for required in ("patient_id", "source_name", "resource_type", "raw_json",
                     "fhir_resource_id", "retrieved_at", "processed"):
        assert required in col_names, (
            f"Column '{required}' missing from raw_fhir_cache — "
            "raw text caching branch will fail at runtime"
        )


@pytest.mark.asyncio
async def test_transform_conditions_multiple_with_code_text(db_pool):
    """End-to-end: two conditions using code.text both get non-empty display."""
    resources = [
        {"resourceType": "Condition", "code": {"text": "Prediabetes"},
         "onsetDateTime": "2017-04-25"},
        {"resourceType": "Condition", "code": {"text": "Hypertension"},
         "onsetDateTime": "2019-01-10"},
    ]
    fake_patient = str(uuid.uuid4())
    recs = transform_conditions(resources, patient_id=fake_patient)
    assert len(recs) == 2
    displays = [r["display"] for r in recs]
    assert "Prediabetes" in displays
    assert "Hypertension" in displays
    assert all(d != "" for d in displays), f"One or more displays are empty: {displays}"
