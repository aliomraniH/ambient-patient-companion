"""Tests for Claude config generation and .env export endpoints."""

import json
import pytest
from server import SERVER_MAP, MCP_DISPLAY_NAMES


# ── GET /api/generate/claude-config ──────────────────────────────────────────

@pytest.mark.anyio
async def test_generate_config_empty(client):
    """With no MCP URLs set, config should have empty mcpServers."""
    r = await client.get("/api/generate/claude-config")
    assert r.status_code == 200
    data = r.json()
    assert data["config"]["mcpServers"] == {}
    assert data["claude_code_commands"] == []
    assert data["servers_configured"] == 0
    assert data["servers_total"] == 5


@pytest.mark.anyio
async def test_generate_config_with_one_server(client):
    """Setting one MCP URL should produce one entry in mcpServers."""
    url = "https://synthetic-patient.replit.app/mcp"
    await client.post("/api/config", json={"MCP_SYNTHETIC_PATIENT_URL": url})
    r = await client.get("/api/generate/claude-config")
    data = r.json()
    assert data["servers_configured"] == 1
    assert "synthetic-patient" in data["config"]["mcpServers"]
    srv = data["config"]["mcpServers"]["synthetic-patient"]
    assert srv["command"] == "npx"
    assert srv["args"] == ["mcp-remote", url]


@pytest.mark.anyio
async def test_generate_config_cli_commands(client):
    """CLI commands should use correct transport and display name."""
    url = "https://care-gap.replit.app/mcp"
    await client.post("/api/config", json={"MCP_CARE_GAP_ANALYZER_URL": url})
    r = await client.get("/api/generate/claude-config")
    data = r.json()
    assert len(data["claude_code_commands"]) == 1
    cmd = data["claude_code_commands"][0]
    assert "claude mcp add" in cmd
    assert "--transport streamable-http" in cmd
    assert "care-gap-analyzer" in cmd
    assert url in cmd


@pytest.mark.anyio
async def test_generate_config_multiple_servers(client):
    """Setting all 5 MCP URLs should produce 5 entries."""
    urls = {
        "MCP_SYNTHETIC_PATIENT_URL": "https://a.replit.app/mcp",
        "MCP_EHR_INTEGRATION_URL": "https://b.replit.app/mcp",
        "MCP_CARE_GAP_ANALYZER_URL": "https://c.replit.app/mcp",
        "MCP_LAB_PROCESSOR_URL": "https://d.replit.app/mcp",
        "MCP_LANGSMITH_FEEDBACK_URL": "https://e.replit.app/mcp",
    }
    await client.post("/api/config", json=urls)
    r = await client.get("/api/generate/claude-config")
    data = r.json()
    assert data["servers_configured"] == 5
    assert data["servers_total"] == 5
    assert len(data["config"]["mcpServers"]) == 5
    assert len(data["claude_code_commands"]) == 5


@pytest.mark.anyio
async def test_generate_config_display_names(client):
    """MCP server display names should use hyphens, not underscores."""
    await client.post("/api/config", json={
        "MCP_EHR_INTEGRATION_URL": "https://ehr.replit.app/mcp",
    })
    r = await client.get("/api/generate/claude-config")
    data = r.json()
    assert "ehr-integration" in data["config"]["mcpServers"]
    assert "ehr_integration" not in data["config"]["mcpServers"]


# ── GET /api/export/env ──────────────────────────────────────────────────────

@pytest.mark.anyio
async def test_export_env_preview_empty(client):
    """Preview mode with no keys set should show all as '# not set'."""
    r = await client.get("/api/export/env")
    assert r.status_code == 200
    text = r.text
    assert "# ANTHROPIC_API_KEY=" in text
    assert "# not set" in text


@pytest.mark.anyio
async def test_export_env_preview_masks_secrets(client):
    """Preview mode should mask secret values."""
    await client.post("/api/config", json={"ANTHROPIC_API_KEY": "sk-ant-secret"})
    r = await client.get("/api/export/env")
    text = r.text
    assert "sk-ant-secret" not in text
    assert "\u2022\u2022\u2022\u2022\u2022\u2022\u2022\u2022" in text


@pytest.mark.anyio
async def test_export_env_preview_shows_non_secrets(client):
    """Preview mode should show non-secret values in plain text."""
    await client.post("/api/config", json={"CLAUDE_MODEL": "claude-sonnet-4-6"})
    r = await client.get("/api/export/env")
    assert "CLAUDE_MODEL=claude-sonnet-4-6" in r.text


@pytest.mark.anyio
async def test_export_env_download_reveals_secrets(client):
    """Download mode (?download=true) should include real secret values."""
    await client.post("/api/config", json={"ANTHROPIC_API_KEY": "sk-ant-real"})
    r = await client.get("/api/export/env?download=true")
    assert r.status_code == 200
    assert "sk-ant-real" in r.text
    assert r.headers.get("content-disposition") == "attachment; filename=.env"


@pytest.mark.anyio
async def test_export_env_download_all_keys_present(client):
    """Download should include a line for every key in ALL_KEYS."""
    r = await client.get("/api/export/env?download=true")
    from server import ALL_KEYS
    for key in ALL_KEYS:
        assert f"{key}=" in r.text


# ── GET / (HTML serving) ────────────────────────────────────────────────────

@pytest.mark.anyio
async def test_index_serves_html(client):
    """GET / should return the dashboard HTML page."""
    r = await client.get("/")
    assert r.status_code == 200
    assert "<!DOCTYPE html>" in r.text
    assert "Ambient Companion" in r.text
