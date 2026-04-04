"""Skill: run_food_access_nudge — end-of-month food access intervention."""

from __future__ import annotations

import json
import logging
import sys
from datetime import date, datetime

from fastmcp import FastMCP

from db.connection import get_pool
from skills.base import log_skill_execution

logging.basicConfig(level=logging.INFO, stream=sys.stderr)
logger = logging.getLogger(__name__)


def register(mcp: FastMCP):
    @mcp.tool
    async def run_food_access_nudge(
        patient_id: str,
        current_date: str = "",
    ) -> str:
        """Check if a food access nudge should be triggered for end-of-month.

        Trigger condition: day_of_month >= 25 AND patient has food_access SDoH flag.

        Args:
            patient_id: UUID of the patient
            current_date: Current date YYYY-MM-DD (defaults to today)
        """
        pool = await get_pool()
        try:
            if current_date:
                today = date.fromisoformat(current_date)
            else:
                today = date.today()

            async with pool.acquire() as conn:
                # Check trigger conditions
                is_eom = today.day >= 25

                food_flag = await conn.fetchrow(
                    """
                    SELECT domain, severity
                    FROM patient_sdoh_flags
                    WHERE patient_id = $1 AND domain = 'food_access'
                    """,
                    patient_id,
                )

                triggered = is_eom and food_flag is not None
                reason = ""
                content = ""

                if triggered:
                    severity = food_flag["severity"] if food_flag else "unknown"
                    reason = (
                        f"End-of-month (day {today.day}) + "
                        f"food_access flag (severity={severity})"
                    )
                    content = (
                        "End-of-month food access resources: "
                        "Community food banks, SNAP benefits assistance, "
                        "and local meal programs are available. "
                        "Would you like help connecting with these resources?"
                    )

                    # Insert intervention
                    await conn.execute(
                        """
                        INSERT INTO agent_interventions
                            (id, patient_id, intervention_type, channel,
                             summary, content, delivered_at, source_skill,
                             data_source)
                        VALUES (gen_random_uuid(), $1, 'nudge', 'in_app',
                                $2, $3, $4, 'food_access_nudge', $5)
                        """,
                        patient_id, reason, content,
                        datetime.utcnow(), "synthea",
                    )
                else:
                    if not is_eom:
                        reason = f"Not end-of-month (day {today.day})"
                    elif food_flag is None:
                        reason = "No food_access SDoH flag"

                result = {
                    "triggered": triggered,
                    "reason": reason,
                    "content": content,
                }

                await log_skill_execution(
                    conn, "run_food_access_nudge", patient_id, "completed",
                    output_data=result,
                )

            return json.dumps(result)

        except Exception as e:
            logger.error("run_food_access_nudge failed: %s", e)
            try:
                async with pool.acquire() as conn:
                    await log_skill_execution(
                        conn, "run_food_access_nudge", patient_id, "failed",
                        error_message=str(e),
                    )
            except Exception:
                logger.error("Failed to log skill execution error")
            return f"Error: {e}"
