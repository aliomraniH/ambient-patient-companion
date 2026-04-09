#!/usr/bin/env python3
"""
Migration 005 Edge-Case Test Data Creator

Creates a stress-test patient with 10 years of clinical history across all 5
HealthEx data formats, exercising every edge case that migration 005 fixes:

  1. Long UCUM unit codes (>20 chars, previously truncated by VARCHAR(20))
  2. Non-numeric / qualitative lab results (previously crammed into unit field)
  3. Long reference ranges (>20 chars)
  4. Clinical threshold precision (NUMERIC vs FLOAT for 126 mg/dL boundary)
  5. Multi-format history over years (same patient, Formats A through E)
  6. Missing fields across formats (no unit, no ref range, no LOINC, etc.)
  7. Format mismatches for the same test over time (HbA1c in 5 different formats)

Usage:
    python attached_assets/replit-agent-migration-005-test-data.py

Requires: DATABASE_URL environment variable set (Replit PostgreSQL).
"""

import asyncio
import json
import os
import sys
import uuid

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

STRESS_TEST_PATIENT_ID = "b2c3d4e5-f6a7-8901-bcde-f12345678901"
STRESS_TEST_MRN = "STRESS-TEST-005"


async def main():
    try:
        import asyncpg
    except ImportError:
        print("ERROR: asyncpg not installed. Run: pip install asyncpg")
        sys.exit(1)

    db_url = os.environ.get("DATABASE_URL")
    if not db_url:
        print("ERROR: DATABASE_URL not set. Set it in Replit Secrets.")
        sys.exit(1)

    pool = await asyncpg.create_pool(db_url, min_size=1, max_size=3)

    async with pool.acquire() as conn:
        # ── Step 1: Register the stress-test patient ──────────────────────
        print("=== Creating stress-test patient ===")
        await conn.execute(
            """INSERT INTO patients
                   (id, mrn, first_name, last_name, birth_date, gender,
                    city, state, insurance_type, is_synthetic, data_source)
               VALUES ($1, $2, 'Stress', 'TestPatient', '1970-06-15', 'female',
                       'San Francisco', 'CA', 'Commercial', true, 'synthea')
               ON CONFLICT (mrn) DO UPDATE SET
                   first_name = EXCLUDED.first_name,
                   last_name = EXCLUDED.last_name""",
            STRESS_TEST_PATIENT_ID, STRESS_TEST_MRN,
        )
        print(f"  Patient: {STRESS_TEST_MRN} ({STRESS_TEST_PATIENT_ID})")

        # ── Step 2: Edge Case 1 — Long UCUM unit codes ───────────────────
        print("\n=== Edge Case 1: Long UCUM unit codes (>20 chars) ===")
        long_unit_labs = [
            ("spo2", 98.0, "%{HemoglobinSaturation}", "2024-06-15", "4548-4", 95.0, 100.0),
            ("bone_collagen", 3.2, "{BoneCollagen}eq/mmol{Cre}", "2024-03-10", "1234-5", None, None),
            ("egfr", 72.0, "mL/min/{1.73_m2}", "2024-09-01", "33914-3", 60.0, None),
            ("creatinine_clearance", 95.0, "mL/min/1.73 m2", "2023-11-20", "2164-2", 80.0, 120.0),
            ("urine_protein", 15.0, "mg/{total_volume}", "2024-01-05", "2888-6", 0.0, 150.0),
        ]
        for metric, val, unit, date, loinc, ref_low, ref_high in long_unit_labs:
            ref_text = None
            if ref_low is not None and ref_high is not None:
                ref_text = f"{ref_low} - {ref_high} {unit}"
            elif ref_low is not None:
                ref_text = f">{ref_low} {unit}"
            await conn.execute(
                """INSERT INTO biometric_readings
                       (id, patient_id, metric_type, value, unit,
                        measured_at, result_numeric, result_unit,
                        reference_text, reference_low, reference_high,
                        loinc_code, data_source)
                   VALUES (gen_random_uuid(), $1, $2, $3, $4,
                           $5::timestamptz, $3::numeric, $4,
                           $6, $7, $8, $9, 'synthea')
                   ON CONFLICT DO NOTHING""",
                STRESS_TEST_PATIENT_ID, metric, val, unit,
                f"{date}T10:00:00Z", ref_text, ref_low, ref_high, loinc,
            )
            print(f"  {metric}: unit='{unit}' ({len(unit)} chars) loinc={loinc}")

        # ── Step 3: Edge Case 2 — Non-numeric / qualitative results ──────
        print("\n=== Edge Case 2: Qualitative lab results ===")
        qualitative_labs = [
            ("hiv_antibody", "Reactive (Confirmed)", "", "2023-05-12", "68961-2"),
            ("urine_culture", "Moderate growth of Staphylococcus aureus", "", "2024-02-18", "630-4"),
            ("urinalysis_wbc", "Too numerous to count", "/hpf", "2024-02-18", "5821-4"),
            ("hepatitis_b_surface", "Non Reactive", "", "2023-05-12", "5195-3"),
            ("pregnancy_test", "Positive", "", "2022-08-01", "2106-3"),
            ("strep_rapid", "Negative", "", "2024-11-15", "6558-4"),
            ("troponin_i", ">1000", "ng/L", "2024-07-22", "49563-0"),
            ("vitamin_d", "<30", "ng/mL", "2024-01-10", "1989-3"),
        ]
        for metric, result_val, unit, date, loinc in qualitative_labs:
            # Try numeric parse (like the pipeline does)
            numeric_val = 0.0
            result_text = None
            try:
                numeric_val = float(str(result_val).split()[0].lstrip("<>"))
            except (ValueError, TypeError, IndexError):
                numeric_val = 0.0
                result_text = result_val

            # For comparison-prefixed values like ">1000", keep numeric but also text
            if result_val.startswith(">") or result_val.startswith("<"):
                result_text = result_val

            await conn.execute(
                """INSERT INTO biometric_readings
                       (id, patient_id, metric_type, value, unit,
                        measured_at, result_text, result_numeric, result_unit,
                        loinc_code, data_source)
                   VALUES (gen_random_uuid(), $1, $2, $3, $4,
                           $5::timestamptz, $6, $7, $4, $8, 'synthea')
                   ON CONFLICT DO NOTHING""",
                STRESS_TEST_PATIENT_ID, metric, numeric_val, unit,
                f"{date}T10:00:00Z", result_text,
                numeric_val if result_text is None else None,
                loinc,
            )
            print(f"  {metric}: result_text='{result_text or 'NULL'}' value={numeric_val}")

        # ── Step 4: Edge Case 3 — Long reference ranges ──────────────────
        print("\n=== Edge Case 3: Long reference ranges (>20 chars) ===")
        long_ref_labs = [
            ("hemoglobin", 14.2, "g/dL", "2024-06-15", "718-7",
             "Male: 13.5-17.5 g/dL; Female: 12.0-16.0 g/dL", 12.0, 16.0),
            ("wbc", 6.8, "x10^3/uL", "2024-06-15", "6690-2",
             "4.5 - 11.0 x10^3/uL", 4.5, 11.0),
            ("hepatitis_c_ab", 0.0, "", "2023-05-12", "16128-1",
             "Negative (non-reactive expected)", None, None),
            ("testosterone", 450.0, "ng/dL", "2024-03-20", "2986-8",
             "Adult male: 270-1070 ng/dL; Adult female: 15-70 ng/dL", 270.0, 1070.0),
        ]
        for metric, val, unit, date, loinc, ref_text, ref_low, ref_high in long_ref_labs:
            await conn.execute(
                """INSERT INTO biometric_readings
                       (id, patient_id, metric_type, value, unit,
                        measured_at, result_numeric, result_unit,
                        reference_text, reference_low, reference_high,
                        loinc_code, data_source)
                   VALUES (gen_random_uuid(), $1, $2, $3, $4,
                           $5::timestamptz, $3::numeric, $4,
                           $6, $7, $8, $9, 'synthea')
                   ON CONFLICT DO NOTHING""",
                STRESS_TEST_PATIENT_ID, metric, val, unit,
                f"{date}T10:00:00Z", ref_text, ref_low, ref_high, loinc,
            )
            print(f"  {metric}: ref='{ref_text}' ({len(ref_text)} chars)")

        # ── Step 5: Edge Case 4 — Clinical threshold precision ────────────
        print("\n=== Edge Case 4: Diabetes threshold precision ===")
        threshold_labs = [
            # Glucose near 126 mg/dL boundary
            ("glucose_fasting", 125.9, "mg/dL", "2024-01-15", "1558-6", 70.0, 126.0),
            ("glucose_fasting", 126.0, "mg/dL", "2024-04-15", "1558-6", 70.0, 126.0),
            ("glucose_fasting", 126.1, "mg/dL", "2024-07-15", "1558-6", 70.0, 126.0),
            # HbA1c near 6.5% boundary
            ("hemoglobin_a1c", 6.49, "%", "2024-01-15", "4548-4", None, 6.5),
            ("hemoglobin_a1c", 6.50, "%", "2024-04-15", "4548-4", None, 6.5),
            ("hemoglobin_a1c", 6.51, "%", "2024-07-15", "4548-4", None, 6.5),
            # eGFR near 60 mL/min boundary
            ("egfr", 59.9, "mL/min/1.73m2", "2024-01-15", "33914-3", 60.0, None),
            ("egfr", 60.0, "mL/min/1.73m2", "2024-04-15", "33914-3", 60.0, None),
            ("egfr", 60.1, "mL/min/1.73m2", "2024-07-15", "33914-3", 60.0, None),
        ]
        for metric, val, unit, date, loinc, ref_low, ref_high in threshold_labs:
            ref_text = ""
            if ref_low is not None and ref_high is not None:
                ref_text = f"{ref_low}-{ref_high} {unit}"
            elif ref_high is not None:
                ref_text = f"<{ref_high} {unit}"
            elif ref_low is not None:
                ref_text = f">{ref_low} {unit}"

            await conn.execute(
                """INSERT INTO biometric_readings
                       (id, patient_id, metric_type, value, unit,
                        measured_at, result_numeric, result_unit,
                        reference_text, reference_low, reference_high,
                        loinc_code, data_source)
                   VALUES (gen_random_uuid(), $1, $2, $3, $4,
                           $5::timestamptz, $3::numeric, $4,
                           $6, $7, $8, $9, 'synthea')
                   ON CONFLICT DO NOTHING""",
                STRESS_TEST_PATIENT_ID, metric, val, unit,
                f"{date}T10:00:00Z", ref_text or None, ref_low, ref_high, loinc,
            )
            out_of_range = ""
            if ref_low is not None and val < ref_low:
                out_of_range = " [OUT OF RANGE]"
            elif ref_high is not None and val > ref_high:
                out_of_range = " [OUT OF RANGE]"
            print(f"  {metric}: {val} {unit} (ref: {ref_text}){out_of_range}")

        # ── Step 6: Edge Case 5 — Multi-format ingest simulation ─────────
        # These use ingest_from_healthex to test real pipeline paths
        print("\n=== Edge Case 5: Multi-format history (via ingest_from_healthex) ===")
        print("  Note: These payloads test the adaptive parser pipeline.")
        print("  Use ingest_from_healthex MCP tool with these payloads:\n")

        # Format A — Plain text summary (2018 era)
        format_a_payload = (
            "PATIENT: Stress TestPatient, DOB 1970-06-15\n"
            "LABS(6): Hemoglobin A1c:4.8 %(ref:<5.7) 2018-03-15@Legacy Hospital[totalrecords:3] "
            "| Glucose, Ser/Plas:98 mg/dL(ref:70-100 mg/dL) 2018-03-15@Legacy Hospital[totalrecords:2] "
            "| LDL Cholesterol:112 mg/dL(ref:<100 mg/dL) 2018-03-15@Legacy Hospital[OutOfRange:1] "
            "| HIV 1+2 Ab:Non Reactive 2018-03-15@Legacy Hospital[totalrecords:1] "
            "| eGFR:68 mL/min/1.73 m2(ref:>60) 2018-03-15@Legacy Hospital[totalrecords:2] "
            "| Urine Culture:Moderate growth of E. coli 2018-03-15@Legacy Hospital\n"
            "ALLERGIES(1): No Known Allergies 2015-07-21@Legacy Hospital"
        )
        print(f"  Format A (2018): {len(format_a_payload)} chars")
        print(f'    ingest_from_healthex(patient_id="{STRESS_TEST_PATIENT_ID}", '
              f'resource_type="labs", fhir_json=<Format A text above>)')

        # Format B — Compressed dictionary table (2020 era)
        format_b_payload = (
            "#Lab Report 2y|Total:4\n"
            "D:1=2020-06-20|2=2020-01-10|\n"
            "C:1=Hemoglobin A1c|2=Glucose Fasting|3=Creatinine|4=TSH|\n"
            "Date|TestName|Value|Unit|ReferenceRange|Status|LOINC|EffectiveDate\n"
            "@1|@1|5.4|%|<5.7|final|4548-4|2020-06-20\n"
            "@1|@2|110|mg/dL|70-100 mg/dL|final|1558-6|2020-06-20\n"
            "@2|@3|1.1|mg/dL|0.6-1.2 mg/dL|final|2160-0|2020-01-10\n"
            "@2|@4|2.5|mIU/L|0.4-4.0 mIU/L|final|3016-3|2020-01-10\n"
        )
        print(f"  Format B (2020): {len(format_b_payload)} chars")

        # Format D — FHIR R4 Bundle JSON (2023 era)
        format_d_payload = json.dumps({
            "resourceType": "Bundle",
            "type": "searchset",
            "entry": [
                {
                    "resource": {
                        "resourceType": "Observation",
                        "code": {
                            "coding": [{"system": "http://loinc.org", "code": "4548-4",
                                        "display": "Hemoglobin A1c"}],
                            "text": "Hemoglobin A1c"
                        },
                        "valueQuantity": {"value": 5.9, "unit": "%",
                                          "system": "http://unitsofmeasure.org"},
                        "effectiveDateTime": "2023-04-20T08:30:00Z",
                        "referenceRange": [{"high": {"value": 5.7, "unit": "%"},
                                            "text": "<5.7 %"}],
                        "status": "final"
                    }
                },
                {
                    "resource": {
                        "resourceType": "Observation",
                        "code": {
                            "coding": [{"system": "http://loinc.org", "code": "2093-3",
                                        "display": "Cholesterol Total"}]
                        },
                        "valueQuantity": {"value": 210, "unit": "mg/dL"},
                        "effectiveDateTime": "2023-04-20T08:30:00Z",
                        "referenceRange": [{"low": {"value": 0}, "high": {"value": 200, "unit": "mg/dL"},
                                            "text": "<200 mg/dL"}],
                        "status": "final"
                    }
                },
                {
                    "resource": {
                        "resourceType": "Observation",
                        "code": {"text": "Blood Pressure"},
                        "component": [
                            {
                                "code": {"coding": [{"code": "8480-6", "display": "Systolic BP"}]},
                                "valueQuantity": {"value": 138, "unit": "mmHg"}
                            },
                            {
                                "code": {"coding": [{"code": "8462-4", "display": "Diastolic BP"}]},
                                "valueQuantity": {"value": 85, "unit": "mmHg"}
                            }
                        ],
                        "effectiveDateTime": "2023-04-20T10:00:00Z",
                        "status": "final"
                    }
                },
                {
                    "resource": {
                        "resourceType": "Observation",
                        "code": {"text": "Urine Drug Screen"},
                        "valueCodeableConcept": {
                            "coding": [{"display": "Negative"}],
                            "text": "Negative"
                        },
                        "effectiveDateTime": "2023-04-20T08:30:00Z",
                        "status": "final"
                    }
                }
            ]
        })
        print(f"  Format D (2023): {len(format_d_payload)} chars, 4 observations")
        print(f"    Includes: component BP, valueCodeableConcept, referenceRange")

        # Format E — JSON dict arrays (2024 era)
        format_e_payload = json.dumps({
            "labs": [
                {"test_name": "Hemoglobin A1c", "value": "6.2", "unit": "%",
                 "date": "2024-09-15", "loinc_code": "4548-4", "ref_range": "<5.7"},
                {"test_name": "LDL Cholesterol", "value": "128", "unit": "mg/dL",
                 "date": "2024-09-15", "ref_range": "<100 mg/dL"},
                {"test_name": "Vitamin D", "value": "<20", "unit": "ng/mL",
                 "date": "2024-09-15", "ref_range": "30-100 ng/mL"},
                {"test_name": "Urine Protein", "value": "Trace", "unit": "",
                 "date": "2024-09-15"},
                {"test_name": "SpO2", "value": "97", "unit": "%{HemoglobinSaturation}",
                 "date": "2024-09-15"},
            ]
        })
        print(f"  Format E (2024): {len(format_e_payload)} chars, 5 labs")
        print(f"    Includes: long UCUM unit, qualitative 'Trace', comparison '<20'")

        # Format C — Flat FHIR text (2025 era)
        format_c_payload = (
            "resourceType is Observation. code.coding[0].system is http://loinc.org. "
            "code.coding[0].code is 4548-4. code.text is Hemoglobin A1c. "
            "valueQuantity.value is 6.8. valueQuantity.unit is %. "
            "effectiveDateTime is 2025-02-10T09:00:00Z. status is final"
        )
        print(f"  Format C (2025): {len(format_c_payload)} chars")

        # ── Step 7: Edge Case 6 — Missing fields ─────────────────────────
        print("\n=== Edge Case 6: Missing fields ===")
        missing_field_labs = [
            # No unit
            ("platelet_count", 250.0, None, "2024-06-15", None, None, None, None),
            # No reference range
            ("sed_rate", 12.0, "mm/hr", "2024-06-15", "4537-7", None, None, None),
            # No LOINC code
            ("custom_biomarker", 0.85, "ratio", "2024-06-15", None, 0.0, 1.0, "0.0 - 1.0"),
            # No date (use current)
            ("bmi", 27.4, "kg/m2", None, "39156-5", 18.5, 25.0, "18.5 - 25.0 kg/m2"),
        ]
        for metric, val, unit, date, loinc, ref_low, ref_high, ref_text in missing_field_labs:
            ts = f"{date}T10:00:00Z" if date else "2024-06-15T10:00:00Z"
            await conn.execute(
                """INSERT INTO biometric_readings
                       (id, patient_id, metric_type, value, unit,
                        measured_at, result_numeric, result_unit,
                        reference_text, reference_low, reference_high,
                        loinc_code, data_source)
                   VALUES (gen_random_uuid(), $1, $2, $3, $4,
                           $5::timestamptz, $3::numeric, $4,
                           $6, $7, $8, $9, 'synthea')
                   ON CONFLICT DO NOTHING""",
                STRESS_TEST_PATIENT_ID, metric, val, unit or "",
                ts, ref_text, ref_low, ref_high, loinc,
            )
            missing = []
            if not unit:
                missing.append("unit")
            if ref_text is None:
                missing.append("ref_range")
            if not loinc:
                missing.append("loinc")
            if not date:
                missing.append("date")
            print(f"  {metric}: missing [{', '.join(missing)}]")

        # ── Step 8: Edge Case 7 — Conditions with long display names ─────
        print("\n=== Edge Case 7: Long condition/medication display names ===")
        long_conditions = [
            ("E11.65", "Type 2 diabetes mellitus with hyperglycemia, insulin-requiring, "
                       "with chronic kidney disease stage 3 and diabetic nephropathy"),
            ("I10", "Essential (primary) hypertension with left ventricular hypertrophy "
                    "and chronic diastolic heart failure"),
        ]
        for code, display in long_conditions:
            await conn.execute(
                """INSERT INTO patient_conditions
                       (id, patient_id, code, display, onset_date,
                        clinical_status, data_source)
                   VALUES (gen_random_uuid(), $1, $2, $3, '2020-01-15',
                           'active', 'synthea')
                   ON CONFLICT DO NOTHING""",
                STRESS_TEST_PATIENT_ID, code, display,
            )
            print(f"  {code}: display='{display[:60]}...' ({len(display)} chars)")

        long_medications = [
            ("Metformin Hydrochloride 500 MG Extended-Release Oral Tablet "
             "[Glucophage XR] for Type 2 Diabetes Mellitus"),
            ("Insulin Glargine 100 UNT/ML Prefilled Pen Injector "
             "[Lantus SoloStar] with Blood Glucose Monitoring Kit"),
        ]
        for display in long_medications:
            await conn.execute(
                """INSERT INTO patient_medications
                       (id, patient_id, code, display, status,
                        authored_on, data_source)
                   VALUES (gen_random_uuid(), $1, '', $2, 'active',
                           '2023-06-01', 'synthea')
                   ON CONFLICT DO NOTHING""",
                STRESS_TEST_PATIENT_ID, display,
            )
            print(f"  med: '{display[:60]}...' ({len(display)} chars)")

    # ── Final: Print ingest payloads for manual MCP testing ───────────
    print("\n" + "=" * 70)
    print("MULTI-FORMAT INGEST PAYLOADS")
    print("=" * 70)
    print("\nUse these with ingest_from_healthex MCP tool:")
    print(f"  patient_id = \"{STRESS_TEST_PATIENT_ID}\"")
    print(f"  resource_type = \"labs\"")
    print("\n--- Format A (2018, plain text) ---")
    print(format_a_payload)
    print("\n--- Format B (2020, compressed table) ---")
    print(format_b_payload)
    print("\n--- Format D (2023, FHIR Bundle JSON) ---")
    print(format_d_payload[:200] + "...")
    print("\n--- Format E (2024, JSON dict array) ---")
    print(format_e_payload[:200] + "...")
    print("\n--- Format C (2025, flat FHIR text) ---")
    print(format_c_payload)

    # ── Verification counts ───────────────────────────────────────────
    print("\n" + "=" * 70)
    print("VERIFICATION COUNTS")
    print("=" * 70)
    async with pool.acquire() as conn:
        total = await conn.fetchval(
            "SELECT COUNT(*) FROM biometric_readings WHERE patient_id = $1",
            STRESS_TEST_PATIENT_ID,
        )
        with_result_text = await conn.fetchval(
            "SELECT COUNT(*) FROM biometric_readings WHERE patient_id = $1 AND result_text IS NOT NULL",
            STRESS_TEST_PATIENT_ID,
        )
        with_loinc = await conn.fetchval(
            "SELECT COUNT(*) FROM biometric_readings WHERE patient_id = $1 AND loinc_code IS NOT NULL",
            STRESS_TEST_PATIENT_ID,
        )
        with_ref = await conn.fetchval(
            "SELECT COUNT(*) FROM biometric_readings WHERE patient_id = $1 AND reference_text IS NOT NULL",
            STRESS_TEST_PATIENT_ID,
        )
        out_of_range = await conn.fetchval(
            "SELECT COUNT(*) FROM biometric_readings WHERE patient_id = $1 AND is_out_of_range = true",
            STRESS_TEST_PATIENT_ID,
        )
        long_units = await conn.fetchval(
            "SELECT COUNT(*) FROM biometric_readings WHERE patient_id = $1 AND length(result_unit) > 20",
            STRESS_TEST_PATIENT_ID,
        )
        conditions = await conn.fetchval(
            "SELECT COUNT(*) FROM patient_conditions WHERE patient_id = $1",
            STRESS_TEST_PATIENT_ID,
        )
        medications = await conn.fetchval(
            "SELECT COUNT(*) FROM patient_medications WHERE patient_id = $1",
            STRESS_TEST_PATIENT_ID,
        )
        print(f"  biometric_readings total:      {total}")
        print(f"  with result_text (qualitative): {with_result_text}")
        print(f"  with loinc_code:                {with_loinc}")
        print(f"  with reference_text:            {with_ref}")
        print(f"  is_out_of_range = true:         {out_of_range}")
        print(f"  long units (>20 chars):         {long_units}")
        print(f"  patient_conditions:             {conditions}")
        print(f"  patient_medications:             {medications}")

    await pool.close()
    print("\n=== Done. Stress-test patient created successfully. ===")


if __name__ == "__main__":
    asyncio.run(main())
