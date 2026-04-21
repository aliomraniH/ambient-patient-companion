"""On-call pager for the chase-list (`atom_pressure_scores`) refresh.

The dashboard banner and ``/api/health/atom-pressure-refresh`` already
expose freshness, but those only reach somebody who happens to be
looking at the dashboard. This module is the active paging side: it
polls the health endpoint on a schedule and, when the response has
``alert: true``, fires a real notification (Slack webhook and/or
email) so the on-call engineer gets woken up even at 2 AM.

Run modes::

    python scripts/page_chase_refresh_alert.py            # daemon
    python scripts/page_chase_refresh_alert.py --once     # one poll, then exit

Daemon mode polls every
``CHASE_REFRESH_PAGER_INTERVAL_MINUTES`` minutes (default 60 — the
task spec's "at least once an hour"). State is held in-memory so we
don't re-page on every poll while the refresh stays broken; we only
re-alert on a status change or after
``CHASE_REFRESH_PAGER_REPAGE_HOURS`` (default 6) of continued failure.

Notification channels (any subset, all that are configured fire):

* ``CHASE_REFRESH_SLACK_WEBHOOK_URL`` (or generic ``SLACK_WEBHOOK_URL``)
* SMTP email — needs ``CHASE_REFRESH_PAGER_EMAIL_TO`` *and*
  ``SMTP_HOST`` (plus optional ``SMTP_PORT``, ``SMTP_USER``,
  ``SMTP_PASSWORD``, ``SMTP_FROM``, ``SMTP_USE_TLS``).

If no channel is configured the pager logs a loud warning per poll
but keeps running — that's better than crashing the whole
``start.sh`` startup chain on a fresh deploy that hasn't wired up
secrets yet.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import smtplib
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from email.message import EmailMessage
from typing import Optional

import httpx

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(levelname)s page_chase_refresh_alert %(message)s",
    stream=sys.stderr,
)
logger = logging.getLogger("page_chase_refresh_alert")


DEFAULT_HEALTH_URL = "http://localhost:8080/api/health/atom-pressure-refresh"
DEFAULT_INTERVAL_MINUTES = 60.0
DEFAULT_REPAGE_HOURS = 6.0


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        return float(raw)
    except ValueError:
        logger.warning("%s=%r is not a number; using default %r", name, raw, default)
        return default


def _dashboard_url() -> str:
    explicit = os.environ.get("CHASE_REFRESH_DASHBOARD_URL", "").strip()
    if explicit:
        return explicit
    domain = os.environ.get("REPLIT_DEV_DOMAIN", "").strip()
    if domain:
        return f"https://{domain}/"
    return "http://localhost:8080/"


def _health_url() -> str:
    return os.environ.get("CHASE_REFRESH_HEALTH_URL", DEFAULT_HEALTH_URL).strip() \
        or DEFAULT_HEALTH_URL


# ── Notification channels ────────────────────────────────────────────────────


@dataclass
class PageOutcome:
    """Per-poll result so the daemon and tests can reason about what happened."""
    alerted:    bool          # we decided this poll warranted paging
    sent:       list          # channels that successfully sent
    failed:     list          # channels that were tried but errored
    suppressed: bool          # we had an alert but suppressed re-paging
    status:     Optional[str] # the freshness status reported


def _format_alert_text(report: dict) -> tuple[str, str]:
    """Return (short_subject, long_body) describing the failure mode."""
    status = report.get("status", "unknown")
    message = report.get("message", "(no message)")
    age = report.get("age_hours")
    threshold = report.get("threshold_hours")
    last = report.get("last_refresh") or "never"
    dash = _dashboard_url()

    subject = f"[chase-list] atom_pressure_scores refresh: {status.upper()}"
    body_lines = [
        f"Failure mode: {status}",
        f"Detail:       {message}",
        f"Last refresh: {last}",
    ]
    if age is not None:
        body_lines.append(f"Age:          {age:.2f}h")
    if threshold is not None:
        body_lines.append(f"Threshold:    {threshold:.2f}h")
    body_lines.append("")
    body_lines.append(f"Dashboard:    {dash}")
    body_lines.append(
        "Runbook:      restart `python scripts/refresh_atom_pressure_scores.py` "
        "or check DATABASE_URL."
    )
    return subject, "\n".join(body_lines)


async def _send_slack(report: dict) -> bool:
    url = (
        os.environ.get("CHASE_REFRESH_SLACK_WEBHOOK_URL")
        or os.environ.get("SLACK_WEBHOOK_URL")
        or ""
    ).strip()
    if not url:
        return False
    subject, body = _format_alert_text(report)
    payload = {"text": f"*{subject}*\n```\n{body}\n```"}
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.post(url, json=payload)
        resp.raise_for_status()
    return True


def _send_email_sync(report: dict) -> bool:
    to_addr = os.environ.get("CHASE_REFRESH_PAGER_EMAIL_TO", "").strip()
    host = os.environ.get("SMTP_HOST", "").strip()
    if not to_addr or not host:
        return False
    port = int(os.environ.get("SMTP_PORT", "587"))
    user = os.environ.get("SMTP_USER", "").strip()
    password = os.environ.get("SMTP_PASSWORD", "")
    from_addr = (
        os.environ.get("SMTP_FROM", "").strip()
        or user
        or "ambient-pager@localhost"
    )
    use_tls = os.environ.get("SMTP_USE_TLS", "true").lower() not in ("0", "false", "no")

    subject, body = _format_alert_text(report)
    msg = EmailMessage()
    msg["From"] = from_addr
    msg["To"] = to_addr
    msg["Subject"] = subject
    msg.set_content(body)

    with smtplib.SMTP(host, port, timeout=15) as smtp:
        if use_tls:
            smtp.starttls()
        if user:
            smtp.login(user, password)
        smtp.send_message(msg)
    return True


async def _send_email(report: dict) -> bool:
    return await asyncio.to_thread(_send_email_sync, report)


CHANNELS = {
    "slack": _send_slack,
    "email": _send_email,
}


async def fire_notifications(report: dict) -> tuple[list, list]:
    """Send the alert through every configured channel.

    Returns ``(sent_channels, failed_channels)``. A channel that is
    not configured (returns ``False`` without raising) is silently
    skipped — it is in neither list.

    NOTE on log hygiene: we deliberately log only the exception *type*
    here, not the exception's string. ``httpx`` and ``smtplib`` errors
    routinely include the request URL or server hostname, and the
    Slack webhook URL is itself a bearer secret — its path embeds the
    token. Logging the raw exception would leak that into logs/log
    aggregators. The exception type is enough to direct an operator
    to rerun manually and inspect.
    """
    sent: list = []
    failed: list = []
    for name, sender in CHANNELS.items():
        try:
            ok = await sender(report)
        except Exception as exc:
            logger.error(
                "Channel %s failed: %s (details suppressed to avoid "
                "leaking webhook URL / SMTP credentials)",
                name, type(exc).__name__,
            )
            failed.append(name)
            continue
        if ok:
            sent.append(name)
            logger.info("Channel %s notified", name)
    return sent, failed


# ── Polling + de-dup logic ───────────────────────────────────────────────────


async def fetch_health(url: str) -> dict:
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.get(url)
    resp.raise_for_status()
    return resp.json()


@dataclass
class _AlertState:
    """Tracks the last alert so we don't re-page every poll."""
    last_alert_status: Optional[str] = None
    last_alert_at:     Optional[datetime] = None


