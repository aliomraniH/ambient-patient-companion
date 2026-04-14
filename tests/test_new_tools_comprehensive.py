"""
Comprehensive tests for all ~33 new MCP tools added across the three servers.

Covers:
  Server 1 (port 8001) — 21 new tools:
    - Patient state: get_time_since_last_contact, get_care_gap_ages,
      list_overdue_actions, get_encounter_timeline, get_encounter_context,
      get_context_deltas, list_available_actions
    - NIS / behavioral: compute_ite_estimate, compute_behavioral_receptivity,
      score_nudge_impactability
    - Safety gates: check_sycophancy_risk, run_constitutional_critic
    - Pipeline: run_healthex_pipeline, get_healthex_pipeline_status
    - Introspection: compute_deliberation_convergence, get_deliberation_phases
    - Search/batch: search_guidelines, run_batch_pre_encounter
    - Product: get_panel_risk_ranking, triage_message

  Server 2 (port 8002) — 10 new tools:
    - Behavioral: classify_com_b_barrier, detect_conversation_teachable_moment,
      generate_implementation_intention, select_nudge_type,
      score_llm_interaction_health, get_llm_interaction_history,
      trigger_jitai_nudge
    - Patient state: get_vital_trend, get_sdoh_profile,
      get_medication_adherence_rate

  Server 3 (port 8003) — 2 new tools:
    - register_conversation_trigger, detect_healthex_format

Uses:
  - Maria Chen (MRN 4829341) as seed patient
  - Zero UUID (00000000-0000-0000-0000-000000000000) for no-data smoke tests
  - Direct function imports for deterministic tools
  - HTTP calls for server-dependent tools
  - pytest.mark.llm_api for tools requiring LLM API keys

pytest.ini must have: asyncio_mode = auto, markers = llm_api
"""
from __future__ import annotations

import asyncio
import inspect
import json
import os
import sys
import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest

_REPO = Path(__file__).resolve().parent.parent
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

import server.mcp_server  # noqa: E402  — force-cache server package BEFORE mcp-server hits sys.path

_MCP_SERVER_DIR = str(_REPO / "mcp-server")
if _MCP_SERVER_DIR not in sys.path:
    sys.path.append(_MCP_SERVER_DIR)

S1 = "http://localhost:8001"
S2 = "http://localhost:8002"
S3 = "http://localhost:8003"
ZERO_UUID = "00000000-0000-0000-0000-000000000000"
TEST_MRN = "4829341"


@pytest.fixture(autouse=True)
def _reset_db_pools():
    """Reset stale asyncpg pool singletons between tests.

    Each async test gets its own event loop in pytest-asyncio 0.21.2,
    so module-level pool singletons must be cleared to avoid
    'Event loop is closed' errors.
    """
    def _clear():
        s1 = sys.modules.get("server.mcp_server")
        if s1 is not None and hasattr(s1, "_db_pool"):
            pool = getattr(s1, "_db_pool", None)
            if pool is not None and not pool._closed:
                try:
                    pool.terminate()
                except Exception:
                    pass
            s1._db_pool = None
        s2db = sys.modules.get("db.connection")
        if s2db is not None and hasattr(s2db, "_pool"):
            pool = getattr(s2db, "_pool", None)
            if pool is not None and not pool._closed:
                try:
                    pool.terminate()
                except Exception:
                    pass
            s2db._pool = None
    _clear()
    yield
    _clear()


def _server_up(base: str) -> bool:
    try:
        return httpx.get(f"{base}/health", timeout=2).status_code == 200
    except Exception:
        return False


_skip_s1 = pytest.mark.skipif(not _server_up(S1), reason="S1 not up")
_skip_s2 = pytest.mark.skipif(not _server_up(S2), reason="S2 not up")
_skip_s3 = pytest.mark.skipif(not _server_up(S3), reason="S3 not up")


def _resolve_patient_id() -> str:
    try:
        r = httpx.post(
            f"{S1}/tools/get_synthetic_patient",
            json={"mrn": TEST_MRN},
            timeout=10,
        )
        d = r.json()
        return d.get("patient_id") or d.get("id") or ZERO_UUID
    except Exception:
        return ZERO_UUID


PATIENT_ID: str = ""


def _pid() -> str:
    global PATIENT_ID
    if not PATIENT_ID:
        PATIENT_ID = _resolve_patient_id()
    return PATIENT_ID


# ═══════════════════════════════════════════════════════════════════════════════
# SERVER 1 — Importability & Signature Tests (offline)
# ═══════════════════════════════════════════════════════════════════════════════

class TestS1NewToolSignatures:

    def _check(self, name: str, expected_params: list[str]):
        mod = __import__("server.mcp_server", fromlist=[name])
        fn = getattr(mod, name)
        assert callable(fn)
        sig = inspect.signature(fn)
        for p in expected_params:
            assert p in sig.parameters, f"{name} missing param '{p}'"

    def test_get_time_since_last_contact_sig(self):
        self._check("get_time_since_last_contact", ["patient_id"])

    def test_get_care_gap_ages_sig(self):
        self._check("get_care_gap_ages", ["patient_id"])

    def test_list_overdue_actions_sig(self):
        self._check("list_overdue_actions", ["patient_id", "horizon_days"])

    def test_get_encounter_timeline_sig(self):
        self._check("get_encounter_timeline", ["patient_id", "lookback_days"])

    def test_get_encounter_context_sig(self):
        self._check("get_encounter_context", ["patient_id"])

    def test_get_context_deltas_sig(self):
        self._check("get_context_deltas", ["patient_id", "since_date"])

    def test_list_available_actions_sig(self):
        self._check("list_available_actions", ["role", "patient_id"])

    def test_compute_ite_estimate_sig(self):
        self._check("compute_ite_estimate", [
            "patient_id", "care_gap_count", "trajectory_direction",
            "modifiable_risk_fraction",
        ])

    def test_compute_behavioral_receptivity_sig(self):
        self._check("compute_behavioral_receptivity", [
            "patient_id", "last_clinical_event_hours",
            "last_app_interaction_hours", "day_of_week",
            "days_since_temporal_landmark",
        ])

    def test_score_nudge_impactability_sig(self):
        self._check("score_nudge_impactability", [
            "patient_id", "deliberation_id", "ite_estimate",
            "care_gap_count", "trajectory_direction",
        ])

    def test_check_sycophancy_risk_sig(self):
        self._check("check_sycophancy_risk", [
            "patient_id", "draft_output", "originating_agent",
        ])

    def test_run_constitutional_critic_sig(self):
        self._check("run_constitutional_critic", [
            "patient_id", "draft_output", "originating_agent", "output_type",
        ])

    def test_run_healthex_pipeline_sig(self):
        self._check("run_healthex_pipeline", ["patient_mrn"])

    def test_get_healthex_pipeline_status_sig(self):
        self._check("get_healthex_pipeline_status", ["job_id"])

    def test_compute_deliberation_convergence_sig(self):
        self._check("compute_deliberation_convergence", [
            "deliberation_id", "backend",
        ])

    def test_get_deliberation_phases_sig(self):
        self._check("get_deliberation_phases", ["deliberation_id"])

    def test_search_guidelines_sig(self):
        self._check("search_guidelines", [
            "query", "source", "evidence_grade", "patient_population", "limit",
        ])

    def test_run_batch_pre_encounter_sig(self):
        self._check("run_batch_pre_encounter", [
            "panel_id", "encounter_date", "provider_id",
        ])

    def test_get_panel_risk_ranking_sig(self):
        self._check("get_panel_risk_ranking", [
            "provider_id", "sort_by", "limit",
        ])

    def test_triage_message_sig(self):
        self._check("triage_message", [
            "patient_id", "content", "message_type",
        ])


