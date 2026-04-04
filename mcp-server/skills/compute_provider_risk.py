"""Skill: compute_provider_risk — aggregate risk signals for provider panel."""

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
    async def compute_provider_risk(
        patient_id: str,
        score_date: str = "",
    ) -> str:
        """Compute provider risk score for chase list ranking.

        Aggregates signals: OBT score, crisis events, SDoH severity,
        medication adherence, and care gap count.

        Args:
            patient_id: UUID of the patient
            score_date: Date YYYY-MM-DD (defaults to today)
        """
        pool = await get_pool()
        try:
            if score_date:
                target = date.fromisoformat(score_date)
            else:
                target = date.today()

            lookback_30 = target - timedelta(days=30)

            async with pool.acquire() as conn:
                # Factor 1: Latest OBT score (inverted: lower OBT = higher risk)
                obt = await conn.fetchrow(
                    """
                    SELECT score FROM obt_scores
                    WHERE patient_id = $1
                    ORDER BY score_date DESC LIMIT 1
                    """,
                    patient_id,
                )
                obt_score = float(obt["score"]) if obt else 50.0
                obt_risk = max(0.0, 100.0 - obt_score)  # Invert: low OBT = high risk

                # Factor 2: Crisis events in last 30 days
                crisis_count = await conn.fetchval(
                    """
                    SELECT COUNT(*) FROM agent_interventions
                    WHERE patient_id = $1
                      AND intervention_type = 'escalation'
                      AND delivered_at >= $2
                    """,
                    patient_id, lookback_30,
                )
                crisis_risk = min(100.0, float(crisis_count) * 25.0)

                # Factor 3: SDoH severity
                sdoh_rows = await conn.fetch(
                    """
                    SELECT severity FROM patient_sdoh_flags
                    WHERE patient_id = $1
                    """,
                    patient_id,
                )
                sdoh_risk = 0.0
                for row in sdoh_rows:
                    if row["severity"] == "high":
                        sdoh_risk += 33.3
                    elif row["severity"] == "moderate":
                        sdoh_risk += 16.7
                    else:
                        sdoh_risk += 8.3
                sdoh_risk = min(100.0, sdoh_risk)

                # Factor 4: Medication adherence (last 30 days)
                adherence_rows = await conn.fetch(
                    """
                    SELECT taken FROM medication_adherence
                    WHERE patient_id = $1
                      AND adherence_date >= $2
                    """,
                    patient_id, lookback_30,
                )
                if adherence_rows:
                    taken_count = sum(1 for r in adherence_rows if r["taken"])
                    adherence_rate = taken_count / len(adherence_rows)
                    adherence_risk = max(0.0, (1.0 - adherence_rate) * 100.0)
                else:
                    adherence_risk = 50.0  # Unknown = moderate risk

                # Factor 5: Open care gaps
                gap_count = await conn.fetchval(
                    """
                    SELECT COUNT(*) FROM care_gaps
                    WHERE patient_id = $1 AND status = 'open'
                    """,
                    patient_id,
                )
                gap_risk = min(100.0, float(gap_count) * 20.0)

                # Weighted risk score
                risk_score = round(
                    obt_risk * 0.35
                    + crisis_risk * 0.25
                    + sdoh_risk * 0.15
                    + adherence_risk * 0.15
                    + gap_risk * 0.10,
                    1,
                )

                # Risk tier
                if risk_score >= 70:
                    risk_tier = "high"
                elif risk_score >= 40:
                    risk_tier = "moderate"
                else:
                    risk_tier = "low"

                risk_factors = {
                    "obt_risk": round(obt_risk, 1),
                    "crisis_risk": round(crisis_risk, 1),
                    "sdoh_risk": round(sdoh_risk, 1),
                    "adherence_risk": round(adherence_risk, 1),
                    "gap_risk": round(gap_risk, 1),
                }

                # Write to provider_risk_scores
                await conn.execute(
                    """
                    INSERT INTO provider_risk_scores
                        (id, patient_id, score_date, risk_score, risk_tier,
                         risk_factors, data_source)
                    VALUES (gen_random_uuid(), $1, $2, $3, $4, $5, $6)
                    ON CONFLICT (patient_id, score_date) DO UPDATE SET
                        risk_score = EXCLUDED.risk_score,
                        risk_tier = EXCLUDED.risk_tier,
                        risk_factors = EXCLUDED.risk_factors
                    """,
                    patient_id, target, risk_score, risk_tier,
                    json.dumps(risk_factors), "synthea",
                )

                await log_skill_execution(
                    conn, "compute_provider_risk", patient_id, "completed",
                    output_data={
                        "risk_score": risk_score,
                        "risk_tier": risk_tier,
                        "risk_factors": risk_factors,
                    },
                )

            return json.dumps({
                "risk_score": risk_score,
                "risk_tier": risk_tier,
                "chase_list_rank": None,  # Rank computed across all patients
                "risk_factors": risk_factors,
            })

        except Exception as e:
            logger.error("compute_provider_risk failed: %s", e)
            try:
                async with pool.acquire() as conn:
                    await log_skill_execution(
                        conn, "compute_provider_risk", patient_id, "failed",
                        error_message=str(e),
                    )
            except Exception:
                logger.error("Failed to log skill execution error")
            return f"Error: {e}"
