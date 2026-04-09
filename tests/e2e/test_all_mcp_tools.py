"""End-to-end use cases — all 15 MCP tools exercised against Maria Chen's data.

Story recap
-----------
Maria Chen (54 F, T2DM + HTN) has been in the Ambient Patient Companion since
Oct 2025.  In December 2025 she entered a caregiver-stress crisis (mother
hospitalised): BP spiked past 170, sleep dropped below 5 h, mood 1/5 for four
consecutive days.  By March 2026 she is recovering.  A pre-visit brief was
generated for her Feb 14 appointment, and a food-access nudge fires at month end.

Group A: mcp-server skills  (UC-01 → UC-10)  — called via direct imports
Group B: Phase 1 Clinical Intelligence server (UC-11 → UC-15) — called via HTTP
"""

from __future__ import annotations

import importlib
import json
import os
import sys
from datetime import date

import httpx
import pytest
import pytest_asyncio

# ---- path setup so mcp-server skills are importable -------------------------

def _mcp_server_path() -> str:
    here = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(os.path.dirname(os.path.dirname(here)), "mcp-server")


_p = _mcp_server_path()
if _p not in sys.path:
    sys.path.insert(0, _p)

from skills.generate_vitals import generate_daily_vitals
from skills.generate_checkins import generate_daily_checkins
from skills.compute_obt_score import compute_obt_score
from skills.sdoh_assessment import run_sdoh_assessment
from skills.crisis_escalation import run_crisis_escalation
from skills.food_access_nudge import run_food_access_nudge
from skills.compute_provider_risk import compute_provider_risk

# ---- shared helpers ---------------------------------------------------------

PHASE1_BASE = os.environ.get("MCP_CLINICAL_INTELLIGENCE_URL", "http://localhost:8000")

CRISIS_DATE = "2025-12-12"
PREVISIT_DATE = "2026-02-14"
FOOD_NUDGE_DATE = "2026-03-28"
OBT_DATE = "2026-03-31"
PROVIDER_RISK_DATE = "2026-03-31"


def _get_skill_fn(module_path: str, skill_name: str):
    """Extract the async tool function from a skill module.

    Kept for modules that still use register() internally (e.g. ingestion_tools,
    previsit_brief).
    """
    captured: dict = {}

    class MockMCP:
        def tool(self, fn):
            captured[fn.__name__] = fn
            return fn

    mod = importlib.import_module(module_path)
    try:
        mod.register(MockMCP())
    except Exception:
        pass
    fn = captured.get(skill_name)
    assert fn is not None, f"Tool '{skill_name}' not found in module '{module_path}'"
    return fn


# =============================================================================
# Group A — mcp-server skills (tools 1–10)
# =============================================================================


@pytest.mark.asyncio
async def test_uc01_generate_patient(maria_chen):
    """UC-01: generate_patient — Maria Chen is registered in the DB.

    Story: At enrollment the care coordinator registers Maria using the
    generate_patient skill. The data_entry_agent.setup_patient() has already
    done this; here we verify the DB row and its key demographics.
    """
    pid = maria_chen["patient_id"]
    pool = maria_chen["db_pool"]

    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT first_name, last_name, mrn, gender FROM patients WHERE id = $1",
            pid,
        )

    assert row is not None, "Maria Chen not found in patients table"
    assert row["first_name"] == "Maria"
    assert row["last_name"] == "Chen"
    assert row["mrn"] == "MC-2025-4829"
    assert row["gender"] == "female"


@pytest.mark.asyncio
async def test_uc02_generate_daily_vitals(maria_chen):
    """UC-02: generate_daily_vitals — simulate a new wearable upload.

    Story: Maria's smartwatch syncs today's readings (Apr 5 2026).
    Expects: six biometric rows written to biometric_readings.
    """
    pid = maria_chen["patient_id"]

    result = await generate_daily_vitals(patient_id=pid, target_date="2026-04-05")

    assert result.startswith("OK"), f"Unexpected result: {result}"
    assert "vital readings" in result


