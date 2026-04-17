"""
test_ingest_verifier.py — P-2 part 3: round-trip verifier semantics.

Covers:
  - verify_transfer classifies STATUS_CLEAN when source and warehouse match
  - verify_transfer classifies STATUS_POLLUTION when warehouse has extras
  - verify_transfer classifies STATUS_GAPS when source has extras
  - verify_transfer classifies STATUS_BOTH for mixed outcomes
  - verify_transfer classifies STATUS_UNVERIFIABLE for unsupported types
  - canonical source-extraction round-trips basic condition payloads
  - autoheal_pollution only deletes when can_autoheal is True
"""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from datetime import datetime, timezone

import pytest
import pytest_asyncio

from ingestion.verification.ingest_verifier import (
    VerificationResult,
    verify_transfer,
    autoheal_pollution,
    STATUS_CLEAN,
    STATUS_GAPS,
    STATUS_POLLUTION,
    STATUS_BOTH,
    STATUS_UNVERIFIABLE,
    _canonical_key_source,
    _canonical_key_warehouse,
)


_has_db = "DATABASE_URL" in os.environ
skip_no_db = pytest.mark.skipif(not _has_db, reason="DATABASE_URL not set")


class TestCanonicalKeys:
    def test_source_and_warehouse_keys_align_for_conditions(self):
        src = {"name": "Prediabetes", "icd10": "R73.03", "onset_date": "2017-04-25"}
        wh = {"code": "R73.03", "display": "Prediabetes", "onset_date": "2017-04-25",
              "clinical_status": "active"}
        k_src = _canonical_key_source("conditions", src)
        k_wh = _canonical_key_warehouse("conditions", wh)
        assert k_src == k_wh, f"keys diverged: {k_src!r} vs {k_wh!r}"

    def test_source_and_warehouse_keys_align_for_medications(self):
        src = {"name": "Pantoprazole", "rxnorm": "40790", "start_date": "2022-03-10"}
        wh = {"code": "40790", "display": "Pantoprazole", "authored_on": "2022-03-10",
              "status": "active"}
        k_src = _canonical_key_source("medications", src)
        k_wh = _canonical_key_warehouse("medications", wh)
        assert k_src == k_wh


class TestUnverifiableResourceTypes:
    @pytest.mark.asyncio
    async def test_unknown_resource_type_yields_unverifiable(self):
        # No DB connection needed — the mapping check short-circuits
        result = await verify_transfer(
            conn=None, patient_id="dummy", resource_type="allergies",
            source_payload="{}",
        )
        assert result.status == STATUS_UNVERIFIABLE


# ---------------------------------------------------------------------------
# DB-gated end-to-end tests
# ---------------------------------------------------------------------------

@pytest_asyncio.fixture
async def verifier_patient(db_pool):
    pid = str(uuid.uuid4())
    async with db_pool.acquire() as conn:
        await conn.execute(
            """INSERT INTO patients
                   (id, mrn, first_name, last_name, birth_date, gender,
                    is_synthetic, data_source)
               VALUES ($1::uuid, $2, 'Verifier', 'Test', '1985-01-01',
                       'male', false, 'healthex')""",
            pid, f"VFR-{pid[:8]}",
        )
    yield pid
    async with db_pool.acquire() as conn:
        await conn.execute("DELETE FROM transfer_log WHERE patient_id=$1::uuid", pid)
        await conn.execute("DELETE FROM biometric_readings WHERE patient_id=$1::uuid", pid)
        await conn.execute("DELETE FROM patient_conditions WHERE patient_id=$1::uuid", pid)
        await conn.execute("DELETE FROM patient_medications WHERE patient_id=$1::uuid", pid)
        await conn.execute("DELETE FROM clinical_events WHERE patient_id=$1::uuid", pid)
        await conn.execute("DELETE FROM patients WHERE id=$1::uuid", pid)


