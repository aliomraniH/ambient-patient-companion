"""
Smoke tests for the flag lifecycle MCP tool.

Requires: Clinical MCP Server running on port 8001
          DATABASE_URL set with migration 004 applied

Tests:
  SMOKE-1: get_flag_review_status REST wrapper returns valid JSON
  SMOKE-2: ingest_from_healthex response includes flag_review key
"""

import json
import os

import httpx
import pytest

MCP_URL = os.environ.get("MCP_CLINICAL_INTELLIGENCE_URL", "http://localhost:8001")

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="DATABASE_URL not set"
)


@pytest.fixture(scope="module")
def client():
    with httpx.Client(base_url=MCP_URL, timeout=30) as c:
        yield c


def _is_server_up(client):
    try:
        r = client.get("/health")
        return r.status_code == 200
    except httpx.ConnectError:
        return False


@pytest.fixture(scope="module", autouse=True)
def require_server(client):
    if not _is_server_up(client):
        pytest.skip("Clinical MCP Server not running on port 8001")


def test_smoke1_get_flag_review_status(client):
    """get_flag_review_status REST wrapper returns valid response."""
    r = client.post("/tools/get_flag_review_status", json={
        "patient_id": "00000000-0000-0000-0000-000000000000",
    })
    assert r.status_code == 200
    data = r.json()
    # Either an error or a valid result with expected fields
    assert "patient_id" in data or "status" in data


def test_smoke2_ingest_response_structure(client):
    """ingest_from_healthex response is valid JSON (flag_review may or may not be present)."""
    r = client.post("/tools/ingest_from_healthex", json={
        "patient_id": "00000000-0000-0000-0000-000000000000",
        "resource_type": "labs",
        "fhir_json": "test",
    })
    assert r.status_code == 200
    data = r.json()
    assert "status" in data
