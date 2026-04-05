"""Tests for config CRUD endpoints: GET/POST /api/config, GET /api/reveal."""

import pytest
from server import ALL_KEYS, SECRET_KEYS


# ── GET /api/config ──────────────────────────────────────────────────────────

@pytest.mark.anyio
async def test_get_config_returns_all_keys(client):
    """GET /api/config should return all 17 keys."""
    r = await client.get("/api/config")
    assert r.status_code == 200
    data = r.json()
    assert "keys" in data
    assert "completeness" in data
    for key in ALL_KEYS:
        assert key in data["keys"], f"Missing key: {key}"
        assert "value" in data["keys"][key]
        assert "set" in data["keys"][key]


@pytest.mark.anyio
async def test_get_config_empty_env_all_unset(client):
    """With an empty .env, all keys should be unset."""
    r = await client.get("/api/config")
    data = r.json()
    for key in ALL_KEYS:
        assert data["keys"][key]["set"] is False
        assert data["keys"][key]["value"] == ""


@pytest.mark.anyio
async def test_get_config_completeness_zero_when_empty(client):
    """Completeness should be 0% with no keys set."""
    r = await client.get("/api/config")
    data = r.json()
    assert data["completeness"]["set"] == 0
    assert data["completeness"]["total"] == len(ALL_KEYS)
    assert data["completeness"]["pct"] == 0


# ── POST /api/config ─────────────────────────────────────────────────────────

@pytest.mark.anyio
async def test_save_single_key(client):
    """POST /api/config should save a key and return it in saved list."""
    r = await client.post("/api/config", json={"CLAUDE_MODEL": "claude-sonnet-4-6"})
    assert r.status_code == 200
    data = r.json()
    assert "CLAUDE_MODEL" in data["saved"]
    assert data["completeness"]["set"] == 1


@pytest.mark.anyio
async def test_save_multiple_keys(client):
    """POST /api/config should save multiple keys at once."""
    payload = {
        "CLAUDE_MODEL": "claude-sonnet-4-6",
        "LANGSMITH_PROJECT": "ambient-patient-companion",
        "FHIR_BASE_URL": "https://fhir.example.com/R4",
    }
    r = await client.post("/api/config", json=payload)
    data = r.json()
    assert len(data["saved"]) == 3
    assert data["completeness"]["set"] == 3


@pytest.mark.anyio
async def test_save_ignores_unknown_keys(client):
    """POST /api/config should silently ignore keys not in ALL_KEYS."""
    r = await client.post("/api/config", json={"FAKE_KEY": "nope"})
    data = r.json()
    assert data["saved"] == []


@pytest.mark.anyio
async def test_saved_key_persists_across_reads(client):
    """A saved key should appear as set in subsequent GET /api/config."""
    await client.post("/api/config", json={"CLAUDE_MODEL": "claude-sonnet-4-6"})
    r = await client.get("/api/config")
    data = r.json()
    assert data["keys"]["CLAUDE_MODEL"]["set"] is True
    assert data["keys"]["CLAUDE_MODEL"]["value"] == "claude-sonnet-4-6"


@pytest.mark.anyio
async def test_completeness_updates_after_save(client):
    """Completeness percentage should update correctly after saves."""
    await client.post("/api/config", json={
        "ANTHROPIC_API_KEY": "sk-ant-test",
        "CLAUDE_MODEL": "claude-sonnet-4-6",
    })
    r = await client.get("/api/config")
    data = r.json()
    assert data["completeness"]["set"] == 2
    expected_pct = int(2 / len(ALL_KEYS) * 100)
    assert data["completeness"]["pct"] == expected_pct


# ── Secret masking ───────────────────────────────────────────────────────────

@pytest.mark.anyio
async def test_secret_keys_are_masked(client):
    """Secret keys should return masked values in GET /api/config."""
    await client.post("/api/config", json={"ANTHROPIC_API_KEY": "sk-ant-real-key"})
    r = await client.get("/api/config")
    data = r.json()
    assert data["keys"]["ANTHROPIC_API_KEY"]["set"] is True
    assert data["keys"]["ANTHROPIC_API_KEY"]["value"] == "\u2022\u2022\u2022\u2022\u2022\u2022\u2022\u2022"
    assert "sk-ant" not in data["keys"]["ANTHROPIC_API_KEY"]["value"]


@pytest.mark.anyio
async def test_non_secret_keys_show_value(client):
    """Non-secret keys should show their actual value."""
    await client.post("/api/config", json={"CLAUDE_MODEL": "claude-sonnet-4-6"})
    r = await client.get("/api/config")
    data = r.json()
    assert data["keys"]["CLAUDE_MODEL"]["value"] == "claude-sonnet-4-6"


# ── GET /api/reveal/{key} ───────────────────────────────────────────────────

@pytest.mark.anyio
async def test_reveal_returns_real_value(client):
    """GET /api/reveal should return the unmasked value."""
    await client.post("/api/config", json={"ANTHROPIC_API_KEY": "sk-ant-real-key"})
    r = await client.get("/api/reveal/ANTHROPIC_API_KEY")
    assert r.status_code == 200
    data = r.json()
    assert data["key"] == "ANTHROPIC_API_KEY"
    assert data["value"] == "sk-ant-real-key"


@pytest.mark.anyio
async def test_reveal_unknown_key_returns_404(client):
    """GET /api/reveal with an unknown key should return 404."""
    r = await client.get("/api/reveal/FAKE_KEY")
    assert r.status_code == 404


@pytest.mark.anyio
async def test_reveal_empty_key_returns_empty_string(client):
    """GET /api/reveal for an unset key should return empty string."""
    r = await client.get("/api/reveal/ANTHROPIC_API_KEY")
    data = r.json()
    assert data["value"] == ""
