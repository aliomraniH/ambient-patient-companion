"""Skill: generate_previsit_brief — synthesize 6-month patient data for provider."""

from __future__ import annotations

import json
import logging
import sys
from datetime import date, datetime, timedelta, timezone

from fastmcp import FastMCP

from db.connection import get_pool
from shared.datetime_utils import ensure_aware
from skills.base import get_data_track, log_skill_execution

logging.basicConfig(level=logging.INFO, stream=sys.stderr)
logger = logging.getLogger(__name__)


async def generate_previsit_brief(
    patient_id: str,
    visit_date: str = "",
) -> str:
    """Generate a pre-visit brief for an upcoming appointment.

    Queries 6 months of interval data including vitals trends,
    medication changes, care gaps, and patient-reported concerns. When a
    completed deliberation exists within the last 24 hours, its pcp-facing
    outputs (anticipatory scenarios, predicted patient questions,
    missing-data flags) are folded into the brief under `recent_deliberation`.

    This tool is a cache-aware READER — it NEVER synchronously triggers
    `run_deliberation`. If a fresh deliberation is not available, the brief
    is returned from the 6-month query alone. Callers who want a fresh
    deliberation must invoke `run_deliberation` separately (fire-and-forget)
    and poll `get_deliberation_results` before re-requesting the brief.

    Args:
        patient_id: UUID of the patient
        visit_date: Upcoming visit date YYYY-MM-DD (defaults to today)
    """
    pool = await get_pool()
    try:
        if visit_date:
            target = date.fromisoformat(visit_date)
        else:
            target = date.today()

        lookback = target - timedelta(days=180)

        async with pool.acquire() as conn:
            data_track = await get_data_track(conn)

            # Patient demographics
            patient = await conn.fetchrow(
                "SELECT first_name, last_name, birth_date, gender "
                "FROM patients WHERE id = $1",
                patient_id,
            )
            if not patient:
                return f"Error: Patient {patient_id} not found"

            # Latest OBT score
            obt = await conn.fetchrow(
                """
                SELECT score, primary_driver, trend_direction, confidence
                FROM obt_scores
                WHERE patient_id = $1
                ORDER BY score_date DESC LIMIT 1
                """,
                patient_id,
            )

            # Vital trends (avg systolic, avg glucose over 6 months)
            vitals_summary = await conn.fetch(
                """
                SELECT metric_type,
                       ROUND(AVG(value)::numeric, 1) as avg_val,
                       ROUND(MIN(value)::numeric, 1) as min_val,
                       ROUND(MAX(value)::numeric, 1) as max_val,
                       COUNT(*) as reading_count
                FROM biometric_readings
                WHERE patient_id = $1
                  AND measured_at >= $2
                GROUP BY metric_type
                """,
                patient_id, lookback,
            )

            # Active conditions
            conditions = await conn.fetch(
                """
                SELECT code, display, clinical_status
                FROM patient_conditions
                WHERE patient_id = $1
                  AND clinical_status = 'active'
                """,
                patient_id,
            )

            # Active medications
            medications = await conn.fetch(
                """
                SELECT code, display, status
                FROM patient_medications
                WHERE patient_id = $1
                  AND status = 'active'
                """,
                patient_id,
            )

            # Open care gaps
            care_gaps = await conn.fetch(
                """
                SELECT gap_type, description, status
                FROM care_gaps
                WHERE patient_id = $1
                  AND status = 'open'
                """,
                patient_id,
            )

            # SDoH flags
            sdoh_flags = await conn.fetch(
                """
                SELECT domain, severity
                FROM patient_sdoh_flags
                WHERE patient_id = $1
                """,
                patient_id,
            )

            # Recent crisis events
            crises = await conn.fetch(
                """
                SELECT summary, delivered_at
                FROM agent_interventions
                WHERE patient_id = $1
                  AND intervention_type = 'escalation'
                  AND delivered_at >= $2
                ORDER BY delivered_at DESC LIMIT 5
                """,
                patient_id, lookback,
            )

            # Build brief
            brief = {
                "patient": {
                    "name": f"{patient['first_name']} {patient['last_name']}",
                    "birth_date": str(patient["birth_date"]) if patient["birth_date"] else None,
                    "gender": patient["gender"],
                },
                "visit_date": str(target),
                "obt_score": {
                    "score": float(obt["score"]) if obt else None,
                    "primary_driver": obt["primary_driver"] if obt else None,
                    "trend_direction": obt["trend_direction"] if obt else None,
                    "confidence": float(obt["confidence"]) if obt else None,
                },
                "interval_changes": [
                    {
                        "metric": row["metric_type"],
                        "avg": float(row["avg_val"]),
                        "min": float(row["min_val"]),
                        "max": float(row["max_val"]),
                        "readings": row["reading_count"],
                    }
                    for row in vitals_summary
                ],
                "active_conditions": [
                    {"code": r["code"], "display": r["display"]}
                    for r in conditions
                ],
                "active_medications": [
                    {"code": r["code"], "display": r["display"]}
                    for r in medications
                ],
                "open_care_gaps": [
                    {"type": r["gap_type"], "description": r["description"]}
                    for r in care_gaps
                ],
                "sdoh_flags": [
                    {"domain": r["domain"], "severity": r["severity"]}
                    for r in sdoh_flags
                ],
                "recent_crises": [
                    {
                        "summary": r["summary"],
                        "date": r["delivered_at"].isoformat() if r["delivered_at"] else None,
                    }
                    for r in crises
                ],
                "key_flags": [],
                "patient_questions": [],
            }

            # Recent deliberation (cache-aware reader; NEVER triggers run_deliberation).
            # Pulls pcp-facing outputs from the most recent COMPLETE deliberation
            # within the last 24 hours. If none, brief renders 6-month data alone.
            recent_cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
            delib_row = await conn.fetchrow(
                """
                SELECT id, triggered_at, convergence_score, rounds_completed
                FROM deliberations
                WHERE patient_id = $1
                  AND status = 'complete'
                  AND triggered_at >= $2
                ORDER BY triggered_at DESC
                LIMIT 1
                """,
                patient_id, recent_cutoff,
            )
            if delib_row:
                pcp_output_types = (
                    'anticipatory_scenario',
                    'predicted_patient_question',
                    'missing_data_flag',
                    'care_team_nudge',
                )
                outputs = await conn.fetch(
                    """
                    SELECT output_type, output_data, confidence, priority
                    FROM deliberation_outputs
                    WHERE deliberation_id = $1
                      AND output_type = ANY($2::text[])
                    """,
                    delib_row["id"], list(pcp_output_types),
                )
                brief["recent_deliberation"] = {
                    "deliberation_id": str(delib_row["id"]),
                    "triggered_at": delib_row["triggered_at"].isoformat(),
                    "age_hours": round(
                        (
                            datetime.now(timezone.utc)
                            - ensure_aware(delib_row["triggered_at"])
                        ).total_seconds() / 3600, 1
                    ),
                    "convergence_score": delib_row["convergence_score"],
                    "rounds_completed": delib_row["rounds_completed"],
                    "outputs": [dict(o) for o in outputs],
                }
            else:
                brief["recent_deliberation"] = None

            # Generate key flags
            if obt and float(obt["score"]) < 40:
                brief["key_flags"].append("OBT score critically low (<40)")
            if any(f["severity"] == "high" for f in sdoh_flags):
                brief["key_flags"].append("High-severity SDoH flag active")
            if crises:
                brief["key_flags"].append(
                    f"{len(crises)} crisis event(s) in past 6 months"
                )

            await log_skill_execution(
                conn, "generate_previsit_brief", patient_id, "completed",
                output_data={
                    "visit_date": str(target),
                    "conditions": len(conditions),
                    "medications": len(medications),
                    "care_gaps": len(care_gaps),
                },
                data_source=data_track,
            )

        return json.dumps(brief)

    except Exception as e:
        logger.error("generate_previsit_brief failed: %s", e)
        try:
            async with pool.acquire() as conn:
                await log_skill_execution(
                    conn, "generate_previsit_brief", patient_id, "failed",
                    error_message=str(e),
                )
        except Exception:
            logger.error("Failed to log skill execution error")
        return f"Error: {e}"


def register(mcp: FastMCP):
    mcp.tool(generate_previsit_brief)
