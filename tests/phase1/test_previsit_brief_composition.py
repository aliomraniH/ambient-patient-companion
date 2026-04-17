"""
test_previsit_brief_composition.py — P-3 verification tests.

Covers:
  - _classify_freshness maps age to tier/provenance_tag correctly
  - _read_staleness_band falls back to defaults when system_config is empty
  - Brief provenance metadata is attached to every field
  - Stale deliberations are INCLUDED with a PRIOR_SESSION tag, not dropped
"""

from __future__ import annotations

import importlib
import os
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import pytest_asyncio

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_MCP_SERVER = _REPO_ROOT / "mcp-server"
if str(_MCP_SERVER) not in sys.path:
    sys.path.insert(0, str(_MCP_SERVER))

# Import the skill lazily so the per-test sys.path prep takes effect.
from skills import previsit_brief as pvb  # noqa: E402

_has_db = "DATABASE_URL" in os.environ
skip_no_db = pytest.mark.skipif(not _has_db, reason="DATABASE_URL not set")


# ---------------------------------------------------------------------------
# Unit tests — freshness classification
# ---------------------------------------------------------------------------

class TestClassifyFreshness:
    def test_fresh_tier_for_recent_deliberation(self):
        out = pvb._classify_freshness(age_hours=6.0, fresh_hours=24.0, recent_hours=168.0)
        assert out["tier"] == "fresh"
        assert out["provenance_tag"] == "TOOL"
        assert out.get("warning") is None

    def test_recent_tier_between_fresh_and_recent(self):
        out = pvb._classify_freshness(age_hours=72.0, fresh_hours=24.0, recent_hours=168.0)
        assert out["tier"] == "recent"
        assert out["provenance_tag"] == "PRIOR_SESSION"
        assert out.get("warning") is None

    def test_stale_tier_beyond_recent_includes_warning(self):
        out = pvb._classify_freshness(age_hours=215.0, fresh_hours=24.0, recent_hours=168.0)
        assert out["tier"] == "stale"
        assert out["provenance_tag"] == "PRIOR_SESSION_STALE"
        assert "re-verify" in out["warning"].lower()
        assert out["age_hours"] == 215.0

    def test_age_rounded_to_one_decimal(self):
        out = pvb._classify_freshness(age_hours=11.2345, fresh_hours=24.0, recent_hours=168.0)
        assert out["age_hours"] == 11.2


class TestProvenanceHelper:
    def test_default_tier_is_tool(self):
        p = pvb._provenance_tool("patient_conditions")
        assert p["tier"] == "TOOL"
        assert p["source"] == "patient_conditions"
        assert "fetched_at" in p

    def test_explicit_tier_respected(self):
        p = pvb._provenance_tool("deliberations", tier="PRIOR_SESSION_STALE")
        assert p["tier"] == "PRIOR_SESSION_STALE"


# ---------------------------------------------------------------------------
# DB-gated tests — staleness band read + full-brief invariants
# ---------------------------------------------------------------------------

@pytest_asyncio.fixture
async def brief_patient(db_pool):
    pid = str(uuid.uuid4())
    async with db_pool.acquire() as conn:
        await conn.execute(
            """INSERT INTO patients
                   (id, mrn, first_name, last_name, birth_date, gender,
                    is_synthetic, data_source)
               VALUES ($1::uuid, $2, 'Brief', 'Composition', '1972-10-10',
                       'male', false, 'healthex')""",
            pid, f"BFC-{pid[:8]}",
        )
    yield pid
    async with db_pool.acquire() as conn:
        await conn.execute("DELETE FROM deliberation_outputs WHERE deliberation_id IN "
                           "(SELECT id FROM deliberations WHERE patient_id=$1::uuid)", pid)
        await conn.execute("DELETE FROM deliberations WHERE patient_id=$1::uuid", pid)
        await conn.execute("DELETE FROM obt_scores WHERE patient_id=$1::uuid", pid)
        await conn.execute("DELETE FROM patient_conditions WHERE patient_id=$1::uuid", pid)
        await conn.execute("DELETE FROM patient_medications WHERE patient_id=$1::uuid", pid)
        await conn.execute("DELETE FROM biometric_readings WHERE patient_id=$1::uuid", pid)
        await conn.execute("DELETE FROM care_gaps WHERE patient_id=$1::uuid", pid)
        await conn.execute("DELETE FROM patient_sdoh_flags WHERE patient_id=$1::uuid", pid)
        await conn.execute("DELETE FROM agent_interventions WHERE patient_id=$1::uuid", pid)
        await conn.execute("DELETE FROM patients WHERE id=$1::uuid", pid)


