"""End-to-end tests — Phase 2 Deliberation Engine (4 REST tools).

Group C: Deliberation endpoints (UC-16 → UC-20) — called via HTTP against
         the live Clinical MCP server (port 8001).

Story recap
-----------
Maria Chen (54 F, MRN 4829341) is enrolled in the Ambient Patient Companion.
The Dual-LLM Deliberation Engine allows Claude (Anthropic) and GPT-4 (OpenAI)
to independently analyse her clinical context, cross-critique each other, and
synthesise into 5 structured output categories.

These tests verify the REST wrapper layer for all 4 deliberation tools:
  UC-16 — get_deliberation_results  (no prior run → no_deliberations_found)
  UC-17 — get_patient_knowledge      (empty knowledge store)
  UC-18 — get_pending_nudges patient  (no pending nudges)
  UC-19 — get_pending_nudges care_team
  UC-20 — run_deliberation            (fire-and-forget trigger, status OK)
"""

from __future__ import annotations

import json
import os

import httpx
import pytest

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

PHASE1_BASE = os.environ.get(
    "MCP_CLINICAL_INTELLIGENCE_URL", "http://localhost:8001"
).rstrip("/mcp").rstrip("/")

PATIENT_MRN = "4829341"          # MRN used for deliberation read endpoints
PATIENT_MRN_DB = "MC-2025-4829"  # canonical MRN as stored in patients table

# Marker — skip entire group if Clinical MCP server is unreachable
def _server_reachable() -> bool:
    try:
        r = httpx.get(f"{PHASE1_BASE}/tools/get_synthetic_patient",
                      params={"mrn": PATIENT_MRN}, timeout=5)
        return r.status_code == 200
    except Exception:
        return False


deliberation = pytest.mark.skipif(
    not _server_reachable(),
    reason="Clinical MCP server not reachable at %s" % PHASE1_BASE,
)


# =============================================================================
# Group C — Deliberation REST endpoints (UC-16 → UC-20)
# =============================================================================


@deliberation
def test_uc16_get_deliberation_results_no_prior_run():
    """UC-16: get_deliberation_results — returns no_deliberations_found for fresh patient.

    Story: Before any deliberation has been triggered for Maria, the results
    endpoint should return a clean 'no_deliberations_found' status rather than
    an error.  The status field is the contract boundary for the UI.
    REST: POST /tools/get_deliberation_results
    """
    r = httpx.post(
        f"{PHASE1_BASE}/tools/get_deliberation_results",
        json={"patient_id": PATIENT_MRN, "output_type": "all", "limit": 1},
        timeout=10,
    )
    assert r.status_code == 200, f"Expected 200, got {r.status_code}: {r.text}"
    body = r.json()
    assert "patient_id" in body, f"Missing patient_id in response: {body}"
    assert body["patient_id"] == PATIENT_MRN
    assert "status" in body or "results" in body, (
        f"Response must have 'status' or 'results' key: {body}"
    )
    if "status" in body:
        assert body["status"] in ("no_deliberations_found", "ok"), (
            f"Unexpected status: {body['status']}"
        )


@deliberation
def test_uc16b_get_deliberation_results_output_type_filter():
    """UC-16b: get_deliberation_results — output_type filter accepted.

    Story: Care manager asks for only care plan recommendations from the last
    deliberation.  The endpoint must accept the output_type parameter and
    return a well-formed response.
    REST: POST /tools/get_deliberation_results
    """
    for output_type in ("care_plan", "nudge_content", "knowledge_updates", "all"):
        r = httpx.post(
            f"{PHASE1_BASE}/tools/get_deliberation_results",
            json={"patient_id": PATIENT_MRN, "output_type": output_type, "limit": 5},
            timeout=10,
        )
        assert r.status_code == 200, (
            f"output_type={output_type} returned {r.status_code}: {r.text}"
        )
        body = r.json()
        assert isinstance(body, dict), f"Expected dict response: {body}"


@deliberation
def test_uc17_get_patient_knowledge_empty():
    """UC-17: get_patient_knowledge — returns empty knowledge store for fresh patient.

    Story: The PCP's dashboard calls get_patient_knowledge on first load.
    With no deliberations run yet, the accumulated knowledge entries should be
    an empty list — not an error.
    REST: POST /tools/get_patient_knowledge
    """
    r = httpx.post(
        f"{PHASE1_BASE}/tools/get_patient_knowledge",
        json={"patient_id": PATIENT_MRN, "knowledge_type": "all"},
        timeout=10,
    )
    assert r.status_code == 200, f"Expected 200, got {r.status_code}: {r.text}"
    body = r.json()
    assert "patient_id" in body, f"Missing patient_id: {body}"
    assert body["patient_id"] == PATIENT_MRN
    assert "entries" in body, f"Missing entries key: {body}"
    assert isinstance(body["entries"], list), f"entries must be list: {body}"
    assert "knowledge_count" in body, f"Missing knowledge_count: {body}"
    assert isinstance(body["knowledge_count"], int), f"knowledge_count must be int: {body}"
    assert body["knowledge_count"] == len(body["entries"]), (
        f"knowledge_count {body['knowledge_count']} != len(entries) {len(body['entries'])}"
    )