def _should_page(state: _AlertState, status: str, repage_hours: float,
                 now: Optional[datetime] = None) -> bool:
    """Decide whether to actually fire notifications for this poll.

    Page when:
      * the status changed since the last alert (e.g. fresh→stale, or
        stale→never), or
      * we last paged more than ``repage_hours`` ago and the alert is
        still active (so on-call gets reminded).
    """
    now = now or datetime.now(timezone.utc)
    if state.last_alert_status != status or state.last_alert_at is None:
        return True
    age = (now - state.last_alert_at).total_seconds() / 3600.0
    return age >= repage_hours


async def poll_once(state: _AlertState, *, health_url: Optional[str] = None,
                    repage_hours: Optional[float] = None) -> PageOutcome:
    """Run one poll cycle. Updates ``state`` in place when we page."""
    url = health_url or _health_url()
    repage = repage_hours if repage_hours is not None else _env_float(
        "CHASE_REFRESH_PAGER_REPAGE_HOURS", DEFAULT_REPAGE_HOURS,
    )
    try:
        report = await fetch_health(url)
    except Exception as exc:
        # Treat an unreachable health endpoint as its own paging condition;
        # synthesize a report so the alert text is still meaningful.
        logger.error("Health endpoint %s unreachable: %s", url, exc)
        report = {
            "status":  "error",
            "alert":   True,
            "message": f"Pager could not reach {url}: {exc}",
        }

    if not report.get("alert"):
        if state.last_alert_status is not None:
            logger.info(
                "Refresh recovered (was %s); clearing alert state",
                state.last_alert_status,
            )
        state.last_alert_status = None
        state.last_alert_at = None
        return PageOutcome(False, [], [], False, report.get("status"))

    status = str(report.get("status", "unknown"))
    if not _should_page(state, status, repage):
        logger.info(
            "Alert still active (status=%s) — suppressing re-page until %.1fh elapsed",
            status, repage,
        )
        return PageOutcome(True, [], [], True, status)

    sent, failed = await fire_notifications(report)
    if not sent and not failed:
        logger.warning(
            "No notification channel configured! Set CHASE_REFRESH_SLACK_WEBHOOK_URL "
            "or CHASE_REFRESH_PAGER_EMAIL_TO + SMTP_HOST. "
            "Refresh status=%s message=%s",
            status, report.get("message"),
        )
    if sent:
        state.last_alert_status = status
        state.last_alert_at = datetime.now(timezone.utc)
    return PageOutcome(True, sent, failed, False, status)


async def run_daemon() -> int:
    interval_minutes = _env_float(
        "CHASE_REFRESH_PAGER_INTERVAL_MINUTES", DEFAULT_INTERVAL_MINUTES,
    )
    interval_seconds = max(60.0, interval_minutes * 60.0)
    logger.info(
        "Pager daemon starting; poll interval = %.2f min (%.0f s); health url = %s",
        interval_minutes, interval_seconds, _health_url(),
    )
    state = _AlertState()
    while True:
        try:
            await poll_once(state)
        except Exception as exc:
            logger.exception("Poll cycle crashed: %s", exc)
        try:
            await asyncio.sleep(interval_seconds)
        except asyncio.CancelledError:
            logger.info("Pager daemon cancelled; exiting cleanly")
            return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Page on-call (Slack/email) when the chase-list refresh stops. "
            "Polls /api/health/atom-pressure-refresh on a schedule."
        ),
    )
    parser.add_argument(
        "--once", action="store_true",
        help="Run a single poll and exit (default is daemon mode).",
    )
    args = parser.parse_args()
    if args.once:
        outcome = asyncio.run(poll_once(_AlertState()))
        if outcome.failed and not outcome.sent:
            return 2
        return 0
    return asyncio.run(run_daemon())


if __name__ == "__main__":
    raise SystemExit(main())
