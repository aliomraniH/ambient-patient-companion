"""Vital-sign time-series generators.

Produces realistic, correlated readings for BP, glucose, HRV, SpO2, steps,
and weight.  All values are clamped to the physiological ranges defined in
CLAUDE.md.  No flat/constant output — every generator uses numpy for
realistic variance.
"""

from __future__ import annotations

import hashlib
from datetime import date, datetime, timedelta
from typing import Optional

import numpy as np


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _patient_seed(patient_id: str, base_seed: int = 42) -> int:
    """Deterministic seed derived from patient UUID."""
    h = hashlib.sha256(f"{patient_id}:{base_seed}".encode()).hexdigest()
    return int(h[:8], 16)


def _clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


def _is_eom(d: date) -> bool:
    """Return True for days 25-31 (end-of-month stress window)."""
    return d.day >= 25


def _hour_offset_systolic(hour: int) -> float:
    """Morning surge +8, evening dip -5."""
    if 6 <= hour <= 10:
        return 8.0
    elif 18 <= hour <= 22:
        return -5.0
    return 0.0


# ---------------------------------------------------------------------------
# Blood pressure
# ---------------------------------------------------------------------------

def generate_bp_readings(
    patient_id: str,
    start_date: date,
    end_date: date,
    seed: int = 42,
    readings_per_day: int = 3,
) -> list[dict]:
    """Generate systolic/diastolic BP readings.

    - Systolic baseline ~141, stddev >= 8
    - Morning +8, evening -5
    - EOM (days 25-31): +11 avg
    - Diastolic correlated with systolic (r > 0.7), pulse pressure 20-80 mmHg
    """
    rng = np.random.default_rng(_patient_seed(patient_id, seed))
    baseline_sys = 130 + rng.uniform(5, 20)  # ~141 center
    results: list[dict] = []

    current = start_date
    while current <= end_date:
        eom_boost = 11.0 if _is_eom(current) else 0.0
        for _ in range(readings_per_day):
            hour = int(rng.choice([7, 8, 9, 12, 14, 18, 19, 20, 21]))
            tod_offset = _hour_offset_systolic(hour)

            systolic = baseline_sys + eom_boost + tod_offset + rng.normal(0, 9)
            systolic = round(_clamp(systolic, 90, 180))

            # Diastolic: correlated (pulse pressure 20-80)
            pulse_pressure = _clamp(rng.normal(50, 10), 20, 80)
            diastolic = round(_clamp(systolic - pulse_pressure, 55, 115))

            measured_at = datetime(
                current.year, current.month, current.day, hour,
                int(rng.integers(0, 60)),
            )

            results.append({
                "patient_id": patient_id,
                "metric_type": "bp_systolic",
                "value": float(systolic),
                "unit": "mmHg",
                "measured_at": measured_at,
            })
            results.append({
                "patient_id": patient_id,
                "metric_type": "bp_diastolic",
                "value": float(diastolic),
                "unit": "mmHg",
                "measured_at": measured_at,
            })
        current += timedelta(days=1)

    return results


# ---------------------------------------------------------------------------
# Glucose
# ---------------------------------------------------------------------------

def generate_glucose_readings(
    patient_id: str,
    start_date: date,
    end_date: date,
    seed: int = 42,
    stress_days: Optional[set[date]] = None,
) -> list[dict]:
    """Generate fasting and postprandial glucose readings.

    - Fasting 70-300, baseline ~130
    - EOM spike +25 avg vs mid-month
    - Stress days (score >= 8): +20 avg
    - Postprandial = fasting + 30-80
    """
    rng = np.random.default_rng(_patient_seed(patient_id, seed))
    baseline_fasting = 110 + rng.uniform(10, 30)
    results: list[dict] = []
    if stress_days is None:
        stress_days = set()

    current = start_date
    while current <= end_date:
        eom_boost = 25.0 if _is_eom(current) else 0.0
        stress_boost = 20.0 if current in stress_days else 0.0

        fasting = baseline_fasting + eom_boost + stress_boost + rng.normal(0, 12)
        fasting = round(_clamp(fasting, 70, 300))

        pp_add = rng.uniform(30, 80)
        postprandial = round(_clamp(fasting + pp_add, 70, 380))

        fasting_time = datetime(
            current.year, current.month, current.day,
            int(rng.choice([6, 7, 8])), int(rng.integers(0, 60)),
        )
        pp_time = datetime(
            current.year, current.month, current.day,
            int(rng.choice([12, 13, 18, 19])), int(rng.integers(0, 60)),
        )

        results.append({
            "patient_id": patient_id,
            "metric_type": "glucose_fasting",
            "value": float(fasting),
            "unit": "mg/dL",
            "measured_at": fasting_time,
        })
        results.append({
            "patient_id": patient_id,
            "metric_type": "glucose_postprandial",
            "value": float(postprandial),
            "unit": "mg/dL",
            "measured_at": pp_time,
        })
        current += timedelta(days=1)

    return results


