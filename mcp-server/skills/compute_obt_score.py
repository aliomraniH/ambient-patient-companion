"""Skill: compute_obt_score — One Big Thing score calculation.

Implements the exact OBT algorithm from CLAUDE.md:
  score = bp(0.30) + glucose(0.25) + behavioral(0.20) + adherence(0.15) + sleep(0.10)
"""

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


def _deviation_to_score(
    current_avg: float,
    baseline: float,
    good_threshold: float,
    bad_threshold: float,
) -> float:
    """Map deviation from baseline to 0-100 score.

    0 deviation -> 100.  Deviation >= bad_threshold -> 0.
    Linear interpolation between good and bad thresholds.
    """
    deviation = abs(current_avg - baseline)
    if deviation <= good_threshold:
        return 100.0
    if deviation >= bad_threshold:
        return 0.0
    # Linear interpolation
    ratio = (deviation - good_threshold) / (bad_threshold - good_threshold)
    return max(0.0, min(100.0, 100.0 * (1.0 - ratio)))


async def compute_obt_score(
    patient_id: str,
    score_date: str = "",
) -> str:
    """Compute the One Big Thing (OBT) wellness score for a patient.

    Args:
        patient_id: UUID of the patient
        score_date: Date in YYYY-MM-DD format (defaults to today)
    """
    pool = await get_pool()
    try:
        if score_date:
            target = date.fromisoformat(score_date)
        else:
            target = date.today()

        window_start = target - timedelta(days=30)
        week_start = target - timedelta(days=7)
        prev_week_start = target - timedelta(days=14)
        target_plus_one = target + timedelta(days=1)

        async with pool.acquire() as conn:
            data_track = await get_data_track(conn)

            # --- BP data ---
            bp_rows = await conn.fetch(
                """
                SELECT value, measured_at FROM biometric_readings
                WHERE patient_id = $1
                  AND metric_type = 'bp_systolic'
                  AND measured_at >= $2
                  AND measured_at < $3
                ORDER BY measured_at
                """,
                patient_id, window_start, target_plus_one,
            )
            bp_values = [float(r["value"]) for r in bp_rows]

            # --- Glucose data ---
            glc_rows = await conn.fetch(
                """
                SELECT value, measured_at FROM biometric_readings
                WHERE patient_id = $1
                  AND metric_type = 'glucose_fasting'
                  AND measured_at >= $2
                  AND measured_at < $3
                ORDER BY measured_at
                """,
                patient_id, window_start, target_plus_one,
            )
            glc_values = [float(r["value"]) for r in glc_rows]

            # --- Check-in data ---
            checkin_rows = await conn.fetch(
                """
                SELECT mood_numeric, sleep_hours, checkin_date
                FROM daily_checkins
                WHERE patient_id = $1
                  AND checkin_date >= $2
                  AND checkin_date <= $3
                ORDER BY checkin_date
                """,
                patient_id, window_start, target,
            )

            # --- Adherence data ---
            adherence_rows = await conn.fetch(
                """
                SELECT taken FROM medication_adherence
                WHERE patient_id = $1
                  AND adherence_date >= $2
                  AND adherence_date <= $3
                """,
                patient_id, window_start, target,
            )

            # Count data days for confidence
            data_days = len(set(
                [r["measured_at"].date() if hasattr(r["measured_at"], "date") else r["measured_at"] for r in bp_rows]
                + [r["checkin_date"] if isinstance(r["checkin_date"], date) else r["checkin_date"] for r in checkin_rows]
            ))

            # --- Compute domain scores ---

            # BP score (0-100)
            if bp_values:
                bp_baseline = sum(bp_values) / len(bp_values)
                # Use last 7 days for current
                recent_bp = [float(r["value"]) for r in bp_rows
                             if r["measured_at"].date() >= week_start]
                bp_current = sum(recent_bp) / len(recent_bp) if recent_bp else bp_baseline
                bp_score = _deviation_to_score(bp_current, bp_baseline, 5, 30)
            else:
                bp_score = 50.0  # default when no data

            # Glucose score (0-100)
            if glc_values:
                glc_baseline = sum(glc_values) / len(glc_values)
                recent_glc = [float(r["value"]) for r in glc_rows
                              if r["measured_at"].date() >= week_start]
                glc_current = sum(recent_glc) / len(recent_glc) if recent_glc else glc_baseline
                glucose_score = _deviation_to_score(glc_current, glc_baseline, 10, 60)
            else:
                glucose_score = 50.0

            # Behavioral score (mood + energy proxy via mood)
            if checkin_rows:
                mood_values = [r["mood_numeric"] for r in checkin_rows]
                mood_avg = sum(mood_values) / len(mood_values)
                # Normalize: mood 1-5 -> 0-100
                behavioral_score = max(0.0, min(100.0, (mood_avg - 1) / 4.0 * 100.0))
            else:
                behavioral_score = 50.0

            # Adherence score
            if adherence_rows:
                taken_count = sum(1 for r in adherence_rows if r["taken"])
                adherence_score = (taken_count / len(adherence_rows)) * 100.0
            else:
                adherence_score = 50.0

            # Sleep score
            if checkin_rows:
                sleep_values = [r["sleep_hours"] for r in checkin_rows
                                if r["sleep_hours"] is not None]
                if sleep_values:
                    avg_sleep = sum(sleep_values) / len(sleep_values)
                    if 7.0 <= avg_sleep <= 9.0:
                        sleep_score = 100.0
                    elif avg_sleep < 7.0:
                        sleep_score = max(0.0, 100.0 - (7.0 - avg_sleep) * 33.3)
                    else:
                        sleep_score = max(0.0, 100.0 - (avg_sleep - 9.0) * 33.3)
                else:
                    sleep_score = 50.0
            else:
                sleep_score = 50.0

            # --- Final score ---
            score = (
                bp_score * 0.30
                + glucose_score * 0.25
                + behavioral_score * 0.20
                + adherence_score * 0.15
                + sleep_score * 0.10
            )
            score = round(max(0.0, min(100.0, score)), 1)

            # Primary driver = domain with lowest score
            domain_scores = {
                "blood_pressure": bp_score,
                "glucose": glucose_score,
                "behavioral": behavioral_score,
                "adherence": adherence_score,
                "sleep": sleep_score,
            }
            primary_driver = min(domain_scores, key=domain_scores.get)

            # Trend direction: this-week vs last-week OBT
            prev_obt = await conn.fetchrow(
                """
                SELECT score FROM obt_scores
                WHERE patient_id = $1
                  AND score_date >= $2
                  AND score_date < $3
                ORDER BY score_date DESC LIMIT 1
                """,
                patient_id, prev_week_start, week_start,
            )
            if prev_obt:
                diff = score - float(prev_obt["score"])
                if diff > 2:
                    trend_direction = "improving"
                elif diff < -2:
                    trend_direction = "declining"
                else:
                    trend_direction = "stable"
            else:
                trend_direction = "stable"

            # Confidence
            if data_days >= 14:
                confidence = 1.0
            elif data_days >= 7:
                confidence = 0.7
            else:
                confidence = 0.4

            # --- Write OBT score ---
            await conn.execute(
                """
                INSERT INTO obt_scores
                    (id, patient_id, score_date, score, primary_driver,
                     trend_direction, confidence, domain_scores, data_source)
                VALUES (gen_random_uuid(), $1, $2, $3, $4, $5, $6, $7, $8)
                ON CONFLICT (patient_id, score_date) DO UPDATE SET
                    score = EXCLUDED.score,
                    primary_driver = EXCLUDED.primary_driver,
                    trend_direction = EXCLUDED.trend_direction,
                    confidence = EXCLUDED.confidence,
                    domain_scores = EXCLUDED.domain_scores
                """,
                patient_id, target, score, primary_driver,
                trend_direction, confidence,
                json.dumps(domain_scores), data_track,
            )

            # --- Write clinical fact ---
            summary = (
                f"OBT score {score}/100. "
                f"Primary driver: {primary_driver}. "
                f"Trend: {trend_direction}. "
                f"Confidence: {confidence}"
            )
            await conn.execute(
                """
                INSERT INTO clinical_facts
                    (id, patient_id, fact_type, category, summary,
                     ttl_expires_at, source_skill, data_source)
                VALUES (gen_random_uuid(), $1, 'obt_score', 'wellness',
                        $2, NOW() + INTERVAL '30 days', 'compute_obt_score',
                        $3)
                ON CONFLICT DO NOTHING
                """,
                patient_id, summary, data_track,
            )

            await log_skill_execution(
                conn, "compute_obt_score", patient_id, "completed",
                output_data={
                    "score": score,
                    "primary_driver": primary_driver,
                    "trend_direction": trend_direction,
                    "confidence": confidence,
                    "domain_scores": domain_scores,
                },
                data_source=data_track,
            )

        return json.dumps({
            "score": score,
            "primary_driver": primary_driver,
            "trend_direction": trend_direction,
            "confidence": confidence,
            "domain_scores": domain_scores,
            "patient_id": patient_id,
            "date": str(target),
        })

    except Exception as e:
        logger.error("compute_obt_score failed: %s", e)
        try:
            async with pool.acquire() as conn:
                await log_skill_execution(
                    conn, "compute_obt_score", patient_id, "failed",
                    error_message=str(e),
                )
        except Exception:
            logger.error("Failed to log skill execution error")
        return f"Error: {e}"


def register(mcp: FastMCP):
    mcp.tool(compute_obt_score)
