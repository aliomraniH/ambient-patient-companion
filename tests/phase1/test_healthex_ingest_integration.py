"""Integration tests for ingest_from_healthex edge cases.

Calls the actual FastMCP tool end-to-end against the live database, covering:
  - Patient-existence guard: unknown UUID → structured error response
  - Raw text payload: JSON-encoded string → cached, records_written = 0
  - code.text fallback for metric_type (HbA1c with no coding block)
  - onset key alias for native HealthEx conditions
"""

from __future__ import annotations

import json
import uuid as _uuid_mod

import pytest

from server.mcp_server import ingest_from_healthex


# ---------------------------------------------------------------------------
# Fix 5: patient-existence guard
# ---------------------------------------------------------------------------

class TestPatientExistenceGuard:
    """ingest_from_healthex must reject an unknown patient_id immediately."""

    @pytest.mark.asyncio
    async def test_unknown_uuid_returns_error(self):
        fake_id = str(_uuid_mod.uuid4())
        raw = await ingest_from_healthex(
            patient_id=fake_id,
            resource_type="labs",
            fhir_json=json.dumps({"resourceType": "Bundle", "entry": []}),
        )
        result = json.loads(raw)
        assert result["status"] == "error", (
            f"Expected status='error' for unknown patient_id, got: {result}"
        )
        assert "not found" in result.get("error", "").lower(), (
            f"Error message should mention 'not found': {result.get('error')!r}"
        )

    @pytest.mark.asyncio
    async def test_known_uuid_does_not_trigger_guard_error(self, healthex_patient):
        raw = await ingest_from_healthex(
            patient_id=healthex_patient,
            resource_type="labs",
            fhir_json=json.dumps({"resourceType": "Bundle", "entry": []}),
        )
        result = json.loads(raw)
        assert result["status"] == "ok", (
            f"Valid patient_id should not trigger guard error: {result}"
        )


# ---------------------------------------------------------------------------
# Fix 6: raw text payload caching
# ---------------------------------------------------------------------------

class TestRawTextPayloadCaching:
    """JSON-encoded plain strings must be cached and return records_written = 0."""

    @pytest.mark.asyncio
    async def test_raw_text_returns_ok_zero_records(self, healthex_patient):
        raw_text_payload = json.dumps(
            "#Conditions 5y|Total:39\nDate|Condition\n2020-01-01|Prediabetes"
        )
        raw = await ingest_from_healthex(
            patient_id=healthex_patient,
            resource_type="conditions",
            fhir_json=raw_text_payload,
        )
        result = json.loads(raw)
        assert result["status"] == "ok", f"Expected ok, got: {result}"
        assert result["records_written"] == 0, (
            f"records_written must be 0 for raw text, got: {result['records_written']!r}"
        )
        assert result["total_written"] == 0

    @pytest.mark.asyncio
    async def test_raw_text_note_matches_spec(self, healthex_patient):
        raw_text_payload = json.dumps("#Labs|HbA1c: 7.2")
        raw = await ingest_from_healthex(
            patient_id=healthex_patient,
            resource_type="labs",
            fhir_json=raw_text_payload,
        )
        result = json.loads(raw)
        assert result.get("note") == "raw text cached, normalization skipped", (
            f"Note does not match spec exactly: {result.get('note')!r}"
        )


# ---------------------------------------------------------------------------
# Fix 2: code.text fallback for metric_type, end-to-end via ingest
# ---------------------------------------------------------------------------

class TestCodeTextFallbackViaIngest:
    """FHIR Observations with only code.text must write a row with correct metric_type."""

    @pytest.mark.asyncio
    async def test_hba1c_code_text_only_writes_row(self, healthex_patient):
        bundle = {
            "resourceType": "Bundle",
            "entry": [
                {
                    "resource": {
                        "resourceType": "Observation",
                        "code": {"text": "HbA1c"},
                        "valueQuantity": {"value": 7.2, "unit": "%"},
                        "effectiveDateTime": "2024-03-01",
                    }
                }
            ],
        }
        raw = await ingest_from_healthex(
            patient_id=healthex_patient,
            resource_type="labs",
            fhir_json=json.dumps(bundle),
        )
        result = json.loads(raw)
        assert result["status"] == "ok"
        assert result["total_written"] >= 1, (
            f"Expected ≥1 biometric row, got total_written={result['total_written']} — "
            "code.text fallback may be broken"
        )


# ---------------------------------------------------------------------------
# Fix 4: onset key alias for native HealthEx conditions, end-to-end
# ---------------------------------------------------------------------------

class TestOnsetKeyAliasViaIngest:
    """Native HealthEx conditions with 'onset' key must produce 2 written rows."""

    @pytest.mark.asyncio
    async def test_conditions_with_onset_key_writes_rows(self, healthex_patient):
        conditions_payload = {
            "conditions": [
                {"name": "Prediabetes", "onset": "2017-04-25", "status": "active"},
                {"name": "Hypertension", "onset": "2019-01-10", "status": "active"},
            ]
        }
        raw = await ingest_from_healthex(
            patient_id=healthex_patient,
            resource_type="conditions",
            fhir_json=json.dumps(conditions_payload),
        )
        result = json.loads(raw)
        assert result["status"] == "ok"
        assert result["total_written"] == 2, (
            f"Expected 2 condition rows, got {result['total_written']} — "
            "'onset' key alias in _healthex_native_to_fhir_conditions may be broken"
        )
