"""Integration tests for ingest_from_healthex edge cases.

Calls the actual FastMCP tool end-to-end against the live database, covering:
  - Patient-existence guard: unknown UUID → structured error response
  - Format B payload: #-prefixed compressed table → adaptive pipeline detects
    and parses it, returns format_detected/parser_used, records_written is a dict
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
# Format B: compressed table payloads are now detected + parsed adaptively
# (previously cached as raw text with records_written=0; pipeline now handles them)
# ---------------------------------------------------------------------------

class TestFormatBCompressedTableIngest:
    """#-prefixed payloads are detected as compressed_table and run through
    the adaptive pipeline — records_written is a dict, not 0."""

    @pytest.mark.asyncio
    async def test_format_b_detected_and_parsed(self, healthex_patient):
        """Format B compressed table is detected, parsed, and returns metadata."""
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
        assert result["format_detected"] == "compressed_table", (
            f"Expected format_detected='compressed_table', got: {result['format_detected']!r}"
        )
        assert result["parser_used"] == "format_b_compressed_table", (
            f"Expected parser_used='format_b_compressed_table', got: {result['parser_used']!r}"
        )
        assert isinstance(result["records_written"], dict), (
            f"records_written must be a dict from the adaptive pipeline, "
            f"got: {result['records_written']!r}"
        )

    @pytest.mark.asyncio
    async def test_format_b_response_shape(self, healthex_patient):
        """Adaptive pipeline response always includes format_detected and parser_used."""
        raw_text_payload = json.dumps("#Labs|HbA1c: 7.2")
        raw = await ingest_from_healthex(
            patient_id=healthex_patient,
            resource_type="labs",
            fhir_json=raw_text_payload,
        )
        result = json.loads(raw)
        assert result["status"] == "ok", f"Expected ok, got: {result}"
        assert "format_detected" in result, "Response must include format_detected field"
        assert "parser_used" in result, "Response must include parser_used field"
        assert result["format_detected"] == "compressed_table", (
            f"Short #-prefixed payload should be compressed_table, "
            f"got: {result['format_detected']!r}"
        )
        assert "total_written" in result, "Response must include total_written field"


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
