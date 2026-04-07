"""Task 4 — MCP smoke tests: tool registration + REST endpoint verification.

Verifies that:
  1. The Clinical MCP server (port 8001) is reachable and healthy.
  2. All 10 REST tool endpoints respond correctly (no 404 / 500).
  3. Each endpoint returns the expected JSON shape.
  4. The FastMCP tool functions are importable and have the right signatures.

Tests are automatically skipped when the server is not reachable on port 8001
(e.g., in CI without a running server), so they never block the offline suites.
"""

from __future__ import annotations

import inspect
import json

import httpx
import pytest

BASE = "http://localhost:8001"

# ── Server availability gate ──────────────────────────────────────────────────

def _server_up() -> bool:
    try:
        r = httpx.get(f"{BASE}/health", timeout=2)
        return r.status_code == 200
    except Exception:
        return False


_skip_no_server = pytest.mark.skipif(
    not _server_up(),
    reason="Clinical MCP server not reachable on port 8001",
)


# ── Task 4a: Tool function importability and signatures ───────────────────────

class TestToolRegistration:
    """All 15 MCP tools must be importable with correct parameter signatures."""

    def test_clinical_query_importable(self):
        from server.mcp_server import clinical_query
        sig = inspect.signature(clinical_query)
        assert "query" in sig.parameters
        assert "role" in sig.parameters
        assert "patient_context" in sig.parameters

    def test_get_guideline_importable(self):
        from server.mcp_server import get_guideline
        sig = inspect.signature(get_guideline)
        assert "recommendation_id" in sig.parameters

    def test_check_screening_due_importable(self):
        from server.mcp_server import check_screening_due
        sig = inspect.signature(check_screening_due)
        assert "patient_age" in sig.parameters
        assert "sex" in sig.parameters
        assert "conditions" in sig.parameters

    def test_flag_drug_interaction_importable(self):
        from server.mcp_server import flag_drug_interaction
        sig = inspect.signature(flag_drug_interaction)
        assert "medications" in sig.parameters

    def test_get_synthetic_patient_importable(self):
        from server.mcp_server import get_synthetic_patient
        sig = inspect.signature(get_synthetic_patient)
        assert "mrn" in sig.parameters

    def test_use_healthex_importable(self):
        from server.mcp_server import use_healthex
        assert callable(use_healthex)

    def test_use_demo_data_importable(self):
        from server.mcp_server import use_demo_data
        assert callable(use_demo_data)

    def test_switch_data_track_importable(self):
        from server.mcp_server import switch_data_track
        sig = inspect.signature(switch_data_track)
        assert "track" in sig.parameters

    def test_register_healthex_patient_importable(self):
        from server.mcp_server import register_healthex_patient
        sig = inspect.signature(register_healthex_patient)
        assert "health_summary_json" in sig.parameters

    def test_ingest_from_healthex_importable(self):
        from server.mcp_server import ingest_from_healthex
        sig = inspect.signature(ingest_from_healthex)
        assert "patient_id" in sig.parameters
        assert "resource_type" in sig.parameters
        assert "fhir_json" in sig.parameters

    def test_run_deliberation_importable(self):
        from server.mcp_server import run_deliberation
        sig = inspect.signature(run_deliberation)
        assert "patient_id" in sig.parameters

    def test_get_deliberation_results_importable(self):
        from server.mcp_server import get_deliberation_results
        sig = inspect.signature(get_deliberation_results)
        assert "patient_id" in sig.parameters

    def test_get_data_source_status_importable(self):
        from server.mcp_server import get_data_source_status
        assert callable(get_data_source_status)

    def test_all_tools_are_coroutines(self):
        """All MCP tools must be async (coroutine functions)."""
        from server import mcp_server as m
        tools = [
            m.clinical_query, m.get_guideline, m.check_screening_due,
            m.flag_drug_interaction, m.get_synthetic_patient,
            m.use_healthex, m.use_demo_data, m.switch_data_track,
            m.register_healthex_patient, m.ingest_from_healthex,
            m.run_deliberation, m.get_deliberation_results,
            m.get_data_source_status,
        ]
        for fn in tools:
            assert inspect.iscoroutinefunction(fn), (
                f"{fn.__name__} must be an async coroutine function"
            )


