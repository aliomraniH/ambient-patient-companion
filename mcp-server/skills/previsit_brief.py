"""Skill: generate_previsit_brief — synthesize 6-month patient data for provider.

Refactored in P-3 to:
  - compose missing fields (patient_questions, key_flags) from the
    deliberation outputs that sibling tools already surface, rather
    than leaving them as empty placeholders
  - unify the staleness threshold via system_config
    (deliberation_staleness_fresh_hours, deliberation_staleness_recent_days)
    so a 72h deliberation is carried forward with a PRIOR_SESSION tag
    instead of being dropped at the 24h hard cutoff
  - stamp every field with _provenance metadata (source tool + tier)
    so a downstream auditor can see where each value came from
"""

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


# Defaults mirror the system_config keys written by migration 013.
# Reads are best-effort; on failure we fall back to these values.
_DEFAULT_FRESH_HOURS = 24.0
_DEFAULT_RECENT_DAYS = 7.0


async def _read_staleness_band(conn) -> tuple[float, float]:
    """Return (fresh_hours, recent_hours) from system_config.

    fresh_hours  — deliberations newer than this are tagged TOOL.
    recent_hours — deliberations newer than this (but older than fresh)
                   are tagged PRIOR_SESSION.
    Anything older than recent_hours is tagged PRIOR_SESSION_STALE.
    """
    fresh = _DEFAULT_FRESH_HOURS
    recent_days = _DEFAULT_RECENT_DAYS
    try:
        row = await conn.fetchval(
            "SELECT value FROM system_config WHERE key = $1",
            "deliberation_staleness_fresh_hours",
        )
        if row is not None:
            fresh = float(row)
    except Exception as exc:
        logger.debug("read fresh_hours failed: %s", exc)
    try:
        row = await conn.fetchval(
            "SELECT value FROM system_config WHERE key = $1",
            "deliberation_staleness_recent_days",
        )
        if row is not None:
            recent_days = float(row)
    except Exception as exc:
        logger.debug("read recent_days failed: %s", exc)
    return fresh, recent_days * 24.0


def _classify_freshness(age_hours: float, fresh_hours: float, recent_hours: float) -> dict:
    """Classify a deliberation by age and return a freshness descriptor.

    Returns a dict carrying: tier, provenance_tag, age_hours, and
    optional warning text for stale outputs.
    """
    if age_hours < fresh_hours:
        return {
            "tier": "fresh",
            "provenance_tag": "TOOL",
            "age_hours": round(age_hours, 1),
        }
    if age_hours < recent_hours:
        return {
            "tier": "recent",
            "provenance_tag": "PRIOR_SESSION",
            "age_hours": round(age_hours, 1),
        }
    return {
        "tier": "stale",
        "provenance_tag": "PRIOR_SESSION_STALE",
        "age_hours": round(age_hours, 1),
        "warning": (
            f"deliberation is {age_hours:.0f} h old — re-verify against current "
            "tool calls before acting on it"
        ),
    }


def _provenance_tool(source: str, tier: str = "TOOL") -> dict:
    return {
        "tier": tier,
        "source": source,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }


