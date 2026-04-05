"""Tests for connectivity test endpoints (Anthropic, LangSmith, MCP)."""

import pytest
from server import SERVER_MAP


# ── POST /api/test/anthropic ─────────────────────────────────────────────────

@pytest.mark.anyio
async def test_anthropic_no_key_returns_error(client):
    """Without ANTHROPIC_API_KEY set, test should return ok=False."""
    r = await client.post("/api/test/anthropic")
    assert r.status_code == 200
    data = r.json()
    assert data["ok"] is False
    assert "No ANTHROPIC_API_KEY set" in data["error"]


# ── POST /api/test/langsmith ─────────────────────────────────────────────────

@pytest.mark.anyio
async def test_langsmith_no_key_returns_error(client):
    """Without LANGSMITH_API_KEY set, test should return ok=False."""
    r = await client.post("/api/test/langsmith")
    assert r.status_code == 200
    data = r.json()
    assert data["ok"] is False
    assert "No LANGSMITH_API_KEY set" in data["error"]


# ── POST /api/test/mcp/{server_id} ──────────────────────────────────────────

@pytest.mark.anyio
async def test_mcp_unknown_server_returns_404(client):
    """Testing an unknown server_id should return 404."""
    r = await client.post("/api/test/mcp/unknown_server")
    assert r.status_code == 404


@pytest.mark.anyio
async def test_mcp_no_url_returns_error(client):
    """Testing an MCP server with no URL set should return ok=False."""
    r = await client.post("/api/test/mcp/synthetic_patient")
    assert r.status_code == 200
    data = r.json()
    assert data["ok"] is False
    assert data["server"] == "synthetic_patient"
    assert "No URL configured" in data["error"]


@pytest.mark.anyio
async def test_mcp_invalid_url_returns_error(client):
    """Testing an MCP server with an unreachable URL should return ok=False."""
    await client.post("/api/config", json={
        "MCP_SYNTHETIC_PATIENT_URL": "http://localhost:19999/mcp"
    })
    r = await client.post("/api/test/mcp/synthetic_patient")
    data = r.json()
    assert data["ok"] is False
    assert data["server"] == "synthetic_patient"


@pytest.mark.anyio
async def test_mcp_all_server_ids_are_valid(client):
    """Every server_id in SERVER_MAP should be accepted (not 404)."""
    for server_id in SERVER_MAP:
        r = await client.post(f"/api/test/mcp/{server_id}")
        assert r.status_code == 200, f"server_id '{server_id}' returned {r.status_code}"
