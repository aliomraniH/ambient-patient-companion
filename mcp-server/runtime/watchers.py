"""Built-in autonomous watchers for the Skills MCP server.

Two built-in watchers are registered here and started by AgentRuntime at
server startup. They call the same skill logic used by the MCP tools, but
proactively — no clinician trigger required.

Watcher summary
---------------
crisis_scan_watcher  — every 60 min
    Finds patients who had a check-in in the last 24 h and runs
    run_crisis_escalation for each. Catches high-stress / SI signals
    within the hour even when no clinician opens the tool.

care_gap_watcher  — every 24 h
    Finds patients with open care gaps older than 60 days and inserts an
    agent_interventions row of type 'care_gap_reminder' if one has not
    already been created in the last 7 days.

Note: checkin_atom_watcher (every 5 min) has been migrated to
skills/behavioral_atoms.py as a proof-of-concept of the register_watchers()
hook.  It is registered automatically by load_skills() via that module's
register_watchers(runtime) export.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

# Configurable intervals (seconds) — override in tests via monkey-patching
CRISIS_SCAN_INTERVAL: float = 3600.0    # 60 minutes
CARE_GAP_INTERVAL: float = 86400.0      # 24 hours

# Care-gap thresholds
CARE_GAP_AGE_DAYS: int = 60
CARE_GAP_DEDUP_DAYS: int = 7


# ── 1. crisis_scan_watcher ────────────────────────────────────────────────────

async def _crisis_scan_watcher() -> None:
    """Run crisis escalation for every patient who checked in within 24 h."""
    from db.connection import get_pool
    from skills.crisis_escalation import run_crisis_escalation

    pool = await get_pool()

    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT DISTINCT patient_id
            FROM daily_checkins
            WHERE checkin_date >= CURRENT_DATE - 1
            """,
        )

    if not rows:
        logger.debug("crisis_scan_watcher: no recent check-ins to scan")
        return

    patient_ids = [str(r["patient_id"]) for r in rows]
    logger.info(
        "crisis_scan_watcher: scanning %d patient(s) for crisis indicators",
        len(patient_ids),
    )

    escalated = 0
    for pid in patient_ids:
        try:
            result = await run_crisis_escalation(pid)
            import json as _json
            parsed = _json.loads(result) if isinstance(result, str) else result
            if parsed.get("escalation_triggered"):
                escalated += 1
                logger.warning(
                    "crisis_scan_watcher: escalation triggered for patient %s — %s",
                    pid, parsed.get("triggers"),
                )
        except Exception as exc:
            logger.warning(
                "crisis_scan_watcher: failed for patient %s: %s", pid, exc,
            )

    logger.info(
        "crisis_scan_watcher: scanned %d patient(s), %d escalation(s) triggered",
        len(patient_ids), escalated,
    )


# ── 3. care_gap_watcher ───────────────────────────────────────────────────────

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

    async with pool.acquire() as conn:
        for pid in patient_ids:
            recent = await conn.fetchval(
                """
                SELECT COUNT(*) FROM agent_interventions
                WHERE patient_id       = $1
                  AND intervention_type = 'care_gap_reminder'
                  AND delivered_at     >= NOW() - ($2 || ' days')::interval
                """,
                pid, str(CARE_GAP_DEDUP_DAYS),
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


# ── Registration ──────────────────────────────────────────────────────────────

def register_watchers(runtime: "AgentRuntime") -> None:  # noqa: F821
    """Register the two built-in clinical watchers with *runtime*.

    Called once from server.py before ``mcp.run()``; safe to call in tests
    with a fresh AgentRuntime instance.

    Note: checkin_atom_watcher is registered by skills/behavioral_atoms.py
    via its register_watchers(runtime) export — see load_skills() in
    skills/__init__.py.
    """
    from runtime.agent_runtime import AgentRuntime  # noqa: F401 (type reference)

    runtime.watch(
        name="crisis_scan_watcher",
        interval_seconds=CRISIS_SCAN_INTERVAL,
        coro_fn=_crisis_scan_watcher,
    )
    runtime.watch(
        name="care_gap_watcher",
        interval_seconds=CARE_GAP_INTERVAL,
        coro_fn=_care_gap_watcher,
    )

    logger.info("register_watchers: 2 built-in watchers registered")
