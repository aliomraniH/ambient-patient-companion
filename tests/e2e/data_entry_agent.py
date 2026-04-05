"""PatientDataEntryAgent — simulates 6 months of patient data entry.

This agent mimics the data a real patient (Maria Chen) would enter into the
Ambient Patient Companion system over October 2025 – March 2026:
  • Daily vitals uploaded from a wearable device
  • Daily mood / sleep / stress check-ins
  • Medication adherence logging
  • Social Determinants of Health screening responses
  • Source freshness records so ingestion tools have something to check

The agent has three phases:
  normal   — stable but imperfect DM + HTN management
  crisis   — caregiver stress (December 2025): BP spikes, mood crashes, sleep < 5 h
  recovery — gradual improvement (January 2026 onward)

The generated data is deterministic (seeded from patient_id + date ordinal) so
every test run produces identical rows.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import date, datetime, timedelta
from typing import Optional

import asyncpg

MARIA_CHEN_ID = "a1b2c3d4-e5f6-7890-abcd-ef1234567890"

NORMAL_MONTHS = {
    (2025, 10), (2025, 11),
    (2026, 1), (2026, 2), (2026, 3), (2026, 4),
}
CRISIS_MONTH = (2025, 12)

HISTORY_START = date(2025, 10, 1)
HISTORY_END = date(2026, 3, 31)


def _mcp_server_path() -> str:
    here = os.path.dirname(os.path.abspath(__file__))
    root = os.path.dirname(os.path.dirname(here))
    return os.path.join(root, "mcp-server")


def _ensure_path() -> None:
    p = _mcp_server_path()
    if p not in sys.path:
        sys.path.insert(0, p)


_ensure_path()

from generators.vitals_timeseries import (
    generate_bp_readings,
    generate_glucose_readings,
    generate_hrv_readings,
    generate_spo2_readings,
    generate_steps_readings,
    generate_weight_readings,
)
from generators.behavioral_model import generate_checkins, generate_adherence_records
from generators.sdoh_profile import generate_sdoh_flags


class PatientDataEntryAgent:
    """Autonomous agent that seeds the DB with Maria Chen's complete history.

    Usage (inside an async test or fixture):
        agent = PatientDataEntryAgent(pool)
        patient_id = await agent.setup_patient()
        await agent.seed_all(patient_id)
    """

    def __init__(self, pool: asyncpg.Pool) -> None:
        self.pool = pool

    # ------------------------------------------------------------------
    # Patient registration
    # ------------------------------------------------------------------

    async def setup_patient(self) -> str:
        """Upsert Maria Chen into the patients table.

        Returns deterministic UUID for use across all subsequent tool calls.
        """
        pid = MARIA_CHEN_ID

        async with self.pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO patients
                    (id, mrn, first_name, last_name, birth_date, gender,
                     race, ethnicity, city, state, zip_code,
                     insurance_type, is_synthetic, data_source)
                VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14)
                ON CONFLICT (id) DO NOTHING
                """,
                pid,
                "MC-2025-4829",
                "Maria",
                "Chen",
                date(1971, 3, 15),
                "female",
                "asian",
                "non-hispanic",
                "Fresno",
                "CA",
                "93722",
                "medicaid",
                True,
                "synthea",
            )

            await conn.execute(
                """
                INSERT INTO patient_conditions
                    (id, patient_id, code, display, system, onset_date,
                     clinical_status, data_source)
                VALUES
                    (gen_random_uuid(),$1,'E11.9','Type 2 Diabetes Mellitus','ICD-10','2018-06-01','active','synthea'),
                    (gen_random_uuid(),$1,'I10','Essential Hypertension','ICD-10','2019-01-15','active','synthea'),
                    (gen_random_uuid(),$1,'E66.01','Morbid Obesity','ICD-10','2020-03-22','active','synthea')
                ON CONFLICT DO NOTHING
                """,
                pid,
            )

            for code, display in [
                ("372687004", "Metformin 1000mg BID"),
                ("29046", "Lisinopril 10mg QD"),
                ("83367", "Atorvastatin 40mg QD"),
            ]:
                await conn.execute(
                    """
                    INSERT INTO patient_medications
                        (id, patient_id, code, display, system,
                         status, authored_on, data_source)
                    VALUES (gen_random_uuid(),$1,$2,$3,'RxNorm','active','2025-10-01','synthea')
                    ON CONFLICT DO NOTHING
                    """,
                    pid, code, display,
                )

        return pid

    # ------------------------------------------------------------------
    # Historical vitals (batch insert via generator functions directly)
    # ------------------------------------------------------------------

    async def seed_historical_vitals(
        self,
        patient_id: str,
        start: date = HISTORY_START,
        end: date = HISTORY_END,
    ) -> int:
        """Batch-insert 6 months of biometric readings.

        December 2025 is the crisis month — BP values are pushed high
        (155-175 systolic) so the crisis-escalation tool can fire.
        """
        inserted = 0
        current = start

        async with self.pool.acquire() as conn:
            while current <= end:
                seed = int(current.toordinal())
                is_crisis_day = (current.year, current.month) == CRISIS_MONTH

                all_readings: list[dict] = []
                all_readings.extend(generate_bp_readings(patient_id, current, current, seed))
                all_readings.extend(generate_glucose_readings(patient_id, current, current, seed))
                all_readings.extend(generate_hrv_readings(patient_id, current, current, seed))
                all_readings.extend(generate_spo2_readings(patient_id, current, current, seed))
                all_readings.extend(generate_steps_readings(patient_id, current, current, seed))
                all_readings.extend(generate_weight_readings(patient_id, current, current, seed))

                for r in all_readings:
                    value = r["value"]

                    if is_crisis_day and r["metric_type"] == "bp_systolic":
                        value = min(178.0, value + 30.0)
                    elif is_crisis_day and r["metric_type"] == "glucose_fasting":
                        value = min(255.0, value + 45.0)

                    is_abnormal = (
                        (r["metric_type"] == "bp_systolic" and value > 160)
                        or (r["metric_type"] == "glucose_fasting" and value > 250)
                    )

                    result = await conn.execute(
                        """
                        INSERT INTO biometric_readings
                            (id, patient_id, metric_type, value, unit,
                             measured_at, is_abnormal, day_of_month, data_source)
                        VALUES (gen_random_uuid(),$1,$2,$3,$4,$5,$6,$7,$8)
                        ON CONFLICT DO NOTHING
                        """,
                        patient_id,
                        r["metric_type"],
                        value,
                        r["unit"],
                        r["measured_at"],
                        is_abnormal,
                        current.day,
                        "synthea",
                    )
                    if "INSERT" in result:
                        inserted += 1

                current += timedelta(days=1)

        return inserted

    # ------------------------------------------------------------------
    # Historical check-ins
    # ------------------------------------------------------------------

    async def seed_historical_checkins(
        self,
        patient_id: str,
        start: date = HISTORY_START,
        end: date = HISTORY_END,
    ) -> tuple[int, int]:
        """Batch-insert 6 months of daily check-ins and adherence records.

        December 2025 uses caregiver_stress scenario so mood/sleep/stress
        hit the crisis thresholds.

        Returns (checkins_inserted, adherence_inserted).
        """
        ci_total = 0
        ad_total = 0
        current = start

        async with self.pool.acquire() as conn:
            med_rows = await conn.fetch(
                "SELECT id FROM patient_medications WHERE patient_id = $1",
                patient_id,
            )
            med_ids = [str(row["id"]) for row in med_rows]

            while current <= end:
                seed = int(current.toordinal())
                is_crisis_day = (current.year, current.month) == CRISIS_MONTH
                crisis_months: set = set()
                if is_crisis_day:
                    crisis_months = {(current.year, current.month)}

                checkins = generate_checkins(
                    patient_id, current, current, seed, crisis_months=crisis_months
                )

                for ci in checkins:
                    mood = ci["mood"]
                    mood_numeric = ci["mood_numeric"]
                    sleep_h = ci["sleep_hours"]
                    stress = ci["stress_level"]

                    if is_crisis_day:
                        if current.day in range(8, 15):
                            mood = "bad"
                            mood_numeric = 1
                            sleep_h = 4.0
                            stress = 9
                        elif current.day in range(15, 20):
                            mood = "low"
                            mood_numeric = 2
                            sleep_h = 4.5
                            stress = 8

                    result = await conn.execute(
                        """
                        INSERT INTO daily_checkins
                            (id, patient_id, checkin_date, mood, mood_numeric,
                             energy, stress_level, sleep_hours, notes, data_source)
                        VALUES (gen_random_uuid(),$1,$2,$3,$4,$5,$6,$7,$8,$9)
                        ON CONFLICT (patient_id, checkin_date) DO NOTHING
                        """,
                        patient_id,
                        ci["checkin_date"],
                        mood,
                        mood_numeric,
                        ci["energy"],
                        stress,
                        sleep_h,
                        ci.get("notes", ""),
                        "manual",
                    )
                    if "INSERT" in result:
                        ci_total += 1

                if med_ids:
                    adherence_recs = generate_adherence_records(
                        patient_id, med_ids, current, current, seed,
                        crisis_months=crisis_months,
                    )
                    for ar in adherence_recs:
                        taken = ar["taken"]
                        if is_crisis_day:
                            import random
                            rng = random.Random(seed + ord(str(ar["medication_id"])[0]))
                            taken = rng.random() > 0.55

                        result = await conn.execute(
                            """
                            INSERT INTO medication_adherence
                                (id, patient_id, medication_id, adherence_date,
                                 taken, notes, data_source)
                            VALUES (gen_random_uuid(),$1,$2,$3,$4,$5,$6)
                            ON CONFLICT (patient_id, medication_id, adherence_date)
                                DO NOTHING
                            """,
                            patient_id,
                            ar["medication_id"],
                            ar["adherence_date"],
                            taken,
                            ar.get("notes", ""),
                            "synthea",
                        )
                        if "INSERT" in result:
                            ad_total += 1

                current += timedelta(days=1)

        return ci_total, ad_total

    # ------------------------------------------------------------------
    # SDOH flags
    # ------------------------------------------------------------------

    async def seed_sdoh_flags(self, patient_id: str) -> int:
        """Insert SDOH flags including food_access at moderate severity.

        The run_food_access_nudge and run_sdoh_assessment tools both rely
        on patient_sdoh_flags containing a food_access row.
        """
        flags = [
            ("food_access", "moderate", "Reports stretching food budget at end of month; uses food bank occasionally"),
            ("housing_insecurity", "low", "Stable housing but reports worry about rent increases"),
            ("social_isolation", "low", "Limited social support; lives alone since mother's hospitalization"),
        ]

        inserted = 0
        async with self.pool.acquire() as conn:
            for domain, severity, notes in flags:
                result = await conn.execute(
                    """
                    INSERT INTO patient_sdoh_flags
                        (id, patient_id, domain, severity, screening_date,
                         notes, data_source)
                    VALUES (gen_random_uuid(),$1,$2,$3,$4,$5,$6)
                    ON CONFLICT (patient_id, domain) DO UPDATE SET
                        severity = EXCLUDED.severity,
                        screening_date = EXCLUDED.screening_date,
                        notes = EXCLUDED.notes
                    """,
                    patient_id,
                    domain,
                    severity,
                    date(2025, 10, 5),
                    notes,
                    "manual",
                )
                if "INSERT" in result or "UPDATE" in result:
                    inserted += 1

        return inserted

    # ------------------------------------------------------------------
    # Source freshness
    # ------------------------------------------------------------------

    async def seed_source_freshness(self, patient_id: str) -> None:
        """Insert/update source_freshness rows for wearable, ehr, manual."""
        sources = [
            ("wearable", 1080, 24),
            ("ehr", 45, 72),
            ("manual", 182, 48),
        ]

        async with self.pool.acquire() as conn:
            for source_name, records_count, ttl_hours in sources:
                await conn.execute(
                    """
                    INSERT INTO source_freshness
                        (id, patient_id, source_name, last_ingested_at,
                         records_count, ttl_hours, is_stale)
                    VALUES (gen_random_uuid(),$1,$2,NOW(),$3,$4,false)
                    ON CONFLICT (patient_id, source_name) DO UPDATE SET
                        last_ingested_at = NOW(),
                        records_count = EXCLUDED.records_count,
                        is_stale = false
                    """,
                    patient_id,
                    source_name,
                    records_count,
                    ttl_hours,
                )

    # ------------------------------------------------------------------
    # Full seed (convenience method)
    # ------------------------------------------------------------------

    async def seed_all(self, patient_id: str) -> dict:
        """Run all seeding steps and return a summary."""
        vitals = await self.seed_historical_vitals(patient_id)
        checkins, adherence = await self.seed_historical_checkins(patient_id)
        sdoh = await self.seed_sdoh_flags(patient_id)
        await self.seed_source_freshness(patient_id)

        return {
            "patient_id": patient_id,
            "vitals_inserted": vitals,
            "checkins_inserted": checkins,
            "adherence_inserted": adherence,
            "sdoh_flags_inserted": sdoh,
        }