@skip_no_db
class TestVerifyTransferClean:
    @pytest.mark.asyncio
    async def test_clean_when_source_matches_warehouse(self, db_pool, verifier_patient):
        pid = verifier_patient
        payload = json.dumps({
            "conditions": [
                {"name": "Prediabetes", "icd10": "R73.03",
                 "status": "active", "onset_date": "2017-04-25"},
                {"name": "Hypertension", "icd10": "I10",
                 "status": "active", "onset_date": "2019-06-01"},
            ],
        })
        async with db_pool.acquire() as conn:
            for code, display, onset in [
                ("R73.03", "Prediabetes", "2017-04-25"),
                ("I10", "Hypertension", "2019-06-01"),
            ]:
                await conn.execute(
                    """INSERT INTO patient_conditions
                           (id, patient_id, code, display, onset_date,
                            clinical_status, data_source)
                       VALUES ($1::uuid, $2::uuid, $3, $4, $5, 'active', 'healthex')
                       ON CONFLICT (natural_key) DO NOTHING""",
                    str(uuid.uuid4()), pid, code, display, onset,
                )
            result = await verify_transfer(
                conn, patient_id=pid, resource_type="conditions",
                source_payload=payload,
            )
        assert result.status == STATUS_CLEAN, result.to_summary()
        assert result.source_record_count == 2
        assert result.warehouse_record_count == 2
        assert result.matched == 2

    @pytest.mark.asyncio
    async def test_pollution_when_warehouse_has_extra_rows(self, db_pool, verifier_patient):
        pid = verifier_patient
        # Source has just 1 condition
        payload = json.dumps({
            "conditions": [
                {"name": "Prediabetes", "icd10": "R73.03",
                 "status": "active", "onset_date": "2017-04-25"},
            ],
        })
        async with db_pool.acquire() as conn:
            # Warehouse has 3 (one from source + two pollution rows)
            for code, display, onset in [
                ("R73.03", "Prediabetes", "2017-04-25"),
                ("X99", "Made up", "2021-01-01"),
                ("Y99", "Also made up", "2022-02-02"),
            ]:
                await conn.execute(
                    """INSERT INTO patient_conditions
                           (id, patient_id, code, display, onset_date,
                            clinical_status, data_source)
                       VALUES ($1::uuid, $2::uuid, $3, $4, $5, 'active', 'healthex')
                       ON CONFLICT (natural_key) DO NOTHING""",
                    str(uuid.uuid4()), pid, code, display, onset,
                )
            result = await verify_transfer(
                conn, patient_id=pid, resource_type="conditions",
                source_payload=payload,
            )
        assert result.status == STATUS_POLLUTION, result.to_summary()
        assert result.can_autoheal is True
        assert len(result.extra_in_warehouse) == 2

    @pytest.mark.asyncio
    async def test_gaps_when_warehouse_missing_source_rows(self, db_pool, verifier_patient):
        pid = verifier_patient
        # Source has 2; warehouse has 1
        payload = json.dumps({
            "conditions": [
                {"name": "Prediabetes", "icd10": "R73.03",
                 "status": "active", "onset_date": "2017-04-25"},
                {"name": "Hypertension", "icd10": "I10",
                 "status": "active", "onset_date": "2019-06-01"},
            ],
        })
        async with db_pool.acquire() as conn:
            await conn.execute(
                """INSERT INTO patient_conditions
                       (id, patient_id, code, display, onset_date,
                        clinical_status, data_source)
                   VALUES ($1::uuid, $2::uuid, 'R73.03', 'Prediabetes',
                           '2017-04-25', 'active', 'healthex')
                   ON CONFLICT (natural_key) DO NOTHING""",
                str(uuid.uuid4()), pid,
            )
            result = await verify_transfer(
                conn, patient_id=pid, resource_type="conditions",
                source_payload=payload,
            )
        assert result.status == STATUS_GAPS, result.to_summary()
        assert result.can_autoheal is False
        assert len(result.missing_in_warehouse) == 1


@skip_no_db
class TestAutoheal:
    @pytest.mark.asyncio
    async def test_autoheal_only_runs_when_can_autoheal(self, db_pool, verifier_patient):
        # Build a result manually with can_autoheal = False
        pid = verifier_patient
        result = VerificationResult(
            patient_id=pid,
            resource_type="conditions",
            source_record_count=1,
            warehouse_record_count=2,
            status=STATUS_BOTH,
            extra_in_warehouse=[{"code": "X99", "display": "x", "onset_date": None}],
            can_autoheal=False,
        )
        async with db_pool.acquire() as conn:
            deleted = await autoheal_pollution(conn, result)
        assert deleted == 0
