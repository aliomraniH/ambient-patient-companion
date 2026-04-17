-- Migration 012: Idempotency keys on clinical warehouse tables.
--
-- Problem: the ON CONFLICT DO NOTHING clauses in _write_condition_rows,
-- _write_medication_rows, _write_encounter_rows, and the ingestion path
-- for behavioral_screenings were no-ops because none of these tables
-- had a UNIQUE constraint matching the intended natural key. Re-running
-- the same HealthEx summary would write duplicate rows each time — the
-- duplicate BMI condition on patient ce1600a3 is the fingerprint.
--
-- Fix: add a STORED natural_key column per table and a UNIQUE index on
-- it. The key combines the coded identifier (ICD-10, RxNorm, LOINC,
-- CVX, instrument_key) with the date (onset_date, authored_on,
-- measured_at, administered_at) — a value pairing that must be unique
-- per patient. For rows without a code, fall back to an MD5 of the
-- display + date so two untyped rows with the same text still collapse.
--
-- biometric_readings already has a UNIQUE index on
-- (patient_id, metric_type, measured_at); we keep that.
--
-- Safety: all operations are additive (ADD COLUMN IF NOT EXISTS,
-- CREATE UNIQUE INDEX IF NOT EXISTS). Existing duplicate rows are NOT
-- removed — the DO block at the bottom first collapses them so the
-- UNIQUE index can be created successfully. Runs inside one
-- transaction so a partial failure rolls back.

BEGIN;

-- ============================================================
-- patient_conditions
-- ============================================================

-- Collapse existing duplicates (keep the earliest created_at per key).
-- conditions has no created_at — use id MIN as the tiebreaker.
DELETE FROM patient_conditions pc
USING patient_conditions other
WHERE pc.patient_id = other.patient_id
  AND COALESCE(pc.code, '') = COALESCE(other.code, '')
  AND COALESCE(pc.display, '') = COALESCE(other.display, '')
  AND COALESCE(pc.onset_date::text, '') = COALESCE(other.onset_date::text, '')
  AND pc.id > other.id;

ALTER TABLE patient_conditions
    ADD COLUMN IF NOT EXISTS natural_key TEXT
    GENERATED ALWAYS AS (
        patient_id::text || ':cond:' ||
        COALESCE(NULLIF(code, ''), 'HASH:' || md5(COALESCE(display, '')))
        || ':' || COALESCE(onset_date::text, 'null')
    ) STORED;

CREATE UNIQUE INDEX IF NOT EXISTS idx_patient_conditions_natural
    ON patient_conditions (natural_key);


-- ============================================================
-- patient_medications
-- ============================================================

DELETE FROM patient_medications pm
USING patient_medications other
WHERE pm.patient_id = other.patient_id
  AND COALESCE(pm.code, '') = COALESCE(other.code, '')
  AND COALESCE(pm.display, '') = COALESCE(other.display, '')
  AND COALESCE(pm.authored_on::text, '') = COALESCE(other.authored_on::text, '')
  AND pm.id > other.id;

ALTER TABLE patient_medications
    ADD COLUMN IF NOT EXISTS natural_key TEXT
    GENERATED ALWAYS AS (
        patient_id::text || ':med:' ||
        COALESCE(NULLIF(code, ''), 'HASH:' || md5(COALESCE(display, '')))
        || ':' || COALESCE(authored_on::text, 'null')
    ) STORED;

CREATE UNIQUE INDEX IF NOT EXISTS idx_patient_medications_natural
    ON patient_medications (natural_key);


-- ============================================================
-- clinical_events
-- ============================================================

DELETE FROM clinical_events ce
USING clinical_events other
WHERE ce.patient_id = other.patient_id
  AND COALESCE(ce.event_type, '') = COALESCE(other.event_type, '')
  AND COALESCE(ce.event_date::text, '') = COALESCE(other.event_date::text, '')
  AND COALESCE(ce.description, '') = COALESCE(other.description, '')
  AND ce.id > other.id;

ALTER TABLE clinical_events
    ADD COLUMN IF NOT EXISTS natural_key TEXT
    GENERATED ALWAYS AS (
        patient_id::text || ':enc:' ||
        COALESCE(NULLIF(event_type, ''), 'NOTYPE')
        || ':' || COALESCE(event_date::text, 'null')
        || ':' || md5(COALESCE(description, ''))
    ) STORED;

CREATE UNIQUE INDEX IF NOT EXISTS idx_clinical_events_natural
    ON clinical_events (natural_key);


-- ============================================================
-- behavioral_screenings
-- ============================================================

DELETE FROM behavioral_screenings bs
USING behavioral_screenings other
WHERE bs.patient_id = other.patient_id
  AND bs.instrument_key = other.instrument_key
  AND bs.administered_at = other.administered_at
  AND bs.id > other.id;

ALTER TABLE behavioral_screenings
    ADD COLUMN IF NOT EXISTS natural_key TEXT
    GENERATED ALWAYS AS (
        patient_id::text || ':screen:' || instrument_key
        || ':' || administered_at::text
    ) STORED;

CREATE UNIQUE INDEX IF NOT EXISTS idx_behavioral_screenings_natural
    ON behavioral_screenings (natural_key);


-- biometric_readings already has idx_biometric_readings_unique on
-- (patient_id, metric_type, measured_at) — no change required.

COMMIT;