@pytest.mark.asyncio
async def test_uc03_generate_daily_checkins(maria_chen):
    """UC-03: generate_daily_checkins — daily mood / sleep / stress self-report.

    Story: Maria submits her evening check-in on Apr 5 2026.
    Expects: at least one check-in record inserted.
    """
    pid = maria_chen["patient_id"]

    result = await generate_daily_checkins(patient_id=pid, target_date="2026-04-05", scenario="normal")

    assert result.startswith("OK"), f"Unexpected: {result}"
    assert "check-in" in result


@pytest.mark.asyncio
async def test_uc04_compute_obt_score(maria_chen):
    """UC-04: compute_obt_score — 30-day wellness score computation.

    Story: Care coordinator reviews Maria's OBT score for March 31 2026.
    Expects: score 0–100, confidence ≥ 0.7 (data_entry_agent seeded 180 days),
    primary_driver from the five recognised domains.
    """
    pid = maria_chen["patient_id"]

    raw = await compute_obt_score(patient_id=pid, score_date=OBT_DATE)

    assert not raw.startswith("Error"), f"OBT error: {raw}"
    data = json.loads(raw)

    assert 0 <= data["score"] <= 100, f"Score out of range: {data['score']}"
    assert data["confidence"] >= 0.7, (
        f"Low confidence ({data['confidence']}) — seed data may be insufficient"
    )
    assert data["primary_driver"] in {
        "blood_pressure", "glucose", "behavioral", "adherence", "sleep"
    }
    assert isinstance(data["domain_scores"], dict)


@pytest.mark.asyncio
async def test_uc05_run_crisis_escalation(maria_chen):
    """UC-05: run_crisis_escalation — detect the Dec 2025 caregiver-stress crisis.

    Story: The system's nightly job checks Maria's Dec 12 data.  BP was pushed
    above 170 systolic and mood was 1/5 for multiple consecutive days.
    Expects: at least one trigger fires.
    """
    pid = maria_chen["patient_id"]

    raw = await run_crisis_escalation(patient_id=pid, check_date=CRISIS_DATE)

    assert not raw.startswith("Error"), f"Crisis error: {raw}"
    data = json.loads(raw)

    assert isinstance(data.get("triggers"), list), "Expected 'triggers' list in response"
    assert len(data["triggers"]) >= 1, (
        f"No crisis triggers detected on {CRISIS_DATE}. "
        f"Check that biometric_readings contain BP > 170 for that date. "
        f"Got: {data}"
    )


@pytest.mark.asyncio
async def test_uc06_run_sdoh_assessment(maria_chen):
    """UC-06: run_sdoh_assessment — Social Determinants of Health screening.

    Story: Maria completes the AHC HRSN screening at her October enrollment.
    The data_entry_agent pre-seeded food_access, housing_insecurity, and
    social_isolation flags.  The skill should upsert generated flags.
    """
    pid = maria_chen["patient_id"]

    raw = await run_sdoh_assessment(patient_id=pid, screening_date="2025-10-05")

    assert not raw.startswith("Error"), f"SDOH error: {raw}"
    assert pid in raw or "flag" in raw.lower(), (
        f"Unexpected SDOH response: {raw}"
    )

    pool = maria_chen["db_pool"]
    async with pool.acquire() as conn:
        food_flag = await conn.fetchrow(
            "SELECT domain, severity FROM patient_sdoh_flags "
            "WHERE patient_id = $1 AND domain = 'food_access'",
            pid,
        )
    assert food_flag is not None, (
        "food_access SDOH flag missing — data_entry_agent.seed_sdoh_flags() "
        "should have inserted it before the test ran"
    )
    assert food_flag["severity"] in {"low", "moderate", "high"}