# ---------------------------------------------------------------------------
# HRV (rmssd)
# ---------------------------------------------------------------------------

def generate_hrv_readings(
    patient_id: str,
    start_date: date,
    end_date: date,
    seed: int = 42,
    mood_scores: Optional[dict[date, int]] = None,
) -> list[dict]:
    """Generate HRV (RMSSD) readings.

    - Range 12-100 ms
    - Lower = more stress; inversely correlates with mood
    """
    rng = np.random.default_rng(_patient_seed(patient_id, seed))
    baseline_hrv = rng.uniform(45, 65)
    results: list[dict] = []
    if mood_scores is None:
        mood_scores = {}

    current = start_date
    while current <= end_date:
        mood = mood_scores.get(current, 3)  # default "okay"
        # Higher mood -> higher HRV (less stress)
        mood_factor = (mood - 3) * 5.0  # -10 for bad, +10 for great

        hrv = baseline_hrv + mood_factor + rng.normal(0, 8)
        hrv = round(_clamp(hrv, 12, 100), 1)

        measured_at = datetime(
            current.year, current.month, current.day,
            int(rng.choice([7, 8, 22, 23])), int(rng.integers(0, 60)),
        )
        results.append({
            "patient_id": patient_id,
            "metric_type": "hrv_rmssd",
            "value": float(hrv),
            "unit": "ms",
            "measured_at": measured_at,
        })
        current += timedelta(days=1)

    return results


# ---------------------------------------------------------------------------
# SpO2
# ---------------------------------------------------------------------------

def generate_spo2_readings(
    patient_id: str,
    start_date: date,
    end_date: date,
    seed: int = 42,
) -> list[dict]:
    """Generate SpO2 readings. Stable 95-99 baseline, range 88-100."""
    rng = np.random.default_rng(_patient_seed(patient_id, seed))
    results: list[dict] = []

    current = start_date
    while current <= end_date:
        spo2 = rng.normal(97, 1)
        spo2 = round(_clamp(spo2, 88, 100))

        measured_at = datetime(
            current.year, current.month, current.day,
            int(rng.choice([8, 12, 20])), int(rng.integers(0, 60)),
        )
        results.append({
            "patient_id": patient_id,
            "metric_type": "spo2",
            "value": float(spo2),
            "unit": "%",
            "measured_at": measured_at,
        })
        current += timedelta(days=1)

    return results


# ---------------------------------------------------------------------------
# Steps
# ---------------------------------------------------------------------------

def generate_steps_readings(
    patient_id: str,
    start_date: date,
    end_date: date,
    seed: int = 42,
    crisis_months: Optional[set[tuple[int, int]]] = None,
) -> list[dict]:
    """Generate daily step counts.

    - Range 800-14000
    - Weekday vs weekend ±20%
    - Crisis month: -40% from baseline
    """
    rng = np.random.default_rng(_patient_seed(patient_id, seed))
    baseline_steps = rng.uniform(5000, 9000)
    results: list[dict] = []
    if crisis_months is None:
        crisis_months = set()

    current = start_date
    while current <= end_date:
        is_weekend = current.weekday() >= 5
        weekend_factor = 0.8 if is_weekend else 1.2
        crisis_factor = 0.6 if (current.year, current.month) in crisis_months else 1.0

        steps = baseline_steps * weekend_factor * crisis_factor + rng.normal(0, 1200)
        steps = round(_clamp(steps, 800, 14000))

        measured_at = datetime(
            current.year, current.month, current.day, 23, 59,
        )
        results.append({
            "patient_id": patient_id,
            "metric_type": "steps_daily",
            "value": float(steps),
            "unit": "count",
            "measured_at": measured_at,
        })
        current += timedelta(days=1)

    return results


# ---------------------------------------------------------------------------
# Weight
# ---------------------------------------------------------------------------

def generate_weight_readings(
    patient_id: str,
    start_date: date,
    end_date: date,
    seed: int = 42,
) -> list[dict]:
    """Generate weekly weight readings. Drift ±0.3 kg/week, range 45-180 kg."""
    rng = np.random.default_rng(_patient_seed(patient_id, seed))
    weight = rng.uniform(65, 100)
    results: list[dict] = []

    current = start_date
    while current <= end_date:
        # Weekly measurement (every 7 days)
        if (current - start_date).days % 7 == 0:
            weight += rng.uniform(-0.3, 0.3)
            weight = _clamp(weight, 45, 180)

            measured_at = datetime(
                current.year, current.month, current.day,
                int(rng.choice([7, 8])), int(rng.integers(0, 60)),
            )
            results.append({
                "patient_id": patient_id,
                "metric_type": "weight",
                "value": round(weight, 1),
                "unit": "kg",
                "measured_at": measured_at,
            })
        current += timedelta(days=1)

    return results