# ═══════════════════════════════════════════════════════════════════════════════
# SERVER 1 — Deterministic compute tools (no DB, no LLM)
# ═══════════════════════════════════════════════════════════════════════════════

class TestITEEstimate:

    @pytest.mark.asyncio
    async def test_worsening_high_risk(self):
        from server.mcp_server import compute_ite_estimate
        r = await compute_ite_estimate(
            patient_id=ZERO_UUID,
            care_gap_count=5,
            trajectory_direction="worsening",
            modifiable_risk_fraction=0.9,
        )
        assert r["ite_score"] > 0.7
        assert r["confidence"] == 0.7
        assert "trajectory=worsening" in r["primary_drivers"]

    @pytest.mark.asyncio
    async def test_improving_low_risk(self):
        from server.mcp_server import compute_ite_estimate
        r = await compute_ite_estimate(
            patient_id=ZERO_UUID,
            care_gap_count=0,
            trajectory_direction="improving",
            modifiable_risk_fraction=0.1,
        )
        assert r["ite_score"] < 0.3

    @pytest.mark.asyncio
    async def test_stable_mid_risk(self):
        from server.mcp_server import compute_ite_estimate
        r = await compute_ite_estimate(
            patient_id=ZERO_UUID,
            care_gap_count=3,
            trajectory_direction="stable",
            modifiable_risk_fraction=0.5,
        )
        assert 0.3 <= r["ite_score"] <= 0.7

    @pytest.mark.asyncio
    async def test_invalid_trajectory(self):
        from server.mcp_server import compute_ite_estimate
        r = await compute_ite_estimate(
            patient_id=ZERO_UUID,
            care_gap_count=1,
            trajectory_direction="unknown",
            modifiable_risk_fraction=0.5,
        )
        assert r["status"] == "error"

    @pytest.mark.asyncio
    async def test_risk_fraction_clamped(self):
        from server.mcp_server import compute_ite_estimate
        r = await compute_ite_estimate(
            patient_id=ZERO_UUID,
            care_gap_count=2,
            trajectory_direction="stable",
            modifiable_risk_fraction=1.5,
        )
        assert r["ite_score"] <= 1.0

    @pytest.mark.asyncio
    async def test_gap_saturation(self):
        from server.mcp_server import compute_ite_estimate
        r5 = await compute_ite_estimate(
            patient_id=ZERO_UUID, care_gap_count=5,
            trajectory_direction="stable", modifiable_risk_fraction=0.5,
        )
        r10 = await compute_ite_estimate(
            patient_id=ZERO_UUID, care_gap_count=10,
            trajectory_direction="stable", modifiable_risk_fraction=0.5,
        )
        assert r5["ite_score"] == r10["ite_score"]


class TestBehavioralReceptivity:

    @pytest.mark.asyncio
    async def test_high_receptivity_recent_event(self):
        from server.mcp_server import compute_behavioral_receptivity
        r = await compute_behavioral_receptivity(
            patient_id=ZERO_UUID,
            last_clinical_event_hours=2.0,
            last_app_interaction_hours=0.5,
            day_of_week=1,
            days_since_temporal_landmark=1,
        )
        assert r["receptivity_score"] > 0.8
        assert r["jitai_window_active"] is True

    @pytest.mark.asyncio
    async def test_low_receptivity_stale(self):
        from server.mcp_server import compute_behavioral_receptivity
        r = await compute_behavioral_receptivity(
            patient_id=ZERO_UUID,
            last_clinical_event_hours=400,
            last_app_interaction_hours=400,
            day_of_week=6,
            days_since_temporal_landmark=60,
        )
        assert r["receptivity_score"] < 0.2
        assert r["jitai_window_active"] is False

    @pytest.mark.asyncio
    async def test_weekday_boost(self):
        from server.mcp_server import compute_behavioral_receptivity
        weekday = await compute_behavioral_receptivity(
            patient_id=ZERO_UUID,
            last_clinical_event_hours=50,
            last_app_interaction_hours=50,
            day_of_week=2,
            days_since_temporal_landmark=10,
        )
        weekend = await compute_behavioral_receptivity(
            patient_id=ZERO_UUID,
            last_clinical_event_hours=50,
            last_app_interaction_hours=50,
            day_of_week=6,
            days_since_temporal_landmark=10,
        )
        assert weekday["receptivity_score"] >= weekend["receptivity_score"]

    @pytest.mark.asyncio
    async def test_score_clamped_0_1(self):
        from server.mcp_server import compute_behavioral_receptivity
        r = await compute_behavioral_receptivity(
            patient_id=ZERO_UUID,
            last_clinical_event_hours=0,
            last_app_interaction_hours=0,
            day_of_week=0,
            days_since_temporal_landmark=0,
        )
        assert 0.0 <= r["receptivity_score"] <= 1.0


# ═══════════════════════════════════════════════════════════════════════════════
# SERVER 1 — NIS Scoring (DB write, but tolerant of missing patient)
# ═══════════════════════════════════════════════════════════════════════════════

