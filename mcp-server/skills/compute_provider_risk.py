"""Skill: compute_provider_risk — aggregate risk signals for provider panel."""

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

# PHQ-9 item 9 answer key aliases — different ingestion paths store the key differently
_PHQ9_ITEM9_KEYS = ("9", "item_9", "phq9_item9", "q9", "Q9", "Q09", "item9", "PHQ9_9")


def _extract_phq9_item9(item_answers: dict) -> int | None:
    """Return the PHQ-9 item 9 score (0-3) or None if not present."""
    if not item_answers:
        return None
    for key in _PHQ9_ITEM9_KEYS:
        val = item_answers.get(key)
        if val is not None:
            try:
                return int(val)
            except (TypeError, ValueError):
                pass
    return None


async def compute_provider_risk(
    patient_id: str,
    score_date: str = "",
) -> str:
    """Compute provider risk score for chase list ranking.

    Aggregates five risk factors with a weighted sum (0–100):

    | Factor          | Weight | Source                                       |
    |-----------------|--------|----------------------------------------------|
    | obt_risk        | 35 %   | obt_scores (inverted)                        |
    | crisis_risk     | 25 %   | agent_interventions + PHQ-9 item 9 (SI) [*]  |
    | sdoh_risk       | 15 %   | patient_sdoh_flags                           |
    | adherence_risk  | 15 %   | medication_adherence                         |
    | gap_risk        | 10 %   | care_gaps                                    |

    [*] PHQ-9 item 9 (suicidal ideation) is a safety-critical signal.
    Any positive screen contributes to crisis_risk even if it pre-dates the
    30-day escalation window.  A positive screen with NO follow-up escalation
    on record is amplified.  Decay is capped at 50 % weight at 24 months so an
    old unresolved SI screen never silently disappears from the score.

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
            data_track = await get_data_track(conn)

            # ── Factor 1: Latest OBT score (inverted: lower OBT = higher risk) ──
            obt = await conn.fetchrow(
                """
                SELECT score FROM obt_scores
                WHERE patient_id = $1
                ORDER BY score_date DESC LIMIT 1
                """,
                patient_id,
            )
            obt_score = float(obt["score"]) if obt else 50.0
            obt_risk = max(0.0, 100.0 - obt_score)

            # ── Factor 2: Crisis risk — escalations + PHQ-9 item 9 SI screening ──
            #
            # 2a. Recent crisis escalations (30-day window)
            crisis_count = await conn.fetchval(
                """
                SELECT COUNT(*) FROM agent_interventions
                WHERE patient_id = $1
                  AND intervention_type = 'escalation'
                  AND delivered_at >= $2
                """,
                patient_id, lookback_30,
            )
            crisis_risk_from_escalations = min(100.0, float(crisis_count) * 25.0)

            # 2b. PHQ-9 item 9 (suicidal ideation) — SAFETY CRITICAL
            #
            # We look at the most recent PHQ-9 screen regardless of age.
            # Positive item 9 (score > 0) is a safety signal that MUST surface
            # in the risk score even when it pre-dates the 30-day window.
            # Logic:
            #   item9 == 0               → no contribution
            #   item9 == 1 (passive SI)  → base 50 pts
            #   item9 >= 2 (active SI)   → base 75 pts
            # Time-decay: 1.0 at 0 months → 0.5 at 24 months (floor 0.5 forever)
            # No-followup amplifier: ×1.3 when no escalation exists in system
            phq9_row = await conn.fetchrow(
                """
                SELECT instrument_key, item_answers, triggered_critical,
                       administered_at
                FROM behavioral_screenings
                WHERE patient_id = $1
                  AND (instrument_key ILIKE 'phq%'
                       OR instrument_key ILIKE '%phq-9%'
                       OR instrument_key ILIKE '%phq9%')
                ORDER BY administered_at DESC
                LIMIT 1
                """,
                patient_id,
            )

            si_risk_contribution = 0.0
            si_flag: dict | None = None

            if phq9_row:
                # asyncpg returns JSONB as raw JSON strings when no codec is
                # registered — parse defensively
                _ia_raw = phq9_row["item_answers"]
                if isinstance(_ia_raw, str):
                    _ia_raw = json.loads(_ia_raw) if _ia_raw else {}
                item_answers = _ia_raw or {}

                _tc_raw = phq9_row["triggered_critical"]
                if isinstance(_tc_raw, str):
                    _tc_raw = json.loads(_tc_raw) if _tc_raw else []
                triggered    = _tc_raw or []

                administered = phq9_row["administered_at"]

                item9_score = _extract_phq9_item9(item_answers)

                # triggered_critical is a JSONB ARRAY of alert objects:
                #   [{"item_number": 9, "tag": "passive_suicidal_ideation", ...}]
                # (see migration 011 — partial index uses jsonb_array_length)
                triggered_list = triggered if isinstance(triggered, list) else []
                triggered_has_si = any(
                    (
                        str(alert.get("item_number", "")) == "9"
                        or "suicid" in str(alert.get("tag", "")).lower()
                        or "suicid" in str(alert.get("alert_text", "")).lower()
                        or "si" == str(alert.get("tag", "")).lower()
                        or "crisis" in str(alert.get("tag", "")).lower()
                    )
                    for alert in triggered_list
                    if isinstance(alert, dict)
                )

                if item9_score and item9_score > 0:
                    # Months elapsed since the screen
                    days_since = (target - administered.date()).days
                    months_since = days_since / 30.0

                    # Base risk by severity
                    if item9_score >= 2:
                        base_si_risk = 75.0   # Active / near-active SI
                    else:
                        base_si_risk = 50.0   # Passive SI

                    # No-followup amplifier — if no escalation ever logged
                    all_time_crisis = await conn.fetchval(
                        """
                        SELECT COUNT(*) FROM agent_interventions
                        WHERE patient_id = $1
                          AND intervention_type = 'escalation'
                        """,
                        patient_id,
                    )
                    if all_time_crisis == 0:
                        base_si_risk = min(100.0, base_si_risk * 1.3)

                    # Time-decay: linear from 1.0 → 0.5 over 24 months, then floor
                    decay = max(0.5, 1.0 - (months_since / 24.0) * 0.5)
                    si_risk_contribution = base_si_risk * decay

                    si_flag = {
                        "instrument":      phq9_row["instrument_key"],
                        "item9_score":     item9_score,
                        "item9_label":     (
                            "active_si" if item9_score >= 2 else "passive_si"
                        ),
                        "administered_at": administered.isoformat(),
                        "days_since":      days_since,
                        "months_since":    round(months_since, 1),
                        "no_escalation_on_record": all_time_crisis == 0,
                        "decay_factor":    round(decay, 2),
                    }
                elif triggered_has_si:
                    # triggered_critical flagged SI even if item9 key not found
                    si_risk_contribution = 50.0
                    si_flag = {
                        "instrument":   phq9_row["instrument_key"],
                        "source":       "triggered_critical",
                        "administered_at": phq9_row["administered_at"].isoformat(),
                    }

            # 2c. Crisis-related open care gaps (unresolved SI / mental health)
            si_gap_count = await conn.fetchval(
                """
                SELECT COUNT(*) FROM care_gaps
                WHERE patient_id = $1
                  AND status = 'open'
                  AND (
                    gap_type ILIKE '%suicid%'
                    OR gap_type ILIKE '%crisis%'
                    OR gap_type ILIKE '%mental%'
                    OR gap_type ILIKE '%phq%'
                    OR description ILIKE '%suicid%'
                    OR description ILIKE '%item 9%'
                    OR description ILIKE '%item9%'
                  )
                """,
                patient_id,
            )
            si_gap_contribution = min(40.0, float(si_gap_count) * 40.0)

            # 2d. Conversation-derived atom pressure (behavioral signals from
            # clinical notes / companion dialogue). atom_pressure_scores is
            # a materialized view pivoted on signal_type, so we iterate the
            # rows rather than expecting per-column named fields.
            atom_contribution = 0.0
            atom_signals: list[dict] = []
            try:
                atom_rows = await conn.fetch(
                    """
                    SELECT signal_type, pressure_score, present_atom_count,
                           last_atom_at
                    FROM atom_pressure_scores
                    WHERE patient_id = $1::uuid
                      AND signal_type IN ('suicidality', 'depression', 'anxiety')
                    """,
                    patient_id,
                )
            except Exception as _atom_exc:
                logger.debug("atom_pressure_scores read skipped: %s", _atom_exc)
                atom_rows = []

            # Weight atom pressure below formal screening instruments:
            # item 9 = 1 on PHQ-9 is worth 50 base points. Atom-derived
            # suicidality at pressure 0.9 is worth at most ~30 pts.
            _ATOM_WEIGHTS = {
                "suicidality": 35.0,
                "depression":  10.0,
                "anxiety":      5.0,
            }
            for row in atom_rows:
                sig = row["signal_type"]
                pressure = float(row["pressure_score"] or 0.0)
                if pressure <= 0.0:
                    continue
                w = _ATOM_WEIGHTS.get(sig, 0.0)
                if w == 0.0:
                    continue
                contrib = w * pressure
                atom_contribution += contrib
                atom_signals.append({
                    "signal_type": sig,
                    "pressure_score": round(pressure, 3),
                    "present_atom_count": int(row["present_atom_count"] or 0),
                    "last_atom_at": (
                        row["last_atom_at"].isoformat()
                        if row["last_atom_at"] else None
                    ),
                    "contribution": round(contrib, 1),
                })
            atom_contribution = min(60.0, atom_contribution)

            # Final crisis_risk = escalations + SI screen + SI care gap + atoms
            crisis_risk = min(
                100.0,
                crisis_risk_from_escalations
                + si_risk_contribution
                + si_gap_contribution
                + atom_contribution,
            )

            # Data-status classification: distinguishes a zero-risk score
            # driven by "we looked and found nothing" from one driven by
            # "we never looked". Downstream UIs render these differently.
            has_screen = phq9_row is not None
            latest_screen_age_days = None
            if phq9_row and phq9_row["administered_at"]:
                latest_screen_age_days = (
                    target - phq9_row["administered_at"].date()
                ).days

            has_atom_signal = any(
                float(r["pressure_score"] or 0.0) > 0.3
                for r in atom_rows
            )

            if has_screen and (si_risk_contribution > 0 or has_atom_signal):
                data_status = "screened_abnormal"
            elif has_screen and latest_screen_age_days is not None and latest_screen_age_days > 365:
                data_status = "overdue"
            elif has_screen:
                data_status = "screened_normal"
            elif has_atom_signal:
                data_status = "atoms_only"
            else:
                data_status = "never_screened"

            # ── Factor 3: SDoH severity ──────────────────────────────────────────
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

            # ── Factor 4: Medication adherence (last 30 days) ────────────────────
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

            # ── Factor 5: Open care gaps ─────────────────────────────────────────
            gap_count = await conn.fetchval(
                """
                SELECT COUNT(*) FROM care_gaps
                WHERE patient_id = $1 AND status = 'open'
                """,
                patient_id,
            )
            gap_risk = min(100.0, float(gap_count) * 20.0)

            # ── Weighted risk score ───────────────────────────────────────────────
            risk_score = round(
                obt_risk      * 0.35
                + crisis_risk * 0.25
                + sdoh_risk   * 0.15
                + adherence_risk * 0.15
                + gap_risk    * 0.10,
                1,
            )

            # Risk tier
            if risk_score >= 70:
                risk_tier = "high"
            elif risk_score >= 40:
                risk_tier = "moderate"
            else:
                risk_tier = "low"

            risk_factors: dict = {
                "obt_risk":       round(obt_risk, 1),
                "crisis_risk":    round(crisis_risk, 1),
                "sdoh_risk":      round(sdoh_risk, 1),
                "adherence_risk": round(adherence_risk, 1),
                "gap_risk":       round(gap_risk, 1),
                # Breakdown of crisis_risk sub-components for transparency
                "crisis_breakdown": {
                    "escalations_30d":     round(crisis_risk_from_escalations, 1),
                    "si_screening":        round(si_risk_contribution, 1),
                    "si_care_gap":         round(si_gap_contribution, 1),
                    "atom_pressure":       round(atom_contribution, 1),
                    "si_flag":             si_flag,
                    "atom_signals":        atom_signals,
                    "data_status":         data_status,
                },
            }

            # ── Persist ───────────────────────────────────────────────────────────
            await conn.execute(
                """
                INSERT INTO provider_risk_scores
                    (id, patient_id, score_date, risk_score, risk_tier,
                     risk_factors, data_source)
                VALUES (gen_random_uuid(), $1, $2, $3, $4, $5, $6)
                ON CONFLICT (patient_id, score_date) DO UPDATE SET
                    risk_score   = EXCLUDED.risk_score,
                    risk_tier    = EXCLUDED.risk_tier,
                    risk_factors = EXCLUDED.risk_factors
                """,
                patient_id, target, risk_score, risk_tier,
                json.dumps(risk_factors, default=str), data_track,
            )

            await log_skill_execution(
                conn, "compute_provider_risk", patient_id, "completed",
                output_data={
                    "risk_score": risk_score,
                    "risk_tier":  risk_tier,
                    "risk_factors": risk_factors,
                },
                data_source=data_track,
            )

        return json.dumps({
            "risk_score":   risk_score,
            "risk_tier":    risk_tier,
            "chase_list_rank": None,
            "risk_factors": risk_factors,
        }, default=str)

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


def register(mcp: FastMCP):
    mcp.tool(compute_provider_risk)
