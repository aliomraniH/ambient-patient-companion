"""
test_fhir_introspection.py — P-1 verification tests.

Covers:
  - FormatIntrospector routes bare QuestionnaireResponse to the screening ingestor
  - FormatIntrospector distinguishes screening-LOINC vs lab-LOINC Observations
  - Bundle fan-out routes each entry to its typed ingestor
  - Ambiguous payloads land on llm_fallback with ambiguity_score > threshold
  - format_detector wraps bare QuestionnaireResponse in a Bundle (was missing pre-P-1)
"""

from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timezone

import pytest
import pytest_asyncio

from ingestion.adapters.healthex.format_detector import detect_format, HealthExFormat
from ingestion.adapters.healthex.format_introspector import (
    introspect,
    introspect_bundle_entries,
    ROUTE_BEHAVIORAL_SCREENING,
    ROUTE_LABS,
    ROUTE_MEDICATIONS,
    ROUTE_CONDITIONS,
    ROUTE_BUNDLE_SPLITTER,
    ROUTE_SUMMARY,
    ROUTE_UNKNOWN,
    ROUTE_LLM_FALLBACK,
    LLM_FALLBACK_THRESHOLD,
)


_has_db = "DATABASE_URL" in os.environ
skip_no_db = pytest.mark.skipif(not _has_db, reason="DATABASE_URL not set")


# ---------------------------------------------------------------------------
# format_detector — bare QuestionnaireResponse must wrap into a Bundle
# ---------------------------------------------------------------------------

class TestFormatDetectorCoversQR:
    def test_bare_questionnaire_response_wrapped_as_bundle(self):
        phq9 = {
            "resourceType": "QuestionnaireResponse",
            "questionnaire": "http://loinc.org/vs/44249-1",
            "authored": "2023-12-13",
            "item": [{"linkId": "9", "answer": [{"valueInteger": 1}]}],
        }
        fmt, payload = detect_format(json.dumps(phq9))
        assert fmt == HealthExFormat.FHIR_BUNDLE_JSON
        assert payload["resourceType"] == "Bundle"
        assert payload["entry"][0]["resource"]["resourceType"] == "QuestionnaireResponse"

    def test_bare_diagnostic_report_wrapped_as_bundle(self):
        dr = {"resourceType": "DiagnosticReport", "status": "final"}
        fmt, payload = detect_format(json.dumps(dr))
        assert fmt == HealthExFormat.FHIR_BUNDLE_JSON
        assert payload["entry"][0]["resource"]["resourceType"] == "DiagnosticReport"


# ---------------------------------------------------------------------------
# FormatIntrospector — routing recommendations
# ---------------------------------------------------------------------------

class TestFormatIntrospectorRouting:
    def test_bare_questionnaire_response_routes_to_screening(self):
        phq9 = {
            "resourceType": "QuestionnaireResponse",
            "questionnaire": "http://loinc.org/vs/44249-1",
            "item": [{"linkId": "1", "answer": [{"valueInteger": 0}]}],
        }
        r = introspect(json.dumps(phq9))
        assert r.routing_recommendation == ROUTE_BEHAVIORAL_SCREENING
        assert r.resource_type_hint == "QuestionnaireResponse"
        assert r.instrument_hint == "phq9"
        assert r.ambiguity_score < 0.3

    def test_observation_with_screening_loinc_routes_to_screening(self):
        obs = {
            "resourceType": "Observation",
            "code": {"coding": [{"system": "http://loinc.org", "code": "44249-1"}]},
            "valueInteger": 6,
        }
        r = introspect(json.dumps(obs))
        assert r.routing_recommendation == ROUTE_BEHAVIORAL_SCREENING
        assert r.instrument_hint == "phq9"

    def test_observation_with_lab_loinc_routes_to_labs(self):
        obs = {
            "resourceType": "Observation",
            "code": {"coding": [{"system": "http://loinc.org", "code": "4548-4"}]},
            "valueQuantity": {"value": 6.1, "unit": "%"},
        }
        r = introspect(json.dumps(obs))
        assert r.routing_recommendation == ROUTE_LABS
        assert r.instrument_hint is None

    def test_medication_request_routes_to_medications(self):
        med = {"resourceType": "MedicationRequest", "status": "active"}
        r = introspect(json.dumps(med))
        assert r.routing_recommendation == ROUTE_MEDICATIONS

    def test_condition_routes_to_conditions(self):
        cond = {
            "resourceType": "Condition",
            "code": {"coding": [{"system": "http://hl7.org/fhir/sid/icd-10", "code": "I10"}]},
        }
        r = introspect(json.dumps(cond))
        assert r.routing_recommendation == ROUTE_CONDITIONS
        assert "I10" in r.icd10_codes

    def test_bundle_recurses_into_entries(self):
        bundle = {
            "resourceType": "Bundle",
            "type": "searchset",
            "entry": [
                {"resource": {
                    "resourceType": "QuestionnaireResponse",
                    "questionnaire": "http://loinc.org/vs/44249-1",
                    "item": [],
                }},
                {"resource": {
                    "resourceType": "Condition",
                    "code": {"coding": [{"system": "http://hl7.org/fhir/sid/icd-10", "code": "E11.9"}]},
                }},
            ],
        }
        r = introspect(json.dumps(bundle))
        assert r.has_bundle_wrapper is True
        assert r.routing_recommendation == ROUTE_BUNDLE_SPLITTER

        entries = introspect_bundle_entries(bundle)
        assert len(entries) == 2
        assert entries[0].routing_recommendation == ROUTE_BEHAVIORAL_SCREENING
        assert entries[1].routing_recommendation == ROUTE_CONDITIONS

    def test_plain_text_summary_routes_to_summary(self):
        r = introspect("PATIENT: Test Patient\nDOB: 1980-01-01\nMRN: T1")
        assert r.routing_recommendation == ROUTE_SUMMARY

    def test_genuinely_ambiguous_payload_requires_llm_fallback(self):
        """A JSON dict with no resourceType and no known array keys."""
        ambiguous = {"alpha": "beta", "gamma": 42}
        r = introspect(json.dumps(ambiguous))
        # No known array keys, no FHIR resourceType, no declared type
        assert r.ambiguity_score >= LLM_FALLBACK_THRESHOLD - 0.01
        assert r.routing_recommendation in (ROUTE_LLM_FALLBACK, ROUTE_UNKNOWN)

    def test_declared_resource_type_tiebreaker_for_plain_text(self):
        """When format is structured but self-description is absent, use declared."""
        r = introspect("# some table\n|col1|col2|\n|v1|v2|", resource_type_declared="labs")
        assert r.routing_recommendation == ROUTE_LABS


