"""Built-in autonomous watchers for the Skills MCP server.

Three watchers are registered here and started by AgentRuntime at server
startup. They call the same skill logic used by the MCP tools, but proactively
— no clinician trigger required.

Watcher summary
---------------
checkin_atom_watcher  — every 5 min
    Finds daily check-ins entered today that have no behavioral atoms yet.
    Extracts atoms + runs gap detection for each affected patient. This
    closes the "new check-in → manual MCP call required" gap.

crisis_scan_watcher  — every 60 min
    Finds patients who had a check-in in the last 24 h and runs
    run_crisis_escalation for each. Catches high-stress / SI signals
    within the hour even when no clinician opens the tool.

care_gap_watcher  — every 24 h
    Finds patients with open care gaps older than 60 days and inserts an
    agent_interventions row of type 'care_gap_reminder' if one has not
    already been created in the last 7 days.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

# Configurable intervals (seconds) — override in tests via monkey-patching
CHECKIN_ATOM_INTERVAL: float = 300.0     # 5 minutes
CRISIS_SCAN_INTERVAL: float = 3600.0    # 60 minutes
CARE_GAP_INTERVAL: float = 86400.0      # 24 hours

# Care-gap thresholds
CARE_GAP_AGE_DAYS: int = 60
CARE_GAP_DEDUP_DAYS: int = 7


# ── 1. checkin_atom_watcher ───────────────────────────────────────────────────

async def _checkin_atom_watcher() -> None:
    """Extract behavioral atoms from today's unprocessed check-ins."""
    from db.connection import get_pool
    from skills.behavioral_atom_extractor import extract_atoms_from_checkin
    from skills.atom_embedder import embed_signal_value, active_backend
    from skills.atom_vector_search import refresh_atom_pressure_view
    from skills.behavioral_gap_detector import run_gap_detector_for_patient

    pool = await get_pool()

    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT dc.id, dc.patient_id,
                   dc.mood, dc.mood_numeric, dc.stress_level,
                   dc.sleep_hours, dc.sleep_quality, dc.notes
            FROM daily_checkins dc
            WHERE dc.completed_at >= NOW() - INTERVAL '10 minutes'
              AND NOT EXISTS (
                SELECT 1 FROM behavioral_signal_atoms bsa
                WHERE bsa.source_type = 'checkin'
                  AND bsa.source_id   = dc.id
              )
            LIMIT 50
            """,
        )

    if not rows:
        return

    logger.info(
        "checkin_atom_watcher: found %d unprocessed check-in(s) — extracting atoms",
        len(rows),
    )

    patients_processed: set[str] = set()
    atoms_stored_total = 0

    for row in rows:
        checkin_id = str(row["id"])
        patient_id = str(row["patient_id"])
        checkin = dict(row)

        atoms = extract_atoms_from_checkin(checkin, source_id=checkin_id)
        if not atoms:
            continue

        stored = 0
        async with pool.acquire() as conn:
            for atom in atoms:
                embedding = embed_signal_value(atom.signal_value)
                embedding_str = (
                    "[" + ",".join(str(x) for x in embedding) + "]"
                    if embedding else None
                )
                try:
                    await conn.execute(
                        """
                        INSERT INTO behavioral_signal_atoms
                            (id, patient_id, signal_type, signal_value, confidence,
                             source_type, source_id, extracted_at, embedding, data_source)
                        VALUES ($1, $2, $3, $4, $5, $6, $7, NOW(), $8::vector, 'healthex')
                        ON CONFLICT DO NOTHING
                        """,
                        str(uuid.uuid4()),
                        patient_id,
                        atom.signal_type,
                        atom.signal_value,
                        float(atom.confidence),
                        "checkin",
                        checkin_id,
                        embedding_str,
                    )
                    stored += 1
                except Exception as exc:
                    logger.warning(
                        "checkin_atom_watcher: insert failed for patient %s: %s",
                        patient_id, exc,
                    )

        atoms_stored_total += stored
        patients_processed.add(patient_id)

    if atoms_stored_total > 0:
        await refresh_atom_pressure_view(pool)

    for patient_id in patients_processed:
        try:
            await run_gap_detector_for_patient(pool, patient_id)
        except Exception as exc:
            logger.warning(
                "checkin_atom_watcher: gap detection failed for %s: %s",
                patient_id, exc,
            )

    logger.info(
        "checkin_atom_watcher: stored %d atom(s) for %d patient(s); backend=%s",
        atoms_stored_total, len(patients_processed), active_backend(),
    )


# ── 2. crisis_scan_watcher ────────────────────────────────────────────────────

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
    """Register the three built-in clinical watchers with *runtime*.

    Called once from server.py before ``mcp.run()``; safe to call in tests
    with a fresh AgentRuntime instance.
    """
    from runtime.agent_runtime import AgentRuntime  # noqa: F401 (type reference)

    runtime.watch(
        name="checkin_atom_watcher",
        interval_seconds=CHECKIN_ATOM_INTERVAL,
        coro_fn=_checkin_atom_watcher,
    )
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

    logger.info("register_watchers: 3 built-in watchers registered")
