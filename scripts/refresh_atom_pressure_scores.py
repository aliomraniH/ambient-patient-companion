"""Daily refresh of the `atom_pressure_scores` materialized view.

`atom_pressure_scores` powers the provider chase-list ranking
(`mcp-server/skills/compute_provider_risk.py`, ~lines 230–250). It is a
PostgreSQL materialized view that only updates when something explicitly
issues `REFRESH MATERIALIZED VIEW`. Without a schedule, the chase list
slowly goes stale.

Replit's managed PostgreSQL does not ship with `pg_cron`, so this module
acts as the scheduler. It runs in three modes:

    python scripts/refresh_atom_pressure_scores.py            # daemon
    python scripts/refresh_atom_pressure_scores.py --once     # single refresh
    python scripts/refresh_atom_pressure_scores.py --check    # freshness probe

Daemon mode performs an immediate refresh on startup, then sleeps for
``ATOM_PRESSURE_REFRESH_INTERVAL_HOURS`` (default 24) between refreshes,
forever. Each successful refresh writes the UTC timestamp to
``system_config['atom_pressure_scores_last_refresh']`` so monitoring can
verify freshness.

Check mode reads that key and exits non-zero (and prints a warning) when
the last refresh is older than
``ATOM_PRESSURE_FRESHNESS_THRESHOLD_HOURS`` (default 26 — one daily
window plus a 2h grace period).
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from datetime import datetime, timezone

import asyncpg

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(levelname)s refresh_atom_pressure_scores %(message)s",
    stream=sys.stderr,
)
logger = logging.getLogger("refresh_atom_pressure_scores")

LAST_REFRESH_KEY = "atom_pressure_scores_last_refresh"
INTERVAL_HOURS = float(os.environ.get("ATOM_PRESSURE_REFRESH_INTERVAL_HOURS", "24"))
FRESHNESS_THRESHOLD_HOURS = float(
    os.environ.get("ATOM_PRESSURE_FRESHNESS_THRESHOLD_HOURS", "26")
)

REFRESH_SQL_CONCURRENT = "REFRESH MATERIALIZED VIEW CONCURRENTLY atom_pressure_scores"
REFRESH_SQL_PLAIN = "REFRESH MATERIALIZED VIEW atom_pressure_scores"


async def _refresh(conn: asyncpg.Connection) -> None:
    """Execute the refresh, falling back to a non-concurrent refresh."""
    try:
        await conn.execute(REFRESH_SQL_CONCURRENT)
        logger.info("atom_pressure_scores refreshed (CONCURRENTLY)")
    except Exception as exc:
        logger.info(
            "Concurrent refresh failed (%s); retrying plain refresh",
            type(exc).__name__,
        )
        await conn.execute(REFRESH_SQL_PLAIN)
        logger.info("atom_pressure_scores refreshed (plain)")


async def _record_last_refresh(conn: asyncpg.Connection) -> datetime:
    now = datetime.now(timezone.utc)
    await conn.execute(
        """
        INSERT INTO system_config (key, value, updated_at)
        VALUES ($1, $2, $3)
        ON CONFLICT (key) DO UPDATE
            SET value = EXCLUDED.value,
                updated_at = EXCLUDED.updated_at
        """,
        LAST_REFRESH_KEY,
        now.isoformat(),
        now,
    )
    return now


async def refresh_once(dsn: str) -> int:
    conn = await asyncpg.connect(dsn)
    try:
        await _refresh(conn)
        await _record_last_refresh(conn)
    finally:
        await conn.close()
    return 0


async def run_daemon(dsn: str) -> int:
    interval_seconds = max(60.0, INTERVAL_HOURS * 3600.0)
    logger.info(
        "Daemon starting; refresh interval = %.2f hours (%.0f s)",
        INTERVAL_HOURS,
        interval_seconds,
    )
    while True:
        try:
            await refresh_once(dsn)
        except Exception as exc:
            # Never let a transient DB error kill the scheduler — log and
            # try again on the next tick.
            logger.exception("Refresh attempt failed: %s", exc)
        try:
            await asyncio.sleep(interval_seconds)
        except asyncio.CancelledError:
            logger.info("Daemon cancelled; exiting cleanly")
            return 0


async def check_freshness(dsn: str) -> int:
    conn = await asyncpg.connect(dsn)
    try:
        row = await conn.fetchrow(
            "SELECT value, updated_at FROM system_config WHERE key = $1",
            LAST_REFRESH_KEY,
        )
    finally:
        await conn.close()

    if row is None:
        logger.error(
            "No %s entry in system_config — has the refresh ever run?",
            LAST_REFRESH_KEY,
        )
        return 2

    # `value` is the source of truth for "when did the refresh actually
    # happen" — `updated_at` only records when the row was last touched
    # (migration 014 seeds the row with value='never' and a fresh
    # updated_at, so we must not fall back to updated_at here).
    value = (row["value"] or "").strip()
    if not value or value.lower() == "never":
        logger.error(
            "%s is %r — refresh has never completed successfully",
            LAST_REFRESH_KEY,
            value or None,
        )
        return 2
    try:
        last_refresh = datetime.fromisoformat(value)
    except ValueError:
        logger.error(
            "%s value %r is not an ISO timestamp — cannot evaluate freshness",
            LAST_REFRESH_KEY,
            value,
        )
        return 2
    if last_refresh.tzinfo is None:
        last_refresh = last_refresh.replace(tzinfo=timezone.utc)
    age_hours = (datetime.now(timezone.utc) - last_refresh).total_seconds() / 3600.0
    if age_hours > FRESHNESS_THRESHOLD_HOURS:
        logger.error(
            "atom_pressure_scores is STALE: last refresh %.2fh ago "
            "(threshold %.2fh)",
            age_hours,
            FRESHNESS_THRESHOLD_HOURS,
        )
        return 1

    logger.info(
        "atom_pressure_scores is fresh: last refresh %.2fh ago "
        "(threshold %.2fh)",
        age_hours,
        FRESHNESS_THRESHOLD_HOURS,
    )
    return 0


def _require_dsn() -> str:
    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        logger.error("DATABASE_URL is not set")
        sys.exit(2)
    return dsn


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Refresh the atom_pressure_scores materialized view.",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--once",
        action="store_true",
        help="Run a single refresh and exit (default is daemon mode).",
    )
    mode.add_argument(
        "--check",
        action="store_true",
        help="Print freshness; exit 1 if stale, 2 if never run.",
    )
    args = parser.parse_args()

    dsn = _require_dsn()
    if args.check:
        return asyncio.run(check_freshness(dsn))
    if args.once:
        return asyncio.run(refresh_once(dsn))
    return asyncio.run(run_daemon(dsn))


if __name__ == "__main__":
    raise SystemExit(main())