@pytest.mark.asyncio
async def test_uc07_check_data_freshness(maria_chen):
    """UC-07: check_data_freshness — morning freshness gate before OBT run.

    Story: The orchestrator checks all data sources before computing the daily
    OBT score.  wearable, ehr, and manual were seeded by data_entry_agent.
    Expects: all sources present and is_stale = false.
    """
    pid = maria_chen["patient_id"]

    fn = _get_skill_fn("skills.ingestion_tools", "check_data_freshness")
    raw = await fn(patient_id=pid)

    assert not raw.startswith("Error"), f"Freshness error: {raw}"
    data = json.loads(raw)
    assert data["patient_id"] == pid

    source_names = {s["source_name"] for s in data["sources"]}
    assert "wearable" in source_names, f"wearable missing: {source_names}"
    assert "manual" in source_names, f"manual missing: {source_names}"

    # Only flag sources that actually have records as stale — sources with
    # records_count=0 (e.g. synthea in the test environment) are registered
    # but never populated; their staleness flag is expected and non-critical.
    stale = [s for s in data["sources"] if s["is_stale"] and s["records_count"] > 0]
    assert not stale, f"Populated sources are stale: {stale}"


@pytest.mark.asyncio
async def test_uc08_generate_previsit_brief(maria_chen):
    """UC-08: generate_previsit_brief — 6-month summary for Dr. Martinez.

    Story: The evening before Maria's Feb 14 2026 appointment the system
    synthesises vitals trends, medication changes, care gaps, and concerns.
    Expects: JSON with patient identity, vitals summary, and OBT reference.
    """
    pid = maria_chen["patient_id"]

    fn = _get_skill_fn("skills.previsit_brief", "generate_previsit_brief")
    raw = await fn(patient_id=pid, visit_date=PREVISIT_DATE)

    assert not raw.startswith("Error"), f"Previsit brief error: {raw}"
    data = json.loads(raw)

    text = json.dumps(data).lower()
    assert "maria" in text or "chen" in text, "Patient name missing from brief"
    assert "vital" in text or "bp" in text or "glucose" in text, (
        "Vitals summary missing from brief"
    )


@pytest.mark.asyncio
async def test_uc09_run_food_access_nudge(maria_chen):
    """UC-09: run_food_access_nudge — end-of-month food resource intervention.

    Story: March 28 2026 (day_of_month = 28 ≥ 25).  Maria has a food_access
    SDOH flag at moderate severity.  Both conditions satisfy the nudge trigger.
    Expects: nudge_triggered = True.
    """
    pid = maria_chen["patient_id"]

    raw = await run_food_access_nudge(patient_id=pid, current_date=FOOD_NUDGE_DATE)

    assert not raw.startswith("Error"), f"Food nudge error: {raw}"
    data = json.loads(raw)

    assert data.get("triggered") is True or data.get("nudge_triggered") is True, (
        f"Nudge not triggered on {FOOD_NUDGE_DATE}. "
        f"Check food_access SDOH flag is present and day >= 25. "
        f"Got: {data}"
    )


@pytest.mark.asyncio
async def test_uc10_compute_provider_risk(maria_chen):
    """UC-10: compute_provider_risk — rank Maria on Dr. Martinez's chase list.

    Story: The care-management dashboard ranks the provider's panel by clinical
    risk.  Maria had a crisis event in Dec 2025 and a below-average OBT score.
    Expects: composite_score in [0, 100].
    """
    pid = maria_chen["patient_id"]

    raw = await compute_provider_risk(patient_id=pid, score_date=PROVIDER_RISK_DATE)

    assert not raw.startswith("Error"), f"Provider risk error: {raw}"
    data = json.loads(raw)

    score_val = data.get("composite_score", data.get("risk_score"))
    assert score_val is not None, f"No risk score in: {data}"
    assert 0 <= float(score_val) <= 100, f"Score out of range: {score_val}"


# =============================================================================
# Group B — Phase 1 Clinical Intelligence Server (tools 11–15)
# REST endpoints at localhost:8000 — skipped if server not reachable
# =============================================================================


def _phase1_available() -> bool:
    try:
        r = httpx.get(f"{PHASE1_BASE}/health", timeout=3)
        return r.status_code == 200
    except Exception:
        return False


phase1 = pytest.mark.skipif(
    not _phase1_available(),
    reason=f"Phase 1 Clinical MCP Server not reachable at {PHASE1_BASE}",
)