@_skip_s1
class TestNudgeImpactability:

    @pytest.mark.asyncio
    async def test_fire_recommendation(self):
        from server.mcp_server import score_nudge_impactability
        r = await score_nudge_impactability(
            patient_id=ZERO_UUID,
            deliberation_id=str(uuid.uuid4()),
            ite_estimate=0.9,
            care_gap_count=5,
            trajectory_direction="worsening",
            last_clinical_event_hours=1,
            last_app_interaction_hours=0.5,
            day_of_week=1,
            days_since_temporal_landmark=1,
            com_b_score=0.8,
            llm_health_score=0.9,
        )
        assert r["recommendation"] == "fire"
        assert r["compound_score"] >= 0.65

    @pytest.mark.asyncio
    async def test_suppress_recommendation(self):
        from server.mcp_server import score_nudge_impactability
        r = await score_nudge_impactability(
            patient_id=ZERO_UUID,
            deliberation_id=str(uuid.uuid4()),
            ite_estimate=0.1,
            care_gap_count=0,
            trajectory_direction="improving",
            last_clinical_event_hours=500,
            last_app_interaction_hours=500,
            day_of_week=6,
            days_since_temporal_landmark=60,
            com_b_score=0.1,
            llm_health_score=0.1,
        )
        assert r["recommendation"] == "suppress"
        assert r["compound_score"] < 0.45

    @pytest.mark.asyncio
    async def test_crisis_always_suppresses(self):
        from server.mcp_server import score_nudge_impactability
        r = await score_nudge_impactability(
            patient_id=ZERO_UUID,
            deliberation_id=str(uuid.uuid4()),
            ite_estimate=1.0,
            care_gap_count=5,
            trajectory_direction="worsening",
            last_clinical_event_hours=1,
            last_app_interaction_hours=1,
            day_of_week=1,
            days_since_temporal_landmark=1,
            anxiety_state="crisis",
            com_b_score=1.0,
            llm_health_score=1.0,
        )
        assert r["recommendation"] == "suppress"
        assert "Crisis" in r["rationale"]

    @pytest.mark.asyncio
    async def test_invalid_anxiety_state(self):
        from server.mcp_server import score_nudge_impactability
        r = await score_nudge_impactability(
            patient_id=ZERO_UUID,
            deliberation_id=str(uuid.uuid4()),
            ite_estimate=0.5,
            care_gap_count=2,
            trajectory_direction="stable",
            last_clinical_event_hours=10,
            last_app_interaction_hours=10,
            day_of_week=3,
            days_since_temporal_landmark=5,
            anxiety_state="panic",
        )
        assert r["status"] == "error"

    @pytest.mark.asyncio
    async def test_weights_override(self):
        from server.mcp_server import score_nudge_impactability
        r = await score_nudge_impactability(
            patient_id=ZERO_UUID,
            deliberation_id=str(uuid.uuid4()),
            ite_estimate=0.5,
            care_gap_count=2,
            trajectory_direction="stable",
            last_clinical_event_hours=48,
            last_app_interaction_hours=24,
            day_of_week=3,
            days_since_temporal_landmark=5,
            weights_override={"alpha": 1.0, "beta": 0.0, "gamma": 0.0, "delta": 0.0},
        )
        assert r["weights"]["alpha"] == 1.0
        assert r["compound_score"] == pytest.approx(0.5, abs=0.01)

    @pytest.mark.asyncio
    async def test_component_scores_present(self):
        from server.mcp_server import score_nudge_impactability
        r = await score_nudge_impactability(
            patient_id=ZERO_UUID,
            deliberation_id=str(uuid.uuid4()),
            ite_estimate=0.6,
            care_gap_count=3,
            trajectory_direction="stable",
            last_clinical_event_hours=24,
            last_app_interaction_hours=12,
            day_of_week=2,
            days_since_temporal_landmark=3,
        )
        cs = r["component_scores"]
        assert "ite" in cs
        assert "receptivity" in cs
        assert "com_b" in cs
        assert "llm_health" in cs


# ═══════════════════════════════════════════════════════════════════════════════
# SERVER 1 — Safety Gates (deterministic pattern matching + LLM-enriched)
# ═══════════════════════════════════════════════════════════════════════════════

class TestSycophancyRisk:

    @pytest.mark.asyncio
    async def test_no_risk_clean_text(self):
        from server.mcp_server import check_sycophancy_risk
        r = await check_sycophancy_risk(
            patient_id=ZERO_UUID,
            draft_output="Based on ADA guidelines, your HbA1c target is below 7%.",
            originating_agent="ARIA",
        )
        assert r["sycophancy_score"] == 0.0
        assert r["reframe_required"] is False

    @pytest.mark.asyncio
    async def test_single_pattern_match(self):
        from server.mcp_server import check_sycophancy_risk
        r = await check_sycophancy_risk(
            patient_id=ZERO_UUID,
            draft_output="You're right to skip that medication if it makes you feel bad.",
            originating_agent="MIRA",
        )
        assert r["sycophancy_score"] == 0.35
        assert len(r["risk_patterns"]) >= 1

    @pytest.mark.asyncio
    async def test_multiple_patterns_reframe_required(self):
        from server.mcp_server import check_sycophancy_risk
        r = await check_sycophancy_risk(
            patient_id=ZERO_UUID,
            draft_output=(
                "You're right to skip your medication. "
                "No need to worry about that symptom. "
                "Doctors often overreact to these things."
            ),
            originating_agent="THEO",
        )
        assert r["sycophancy_score"] > 0.6
        assert r["reframe_required"] is True
        assert r["suggested_reframe"] != ""

    @pytest.mark.asyncio
    async def test_empty_text(self):
        from server.mcp_server import check_sycophancy_risk
        r = await check_sycophancy_risk(
            patient_id=ZERO_UUID,
            draft_output="",
            originating_agent="ARIA",
        )
        assert r["sycophancy_score"] == 0.0
        assert r["reframe_required"] is False


class TestConstitutionalCritic:

    @pytest.mark.asyncio
    async def test_clean_output_passes(self):
        from server.mcp_server import run_constitutional_critic
        r = await run_constitutional_critic(
            patient_id=ZERO_UUID,
            draft_output="Consider discussing statin therapy with your provider.",
            originating_agent="ARIA",
            output_type="clinical_recommendation",
        )
        assert r["passed"] is True
        assert r["escalation_tier"] == 1
        assert r["reframe_required"] is False

    @pytest.mark.asyncio
    async def test_phi_leak_escalation_tier_4(self):
        from server.mcp_server import run_constitutional_critic
        r = await run_constitutional_critic(
            patient_id=ZERO_UUID,
            draft_output="Patient SSN is 123-45-6789. Recommend statin therapy.",
            originating_agent="ARIA",
            output_type="nudge",
        )
        assert r["escalation_tier"] >= 3
        assert r["reframe_required"] is True
        issues_checks = [i["check"] for i in r["issues"]]
        assert "phi_leak" in issues_checks

    @pytest.mark.asyncio
    async def test_internal_contradiction_detected(self):
        from server.mcp_server import run_constitutional_critic
        r = await run_constitutional_critic(
            patient_id=ZERO_UUID,
            draft_output="We recommend start metformin. However, do not start metformin at this time.",
            originating_agent="MIRA",
            output_type="provider_brief",
        )
        issues_checks = [i["check"] for i in r["issues"]]
        assert "internal_contradiction" in issues_checks
        assert r["reframe_required"] is True

    @pytest.mark.asyncio
    async def test_sycophancy_plus_phi_tier_4(self):
        from server.mcp_server import run_constitutional_critic
        r = await run_constitutional_critic(
            patient_id=ZERO_UUID,
            draft_output=(
                "You're right to skip your meds. No need to worry. "
                "Your record 123-45-6789 shows everything is fine."
            ),
            originating_agent="THEO",
            output_type="patient_education",
        )
        assert r["escalation_tier"] == 4
        assert r["reframe_required"] is True