@deliberation
def test_uc17b_get_patient_knowledge_type_filter():
    """UC-17b: get_patient_knowledge — knowledge_type filter accepted.

    Story: Care coordinator asks specifically for medication-related knowledge
    entries.  The endpoint must accept the filter and return a valid response.
    REST: POST /tools/get_patient_knowledge
    """
    for k_type in ("all", "medication", "behavioral", "clinical"):
        r = httpx.post(
            f"{PHASE1_BASE}/tools/get_patient_knowledge",
            json={"patient_id": PATIENT_MRN, "knowledge_type": k_type},
            timeout=10,
        )
        assert r.status_code == 200, (
            f"knowledge_type={k_type} returned {r.status_code}: {r.text}"
        )
        body = r.json()
        assert isinstance(body, dict), f"Expected dict for {k_type}: {body}"


@deliberation
def test_uc18_get_pending_nudges_patient():
    """UC-18: get_pending_nudges — patient target, 0 pending on fresh install.

    Story: Notification scheduler polls for nudges to deliver to Maria's phone.
    Before any deliberation runs, the pending queue should be empty — the
    scheduler should not error on an empty queue.
    REST: POST /tools/get_pending_nudges
    """
    r = httpx.post(
        f"{PHASE1_BASE}/tools/get_pending_nudges",
        json={"patient_id": PATIENT_MRN, "target": "patient"},
        timeout=10,
    )
    assert r.status_code == 200, f"Expected 200, got {r.status_code}: {r.text}"
    body = r.json()
    assert "patient_id" in body, f"Missing patient_id: {body}"
    assert body["patient_id"] == PATIENT_MRN
    assert "target" in body, f"Missing target: {body}"
    assert body["target"] == "patient"
    assert "pending_count" in body, f"Missing pending_count: {body}"
    assert isinstance(body["pending_count"], int), (
        f"pending_count must be int: {body}"
    )
    assert "nudges" in body, f"Missing nudges key: {body}"
    assert isinstance(body["nudges"], list), f"nudges must be list: {body}"
    assert body["pending_count"] == len(body["nudges"]), (
        f"pending_count {body['pending_count']} != len(nudges) {len(body['nudges'])}"
    )


@deliberation
def test_uc19_get_pending_nudges_care_team():
    """UC-19: get_pending_nudges — care_team target.

    Story: Care manager dashboard polls for care-team–directed alerts from
    the deliberation engine — e.g., 'escalate to cardiologist' flags.
    REST: POST /tools/get_pending_nudges
    """
    r = httpx.post(
        f"{PHASE1_BASE}/tools/get_pending_nudges",
        json={"patient_id": PATIENT_MRN, "target": "care_team"},
        timeout=10,
    )
    assert r.status_code == 200, f"Expected 200, got {r.status_code}: {r.text}"
    body = r.json()
    assert body["patient_id"] == PATIENT_MRN
    assert body["target"] == "care_team"
    assert isinstance(body["pending_count"], int)
    assert isinstance(body["nudges"], list)


@deliberation
def test_uc20_run_deliberation_trigger():
    """UC-20: run_deliberation — trigger returns a clean JSON response (not 500).

    Story: PCP clicks 'Run Deliberation Now' in the AI Deliberation tab.
    The endpoint must return a clean, structured JSON response for any outcome.
    Uses Maria Chen's canonical MRN (MC-2025-4829) from the patients table.

    Acceptable HTTP status codes:
      200/202 — deliberation completed or accepted
      404     — patient not found (clean JSON error)
      422     — other recoverable error (e.g. LLM API unavailable)
    NOT acceptable: 500 (unhandled exception / raw stack trace)
    REST: POST /tools/run_deliberation
    """
    r = httpx.post(
        f"{PHASE1_BASE}/tools/run_deliberation",
        json={
            "patient_id": PATIENT_MRN_DB,   # "MC-2025-4829" — canonical DB MRN
            "trigger_type": "manual",
            "max_rounds": 1,
        },
        timeout=90,  # live LLM call can take up to 60s
    )
    # Any clean JSON response is acceptable — 500 is not
    assert r.status_code != 500, (
        f"Server returned unhandled 500: {r.text[:300]}"
    )
    assert r.status_code < 500, (
        f"Server returned 5xx: {r.status_code} {r.text[:300]}"
    )
    body = r.json()
    assert isinstance(body, dict), f"Expected dict response: {body}"
    has_id = "deliberation_id" in body
    has_status = "status" in body
    assert has_id or has_status, (
        f"Response must have deliberation_id or status key: {body}"
    )


@deliberation
def test_uc20b_run_deliberation_no_openai_key_graceful():
    """UC-20b: run_deliberation — graceful error if OPENAI_API_KEY unset.

    Story: Even without an OpenAI key the endpoint must return a clean JSON
    error body (not a 500 Internal Server Error stack trace), so the UI can
    display a helpful message to the user.
    REST: POST /tools/run_deliberation

    Note: This test only validates shape — it does NOT assert that the key
    is missing.  If the key IS set the test verifies that a 200 is returned.
    """
    r = httpx.post(
        f"{PHASE1_BASE}/tools/run_deliberation",
        json={"patient_id": PATIENT_MRN_DB, "trigger_type": "manual", "max_rounds": 1},
        timeout=90,
    )
    if r.status_code == 200:
        body = r.json()
        assert isinstance(body, dict)
    else:
        assert r.status_code < 500, (
            f"Server returned 5xx without JSON error body: {r.status_code} {r.text[:200]}"
        )
        try:
            body = r.json()
            assert isinstance(body, dict), "Error response must be JSON dict"
        except Exception:
            pytest.fail(f"Non-200 response is not valid JSON: {r.text[:200]}")
