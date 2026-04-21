"""Tests for the on-call pager that wraps /api/health/atom-pressure-refresh.

These tests don't talk to a real Slack/SMTP/HTTP server — they patch the
notification channels and the health-fetch function so we can prove:

  * a fresh report fires no alert;
  * each non-fresh status (`stale`, `never`, `error`) fires an alert that
    names the failure mode and links to the dashboard;
  * back-to-back polls of the same failure don't re-page;
  * a status flip (e.g. `stale` → `never`) DOES re-page;
  * a recovery (`fresh`) clears state so the next failure pages again;
  * with no channel configured we still record the attempt without crashing.
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts import page_chase_refresh_alert as pager  # noqa: E402


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture(autouse=True)
def clean_pager_env(monkeypatch):
    """Strip every env knob this module looks at so each test starts clean."""
    for var in (
        "CHASE_REFRESH_HEALTH_URL",
        "CHASE_REFRESH_DASHBOARD_URL",
        "CHASE_REFRESH_SLACK_WEBHOOK_URL",
        "SLACK_WEBHOOK_URL",
        "CHASE_REFRESH_PAGER_EMAIL_TO",
        "SMTP_HOST", "SMTP_PORT", "SMTP_USER", "SMTP_PASSWORD",
        "SMTP_FROM", "SMTP_USE_TLS",
        "REPLIT_DEV_DOMAIN",
        "CHASE_REFRESH_PAGER_INTERVAL_MINUTES",
        "CHASE_REFRESH_PAGER_REPAGE_HOURS",
    ):
        monkeypatch.delenv(var, raising=False)


def _stub_fetch(report):
    async def _f(_url):
        return report
    return _f


# ── Alert text ───────────────────────────────────────────────────────────────


def test_alert_text_names_failure_mode_and_links_dashboard(monkeypatch):
    monkeypatch.setenv("CHASE_REFRESH_DASHBOARD_URL", "https://dash.example/")
    subject, body = pager._format_alert_text({
        "status": "stale",
        "message": "atom_pressure_scores is STALE: last refresh 30h ago",
        "age_hours": 30.0,
        "threshold_hours": 26.0,
        "last_refresh": "2026-04-19T00:00:00+00:00",
    })
    assert "STALE" in subject
    assert "Failure mode: stale" in body
    assert "https://dash.example/" in body
    assert "30.00h" in body
    assert "26.00h" in body


def test_dashboard_url_falls_back_to_replit_domain(monkeypatch):
    monkeypatch.setenv("REPLIT_DEV_DOMAIN", "abc.replit.dev")
    assert pager._dashboard_url() == "https://abc.replit.dev/"


# ── Per-poll behaviour ───────────────────────────────────────────────────────


@pytest.mark.anyio
async def test_fresh_report_does_not_alert(monkeypatch):
    monkeypatch.setattr(pager, "fetch_health", _stub_fetch({
        "status": "fresh", "alert": False, "message": "ok",
    }))
    sent = []
    monkeypatch.setattr(pager, "fire_notifications",
                        lambda r: _async_return(([], [])))  # never called

    out = await pager.poll_once(pager._AlertState())
    assert out.alerted is False
    assert out.sent == [] and out.failed == []
    assert sent == []


def _async_return(value):
    async def _f():
        return value
    return _f()


@pytest.mark.anyio
@pytest.mark.parametrize("status", ["stale", "never", "error"])
async def test_each_failure_mode_fires_notifications(monkeypatch, status):
    monkeypatch.setattr(pager, "fetch_health", _stub_fetch({
        "status": status, "alert": True, "message": f"sim {status}",
    }))

    captured = {}
    async def fake_fire(report):
        captured["report"] = report
        return ["slack"], []
    monkeypatch.setattr(pager, "fire_notifications", fake_fire)

    out = await pager.poll_once(pager._AlertState())
    assert out.alerted is True
    assert out.sent == ["slack"]
    assert out.failed == []
    assert captured["report"]["status"] == status


@pytest.mark.anyio
async def test_repeat_failure_does_not_repage(monkeypatch):
    monkeypatch.setattr(pager, "fetch_health", _stub_fetch({
        "status": "stale", "alert": True, "message": "sim",
    }))
    calls = {"n": 0}
    async def fake_fire(report):
        calls["n"] += 1
        return ["slack"], []
    monkeypatch.setattr(pager, "fire_notifications", fake_fire)

    state = pager._AlertState()
    first = await pager.poll_once(state)
    second = await pager.poll_once(state)

    assert first.sent == ["slack"]
    assert second.suppressed is True
    assert second.sent == []
    assert calls["n"] == 1


@pytest.mark.anyio
async def test_status_change_repages(monkeypatch):
    reports = iter([
        {"status": "stale", "alert": True, "message": "s"},
        {"status": "never", "alert": True, "message": "n"},
    ])
    async def fake_fetch(_):
        return next(reports)
    monkeypatch.setattr(pager, "fetch_health", fake_fetch)

    calls = []
    async def fake_fire(report):
        calls.append(report["status"])
        return ["slack"], []
    monkeypatch.setattr(pager, "fire_notifications", fake_fire)

    state = pager._AlertState()
    await pager.poll_once(state)
    await pager.poll_once(state)
    assert calls == ["stale", "never"]


@pytest.mark.anyio
async def test_recovery_clears_state_then_next_failure_pages(monkeypatch):
    reports = iter([
        {"status": "stale", "alert": True, "message": "s"},
        {"status": "fresh", "alert": False, "message": "ok"},
        {"status": "stale", "alert": True, "message": "s2"},
    ])
    async def fake_fetch(_):
        return next(reports)
    monkeypatch.setattr(pager, "fetch_health", fake_fetch)

    calls = []
    async def fake_fire(report):
        calls.append(report["status"])
        return ["slack"], []
    monkeypatch.setattr(pager, "fire_notifications", fake_fire)

    state = pager._AlertState()
    await pager.poll_once(state)
    await pager.poll_once(state)
    assert state.last_alert_status is None
    await pager.poll_once(state)
    assert calls == ["stale", "stale"]


@pytest.mark.anyio
async def test_repage_after_repage_window(monkeypatch):
    monkeypatch.setattr(pager, "fetch_health", _stub_fetch({
        "status": "stale", "alert": True, "message": "s",
    }))
    calls = []
    async def fake_fire(report):
        calls.append(report["status"])
        return ["slack"], []
    monkeypatch.setattr(pager, "fire_notifications", fake_fire)

    state = pager._AlertState()
    await pager.poll_once(state, repage_hours=6.0)
    # Pretend the previous page happened 7 hours ago.
    state.last_alert_at = datetime.now(timezone.utc) - timedelta(hours=7)
    await pager.poll_once(state, repage_hours=6.0)
    assert calls == ["stale", "stale"]


@pytest.mark.anyio
async def test_unreachable_health_endpoint_treated_as_alert(monkeypatch):
    async def boom(_):
        raise RuntimeError("connection refused")
    monkeypatch.setattr(pager, "fetch_health", boom)
    calls = []
    async def fake_fire(report):
        calls.append(report)
        return ["slack"], []
    monkeypatch.setattr(pager, "fire_notifications", fake_fire)

    out = await pager.poll_once(pager._AlertState())
    assert out.alerted is True
    assert out.sent == ["slack"]
    assert calls and calls[0]["status"] == "error"
    assert "connection refused" in calls[0]["message"]


@pytest.mark.anyio
async def test_no_channel_configured_does_not_crash(monkeypatch, caplog):
    monkeypatch.setattr(pager, "fetch_health", _stub_fetch({
        "status": "stale", "alert": True, "message": "s",
    }))
    # Real fire_notifications, but neither Slack nor SMTP env configured.
    out = await pager.poll_once(pager._AlertState())
    assert out.alerted is True
    assert out.sent == [] and out.failed == []
    # State must NOT advance, so the next poll will try again.
    # (Otherwise we'd silently swallow the alert forever.)


@pytest.mark.anyio
async def test_fire_notifications_skips_unconfigured_channels(monkeypatch):
    sent, failed = await pager.fire_notifications({
        "status": "stale", "message": "s",
    })
    assert sent == [] and failed == []


@pytest.mark.anyio
async def test_fire_notifications_calls_slack_when_configured(monkeypatch):
    monkeypatch.setenv("CHASE_REFRESH_SLACK_WEBHOOK_URL", "https://hooks.example/T/B/X")

    captured = {}
    class FakeResp:
        def raise_for_status(self):
            return None
    class FakeClient:
        def __init__(self, *a, **kw):
            pass
        async def __aenter__(self):
            return self
        async def __aexit__(self, *a):
            return None
        async def post(self, url, json):
            captured["url"] = url
            captured["payload"] = json
            return FakeResp()
    monkeypatch.setattr(pager.httpx, "AsyncClient", FakeClient)

    sent, failed = await pager.fire_notifications({
        "status": "stale",
        "message": "atom_pressure_scores is STALE",
        "age_hours": 30.0,
        "threshold_hours": 26.0,
        "last_refresh": "2026-04-19T00:00:00+00:00",
    })
    assert sent == ["slack"]
    assert failed == []
    assert captured["url"].startswith("https://hooks.example/")
    assert "STALE" in captured["payload"]["text"]


@pytest.mark.anyio
async def test_slack_failure_does_not_leak_webhook_url_in_logs(monkeypatch, caplog):
    """A Slack send error must not log the webhook URL — that path is a secret."""
    secret_url = "https://hooks.example/T/B/SUPER-SECRET-TOKEN-12345"
    monkeypatch.setenv("CHASE_REFRESH_SLACK_WEBHOOK_URL", secret_url)

    class ExplodingClient:
        def __init__(self, *a, **kw):
            pass
        async def __aenter__(self):
            return self
        async def __aexit__(self, *a):
            return None
        async def post(self, url, json):
            # Real httpx errors include the URL — emulate that here.
            raise RuntimeError(f"connection refused while POSTing {url}")
    monkeypatch.setattr(pager.httpx, "AsyncClient", ExplodingClient)

    import logging
    caplog.set_level(logging.DEBUG, logger="page_chase_refresh_alert")

    sent, failed = await pager.fire_notifications({
        "status": "stale", "message": "s",
    })
    assert sent == []
    assert failed == ["slack"]

    full_log = "\n".join(rec.getMessage() for rec in caplog.records)
    assert "SUPER-SECRET-TOKEN" not in full_log
    assert secret_url not in full_log
    # But we still want the operator to know SOMETHING failed.
    assert "slack" in full_log.lower()
    assert "RuntimeError" in full_log