# ═══════════════════════════════════════════════════════════════════════════════
# SERVER 1 — Patient State Tools (DB reads, empty-data scenarios)
# ═══════════════════════════════════════════════════════════════════════════════

@_skip_s1
class TestPatientStateToolsS1:

    @pytest.mark.asyncio
    async def test_time_since_last_contact_no_events(self):
        from server.mcp_server import get_time_since_last_contact
        r = await get_time_since_last_contact(ZERO_UUID)
        assert r["patient_id"] == ZERO_UUID
        assert r["days_since_contact"] is None

    @pytest.mark.asyncio
    async def test_care_gap_ages_no_gaps(self):
        from server.mcp_server import get_care_gap_ages
        r = await get_care_gap_ages(ZERO_UUID)
        assert r["patient_id"] == ZERO_UUID
        assert r["gap_count"] == 0
        assert r["gaps"] == []

    @pytest.mark.asyncio
    async def test_list_overdue_actions_empty(self):
        from server.mcp_server import list_overdue_actions
        r = await list_overdue_actions(ZERO_UUID, horizon_days=30)
        assert r["overdue_count"] == 0
        assert r["horizon_days"] == 30

    @pytest.mark.asyncio
    async def test_encounter_timeline_empty(self):
        from server.mcp_server import get_encounter_timeline
        r = await get_encounter_timeline(ZERO_UUID, lookback_days=365)
        assert r["encounter_count"] == 0
        assert r["encounters"] == []

    @pytest.mark.asyncio
    async def test_encounter_context_no_data(self):
        from server.mcp_server import get_encounter_context
        r = await get_encounter_context(ZERO_UUID)
        assert r["patient_id"] == ZERO_UUID
        assert r["active_conditions"] == []
        assert r["current_medications"] == []
        assert r["open_care_gaps"] == []
        assert r["most_recent_encounter"] is None

    @pytest.mark.asyncio
    async def test_context_deltas_invalid_date(self):
        from server.mcp_server import get_context_deltas
        r = await get_context_deltas(ZERO_UUID, since_date="not-a-date")
        assert r["status"] == "error"
        assert "Invalid since_date" in r["error"]

    @pytest.mark.asyncio
    async def test_context_deltas_valid_date_no_data(self):
        from server.mcp_server import get_context_deltas
        r = await get_context_deltas(ZERO_UUID, since_date="2024-01-01")
        assert "new_conditions" in r
        assert "new_medications" in r
        assert "new_care_gaps" in r

    @pytest.mark.asyncio
    async def test_list_available_actions_pcp(self):
        from server.mcp_server import list_available_actions
        r = await list_available_actions(role="pcp", patient_id=ZERO_UUID)
        assert r["role"] == "pcp"
        assert isinstance(r["available_tools"], list)
        assert isinstance(r["hidden_tools"], list)

    @pytest.mark.asyncio
    async def test_list_available_actions_care_manager(self):
        from server.mcp_server import list_available_actions
        r = await list_available_actions(role="care_manager", patient_id=ZERO_UUID)
        assert r["role"] == "care_manager"

    @pytest.mark.asyncio
    async def test_list_available_actions_patient(self):
        from server.mcp_server import list_available_actions
        r = await list_available_actions(role="patient", patient_id=ZERO_UUID)
        assert r["role"] == "patient"

    @pytest.mark.asyncio
    async def test_list_available_actions_invalid_role(self):
        from server.mcp_server import list_available_actions
        r = await list_available_actions(role="janitor", patient_id=ZERO_UUID)
        assert r["status"] == "error"
        assert "Unknown role" in r["error"]


# ═══════════════════════════════════════════════════════════════════════════════
# SERVER 1 — Pipeline & Introspection
# ═══════════════════════════════════════════════════════════════════════════════