# ── Task 4b: REST endpoint smoke tests (require live server) ──────────────────

class TestRestEndpoints:
    """REST endpoints at /tools/<name> must respond with non-500 status codes."""

    @_skip_no_server
    def test_health_returns_ok(self):
        r = httpx.get(f"{BASE}/health", timeout=5)
        assert r.status_code == 200
        body = r.json()
        assert body.get("ok") is True
        assert "server" in body
        assert "version" in body

    @_skip_no_server
    def test_get_synthetic_patient_maria_chen(self):
        r = httpx.get(
            f"{BASE}/tools/get_synthetic_patient",
            params={"mrn": "4829341"},
            timeout=5,
        )
        assert r.status_code == 200
        body = r.json()
        assert body.get("first_name") == "Maria"
        assert body.get("last_name") == "Chen"
        assert body.get("mrn") == "4829341"

    @_skip_no_server
    def test_check_screening_due_returns_list(self):
        r = httpx.post(
            f"{BASE}/tools/check_screening_due",
            json={"patient_age": 54, "sex": "female", "conditions": ["T2DM", "HTN"]},
            timeout=5,
        )
        assert r.status_code == 200
        body = r.json()
        assert isinstance(body, list), f"Expected list, got {type(body)}"
        assert len(body) >= 1, "Expected at least 1 screening for 54F with T2DM+HTN"

    @_skip_no_server
    def test_flag_drug_interaction_no_crash(self):
        r = httpx.post(
            f"{BASE}/tools/flag_drug_interaction",
            json={"medications": ["metformin", "lisinopril", "atorvastatin"]},
            timeout=5,
        )
        assert r.status_code == 200
        body = r.json()
        assert isinstance(body, list)

    @_skip_no_server
    def test_get_guideline_ada_endpoint(self):
        r = httpx.get(
            f"{BASE}/tools/get_guideline",
            params={"recommendation_id": "ADA-9.1a"},
            timeout=5,
        )
        assert r.status_code == 200
        body = r.json()
        assert isinstance(body, dict)

    @_skip_no_server
    def test_get_data_source_status_returns_dict(self):
        r = httpx.get(f"{BASE}/tools/get_data_source_status", timeout=5)
        assert r.status_code == 200
        body = r.json()
        assert isinstance(body, dict)
        assert "active_track" in body or "track" in body or "status" in body

    @_skip_no_server
    def test_clinical_query_blocked_without_patient_context(self):
        """clinical_query must return a structured response (not 500) even without context."""
        r = httpx.post(
            f"{BASE}/tools/clinical_query",
            json={
                "query": "What is the HbA1c target for T2DM?",
                "role": "pcp",
                "patient_context": {},
            },
            timeout=15,
        )
        assert r.status_code == 200
        body = r.json()
        assert "status" in body, f"Expected 'status' in response: {body}"
        assert body["status"] in (
            "success", "warning", "blocked", "escalated", "error"
        )

    @_skip_no_server
    def test_nonexistent_endpoint_returns_404(self):
        r = httpx.get(f"{BASE}/tools/nonexistent_tool_xyz", timeout=5)
        assert r.status_code == 404

    @_skip_no_server
    def test_health_response_has_version_field(self):
        r = httpx.get(f"{BASE}/health", timeout=5)
        body = r.json()
        assert "version" in body
        assert isinstance(body["version"], str)

    @_skip_no_server
    def test_check_screening_due_each_item_has_required_fields(self):
        r = httpx.post(
            f"{BASE}/tools/check_screening_due",
            json={"patient_age": 54, "sex": "female", "conditions": ["T2DM"]},
            timeout=5,
        )
        body = r.json()
        for item in body:
            assert "screening_name" in item or "recommendation_text" in item, (
                f"Screening item missing required fields: {item}"
            )