@skip_no_db
class TestStalenessBandRead:
    @pytest.mark.asyncio
    async def test_read_staleness_band_respects_system_config(self, db_pool):
        async with db_pool.acquire() as conn:
            # Seed (and restore) the two keys
            orig = {}
            for key in (
                "deliberation_staleness_fresh_hours",
                "deliberation_staleness_recent_days",
            ):
                row = await conn.fetchval(
                    "SELECT value FROM system_config WHERE key=$1", key,
                )
                orig[key] = row

            await conn.execute(
                """INSERT INTO system_config (key, value, updated_at)
                   VALUES ('deliberation_staleness_fresh_hours', '12', NOW())
                   ON CONFLICT (key) DO UPDATE SET value='12', updated_at=NOW()""",
            )
            await conn.execute(
                """INSERT INTO system_config (key, value, updated_at)
                   VALUES ('deliberation_staleness_recent_days', '10', NOW())
                   ON CONFLICT (key) DO UPDATE SET value='10', updated_at=NOW()""",
            )
            fresh, recent = await pvb._read_staleness_band(conn)
            assert fresh == 12.0
            assert recent == 10.0 * 24.0

            # Restore
            for key, val in orig.items():
                if val is None:
                    await conn.execute(
                        "DELETE FROM system_config WHERE key=$1", key,
                    )
                else:
                    await conn.execute(
                        """INSERT INTO system_config (key, value, updated_at)
                           VALUES ($1, $2, NOW())
                           ON CONFLICT (key) DO UPDATE SET value=$2, updated_at=NOW()""",
                        key, val,
                    )


@skip_no_db
class TestBriefComposition:
    @pytest.mark.asyncio
    async def test_every_field_carries_provenance(self, db_pool, brief_patient):
        import json
        pid = brief_patient
        result_str = await pvb.generate_previsit_brief(pid)
        assert not result_str.startswith("Error"), result_str
        brief = json.loads(result_str)

        expected_fields = {
            "obt_score", "interval_changes", "active_conditions",
            "active_medications", "open_care_gaps", "sdoh_flags",
            "recent_crises", "key_flags", "patient_questions",
            "recent_deliberation",
        }
        for f in expected_fields:
            assert f in brief, f"missing field {f}"
            assert isinstance(brief[f], dict), f"{f} should be {{value, _provenance}}"
            assert "_provenance" in brief[f], f"{f} missing _provenance"
            prov = brief[f]["_provenance"]
            assert "tier" in prov
            assert "source" in prov
            assert "fetched_at" in prov

    @pytest.mark.asyncio
    async def test_stale_deliberation_included_with_prior_session_tag(
        self, db_pool, brief_patient
    ):
        import json as _json
        pid = brief_patient
        # Insert a deliberation 200 hours old — beyond fresh, within recent
        delib_id = str(uuid.uuid4())
        old_ts = datetime.now(timezone.utc) - timedelta(hours=200)
        async with db_pool.acquire() as conn:
            # Ensure the staleness band covers this age
            await conn.execute(
                """INSERT INTO system_config (key, value, updated_at)
                   VALUES ('deliberation_staleness_recent_days', '10', NOW())
                   ON CONFLICT (key) DO UPDATE SET value='10', updated_at=NOW()""",
            )
            try:
                await conn.execute(
                    """INSERT INTO deliberations
                           (id, patient_id, status, triggered_at,
                            convergence_score, rounds_completed, deliberation_trigger)
                       VALUES ($1::uuid, $2::uuid, 'complete', $3, 0.9, 2, 'pcp')""",
                    delib_id, pid, old_ts,
                )
                await conn.execute(
                    """INSERT INTO deliberation_outputs
                           (id, deliberation_id, output_type, output_data,
                            confidence, priority)
                       VALUES ($1::uuid, $2::uuid, 'predicted_patient_question',
                               $3::jsonb, 0.8, 'medium')""",
                    str(uuid.uuid4()), delib_id,
                    _json.dumps({"question": "Why am I tired?"}),
                )
            except Exception as exc:
                pytest.skip(f"deliberations schema incompatible with test: {exc}")

        result_str = await pvb.generate_previsit_brief(pid)
        assert not result_str.startswith("Error"), result_str
        brief = _json.loads(result_str)

        delib = brief["recent_deliberation"]["value"]
        assert delib is not None, "stale deliberation must still be included"
        assert delib["freshness"]["tier"] in ("recent", "stale")
        assert delib["freshness"]["provenance_tag"] in (
            "PRIOR_SESSION", "PRIOR_SESSION_STALE"
        )
        # Composed field picks up the prior_session tier
        assert brief["patient_questions"]["_provenance"]["tier"] in (
            "PRIOR_SESSION", "PRIOR_SESSION_STALE"
        )
        assert len(brief["patient_questions"]["value"]) >= 1