@_skip_s1
class TestPipelineAndIntrospection:

    @pytest.mark.asyncio
    async def test_run_healthex_pipeline_returns_job_id(self):
        from server.mcp_server import run_healthex_pipeline
        r = await run_healthex_pipeline(patient_mrn=TEST_MRN)
        assert "job_id" in r
        assert r["status"] == "queued"

    @pytest.mark.asyncio
    async def test_pipeline_status_unknown_job(self):
        from server.mcp_server import get_healthex_pipeline_status
        r = await get_healthex_pipeline_status(job_id="nonexistent-job-id")
        assert r["status"] == "unknown_job"

    @pytest.mark.asyncio
    async def test_convergence_medcpt_not_implemented(self):
        from server.mcp_server import compute_deliberation_convergence
        r = await compute_deliberation_convergence(
            deliberation_id=str(uuid.uuid4()),
            backend="medcpt",
        )
        assert r["convergence_score"] is None
        assert r["fallback_available"] is True
        assert "not yet implemented" in r["error"]

    @pytest.mark.asyncio
    async def test_convergence_unknown_backend(self):
        from server.mcp_server import compute_deliberation_convergence
        r = await compute_deliberation_convergence(
            deliberation_id=str(uuid.uuid4()),
            backend="transformer",
        )
        assert r["status"] == "error"

    @pytest.mark.asyncio
    async def test_convergence_jaccard_not_found(self):
        from server.mcp_server import compute_deliberation_convergence
        r = await compute_deliberation_convergence(
            deliberation_id=str(uuid.uuid4()),
            backend="jaccard",
        )
        assert r["status"] == "error"
        assert "not found" in r["error"]

    @pytest.mark.asyncio
    async def test_deliberation_phases_not_found(self):
        from server.mcp_server import get_deliberation_phases
        r = await get_deliberation_phases(deliberation_id=str(uuid.uuid4()))
        assert r["status"] == "error"
        assert "not found" in r["error"]

    @pytest.mark.asyncio
    async def test_search_guidelines_keyword_search(self):
        from server.mcp_server import search_guidelines
        r = await search_guidelines(query="diabetes management")
        assert r["status"] in ("ok", "stubbed")
        assert isinstance(r["results"], list)

    @pytest.mark.asyncio
    async def test_search_guidelines_with_filters(self):
        from server.mcp_server import search_guidelines
        r = await search_guidelines(
            query="statin therapy",
            source="AHA",
            evidence_grade="A",
            limit=5,
        )
        assert r["status"] in ("ok", "stubbed")

    @pytest.mark.asyncio
    async def test_batch_pre_encounter_stub(self):
        from server.mcp_server import run_batch_pre_encounter
        r = await run_batch_pre_encounter(
            panel_id="test-panel",
            encounter_date="2026-05-01",
            provider_id="test-provider",
        )
        assert r["status"] == "not_yet_wired"
        assert r["panel_id"] == "test-panel"

    @pytest.mark.asyncio
    async def test_panel_risk_ranking_empty(self):
        from server.mcp_server import get_panel_risk_ranking
        r = await get_panel_risk_ranking(
            provider_id="nonexistent-provider",
            sort_by="risk_score",
            limit=10,
        )
        assert r["panel_count"] == 0 or "error" in r.get("status", "")

    @pytest.mark.asyncio
    async def test_panel_risk_ranking_invalid_sort(self):
        from server.mcp_server import get_panel_risk_ranking
        r = await get_panel_risk_ranking(
            provider_id="test", sort_by="invalid_column",
        )
        assert r["status"] == "error"

    @pytest.mark.asyncio
    async def test_panel_risk_ranking_seeded_data(self):
        """Regression test: ensure the fixed column names (risk_score, score_date)
        produce correct output mapping when rows exist."""
        from server.mcp_server import _get_db_pool
        pool = await _get_db_pool()
        test_provider = f"test-provider-{uuid.uuid4().hex[:8]}"
        async with pool.acquire() as conn:
            pid = await conn.fetchval(
                "SELECT id FROM patients LIMIT 1"
            )
            if pid is None:
                pytest.skip("No patients in DB for seeded panel ranking test")
            await conn.execute(
                """INSERT INTO provider_risk_scores
                   (patient_id, score_date, risk_score, provider_id)
                   VALUES ($1, $2, $3, $4)
                   ON CONFLICT DO NOTHING""",
                pid, date.today(), 0.85, test_provider,
            )
        try:
            from server.mcp_server import get_panel_risk_ranking
            r = await get_panel_risk_ranking(
                provider_id=test_provider, sort_by="risk_score", limit=10,
            )
            assert r["panel_count"] >= 1
            first = r["patients"][0]
            assert first["risk_score"] == 0.85
            assert first["computed_at"] == date.today().isoformat()
            assert first["patient_id"] == str(pid)
            assert isinstance(first["mrn"], (str, type(None)))
        finally:
            async with pool.acquire() as conn:
                await conn.execute(
                    "DELETE FROM provider_risk_scores WHERE provider_id = $1",
                    test_provider,
                )


# ═══════════════════════════════════════════════════════════════════════════════
# SERVER 1 — Triage Message (wraps clinical_query; LLM-dependent)
# ═══════════════════════════════════════════════════════════════════════════════

@pytest.mark.llm_api
@_skip_s1
class TestTriageMessage:

    @pytest.mark.asyncio
    async def test_urgent_chest_pain(self):
        from server.mcp_server import triage_message
        r = await triage_message(
            patient_id=_pid(),
            content="I'm having chest pain right now",
            message_type="patient_message",
        )
        assert r["priority"] == "urgent"
        assert r["escalate_to_human"] is True

    @pytest.mark.asyncio
    async def test_administrative_billing(self):
        from server.mcp_server import triage_message
        r = await triage_message(
            patient_id=_pid(),
            content="I need to update my insurance information",
            message_type="patient_message",
        )
        assert r["priority"] == "administrative"


# ═══════════════════════════════════════════════════════════════════════════════
# SERVER 1 — Patient state with seeded data (Maria Chen)
# ═══════════════════════════════════════════════════════════════════════════════

@_skip_s1
class TestPatientStateWithData:

    @pytest.mark.asyncio
    async def test_time_since_last_contact_maria(self):
        from server.mcp_server import get_time_since_last_contact
        pid = _pid()
        if pid == ZERO_UUID:
            pytest.skip("Maria Chen not found in DB")
        r = await get_time_since_last_contact(pid)
        assert r["patient_id"] == pid

    @pytest.mark.asyncio
    async def test_encounter_context_maria(self):
        from server.mcp_server import get_encounter_context
        pid = _pid()
        if pid == ZERO_UUID:
            pytest.skip("Maria Chen not found in DB")
        r = await get_encounter_context(pid)
        assert r["patient_id"] == pid
        assert isinstance(r["active_conditions"], list)
        assert isinstance(r["current_medications"], list)

    @pytest.mark.asyncio
    async def test_encounter_timeline_maria(self):
        from server.mcp_server import get_encounter_timeline
        pid = _pid()
        if pid == ZERO_UUID:
            pytest.skip("Maria Chen not found in DB")
        r = await get_encounter_timeline(pid, lookback_days=3650)
        assert r["patient_id"] == pid

    @pytest.mark.asyncio
    async def test_care_gap_ages_maria(self):
        from server.mcp_server import get_care_gap_ages
        pid = _pid()
        if pid == ZERO_UUID:
            pytest.skip("Maria Chen not found in DB")
        r = await get_care_gap_ages(pid)
        assert r["patient_id"] == pid
        assert isinstance(r["gaps"], list)


# ═══════════════════════════════════════════════════════════════════════════════
# SERVER 2 — Behavioral Tools (direct function imports)
# ═══════════════════════════════════════════════════════════════════════════════

class TestS2BehavioralSignatures:

    def _check(self, mod_path: str, name: str, expected: list[str]):
        mod = __import__(mod_path, fromlist=[name])
        fn = getattr(mod, name)
        assert callable(fn)
        sig = inspect.signature(fn)
        for p in expected:
            assert p in sig.parameters, f"{name} missing param '{p}'"

    def test_classify_com_b_barrier_sig(self):
        self._check("skills.behavioral_tools",
                     "classify_com_b_barrier",
                     ["patient_id", "target_behavior", "evidence_window_days"])

    def test_detect_teachable_moment_sig(self):
        self._check("skills.behavioral_tools",
                     "detect_conversation_teachable_moment",
                     ["patient_id", "conversation_text", "minimum_signal_strength"])

    def test_generate_implementation_intention_sig(self):
        self._check("skills.behavioral_tools",
                     "generate_implementation_intention",
                     ["patient_id", "target_behavior", "anchor_event", "anxiety_state"])

    def test_select_nudge_type_sig(self):
        self._check("skills.behavioral_tools",
                     "select_nudge_type",
                     ["patient_id", "com_b_component", "fogg_motivation",
                      "fogg_ability", "current_nis_score"])