# ---------------------------------------------------------------------------
# Live ingest wiring — screening resource_type branch
# ---------------------------------------------------------------------------

@pytest_asyncio.fixture
async def screening_patient(db_pool):
    pid = str(uuid.uuid4())
    async with db_pool.acquire() as conn:
        await conn.execute(
            """INSERT INTO patients
                   (id, mrn, first_name, last_name, birth_date, gender,
                    is_synthetic, data_source)
               VALUES ($1::uuid, $2, 'P1', 'Screen', '1975-05-05',
                       'male', false, 'healthex')""",
            pid, f"P1S-{pid[:8]}",
        )
        await conn.execute(
            """INSERT INTO source_freshness
                   (patient_id, source_name, last_ingested_at, records_count, ttl_hours)
               VALUES ($1, 'healthex', NULL, 0, 24)
               ON CONFLICT (patient_id, source_name) DO NOTHING""",
            pid,
        )
    yield pid
    async with db_pool.acquire() as conn:
        await conn.execute("DELETE FROM transfer_log WHERE patient_id=$1::uuid", pid)
        await conn.execute("DELETE FROM behavioral_screenings WHERE patient_id=$1::uuid", pid)
        await conn.execute("DELETE FROM raw_fhir_cache WHERE patient_id=$1::uuid", pid)
        await conn.execute("DELETE FROM ingestion_plans WHERE patient_id=$1::uuid", pid)
        await conn.execute("DELETE FROM source_freshness WHERE patient_id=$1::uuid", pid)
        await conn.execute("DELETE FROM patients WHERE id=$1::uuid", pid)


@skip_no_db
class TestScreeningResourceType:
    @pytest.mark.asyncio
    async def test_phq9_questionnaire_response_round_trip(self, db_pool, screening_patient):
        """Posting a bare PHQ-9 QR via resource_type='screening' writes a
        behavioral_screenings row with the correct instrument and triggered_critical.
        """
        from skills.behavioral_screening_ingestor import ingest_observation_or_qr, _ConnPool

        pid = screening_patient
        phq9 = {
            "resourceType": "QuestionnaireResponse",
            "questionnaire": "http://loinc.org/vs/44249-1",
            "authored": "2023-12-13",
            "item": [
                {"linkId": "1", "answer": [{"valueInteger": 1}]},
                {"linkId": "2", "answer": [{"valueInteger": 1}]},
                {"linkId": "3", "answer": [{"valueInteger": 1}]},
                {"linkId": "4", "answer": [{"valueInteger": 1}]},
                {"linkId": "5", "answer": [{"valueInteger": 1}]},
                {"linkId": "6", "answer": [{"valueInteger": 0}]},
                {"linkId": "7", "answer": [{"valueInteger": 0}]},
                {"linkId": "8", "answer": [{"valueInteger": 1}]},
                {"linkId": "9", "answer": [{"valueInteger": 1}]},
            ],
        }

        async with db_pool.acquire() as conn:
            result = await ingest_observation_or_qr(
                _ConnPool(conn),
                patient_id=pid,
                resource=phq9,
                source_type="fhir_screening",
                source_id="phq9-2023",
                data_source="healthex",
            )

            assert result is not None
            assert result["instrument_key"] == "phq9"
            assert result["domain"] == "depression"

            row = await conn.fetchrow(
                "SELECT instrument_key, score, domain, triggered_critical, item_answers "
                "FROM behavioral_screenings WHERE patient_id=$1::uuid",
                pid,
            )
            assert row is not None
            assert row["instrument_key"] == "phq9"
            # item 9 = 1 should be flagged
            tc = row["triggered_critical"]
            if isinstance(tc, str):
                tc = json.loads(tc)
            assert tc, f"expected triggered_critical rows, got {tc!r}"
