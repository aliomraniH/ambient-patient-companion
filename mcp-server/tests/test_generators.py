"""V1-V14: Generator physiological range and correctness tests."""

from __future__ import annotations

import os
import sys
from datetime import date, timedelta

import numpy as np
import pytest

# Allow importing from mcp-server/
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from generators.vitals_timeseries import (
    generate_bp_readings,
    generate_glucose_readings,
    generate_hrv_readings,
    generate_steps_readings,
)
from generators.behavioral_model import (
    generate_checkins,
    generate_adherence_records,
    MOOD_LABELS,
    MOOD_NUMERIC,
)

PID = "test-patient-00000001"
START = date(2024, 1, 1)
END = date(2024, 6, 28)  # 180 days


# ── V1: bp_systolic always in 90-180 ──
def test_bp_systolic_range():
    readings = generate_bp_readings(PID, START, END)
    systolic = [r["value"] for r in readings if r["metric_type"] == "bp_systolic"]
    assert len(systolic) > 0
    assert all(90 <= v <= 180 for v in systolic), f"Out of range: {min(systolic)}-{max(systolic)}"


# ── V2: bp_diastolic always in 55-115 ──
def test_bp_diastolic_range():
    readings = generate_bp_readings(PID, START, END)
    diastolic = [r["value"] for r in readings if r["metric_type"] == "bp_diastolic"]
    assert len(diastolic) > 0
    assert all(55 <= v <= 115 for v in diastolic), f"Out of range: {min(diastolic)}-{max(diastolic)}"


# ── V3: pulse pressure always 20-80 ──
def test_pulse_pressure_range():
    readings = generate_bp_readings(PID, START, END)
    # Group by measured_at to pair systolic/diastolic
    by_time: dict = {}
    for r in readings:
        key = r["measured_at"]
        by_time.setdefault(key, {})[r["metric_type"]] = r["value"]

    for ts, vals in by_time.items():
        if "bp_systolic" in vals and "bp_diastolic" in vals:
            pp = vals["bp_systolic"] - vals["bp_diastolic"]
            assert 20 <= pp <= 80, f"Pulse pressure {pp} at {ts}"


# ── V4: bp_systolic StdDev >= 8 ──
def test_bp_stddev():
    readings = generate_bp_readings(PID, START, END)
    systolic = [r["value"] for r in readings if r["metric_type"] == "bp_systolic"]
    std = np.std(systolic)
    assert std >= 8.0, f"StdDev too low: {std:.2f}"


# ── V5: EOM days 25-31 systolic avg >= mid-month avg + 8 ──
def test_eom_bp_elevation():
    readings = generate_bp_readings(PID, START, END)
    eom = [r["value"] for r in readings
           if r["metric_type"] == "bp_systolic" and r["measured_at"].day >= 25]
    mid = [r["value"] for r in readings
           if r["metric_type"] == "bp_systolic" and 10 <= r["measured_at"].day <= 20]
    assert len(eom) > 0 and len(mid) > 0
    assert np.mean(eom) >= np.mean(mid) + 8, (
        f"EOM avg {np.mean(eom):.1f} not >= mid avg {np.mean(mid):.1f} + 8"
    )


# ── V6: glucose_fasting always 70-300 ──
def test_glucose_fasting_range():
    readings = generate_glucose_readings(PID, START, END)
    fasting = [r["value"] for r in readings if r["metric_type"] == "glucose_fasting"]
    assert len(fasting) > 0
    assert all(70 <= v <= 300 for v in fasting), f"Out of range: {min(fasting)}-{max(fasting)}"


# ── V7: EOM glucose avg >= mid-month avg + 15 ──
def test_eom_glucose_elevation():
    readings = generate_glucose_readings(PID, START, END)
    eom = [r["value"] for r in readings
           if r["metric_type"] == "glucose_fasting" and r["measured_at"].day >= 25]
    mid = [r["value"] for r in readings
           if r["metric_type"] == "glucose_fasting" and 10 <= r["measured_at"].day <= 20]
    assert len(eom) > 0 and len(mid) > 0
    assert np.mean(eom) >= np.mean(mid) + 15, (
        f"EOM avg {np.mean(eom):.1f} not >= mid avg {np.mean(mid):.1f} + 15"
    )


# ── V8: postprandial >= fasting for same date ──
def test_postprandial_gte_fasting():
    readings = generate_glucose_readings(PID, START, END)
    fasting_by_date: dict = {}
    pp_by_date: dict = {}
    for r in readings:
        d = r["measured_at"].date()
        if r["metric_type"] == "glucose_fasting":
            fasting_by_date[d] = r["value"]
        elif r["metric_type"] == "glucose_postprandial":
            pp_by_date[d] = r["value"]

    for d in fasting_by_date:
        if d in pp_by_date:
            assert pp_by_date[d] >= fasting_by_date[d], (
                f"PP {pp_by_date[d]} < fasting {fasting_by_date[d]} on {d}"
            )


# ── V9: hrv_rmssd always 12-100 ──
def test_hrv_range():
    readings = generate_hrv_readings(PID, START, END)
    vals = [r["value"] for r in readings]
    assert len(vals) > 0
    assert all(12 <= v <= 100 for v in vals), f"Out of range: {min(vals)}-{max(vals)}"


# ── V10: steps_daily always 800-14000 ──
def test_steps_range():
    readings = generate_steps_readings(PID, START, END)
    vals = [r["value"] for r in readings]
    assert len(vals) > 0
    assert all(800 <= v <= 14000 for v in vals), f"Out of range: {min(vals)}-{max(vals)}"


# ── V11: sleep_hours always 4.0-9.5 ──
def test_sleep_range():
    checkins = generate_checkins(PID, START, END)
    sleep = [c["sleep_hours"] for c in checkins]
    assert len(sleep) > 0
    assert all(4.0 <= v <= 9.5 for v in sleep), f"Out of range: {min(sleep)}-{max(sleep)}"


# ── V12: mood values from valid set ──
def test_mood_valid_values():
    checkins = generate_checkins(PID, START, END)
    valid_moods = set(MOOD_LABELS)
    for c in checkins:
        assert c["mood"] in valid_moods, f"Invalid mood: {c['mood']}"


# ── V13: normal scenario adherence rate 65-90% ──
def test_normal_adherence_rate():
    records = generate_adherence_records(
        PID, ["med-001", "med-002"], START, END
    )
    assert len(records) > 0
    taken = sum(1 for r in records if r["taken"])
    rate = taken / len(records) * 100
    assert 65 <= rate <= 90, f"Adherence rate {rate:.1f}% not in 65-90%"


# ── V14: caregiver_stress scenario avg mood score < 3.0 ──
def test_caregiver_stress_mood():
    crisis = {(START.year, m) for m in range(START.month, START.month + 6)}
    checkins = generate_checkins(PID, START, END, crisis_months=crisis)
    mood_scores = [MOOD_NUMERIC[c["mood"]] for c in checkins]
    avg = np.mean(mood_scores)
    assert avg < 3.0, f"Crisis avg mood {avg:.2f} not < 3.0"