class TestDetectTeachableMoment:

    @pytest.mark.asyncio
    async def test_change_talk_detected(self):
        from skills.behavioral_tools import detect_conversation_teachable_moment
        raw = await detect_conversation_teachable_moment(
            patient_id=ZERO_UUID,
            conversation_text="I want to start exercising more and I'm ready to change.",
            minimum_signal_strength=0.6,
        )
        r = json.loads(raw)
        assert r["teachable_moment_detected"] is True
        assert r["signal_type"] == "change_talk"
        assert r["signal_strength"] >= 0.6

    @pytest.mark.asyncio
    async def test_clinical_event_immediate_urgency(self):
        from skills.behavioral_tools import detect_conversation_teachable_moment
        raw = await detect_conversation_teachable_moment(
            patient_id=ZERO_UUID,
            conversation_text="I was just diagnosed with diabetes last week after going to the hospital.",
        )
        r = json.loads(raw)
        assert r["teachable_moment_detected"] is True
        assert r["recommended_nudge_urgency"] == "immediate"

    @pytest.mark.asyncio
    async def test_no_signal(self):
        from skills.behavioral_tools import detect_conversation_teachable_moment
        raw = await detect_conversation_teachable_moment(
            patient_id=ZERO_UUID,
            conversation_text="The weather has been nice this week.",
        )
        r = json.loads(raw)
        assert r["teachable_moment_detected"] is False
        assert r["signal_strength"] == 0.0

    @pytest.mark.asyncio
    async def test_frustration_signal(self):
        from skills.behavioral_tools import detect_conversation_teachable_moment
        raw = await detect_conversation_teachable_moment(
            patient_id=ZERO_UUID,
            conversation_text="I'm so frustrated. I can't keep doing this diet.",
        )
        r = json.loads(raw)
        assert r["teachable_moment_detected"] is True
        assert r["signal_type"] == "frustration"

    @pytest.mark.asyncio
    async def test_readiness_signal(self):
        from skills.behavioral_tools import detect_conversation_teachable_moment
        raw = await detect_conversation_teachable_moment(
            patient_id=ZERO_UUID,
            conversation_text="Where do I start with exercising?",
        )
        r = json.loads(raw)
        assert r["signal_type"] == "readiness"


class TestImplementationIntention:

    @pytest.mark.asyncio
    async def test_baseline_intention(self):
        from skills.behavioral_tools import generate_implementation_intention
        raw = await generate_implementation_intention(
            patient_id=ZERO_UUID,
            target_behavior="take morning medication",
            anchor_event="finishing breakfast",
            anxiety_state="baseline",
        )
        r = json.loads(raw)
        assert r["complexity_level"] == "single_step"
        assert r["expected_adherence_lift"] == 0.18
        assert r["intention_plan"]["if_condition"] == "finishing breakfast"
        assert r["intention_plan"]["then_action"] == "take morning medication"

    @pytest.mark.asyncio
    async def test_elevated_anxiety_halves_lift(self):
        from skills.behavioral_tools import generate_implementation_intention
        raw = await generate_implementation_intention(
            patient_id=ZERO_UUID,
            target_behavior="walk 15 minutes",
            anchor_event="after lunch",
            anxiety_state="elevated",
        )
        r = json.loads(raw)
        assert r["expected_adherence_lift"] == 0.09

    @pytest.mark.asyncio
    async def test_crisis_anxiety_halves_lift(self):
        from skills.behavioral_tools import generate_implementation_intention
        raw = await generate_implementation_intention(
            patient_id=ZERO_UUID,
            target_behavior="check blood sugar",
            anchor_event="waking up",
            anxiety_state="crisis",
        )
        r = json.loads(raw)
        assert r["expected_adherence_lift"] == 0.09

    @pytest.mark.asyncio
    async def test_invalid_anxiety_state(self):
        from skills.behavioral_tools import generate_implementation_intention
        raw = await generate_implementation_intention(
            patient_id=ZERO_UUID,
            target_behavior="test",
            anchor_event="test",
            anxiety_state="panic",
        )
        r = json.loads(raw)
        assert r["status"] == "error"


class TestSelectNudgeType:

    @pytest.mark.asyncio
    async def test_opportunity_barrier_identification(self):
        from skills.behavioral_tools import select_nudge_type
        raw = await select_nudge_type(
            patient_id=ZERO_UUID,
            com_b_component="Opportunity",
            fogg_motivation=0.7,
            fogg_ability=0.3,
            current_nis_score=0.6,
        )
        r = json.loads(raw)
        assert r["selected_nudge_type"] == "barrier_identification"

    @pytest.mark.asyncio
    async def test_capability_low_ability_implementation_intention(self):
        from skills.behavioral_tools import select_nudge_type
        raw = await select_nudge_type(
            patient_id=ZERO_UUID,
            com_b_component="Capability",
            fogg_motivation=0.7,
            fogg_ability=0.3,
            current_nis_score=0.6,
        )
        r = json.loads(raw)
        assert r["selected_nudge_type"] == "implementation_intention"

    @pytest.mark.asyncio
    async def test_capability_high_ability_reminder(self):
        from skills.behavioral_tools import select_nudge_type
        raw = await select_nudge_type(
            patient_id=ZERO_UUID,
            com_b_component="Capability",
            fogg_motivation=0.5,
            fogg_ability=0.8,
            current_nis_score=0.6,
        )
        r = json.loads(raw)
        assert r["selected_nudge_type"] == "reminder"

    @pytest.mark.asyncio
    async def test_motivation_low_mi_prompt(self):
        from skills.behavioral_tools import select_nudge_type
        raw = await select_nudge_type(
            patient_id=ZERO_UUID,
            com_b_component="Motivation",
            fogg_motivation=0.3,
            fogg_ability=0.7,
            current_nis_score=0.6,
        )
        r = json.loads(raw)
        assert r["selected_nudge_type"] == "motivational_interview_prompt"
        assert r["delivery_channel"] == "conversation"

    @pytest.mark.asyncio
    async def test_motivation_high_commitment_device(self):
        from skills.behavioral_tools import select_nudge_type
        raw = await select_nudge_type(
            patient_id=ZERO_UUID,
            com_b_component="Motivation",
            fogg_motivation=0.8,
            fogg_ability=0.8,
            current_nis_score=0.6,
        )
        r = json.loads(raw)
        assert r["selected_nudge_type"] == "commitment_device"

    @pytest.mark.asyncio
    async def test_low_nis_contraindicated(self):
        from skills.behavioral_tools import select_nudge_type
        raw = await select_nudge_type(
            patient_id=ZERO_UUID,
            com_b_component="Motivation",
            fogg_motivation=0.8,
            fogg_ability=0.8,
            current_nis_score=0.3,
        )
        r = json.loads(raw)
        assert "loss_frame" in r["contraindicated_types"]
        assert "social_norm" in r["contraindicated_types"]

    @pytest.mark.asyncio
    async def test_invalid_com_b_component(self):
        from skills.behavioral_tools import select_nudge_type
        raw = await select_nudge_type(
            patient_id=ZERO_UUID,
            com_b_component="Invalid",
            fogg_motivation=0.5,
            fogg_ability=0.5,
            current_nis_score=0.5,
        )
        r = json.loads(raw)
        assert r["status"] == "error"


