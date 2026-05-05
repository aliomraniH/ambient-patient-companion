"""Skill: care_gap — autonomous care-gap reminder watcher.

No MCP tools are exposed by this module; it exists solely to own the
care_gap_watcher background task so that the watcher lives alongside the
care-gap domain rather than in the central runtime/watchers.py.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

logger = logging.getLogger(__name__)

# Configurable intervals (seconds) — override in tests via monkey-patching
CARE_GAP_INTERVAL: float = 86400.0     # 24 hours

# Care-gap thresholds
CARE_GAP_AGE_DAYS: int = 60
CARE_GAP_DEDUP_DAYS: int = 7


# ── Autonomous watcher ────────────────────────────────────────────────────────

async def _care_gap_watcher() -> None:
    """Flag patients with open care gaps older than CARE_GAP_AGE_DAYS days."""
    from db.connection import get_pool

    pool = await get_pool()

    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT DISTINCT patient_id
            FROM care_gaps
            WHERE status = 'open'
              AND identified_date <= CURRENT_DATE - CAST($1 AS INT)
            """,
            CARE_GAP_AGE_DAYS,
        )

    if not rows:
        logger.debug("care_gap_watcher: no overdue open care gaps found")
        return

    patient_ids = [str(r["patient_id"]) for r in rows]
    logger.info(
        "care_gap_watcher: %d patient(s) with overdue care gaps", len(patient_ids),
    )

    flagged = 0
    now = datetime.now(timezone.utc)
    dedup_cutoff = now - timedelta(days=CARE_GAP_DEDUP_DAYS)

    async with pool.acquire() as conn:
        for pid in patient_ids:
            recent = await conn.fetchval(
                """
                SELECT COUNT(*) FROM agent_interventions
                WHERE patient_id        = $1
                  AND intervention_type = 'care_gap_reminder'
                  AND delivered_at     >= $2
                """,
                pid, dedup_cutoff,
            )
            if recent:
                continue

            try:
                await conn.execute(
                    """
                    INSERT INTO agent_interventions
                        (id, patient_id, intervention_type, channel,
                         summary, delivered_at, source_skill, data_source)
                    VALUES (gen_random_uuid(), $1, 'care_gap_reminder',
                            'provider_alert',
                            'Open care gap(s) unresolved for 60+ days — review required',
                            $2, 'care_gap_watcher', 'synthea')
                    """,
                    pid, now,
                )
                flagged += 1
            except Exception as exc:
                logger.warning(
                    "care_gap_watcher: insert failed for patient %s: %s", pid, exc,
                )

    logger.info(
        "care_gap_watcher: inserted %d new care_gap_reminder intervention(s)", flagged,
    )


def register_watchers(runtime) -> None:
    """Register care-gap background watcher with *runtime*.

    Called automatically by skills/__init__.py load_skills() when a runtime
    instance is provided.  Keeping watcher registration here makes the skill
    self-contained: its autonomous behaviour (register_watchers) lives in the
    same file as its domain constants.
    """
    runtime.watch(
        name="care_gap_watcher",
        interval_seconds=CARE_GAP_INTERVAL,
        coro_fn=_care_gap_watcher,
    )
    logger.info(
        "care_gap: registered care_gap_watcher (interval=%.0fs)", CARE_GAP_INTERVAL,
    )


def register(mcp) -> None:
    """No MCP tools in this module — watcher-only skill."""