async def generate_previsit_brief(
    patient_id: str,
    visit_date: str = "",
) -> str:
    """Generate a pre-visit brief for an upcoming appointment.

    Reads from production tables (patient_conditions, patient_medications,
    care_gaps, obt_scores, deliberations, deliberation_outputs) and
    composes fields that live under sibling MCP tools (key_flags,
    patient_questions). Staleness thresholds come from system_config.

    Cache-aware READER — NEVER synchronously triggers `run_deliberation`.
    Prior deliberations are kept with a PRIOR_SESSION provenance tag
    rather than dropped at a hard 24 h cutoff.

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
            fresh_hours, recent_hours = await _read_staleness_band(conn)

            patient = await conn.fetchrow(
                "SELECT first_name, last_name, birth_date, gender "
                "FROM patients WHERE id = $1",
                patient_id,
            )
            if not patient:
                return f"Error: Patient {patient_id} not found"

            obt = await conn.fetchrow(
                """
                SELECT score, primary_driver, trend_direction, confidence
                FROM obt_scores
                WHERE patient_id = $1
                ORDER BY score_date DESC LIMIT 1
                """,
                patient_id,
            )

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

            conditions = await conn.fetch(
                """
                SELECT code, display, clinical_status
                FROM patient_conditions
                WHERE patient_id = $1
                  AND clinical_status = 'active'
                """,
                patient_id,
            )

            medications = await conn.fetch(
                """
                SELECT code, display, status
                FROM patient_medications
                WHERE patient_id = $1
                  AND status = 'active'
                """,
                patient_id,
            )

            care_gaps = await conn.fetch(
                """
                SELECT gap_type, description, status
                FROM care_gaps
                WHERE patient_id = $1
                  AND status = 'open'
                """,
                patient_id,
            )

            sdoh_flags = await conn.fetch(
                """
                SELECT domain, severity
                FROM patient_sdoh_flags
                WHERE patient_id = $1
                """,
                patient_id,
            )

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

            # Recent deliberation — staleness band instead of hard cutoff.
            # Accept anything within recent_hours; classify by tier.
            recent_cutoff = datetime.now(timezone.utc) - timedelta(hours=recent_hours)
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

            delib_outputs: list[dict] = []
            delib_freshness: dict | None = None
            delib_block: dict | None = None
            if delib_row:
                age_h = (
                    datetime.now(timezone.utc)
                    - ensure_aware(delib_row["triggered_at"])
                ).total_seconds() / 3600.0
                delib_freshness = _classify_freshness(age_h, fresh_hours, recent_hours)

                pcp_output_types = (
                    "anticipatory_scenario",
                    "predicted_patient_question",
                    "missing_data_flag",
                    "care_team_nudge",
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
                delib_outputs = [
                    {
                        **dict(o),
                        "output_data": (
                            json.loads(o["output_data"])
                            if isinstance(o["output_data"], str)
                            else o["output_data"]
                        ),
                    }
                    for o in outputs
                ]
                delib_block = {
                    "deliberation_id": str(delib_row["id"]),
                    "triggered_at": delib_row["triggered_at"].isoformat(),
                    "freshness": delib_freshness,
                    "convergence_score": delib_row["convergence_score"],
                    "rounds_completed": delib_row["rounds_completed"],
                    "scenarios": [
                        o for o in delib_outputs
                        if o["output_type"] == "anticipatory_scenario"
                    ],
                    "missing_data_flags": [
                        o for o in delib_outputs
                        if o["output_type"] == "missing_data_flag"
                    ],
                    "care_team_nudges": [
                        o for o in delib_outputs
                        if o["output_type"] == "care_team_nudge"
                    ],
                }

            # Compose patient_questions from deliberation outputs. Previously
            # an empty placeholder — the data was already on the table, the
            # brief just did not read it.
            patient_questions = [
                o["output_data"] for o in delib_outputs
                if o["output_type"] == "predicted_patient_question"
            ]

            # Compose key_flags from the same sources the care team sees.
            key_flag_items: list[str] = []
            if obt and obt["score"] is not None and float(obt["score"]) < 40:
                key_flag_items.append("OBT score critically low (<40)")
            if any(f["severity"] == "high" for f in sdoh_flags):
                key_flag_items.append("High-severity SDoH flag active")
            if crises:
                key_flag_items.append(
                    f"{len(crises)} crisis event(s) in past 6 months"
                )
            # Fold in missing-data flags from a fresh-or-recent deliberation.
            for o in delib_outputs:
                if o["output_type"] == "missing_data_flag":
                    od = o.get("output_data") or {}
                    label = od.get("label") if isinstance(od, dict) else None
                    if label:
                        key_flag_items.append(f"Missing data: {label}")

            # Provenance stamp for deliberation-sourced fields depends on
            # the freshness tier. Direct-table fields are always TOOL.
            delib_tier = (
                delib_freshness["provenance_tag"]
                if delib_freshness else "TOOL"
            )

            brief = {
                "patient": {
                    "name": f"{patient['first_name']} {patient['last_name']}",
                    "birth_date": str(patient["birth_date"]) if patient["birth_date"] else None,
                    "gender": patient["gender"],
                    "_provenance": _provenance_tool("patients"),
                },
                "visit_date": str(target),
                "staleness_band": {
                    "fresh_hours": fresh_hours,
                    "recent_hours": recent_hours,
                    "_provenance": _provenance_tool("system_config"),
                },
                "obt_score": {
                    "value": {
                        "score": float(obt["score"]) if obt else None,
                        "primary_driver": obt["primary_driver"] if obt else None,
                        "trend_direction": obt["trend_direction"] if obt else None,
                        "confidence": float(obt["confidence"]) if obt else None,
                    },
                    "_provenance": _provenance_tool("obt_scores"),
                },
                "interval_changes": {
                    "value": [
                        {
                            "metric": row["metric_type"],
                            "avg": float(row["avg_val"]),
                            "min": float(row["min_val"]),
                            "max": float(row["max_val"]),
                            "readings": row["reading_count"],
                        }
                        for row in vitals_summary
                    ],
                    "_provenance": _provenance_tool("biometric_readings"),
                },
                "active_conditions": {
                    "value": [
                        {"code": r["code"], "display": r["display"]}
                        for r in conditions
                    ],
                    "_provenance": _provenance_tool("patient_conditions"),
                },
                "active_medications": {
                    "value": [
                        {"code": r["code"], "display": r["display"]}
                        for r in medications
                    ],
                    "_provenance": _provenance_tool("patient_medications"),
                },
                "open_care_gaps": {
                    "value": [
                        {"type": r["gap_type"], "description": r["description"]}
                        for r in care_gaps
                    ],
                    "_provenance": _provenance_tool("care_gaps"),
                },
                "sdoh_flags": {
                    "value": [
                        {"domain": r["domain"], "severity": r["severity"]}
                        for r in sdoh_flags
                    ],
                    "_provenance": _provenance_tool("patient_sdoh_flags"),
                },
                "recent_crises": {
                    "value": [
                        {
                            "summary": r["summary"],
                            "date": r["delivered_at"].isoformat() if r["delivered_at"] else None,
                        }
                        for r in crises
                    ],
                    "_provenance": _provenance_tool("agent_interventions"),
                },
                "key_flags": {
                    "value": key_flag_items,
                    "_provenance": _provenance_tool(
                        "composed", tier=delib_tier,
                    ),
                },
                "patient_questions": {
                    "value": patient_questions,
                    "_provenance": _provenance_tool(
                        "deliberation_outputs", tier=delib_tier,
                    ),
                },
                "recent_deliberation": {
                    "value": delib_block,
                    "_provenance": _provenance_tool(
                        "deliberations", tier=delib_tier,
                    ),
                },
            }

            await log_skill_execution(
                conn, "generate_previsit_brief", patient_id, "completed",
                output_data={
                    "visit_date": str(target),
                    "conditions": len(conditions),
                    "medications": len(medications),
                    "care_gaps": len(care_gaps),
                    "fresh_hours": fresh_hours,
                    "recent_hours": recent_hours,
                    "has_deliberation": delib_block is not None,
                    "deliberation_tier": delib_tier if delib_block else None,
                },
                data_source=data_track,
            )

        return json.dumps(brief, default=str)

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
