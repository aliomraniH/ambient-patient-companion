"""Tests for the chase-list refresh freshness endpoint.

The endpoint surfaces the same logic that
``scripts/refresh_atom_pressure_scores.py --check`` runs, so the on-call
team gets a visible alert (banner + alert flag) the moment the daily
refresh stops running.
"""

import os
import pytest

import server


# ── Endpoint behaviour ───────────────────────────────────────────────────────


@pytest.mark.anyio
async def test_health_endpoint_alerts_when_database_url_missing(client, monkeypatch):
    """Without DATABASE_URL the endpoint must alert (status=error, alert=true)."""
    monkeypatch.delenv("DATABASE_URL", raising=False)
    r = await client.get("/api/health/atom-pressure-refresh")
    assert r.status_code == 200
    data = r.json()
    assert data["status"] == "error"
    assert data["alert"] is True
    assert "DATABASE_URL" in data["message"]


@pytest.mark.anyio
async def test_health_endpoint_reports_fresh(client, monkeypatch):
    """When the helper reports 'fresh', the endpoint sets alert=false."""
    monkeypatch.setenv("DATABASE_URL", "postgres://stub")

    async def fake_status(_dsn):
        return {
            "status": "fresh",
            "threshold_hours": 26.0,
            "last_refresh": "2026-04-21T00:00:00+00:00",
            "age_hours": 1.5,
            "message": "atom_pressure_scores is fresh: last refresh 1.50h ago (threshold 26.00h)",
        }

    monkeypatch.setattr(server._REFRESH_MOD, "freshness_status", fake_status)

    r = await client.get("/api/health/atom-pressure-refresh")
    assert r.status_code == 200
    data = r.json()
    assert data["status"] == "fresh"
    assert data["alert"] is False
    assert data["age_hours"] == 1.5


@pytest.mark.anyio
@pytest.mark.parametrize("status", ["stale", "never", "unknown", "error"])
async def test_health_endpoint_alerts_on_non_fresh_status(client, monkeypatch, status):
    """Every non-fresh status must flip the alert flag so the banner shows."""
    monkeypatch.setenv("DATABASE_URL", "postgres://stub")

    async def fake_status(_dsn):
        return {
            "status": status,
            "threshold_hours": 26.0,
            "last_refresh": None,
            "age_hours": None,
            "message": f"simulated {status}",
        }

    monkeypatch.setattr(server._REFRESH_MOD, "freshness_status", fake_status)

    r = await client.get("/api/health/atom-pressure-refresh")
    assert r.status_code == 200
    data = r.json()
    assert data["status"] == status
    assert data["alert"] is True


# ── Underlying helper behaviour ──────────────────────────────────────────────


@pytest.mark.anyio
async def test_freshness_status_returns_error_when_db_unreachable():
    """A bad DSN must produce status=error rather than raising."""
    report = await server._REFRESH_MOD.freshness_status(
        "postgres://invalid:invalid@127.0.0.1:1/doesnotexist"
    )
    assert report["status"] == "error"
    assert "threshold_hours" in report
    assert report["last_refresh"] is None
