"""Skill: run_crisis_escalation — detect crisis indicators and escalate."""

from __future__ import annotations

import json
import logging
import sys
from datetime import date, datetime, timedelta

from fastmcp import FastMCP

from db.connection import get_pool
from skills.base import get_data_track, log_skill_execution

logging.basicConfig(level=logging.INFO, stream=sys.stderr)
logger = logging.getLogger(__name__)


def _parse_jsonb(raw, default):
    """Parse an asyncpg JSONB field that may be returned as a raw string."""
    if raw is None:
        return default
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except Exception:
            return default
    return raw


async def run_crisis_escalation(
    patient_id: str,
    check_date: str = "",
) -> str:
    """Check for crisis indicators and create escalation interventions.

    Crisis triggers:
    - BP systolic > 170 or < 90
    - Glucose fasting > 250
    - Stress >= 8 for 3+ consecutive days
    - Sleep < 5.0 for 3+ consecutive days
    - Mood 'bad' (1) for 3+ consecutive days
    - PHQ-9 item 9 >= 2 (active suicidal ideation) within last 12 months,
      deduplicated: no new escalation if one already created in last 14 days

    Args:
        patient_id: UUID of the patient
        check_date: Date in YYYY-MM-DD format (defaults to today)
    """
    pool = await get_pool()
    try:
        if check_date:
            target = date.fromisoformat(check_date)
        else:
            target = date.today()

        lookback = target - timedelta(days=7)
        lookback_plus_one = target + timedelta(days=1)
        triggers: list[str] = []

        async with pool.acquire() as conn:
            data_track = await get_data_track(conn)

            # --- BP crisis ---
            bp_rows = await conn.fetch(
                """
                SELECT value FROM biometric_readings
                WHERE patient_id = $1
                  AND metric_type = 'bp_systolic'
                  AND measured_at >= $2
                  AND measured_at < $3
                ORDER BY measured_at DESC LIMIT 5
                """,
                patient_id, lookback, lookback_plus_one,
            )
            for row in bp_rows:
                v = float(row["value"])
                if v > 170:
                    triggers.append(f"BP systolic critically high: {v} mmHg")
                    break
                if v < 90:
                    triggers.append(f"BP systolic critically low: {v} mmHg")
                    break

            # --- Glucose crisis ---
            glc_rows = await conn.fetch(
                """
                SELECT value FROM biometric_readings
                WHERE patient_id = $1
                  AND metric_type = 'glucose_fasting'
                  AND measured_at >= $2
                  AND measured_at < $3
                ORDER BY measured_at DESC LIMIT 5
                """,
                patient_id, lookback, lookback_plus_one,
            )
            for row in glc_rows:
                if float(row["value"]) > 250:
                    triggers.append(
                        f"Fasting glucose critically high: {row['value']} mg/dL"
                    )
                    break

            # --- Consecutive-day check-in triggers ---
            checkin_rows = await conn.fetch(
                """
                SELECT checkin_date, stress_level, sleep_hours, mood_numeric
                FROM daily_checkins
                WHERE patient_id = $1
                  AND checkin_date >= $2
                  AND checkin_date <= $3
                ORDER BY checkin_date DESC
                """,
                patient_id, lookback, target,
            )

            if len(checkin_rows) >= 3:
                # Check last 3 days
                last3 = checkin_rows[:3]

                # Stress >= 8 for 3 consecutive days
                if all(r["stress_level"] >= 8 for r in last3):
                    triggers.append(
                        f"Stress ≥ 8 for 3+ consecutive days "
                        f"(levels: {[r['stress_level'] for r in last3]})"
                    )

                # Sleep < 5.0 for 3 consecutive days
                if all(
                    r["sleep_hours"] is not None and r["sleep_hours"] < 5.0
                    for r in last3
                ):
                    triggers.append(
                        f"Sleep < 5h for 3+ consecutive days "
                        f"(hours: {[r['sleep_hours'] for r in last3]})"
                    )

                # Mood bad (1) for 3 consecutive days
                if all(r["mood_numeric"] == 1 for r in last3):
                    triggers.append("Mood 'bad' for 3+ consecutive days")

            # --- PHQ-9 item 9 active SI screen ---
            # Check the most recent PHQ-9 (or PHQ-* instrument) with item 9
            # score >= 2 (active suicidal ideation) administered within 12 months.
            # If found and no escalation has been created for SI in the last 14
            # days, trigger one regardless of biometric state.
            si_lookback = target - timedelta(days=365)
            dedup_lookback = target - timedelta(days=14)
            phq_si_row = await conn.fetchrow(
                """
                SELECT instrument_key, item_answers, triggered_critical,
                       administered_at
                FROM behavioral_screenings
                WHERE patient_id = $1
                  AND instrument_key LIKE 'phq%'
                  AND administered_at >= $2
                ORDER BY administered_at DESC
                LIMIT 1
                """,
                patient_id, si_lookback,
            )
            if phq_si_row:
                item_answers = _parse_jsonb(phq_si_row["item_answers"], {})
                item9_score = None
                for key in ("9", 9, "item_9"):
                    v = item_answers.get(key) if isinstance(item_answers, dict) else None
                    if v is not None:
                        try:
                            item9_score = int(v)
                        except (TypeError, ValueError):
                            pass
                        break

                if item9_score is not None and item9_score >= 2:
                    # Only create if no SI escalation in the last 14 days
                    recent_si = await conn.fetchval(
                        """
                        SELECT COUNT(*) FROM agent_interventions
                        WHERE patient_id = $1
                          AND intervention_type = 'escalation'
                          AND summary ILIKE '%suicid%'
                          AND delivered_at >= $2
                        """,
                        patient_id, dedup_lookback,
                    )
                    if not recent_si:
                        label = "active" if item9_score >= 2 else "passive"
                        triggers.append(
                            f"PHQ-9 item 9 = {item9_score} ({label} suicidal ideation), "
                            f"screen administered {phq_si_row['administered_at'].date()}"
                        )

            # --- Create interventions if triggered ---
            if triggers:
                summary = "; ".join(triggers)

                await conn.execute(
                    """
                    INSERT INTO agent_interventions
                        (id, patient_id, intervention_type, channel, summary,
                         delivered_at, source_skill, data_source)
                    VALUES (gen_random_uuid(), $1, 'escalation', 'provider_alert',
                            $2, $3, 'crisis_escalation', $4)
                    """,
                    patient_id, summary, datetime.utcnow(), data_track,
                )

                await conn.execute(
                    """
                    INSERT INTO agent_memory_episodes
                        (id, patient_id, episode_type, summary, occurred_at,
                         data_source)
                    VALUES (gen_random_uuid(), $1, 'crisis_detected', $2, $3, $4)
                    """,
                    patient_id, summary, datetime.utcnow(), data_track,
                )

            await log_skill_execution(
                conn, "run_crisis_escalation", patient_id, "completed",
                output_data={
                    "date": str(target),
                    "triggers_found": len(triggers),
                    "triggers": triggers,
                },
                data_source=data_track,
            )

        return json.dumps({
            "escalation_triggered": bool(triggers),
            "triggers": triggers,
            "patient_id": patient_id,
            "date": str(target),
        })

    except Exception as e:
        logger.error("run_crisis_escalation failed: %s", e)
        try:
            async with pool.acquire() as conn:
                await log_skill_execution(
                    conn, "run_crisis_escalation", patient_id, "failed",
                    error_message=str(e),
                )
        except Exception:
            logger.error("Failed to log skill execution error")
        return f"Error: {e}"


def register(mcp: FastMCP):
    mcp.tool(run_crisis_escalation)
