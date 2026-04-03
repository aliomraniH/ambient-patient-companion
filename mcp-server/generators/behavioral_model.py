"""Behavioral model generators: check-ins and medication adherence."""

from __future__ import annotations

import hashlib
from datetime import date, datetime, timedelta
from typing import Optional

import numpy as np


MOOD_LABELS = ["bad", "low", "okay", "good", "great"]
MOOD_NUMERIC = {"bad": 1, "low": 2, "okay": 3, "good": 4, "great": 5}
ENERGY_LABELS = ["low", "moderate", "high"]


def _patient_seed(patient_id: str, base_seed: int = 42) -> int:
    h = hashlib.sha256(f"{patient_id}:{base_seed}".encode()).hexdigest()
    return int(h[:8], 16)


def _clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


def generate_checkins(
    patient_id: str,
    start_date: date,
    end_date: date,
    seed: int = 42,
    crisis_months: Optional[set[tuple[int, int]]] = None,
) -> list[dict]:
    """Generate daily check-in records.

    Fields: mood, mood_numeric, energy, stress_level (1-10),
    sleep_hours (4.0-9.5), notes, checkin_date.

    Crisis months have worse mood, higher stress, less sleep.
    """
    rng = np.random.default_rng(_patient_seed(patient_id, seed))
    results: list[dict] = []
    if crisis_months is None:
        crisis_months = set()

    current = start_date
    while current <= end_date:
        is_crisis = (current.year, current.month) in crisis_months

        # Mood: crisis skews toward bad/low
        if is_crisis:
            mood_idx = int(_clamp(rng.normal(1.2, 0.8), 0, 4))
        else:
            mood_idx = int(_clamp(rng.normal(2.8, 0.9), 0, 4))
        mood = MOOD_LABELS[mood_idx]

        # Energy
        if is_crisis:
            energy_idx = int(_clamp(rng.normal(0.5, 0.6), 0, 2))
        else:
            energy_idx = int(_clamp(rng.normal(1.3, 0.7), 0, 2))
        energy = ENERGY_LABELS[energy_idx]

        # Stress 1-10
        if is_crisis:
            stress = int(_clamp(rng.normal(7.5, 1.5), 1, 10))
        else:
            stress = int(_clamp(rng.normal(4.5, 1.8), 1, 10))

        # Sleep hours 4.0-9.5
        if is_crisis:
            sleep = round(_clamp(rng.normal(5.8, 0.8), 4.0, 9.5), 1)
        else:
            sleep = round(_clamp(rng.normal(7.2, 0.7), 4.0, 9.5), 1)

        results.append({
            "patient_id": patient_id,
            "checkin_date": current,
            "mood": mood,
            "mood_numeric": MOOD_NUMERIC[mood],
            "energy": energy,
            "stress_level": stress,
            "sleep_hours": sleep,
            "notes": None,
        })
        current += timedelta(days=1)

    return results


def generate_adherence_records(
    patient_id: str,
    medication_ids: list[str],
    start_date: date,
    end_date: date,
    seed: int = 42,
    crisis_months: Optional[set[tuple[int, int]]] = None,
) -> list[dict]:
    """Generate per-medication daily adherence records.

    Adherence rate: normal 65-90%, crisis 55-75%.
    """
    rng = np.random.default_rng(_patient_seed(patient_id, seed))
    results: list[dict] = []
    if crisis_months is None:
        crisis_months = set()

    for med_id in medication_ids:
        current = start_date
        while current <= end_date:
            is_crisis = (current.year, current.month) in crisis_months
            if is_crisis:
                taken = rng.random() < rng.uniform(0.55, 0.75)
            else:
                taken = rng.random() < rng.uniform(0.65, 0.90)

            results.append({
                "patient_id": patient_id,
                "medication_id": med_id,
                "adherence_date": current,
                "taken": taken,
                "notes": None,
            })
            current += timedelta(days=1)

    return results
