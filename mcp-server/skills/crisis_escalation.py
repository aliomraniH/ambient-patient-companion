"""Skill: run_crisis_escalation — detect crisis indicators and escalate."""

from __future__ import annotations

import json
import logging
import sys
from datetime import date, datetime, timedelta

from fastmcp import FastMCP

from db.connection import get_pool
from skills.base import log_skill_execution

logging.basicConfig(level=logging.INFO, stream=sys.stderr)
logger = logging.getLogger(__name__)


def register(mcp: FastMCP):
    @mcp.tool
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
            triggers: list[str] = []

            async with pool.acquire() as conn:
                # --- BP crisis ---
                bp_rows = await conn.fetch(
                    """
                    SELECT value FROM biometric_readings
                    WHERE patient_id = $1
                      AND metric_type = 'bp_systolic'
                      AND measured_at >= $2
                      AND measured_at < $3 + INTERVAL '1 day'
                    ORDER BY measured_at DESC LIMIT 5
                    """,
                    patient_id, lookback, target,
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
                      AND measured_at < $3 + INTERVAL '1 day'
                    ORDER BY measured_at DESC LIMIT 5
                    """,
                    patient_id, lookback, target,
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

                # --- Create interventions if triggered ---
                if triggers:
                    summary = "; ".join(triggers)

                    await conn.execute(
                        """
                        INSERT INTO agent_interventions
                            (id, patient_id, intervention_type, summary,
                             delivered_at, source_skill)
                        VALUES (gen_random_uuid(), $1, 'crisis_escalation', $2,
                                $3, 'crisis_escalation')
                        """,
                        patient_id, summary, datetime.utcnow(),
                    )

                    await conn.execute(
                        """
                        INSERT INTO agent_memory_episodes
                            (id, patient_id, episode_type, summary, occurred_at)
                        VALUES (gen_random_uuid(), $1, 'crisis_detected', $2, $3)
                        """,
                        patient_id, summary, datetime.utcnow(),
                    )

                await log_skill_execution(
                    conn, "run_crisis_escalation", patient_id, "completed",
                    output_data={
                        "date": str(target),
                        "triggers_found": len(triggers),
                        "triggers": triggers,
                    },
                )

            if triggers:
                return (
                    f"⚠ CRISIS ESCALATION for patient {patient_id}: "
                    f"{len(triggers)} trigger(s) — {'; '.join(triggers)}"
                )
            return f"✓ No crisis indicators for patient {patient_id} on {target}"

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
