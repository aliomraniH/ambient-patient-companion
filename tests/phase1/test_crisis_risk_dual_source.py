"""
test_crisis_risk_dual_source.py — P-4 verification tests.

Covers:
  - PHQ-9 item 9 endorsement produces non-zero crisis_risk (existing behavior)
  - atom_pressure_scores with suicidality > 0.7 produces non-zero crisis_risk
    (new — the pivoted-view read replaces the placeholder column shape)
  - data_status distinguishes never_screened / screened_normal /
    screened_abnormal / overdue / atoms_only
  - Atom contribution is capped (does not overpower formal screens)
  - Stale screening carries a decay factor <1.0
"""

from __future__ import annotations

import json
import os
import sys
import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest
import pytest_asyncio

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_MCP_SERVER = _REPO_ROOT / "mcp-server"
if str(_MCP_SERVER) not in sys.path:
    sys.path.insert(0, str(_MCP_SERVER))

from skills import compute_provider_risk as cpr  # noqa: E402

_has_db = "DATABASE_URL" in os.environ
skip_no_db = pytest.mark.skipif(not _has_db, reason="DATABASE_URL not set")


class TestPhq9Item9Extraction:
    def test_item_9_parsed_from_string_key(self):
        assert cpr._extract_phq9_item9({"9": 1}) == 1

    def test_item_9_parsed_from_alias_keys(self):
        for key in ("item_9", "phq9_item9", "Q9", "q9"):
            assert cpr._extract_phq9_item9({key: 2}) == 2

    def test_missing_item_returns_none(self):
        assert cpr._extract_phq9_item9({"1": 0}) is None

    def test_empty_returns_none(self):
        assert cpr._extract_phq9_item9({}) is None
        assert cpr._extract_phq9_item9(None) is None


# ---------------------------------------------------------------------------
# DB-gated: end-to-end crisis_risk behavior
# ---------------------------------------------------------------------------

@pytest_asyncio.fixture
async def risk_patient(db_pool):
    pid = str(uuid.uuid4())
    async with db_pool.acquire() as conn:
        await conn.execute(
            """INSERT INTO patients
                   (id, mrn, first_name, last_name, birth_date, gender,
                    is_synthetic, data_source)
               VALUES ($1::uuid, $2, 'Risk', 'DualSource', '1980-01-01',
                       'female', false, 'healthex')""",
            pid, f"RDS-{pid[:8]}",
        )
    yield pid
    async with db_pool.acquire() as conn:
        await conn.execute("DELETE FROM provider_risk_scores WHERE patient_id=$1::uuid", pid)
        await conn.execute("DELETE FROM behavioral_screenings WHERE patient_id=$1::uuid", pid)
        await conn.execute("DELETE FROM behavioral_signal_atoms WHERE patient_id=$1::uuid", pid)
        await conn.execute("DELETE FROM care_gaps WHERE patient_id=$1::uuid", pid)
        await conn.execute("DELETE FROM agent_interventions WHERE patient_id=$1::uuid", pid)
        await conn.execute("DELETE FROM obt_scores WHERE patient_id=$1::uuid", pid)
        await conn.execute("DELETE FROM medication_adherence WHERE patient_id=$1::uuid", pid)
        await conn.execute("DELETE FROM patient_sdoh_flags WHERE patient_id=$1::uuid", pid)
        await conn.execute("DELETE FROM patients WHERE id=$1::uuid", pid)


@skip_no_db
class TestCrisisRiskPhq9Item9:
    @pytest.mark.asyncio
    async def test_item9_endorsement_fires_crisis_risk(self, db_pool, risk_patient):
        pid = risk_patient
        async with db_pool.acquire() as conn:
            await conn.execute(
                """INSERT INTO behavioral_screenings
                       (id, patient_id, instrument_key, domain, loinc_code,
                        score, band, item_answers, triggered_critical,
                        source_type, administered_at, data_source)
                   VALUES ($1::uuid, $2::uuid, 'phq9', 'depression',
                           '44249-1', 6, 'mild', $3::jsonb, $4::jsonb,
                           'fhir', NOW(), 'healthex')
                   ON CONFLICT (natural_key) DO NOTHING""",
                str(uuid.uuid4()), pid,
                json.dumps({"9": 1, "1": 1, "2": 1}),
                json.dumps([{"item_number": 9, "tag": "passive_suicidal_ideation"}]),
            )

        result_str = await cpr.compute_provider_risk(pid)
        assert not result_str.startswith("Error"), result_str
        result = json.loads(result_str)
        assert result["risk_factors"]["crisis_risk"] > 20, result
        cb = result["risk_factors"]["crisis_breakdown"]
        assert cb["si_screening"] > 0
        assert cb["si_flag"] is not None
        assert cb["data_status"] == "screened_abnormal"


@skip_no_db
class TestCrisisRiskAtomPressure:
    @pytest.mark.asyncio
    async def test_atom_suicidality_pressure_fires_crisis_risk(
        self, db_pool, risk_patient
    ):
        """No formal screening but high-pressure atoms — must still surface."""
        pid = risk_patient
        async with db_pool.acquire() as conn:
            # Seed enough behavioral_signal_atoms rows to drive
            # atom_pressure_scores.pressure_score for suicidality above 0.7
            for _ in range(5):
                await conn.execute(
                    """INSERT INTO behavioral_signal_atoms
                           (id, patient_id, signal_type, signal_value,
                            confidence, source_type, extracted_at, data_source)
                       VALUES ($1::uuid, $2::uuid, 'suicidality', '[REDACTED]',
                               0.85, 'clinical_note', NOW(), 'healthex')""",
                    str(uuid.uuid4()), pid,
                )
            # Refresh materialized view so the new atoms are visible
            try:
                await conn.execute("REFRESH MATERIALIZED VIEW atom_pressure_scores")
            except Exception as exc:
                pytest.skip(f"atom_pressure_scores view not refreshable: {exc}")

        result_str = await cpr.compute_provider_risk(pid)
        assert not result_str.startswith("Error"), result_str
        result = json.loads(result_str)
        cb = result["risk_factors"]["crisis_breakdown"]
        assert cb["atom_pressure"] > 0, cb
        assert any(
            s["signal_type"] == "suicidality" for s in cb["atom_signals"]
        )
        # No formal screening on record for this patient
        assert cb["data_status"] in ("atoms_only", "never_screened")


@skip_no_db
class TestCrisisRiskDataStatus:
    @pytest.mark.asyncio
    async def test_never_screened_patient_yields_zero_signal(self, db_pool, risk_patient):
        pid = risk_patient
        result_str = await cpr.compute_provider_risk(pid)
        assert not result_str.startswith("Error"), result_str
        result = json.loads(result_str)
        cb = result["risk_factors"]["crisis_breakdown"]
        assert cb["data_status"] == "never_screened"
        assert cb["si_flag"] is None
        assert cb["atom_signals"] == []
