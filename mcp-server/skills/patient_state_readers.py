"""Skill: patient_state_readers — P-dimension getters for S=f(R,C,P,T).

Tier 2.a read-only patient-state queries:
  - get_vital_trend(patient_id, metric_type, days)
  - get_sdoh_profile(patient_id)
  - get_medication_adherence_rate(patient_id, days)

All pure DB reads. No LLM calls. Auto-discovered by mcp-server/skills/__init__.py
via the register(mcp) convention.
"""
from __future__ import annotations

import json
import logging
import sys
from datetime import date, datetime, timedelta, timezone

from fastmcp import FastMCP

from db.connection import get_pool

logging.basicConfig(level=logging.INFO, stream=sys.stderr)
logger = logging.getLogger(__name__)


_ALLOWED_METRICS = (
    "systolic_bp", "diastolic_bp", "heart_rate", "weight",
    "glucose_fasting", "hba1c", "spo2", "temperature", "bmi",
)


async def get_vital_trend(patient_id: str, metric_type: str, days: int = 90) -> str:
    """Time-series trend for a single vital/lab metric.

    Reads biometric_readings and returns the ordered series plus a coarse
    trend_direction computed from the slope between the first and last half
    of the window. Callers that need a richer statistical model should pull
    the raw readings and compute locally.

    Args:
        patient_id: Patient UUID or MRN-resolved ID.
        metric_type: One of systolic_bp | diastolic_bp | heart_rate | weight |
                     glucose_fasting | hba1c | spo2 | temperature | bmi.
        days: Lookback window in days (default 90).

    Returns:
        JSON with {metric, unit, readings: [{date, value}], trend_direction,
        trend_magnitude, count}. `trend_direction` is one of
        'improving' | 'stable' | 'worsening' | 'unknown'.
        Note: directionality is metric-naive here — caller interprets whether
        e.g. an increasing HbA1c is "improving" or "worsening".
    """
    if metric_type not in _ALLOWED_METRICS:
        return json.dumps({
            "status": "error",
            "error": f"Unsupported metric_type '{metric_type}'. "
                     f"Must be one of {list(_ALLOWED_METRICS)}.",
        })

    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT value, unit, measured_at
               FROM biometric_readings
               WHERE patient_id = $1
                 AND metric_type = $2
                 AND measured_at >= $3
               ORDER BY measured_at ASC""",
            patient_id, metric_type, cutoff,
        )

    readings = [
        {"date": r["measured_at"].isoformat(), "value": float(r["value"])}
        for r in rows
    ]
    unit = rows[0]["unit"] if rows else None

    # Coarse slope: compare mean of first half vs second half.
    trend_direction = "unknown"
    trend_magnitude = 0.0
    if len(readings) >= 4:
        mid = len(readings) // 2
        first_avg = sum(r["value"] for r in readings[:mid]) / mid
        second_avg = sum(r["value"] for r in readings[mid:]) / (len(readings) - mid)
        delta = second_avg - first_avg
        trend_magnitude = round(delta, 3)
        # Epsilon band = 2% of first_avg (prevents noise from flipping trend).
        eps = abs(first_avg) * 0.02 if first_avg else 0.01
        if delta > eps:
            trend_direction = "increasing"
        elif delta < -eps:
            trend_direction = "decreasing"
        else:
            trend_direction = "stable"

    return json.dumps({
        "patient_id": patient_id,
        "metric": metric_type,
        "unit": unit,
        "count": len(readings),
        "readings": readings,
        "trend_direction": trend_direction,
        "trend_magnitude": trend_magnitude,
        "window_days": days,
    }, default=str)


async def get_sdoh_profile(patient_id: str) -> str:
    """Current SDoH assessment results for a patient.

    Reads patient_sdoh_flags (one row per domain, unique(patient_id, domain)).
    Domains observed in production: food_access, housing, transportation,
    financial_stress, social_isolation, employment.

    Returns:
        JSON with {domains: {<domain>: {severity, flag_code, screening_date,
        notes}}, flag_count, high_severity_count, last_screened}.
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT domain, severity, flag_code, description, screening_date, notes
               FROM patient_sdoh_flags
               WHERE patient_id = $1
               ORDER BY screening_date DESC NULLS LAST""",
            patient_id,
        )

    domains: dict = {}
    last_screened = None
    high_severity_count = 0
    for r in rows:
        domains[r["domain"]] = {
            "severity": r["severity"],
            "flag_code": r["flag_code"],
            "description": r["description"],
            "screening_date": r["screening_date"].isoformat() if r["screening_date"] else None,
            "notes": r["notes"],
        }
        if r["severity"] == "high":
            high_severity_count += 1
        if r["screening_date"] and (last_screened is None or r["screening_date"] > last_screened):
            last_screened = r["screening_date"]

    return json.dumps({
        "patient_id": patient_id,
        "domains": domains,
        "flag_count": len(domains),
        "high_severity_count": high_severity_count,
        "last_screened": last_screened.isoformat() if last_screened else None,
    }, default=str)


async def get_medication_adherence_rate(patient_id: str, days: int = 30) -> str:
    """Medication adherence from medication_adherence + patient_medications.

    Computes the rate as taken_days / total_days_expected per medication and
    in aggregate. A simple 7-day / 30-day comparison feeds the trend signal:
    'improving' | 'stable' | 'declining' | 'unknown'.

    Args:
        patient_id: Patient UUID or MRN-resolved ID.
        days: Lookback window in days (default 30).
    """
    cutoff = date.today() - timedelta(days=days)
    pool = await get_pool()
    async with pool.acquire() as conn:
        med_rows = await conn.fetch(
            """SELECT id, code, display
               FROM patient_medications
               WHERE patient_id = $1
                 AND (status IS NULL OR status = 'active')""",
            patient_id,
        )
        if not med_rows:
            return json.dumps({
                "patient_id": patient_id,
                "window_days": days,
                "overall_rate": None,
                "by_medication": [],
                "trend": "unknown",
                "note": "No active medications on file.",
            })

        med_ids = [m["id"] for m in med_rows]
        adherence_rows = await conn.fetch(
            """SELECT medication_id, adherence_date, taken
               FROM medication_adherence
               WHERE patient_id = $1
                 AND medication_id = ANY($2::uuid[])
                 AND adherence_date >= $3
               ORDER BY adherence_date ASC""",
            patient_id, med_ids, cutoff,
        )

    # Per-medication aggregation.
    by_med: dict = {m["id"]: {
        "medication_id": str(m["id"]),
        "display": m["display"],
        "code": m["code"],
        "taken_days": 0,
        "missed_days": 0,
    } for m in med_rows}
    for r in adherence_rows:
        entry = by_med.get(r["medication_id"])
        if entry is None:
            continue
        if r["taken"]:
            entry["taken_days"] += 1
        else:
            entry["missed_days"] += 1

    for entry in by_med.values():
        total = entry["taken_days"] + entry["missed_days"]
        entry["adherence_rate"] = round(entry["taken_days"] / total, 3) if total else None

    # Aggregate rate across all medications.
    total_taken = sum(e["taken_days"] for e in by_med.values())
    total_counted = sum(e["taken_days"] + e["missed_days"] for e in by_med.values())
    overall_rate = round(total_taken / total_counted, 3) if total_counted else None

    # 7-day recency trend vs 30-day window.
    trend = "unknown"
    if days >= 14 and adherence_rows:
        recent_cutoff = date.today() - timedelta(days=7)
        recent = [r for r in adherence_rows if r["adherence_date"] >= recent_cutoff]
        older = [r for r in adherence_rows if r["adherence_date"] < recent_cutoff]
        if recent and older:
            recent_rate = sum(1 for r in recent if r["taken"]) / len(recent)
            older_rate = sum(1 for r in older if r["taken"]) / len(older)
            delta = recent_rate - older_rate
            if delta > 0.05:
                trend = "improving"
            elif delta < -0.05:
                trend = "declining"
            else:
                trend = "stable"

    return json.dumps({
        "patient_id": patient_id,
        "window_days": days,
        "overall_rate": overall_rate,
        "by_medication": list(by_med.values()),
        "trend": trend,
    }, default=str)


def register(mcp: FastMCP) -> None:
    mcp.tool(get_vital_trend)
    mcp.tool(get_sdoh_profile)
    mcp.tool(get_medication_adherence_rate)