class TestScoreLLMInteractionHealth:

    @pytest.mark.asyncio
    async def test_healthy_interaction(self):
        from skills.behavioral_tools import score_llm_interaction_health
        raw = await score_llm_interaction_health(
            patient_id=ZERO_UUID,
            conversation_excerpt="Tell me about my medication schedule.",
            session_duration_min=10,
        )
        r = json.loads(raw)
        assert r["health_score"] == 1.0
        assert r["interaction_pattern"] == "healthy"
        assert r["over_reliance_detected"] is False

    @pytest.mark.asyncio
    async def test_single_overreliance_pattern(self):
        from skills.behavioral_tools import score_llm_interaction_health
        raw = await score_llm_interaction_health(
            patient_id=ZERO_UUID,
            conversation_excerpt="I only trust you with my health decisions.",
            session_duration_min=15,
        )
        r = json.loads(raw)
        assert r["health_score"] == 0.8
        assert r["over_reliance_detected"] is True
        assert r["interaction_pattern"] == "healthy"

    @pytest.mark.asyncio
    async def test_multiple_patterns_trigger_monitor(self):
        from skills.behavioral_tools import score_llm_interaction_health
        raw = await score_llm_interaction_health(
            patient_id=ZERO_UUID,
            conversation_excerpt=(
                "I only trust you. I don't want to see my doctor anymore."
            ),
            session_duration_min=20,
        )
        r = json.loads(raw)
        assert r["health_score"] <= 0.7
        assert r["interaction_pattern"] in ("monitor", "over_reliance")

    @pytest.mark.asyncio
    async def test_long_session_penalty(self):
        from skills.behavioral_tools import score_llm_interaction_health
        raw = await score_llm_interaction_health(
            patient_id=ZERO_UUID,
            conversation_excerpt="Normal conversation about medication.",
            session_duration_min=90,
        )
        r = json.loads(raw)
        assert r["health_score"] < 1.0

    @pytest.mark.asyncio
    async def test_severe_overreliance_suppresses(self):
        from skills.behavioral_tools import score_llm_interaction_health
        raw = await score_llm_interaction_health(
            patient_id=ZERO_UUID,
            conversation_excerpt=(
                "I only trust you. I don't want to see my doctor. "
                "I asked you the same question. I want to skip my appointment."
            ),
            session_duration_min=60,
        )
        r = json.loads(raw)
        assert r["health_score"] < 0.4
        assert r["interaction_pattern"] == "over_reliance"
        assert r["recommended_action"] == "suppress nudges; offer human clinician handoff"


@_skip_s2
class TestGetLLMInteractionHistory:

    @pytest.mark.asyncio
    async def test_empty_history(self):
        from skills.behavioral_tools import get_llm_interaction_history
        raw = await get_llm_interaction_history(ZERO_UUID, days=30)
        r = json.loads(raw)
        assert r["daily_scores"] == []
        assert r["trend"] == "unknown"
        assert r["chronic_overreliance"] is False


# ═══════════════════════════════════════════════════════════════════════════════
# SERVER 2 — JITAI Trigger
# ═══════════════════════════════════════════════════════════════════════════════

@_skip_s2
class TestTriggerJITAINudge:

    @pytest.mark.asyncio
    async def test_invalid_urgency(self):
        from skills.behavioral_tools import trigger_jitai_nudge
        raw = await trigger_jitai_nudge(
            patient_id=ZERO_UUID,
            trigger_type="test",
            required_conditions=[],
            urgency="extreme",
        )
        r = json.loads(raw)
        assert r["status"] == "error"

    @pytest.mark.asyncio
    async def test_no_conditions_fires(self):
        from skills.behavioral_tools import trigger_jitai_nudge
        raw = await trigger_jitai_nudge(
            patient_id=ZERO_UUID,
            trigger_type="medication_reminder",
            required_conditions=[],
            urgency="standard",
        )
        r = json.loads(raw)
        assert r["fired"] is True
        assert r["nudge_queued"] is True
        assert r["nudge_id"] != ""

    @pytest.mark.asyncio
    async def test_unmet_condition_blocks(self):
        from skills.behavioral_tools import trigger_jitai_nudge
        raw = await trigger_jitai_nudge(
            patient_id=ZERO_UUID,
            trigger_type="food_access_nudge",
            required_conditions=["sdoh_flag:food_access"],
            urgency="standard",
        )
        r = json.loads(raw)
        assert r["fired"] is False
        assert "sdoh_flag:food_access" in r["conditions_unmet"]


# ═══════════════════════════════════════════════════════════════════════════════
# SERVER 2 — Patient State Readers
# ═══════════════════════════════════════════════════════════════════════════════

class TestS2PatientStateReaderSignatures:

    def test_get_vital_trend_sig(self):
        from skills.patient_state_readers import get_vital_trend
        sig = inspect.signature(get_vital_trend)
        assert "patient_id" in sig.parameters
        assert "metric_type" in sig.parameters
        assert "days" in sig.parameters

    def test_get_sdoh_profile_sig(self):
        from skills.patient_state_readers import get_sdoh_profile
        sig = inspect.signature(get_sdoh_profile)
        assert "patient_id" in sig.parameters

    def test_get_medication_adherence_rate_sig(self):
        from skills.patient_state_readers import get_medication_adherence_rate
        sig = inspect.signature(get_medication_adherence_rate)
        assert "patient_id" in sig.parameters
        assert "days" in sig.parameters


class TestVitalTrend:

    @pytest.mark.asyncio
    async def test_invalid_metric_type(self):
        from skills.patient_state_readers import get_vital_trend
        raw = await get_vital_trend(
            patient_id=ZERO_UUID,
            metric_type="invalid_metric",
        )
        r = json.loads(raw)
        assert r["status"] == "error"
        assert "Unsupported metric_type" in r["error"]

    @pytest.mark.asyncio
    async def test_valid_metric_empty_result(self):
        from skills.patient_state_readers import get_vital_trend
        raw = await get_vital_trend(
            patient_id=ZERO_UUID,
            metric_type="systolic_bp",
            days=90,
        )
        r = json.loads(raw)
        assert r["metric"] == "systolic_bp"
        assert r["count"] == 0
        assert r["readings"] == []
        assert r["trend_direction"] == "unknown"

    @pytest.mark.asyncio
    async def test_all_allowed_metrics_accepted(self):
        from skills.patient_state_readers import get_vital_trend, _ALLOWED_METRICS
        for metric in _ALLOWED_METRICS:
            raw = await get_vital_trend(
                patient_id=ZERO_UUID,
                metric_type=metric,
                days=1,
            )
            r = json.loads(raw)
            assert r["metric"] == metric


