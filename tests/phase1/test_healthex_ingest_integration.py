"""Integration tests for ingest_from_healthex edge cases.

Calls the actual FastMCP tool end-to-end against the live database, covering:
  - Patient-existence guard: unknown UUID → structured error response
  - Format B payload: #-prefixed compressed table → adaptive pipeline detects
    and parses it, returns format_detected/parser_used, records_written is a dict
  - code.text fallback for metric_type (HbA1c with no coding block)
  - onset key alias for native HealthEx conditions
  - Migration 012 natural-key uniqueness regression: same-day duplicates with
    the same code collapse, but same-day rows with different codes (and
    same-code rows on different days) are preserved.
"""

from __future__ import annotations

import json
import os
import uuid as _uuid_mod

import pytest

from server.mcp_server import ingest_from_healthex


_has_db = "DATABASE_URL" in os.environ
skip_no_db = pytest.mark.skipif(not _has_db, reason="DATABASE_URL not set")


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


# ---------------------------------------------------------------------------
# Migration 012 regression: same-day duplicates collapse but distinct
# same-day events with different codes are preserved. Also covers the
# multi-episode case (same code, different dates) that must NOT collapse.
# ---------------------------------------------------------------------------

@skip_no_db
class TestSameDayDiagnosisUniquenessRegression:
    """Guards the natural_key contract for ICD-10 conditions and RxNorm meds.

    The new uniqueness rule deduplicates on (patient_id, code, date). This
    test pins the boundaries:
      - Two inserts of the SAME code on the SAME day → 1 row (collapse).
      - Two DIFFERENT codes on the SAME day → 2 rows (preserved).
      - SAME code on DIFFERENT days → 2 rows (preserved — distinct episodes).
    A regression here means real, clinically distinct events would silently
    merge in production.
    """

    @pytest.mark.asyncio
    async def test_icd10_same_day_same_code_collapses(
        self, db_pool, healthex_patient
    ):
        pid = healthex_patient
        async with db_pool.acquire() as conn:
            for _ in range(2):
                await conn.execute(
                    """INSERT INTO patient_conditions
                           (id, patient_id, code, display, system,
                            onset_date, clinical_status, data_source)
                       VALUES ($1::uuid, $2::uuid, 'K80.20',
                               'Calculus of gallbladder without cholecystitis',
                               'http://hl7.org/fhir/sid/icd-10',
                               '2024-08-15', 'active', 'healthex')
                       ON CONFLICT (natural_key) DO NOTHING""",
                    str(_uuid_mod.uuid4()), pid,
                )
            count = await conn.fetchval(
                """SELECT COUNT(*) FROM patient_conditions
                   WHERE patient_id=$1::uuid AND code='K80.20'
                     AND onset_date='2024-08-15'""",
                pid,
            )
        assert count == 1, (
            f"Same-day, same-code ICD-10 inserts must collapse to 1, got {count}"
        )

    @pytest.mark.asyncio
    async def test_icd10_same_day_different_codes_preserved(
        self, db_pool, healthex_patient
    ):
        pid = healthex_patient
        async with db_pool.acquire() as conn:
            await conn.execute(
                """INSERT INTO patient_conditions
                       (id, patient_id, code, display, system,
                        onset_date, clinical_status, data_source)
                   VALUES ($1::uuid, $2::uuid, 'K80.20', 'Gallstones',
                           'http://hl7.org/fhir/sid/icd-10',
                           '2024-08-15', 'active', 'healthex')
                   ON CONFLICT (natural_key) DO NOTHING""",
                str(_uuid_mod.uuid4()), pid,
            )
            await conn.execute(
                """INSERT INTO patient_conditions
                       (id, patient_id, code, display, system,
                        onset_date, clinical_status, data_source)
                   VALUES ($1::uuid, $2::uuid, 'I10', 'Essential hypertension',
                           'http://hl7.org/fhir/sid/icd-10',
                           '2024-08-15', 'active', 'healthex')
                   ON CONFLICT (natural_key) DO NOTHING""",
                str(_uuid_mod.uuid4()), pid,
            )
            count = await conn.fetchval(
                """SELECT COUNT(*) FROM patient_conditions
                   WHERE patient_id=$1::uuid AND onset_date='2024-08-15'""",
                pid,
            )
        assert count == 2, (
            f"Two ICD-10 codes on the same day must remain distinct, got {count}"
        )

    @pytest.mark.asyncio
    async def test_icd10_same_code_different_days_preserved(
        self, db_pool, healthex_patient
    ):
        pid = healthex_patient
        async with db_pool.acquire() as conn:
            for d in ("2024-01-10", "2024-08-15"):
                await conn.execute(
                    """INSERT INTO patient_conditions
                           (id, patient_id, code, display, system,
                            onset_date, clinical_status, data_source)
                       VALUES ($1::uuid, $2::uuid, 'K80.20', 'Gallstones',
                               'http://hl7.org/fhir/sid/icd-10',
                               $3::date, 'active', 'healthex')
                       ON CONFLICT (natural_key) DO NOTHING""",
                    str(_uuid_mod.uuid4()), pid, d,
                )
            count = await conn.fetchval(
                """SELECT COUNT(*) FROM patient_conditions
                   WHERE patient_id=$1::uuid AND code='K80.20'""",
                pid,
            )
        assert count == 2, (
            f"Same ICD-10 code on different days must remain distinct, got {count}"
        )

    @pytest.mark.asyncio
    async def test_rxnorm_same_day_same_code_collapses(
        self, db_pool, healthex_patient
    ):
        pid = healthex_patient
        async with db_pool.acquire() as conn:
            for _ in range(3):
                await conn.execute(
                    """INSERT INTO patient_medications
                           (id, patient_id, code, display, system,
                            status, authored_on, data_source)
                       VALUES ($1::uuid, $2::uuid, '197361', 'Lisinopril 10 MG',
                               'http://www.nlm.nih.gov/research/umls/rxnorm',
                               'active', '2024-08-15', 'healthex')
                       ON CONFLICT (natural_key) DO NOTHING""",
                    str(_uuid_mod.uuid4()), pid,
                )
            count = await conn.fetchval(
                """SELECT COUNT(*) FROM patient_medications
                   WHERE patient_id=$1::uuid AND code='197361'
                     AND authored_on='2024-08-15'""",
                pid,
            )
        assert count == 1, (
            f"Same-day, same-code RxNorm inserts must collapse to 1, got {count}"
        )

    @pytest.mark.asyncio
    async def test_rxnorm_same_day_different_codes_preserved(
        self, db_pool, healthex_patient
    ):
        pid = healthex_patient
        async with db_pool.acquire() as conn:
            await conn.execute(
                """INSERT INTO patient_medications
                       (id, patient_id, code, display, system,
                        status, authored_on, data_source)
                   VALUES ($1::uuid, $2::uuid, '197361', 'Lisinopril 10 MG',
                           'http://www.nlm.nih.gov/research/umls/rxnorm',
                           'active', '2024-08-15', 'healthex')
                   ON CONFLICT (natural_key) DO NOTHING""",
                str(_uuid_mod.uuid4()), pid,
            )
            await conn.execute(
                """INSERT INTO patient_medications
                       (id, patient_id, code, display, system,
                        status, authored_on, data_source)
                   VALUES ($1::uuid, $2::uuid, '314076', 'Atorvastatin 20 MG',
                           'http://www.nlm.nih.gov/research/umls/rxnorm',
                           'active', '2024-08-15', 'healthex')
                   ON CONFLICT (natural_key) DO NOTHING""",
                str(_uuid_mod.uuid4()), pid,
            )
            count = await conn.fetchval(
                """SELECT COUNT(*) FROM patient_medications
                   WHERE patient_id=$1::uuid AND authored_on='2024-08-15'""",
                pid,
            )
        assert count == 2, (
            f"Two RxNorm codes on the same day must remain distinct, got {count}"
        )

    @pytest.mark.asyncio
    async def test_rxnorm_same_code_different_days_preserved(
        self, db_pool, healthex_patient
    ):
        pid = healthex_patient
        async with db_pool.acquire() as conn:
            for d in ("2024-01-10", "2024-08-15"):
                await conn.execute(
                    """INSERT INTO patient_medications
                           (id, patient_id, code, display, system,
                            status, authored_on, data_source)
                       VALUES ($1::uuid, $2::uuid, '197361', 'Lisinopril 10 MG',
                               'http://www.nlm.nih.gov/research/umls/rxnorm',
                               'active', $3::date, 'healthex')
                       ON CONFLICT (natural_key) DO NOTHING""",
                    str(_uuid_mod.uuid4()), pid, d,
                )
            count = await conn.fetchval(
                """SELECT COUNT(*) FROM patient_medications
                   WHERE patient_id=$1::uuid AND code='197361'""",
                pid,
            )
        assert count == 2, (
            f"Same RxNorm code on different days must remain distinct, got {count}"
        )