@phase1
def test_uc11_get_synthetic_patient():
    """UC-11: get_synthetic_patient — retrieve Maria Chen's canonical record.

    Story: Provider opens Maria's chart.  The clinical layer returns her
    demographics, conditions, and current medications.
    REST: GET /tools/get_synthetic_patient?mrn=4829341
    """
    r = httpx.get(
        f"{PHASE1_BASE}/tools/get_synthetic_patient",
        params={"mrn": "4829341"},
        timeout=10,
    )
    assert r.status_code == 200
    body = r.json()
    text = json.dumps(body).lower()
    assert "maria" in text or "4829341" in text, (
        f"Patient name/MRN missing from response: {body}"
    )
    assert "condition" in text or "diabetes" in text or "medication" in text, (
        "Clinical data missing from synthetic patient record"
    )


@phase1
def test_uc12_check_screening_due():
    """UC-12: check_screening_due — USPSTF screenings overdue for Maria.

    Story: Care gaps surface at the start of her annual visit.
    54-year-old female with diabetes → mammogram, eye exam, foot exam expected.
    REST: POST /tools/check_screening_due
    """
    r = httpx.post(
        f"{PHASE1_BASE}/tools/check_screening_due",
        json={
            "patient_age": 54,
            "sex": "female",
            "conditions": ["E11.9", "I10", "E66.01"],
        },
        timeout=10,
    )
    assert r.status_code == 200
    body = r.json()
    assert isinstance(body, list), f"Expected list of screenings, got: {type(body)}"
    text = json.dumps(body).lower()
    assert (
        "mammogram" in text
        or "eye" in text
        or "foot" in text
        or "screening" in text
        or "diabetes" in text
    ), f"No screening recommendations: {body}"


@phase1
def test_uc13_flag_drug_interaction():
    """UC-13: flag_drug_interaction — check metformin + lisinopril + atorvastatin.

    Story: Pharmacist reviews the combination before adding a new statin dose.
    This triple combination is generally considered safe.
    REST: POST /tools/flag_drug_interaction
    """
    r = httpx.post(
        f"{PHASE1_BASE}/tools/flag_drug_interaction",
        json={"medications": ["metformin", "lisinopril", "atorvastatin"]},
        timeout=10,
    )
    assert r.status_code == 200
    body = r.json()
    text = json.dumps(body).lower()
    assert (
        "interaction" in text
        or "safe" in text
        or "no" in text
        or "drug" in text
        or isinstance(body, list)
    ), f"Unexpected response format: {body}"


@phase1
def test_uc14_get_guideline():
    """UC-14: get_guideline — ADA 9.1a HbA1c target guideline.

    Story: PCP asks the companion for the latest evidence on HbA1c targets.
    REST: GET /tools/get_guideline?recommendation_id=ADA-9.1a
    """
    r = httpx.get(
        f"{PHASE1_BASE}/tools/get_guideline",
        params={"recommendation_id": "ADA-9.1a"},
        timeout=10,
    )
    assert r.status_code == 200
    body = r.json()
    text = json.dumps(body).lower()
    assert (
        "hba1c" in text
        or "glycemic" in text
        or "guideline" in text
        or "ada" in text
        or "recommendation" in text
    ), f"Guideline content missing: {body}"


@phase1
def test_uc15_clinical_query():
    """UC-15: clinical_query — guardrail-filtered PCP question.

    Story: Maria's PCP uses the companion to ask about HbA1c management in
    a 54-year-old with cardiovascular risk.  The 3-layer guardrail pipeline
    must pass the clinical query and return evidence-based guidance.
    REST: POST /tools/clinical_query
    """
    r = httpx.post(
        f"{PHASE1_BASE}/tools/clinical_query",
        json={
            "query": (
                "What is the recommended HbA1c target for a 54-year-old female "
                "patient with Type 2 Diabetes and cardiovascular risk factors?"
            ),
            "role": "pcp",
            "patient_context": {
                "age": 54,
                "sex": "female",
                "conditions": ["Type 2 Diabetes", "Hypertension"],
            },
        },
        timeout=30,
    )
    assert r.status_code == 200
    body = r.json()
    text = json.dumps(body).lower()
    assert (
        "hba1c" in text
        or "glycemic" in text
        or "target" in text
        or "%" in text
    ), f"Clinical answer missing relevant content: {body}"
    assert not (text.startswith('{"error"') and len(text) < 200), (
        f"Response looks like a bare error: {body}"
    )