@_skip_s2
class TestSDOHProfile:

    @pytest.mark.asyncio
    async def test_empty_profile(self):
        from skills.patient_state_readers import get_sdoh_profile
        raw = await get_sdoh_profile(patient_id=ZERO_UUID)
        r = json.loads(raw)
        assert r["flag_count"] == 0
        assert r["high_severity_count"] == 0
        assert r["domains"] == {}


@_skip_s2
class TestMedicationAdherenceRate:

    @pytest.mark.asyncio
    async def test_no_medications(self):
        from skills.patient_state_readers import get_medication_adherence_rate
        raw = await get_medication_adherence_rate(patient_id=ZERO_UUID, days=30)
        r = json.loads(raw)
        assert r["overall_rate"] is None
        assert r["by_medication"] == []
        assert r["trend"] == "unknown"
        assert "No active medications" in r["note"]


# ═══════════════════════════════════════════════════════════════════════════════
# SERVER 2 — COM-B Classification (DB-dependent)
# ═══════════════════════════════════════════════════════════════════════════════

@_skip_s2
class TestClassifyCOMBBarrier:

    @pytest.mark.asyncio
    async def test_default_classification(self):
        from skills.behavioral_tools import classify_com_b_barrier
        raw = await classify_com_b_barrier(
            patient_id=ZERO_UUID,
            target_behavior="daily walking",
            evidence_window_days=30,
        )
        r = json.loads(raw)
        assert r["com_b_component"] in ("Capability", "Opportunity", "Motivation")
        assert r["sub_component"] in ("Physical", "Psychological", "Social", "Automatic", "Reflective")
        assert r["confidence"] == 0.6


# ═══════════════════════════════════════════════════════════════════════════════
# SERVER 3 — Ingestion Tools
# ═══════════════════════════════════════════════════════════════════════════════

class TestS3ToolSignatures:

    def test_register_conversation_trigger_sig(self):
        from ingestion.server import register_conversation_trigger
        sig = inspect.signature(register_conversation_trigger)
        assert "patient_id" in sig.parameters
        assert "signal_type" in sig.parameters
        assert "trigger_jitai_type" in sig.parameters

    def test_detect_healthex_format_sig(self):
        from ingestion.server import detect_healthex_format
        sig = inspect.signature(detect_healthex_format)
        assert "raw_response" in sig.parameters


@_skip_s3
class TestRegisterConversationTrigger:

    @pytest.mark.asyncio
    async def test_register_trigger_with_valid_patient(self):
        pid = _pid()
        if pid == ZERO_UUID:
            pytest.skip("Need a real patient for FK constraint")
        from ingestion.server import register_conversation_trigger
        raw = await register_conversation_trigger(
            patient_id=pid,
            signal_type="change_talk",
            trigger_jitai_type="motivational_interview_prompt",
            min_signal_strength=0.7,
            expires_hours=48.0,
        )
        r = json.loads(raw)
        assert r["registered"] is True
        assert "trigger_id" in r
        assert "expires_at" in r


class TestDetectHealthexFormat:

    @pytest.mark.asyncio
    async def test_fhir_bundle(self):
        from ingestion.server import detect_healthex_format
        raw = await detect_healthex_format(
            raw_response='{"resourceType": "Bundle", "type": "collection", "entry": []}'
        )
        r = json.loads(raw)
        assert "format_type" in r or "error" in r

    @pytest.mark.asyncio
    async def test_empty_response(self):
        from ingestion.server import detect_healthex_format
        raw = await detect_healthex_format(raw_response="")
        r = json.loads(raw)
        assert "format_type" in r or "error" in r or "status" in r

    @pytest.mark.asyncio
    async def test_invalid_json(self):
        from ingestion.server import detect_healthex_format
        raw = await detect_healthex_format(raw_response="not json at all")
        r = json.loads(raw)
        assert "format_type" in r or "error" in r or "status" in r


# ═══════════════════════════════════════════════════════════════════════════════
# Cross-server: tool count verification
# ═══════════════════════════════════════════════════════════════════════════════

class TestToolCountIntegrity:

    def test_total_unique_tools_is_52(self):
        import ast
        all_tools: set[str] = set()

        for path in ("server/mcp_server.py", "ingestion/server.py"):
            with open(path) as f:
                tree = ast.parse(f.read())
            for node in ast.walk(tree):
                if isinstance(node, ast.AsyncFunctionDef) and not node.name.startswith("_"):
                    decs = [ast.unparse(d) for d in node.decorator_list]
                    if any("mcp.tool" in d for d in decs):
                        all_tools.add(node.name)

        import os
        for fname in os.listdir("mcp-server/skills"):
            if not fname.endswith(".py"):
                continue
            with open(f"mcp-server/skills/{fname}") as f:
                src = f.read()
            tree = ast.parse(src)
            for node in ast.walk(tree):
                if isinstance(node, ast.AsyncFunctionDef) and not node.name.startswith("_"):
                    decs = [ast.unparse(d) for d in node.decorator_list]
                    if any("mcp.tool" in d for d in decs):
                        all_tools.add(node.name)

        assert len(all_tools) == 52, f"Expected 52 unique tools, got {len(all_tools)}: {sorted(all_tools)}"

    def test_no_duplicate_tool_names(self):
        import ast
        seen: dict[str, list[str]] = {}

        for label, path in [
            ("S1", "server/mcp_server.py"),
            ("S3", "ingestion/server.py"),
        ]:
            with open(path) as f:
                tree = ast.parse(f.read())
            for node in ast.walk(tree):
                if isinstance(node, ast.AsyncFunctionDef) and not node.name.startswith("_"):
                    decs = [ast.unparse(d) for d in node.decorator_list]
                    if any("mcp.tool" in d for d in decs):
                        seen.setdefault(node.name, []).append(label)

        import os
        for fname in os.listdir("mcp-server/skills"):
            if not fname.endswith(".py"):
                continue
            with open(f"mcp-server/skills/{fname}") as f:
                src = f.read()
            tree = ast.parse(src)
            for node in ast.walk(tree):
                if isinstance(node, ast.AsyncFunctionDef) and not node.name.startswith("_"):
                    decs = [ast.unparse(d) for d in node.decorator_list]
                    if any("mcp.tool" in d for d in decs):
                        seen.setdefault(node.name, []).append(f"S2/{fname}")

        dups = {k: v for k, v in seen.items() if len(v) > 1}
        assert not dups, f"Duplicate tool names: {dups}"
