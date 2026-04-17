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
-- CREATE UNIQUE INDEX IF NOT EXISTS). Existing duplicate rows ARE
-- removed via ROW_NUMBER() dedup keyed on the SAME fingerprint the
-- natural_key uses, so the UNIQUE index can be created successfully.
-- Runs inside one transaction so a partial failure rolls back.
--
-- IMMUTABILITY NOTE: PostgreSQL STORED generated columns require
-- IMMUTABLE expressions. date::text and timestamptz::text are STABLE
-- (depend on session DateStyle / TimeZone). We use:
--   - (date_col - DATE '1970-01-01')::text       for date columns
--   - extract(epoch from (tstz - TIMESTAMPTZ 'epoch'))::bigint::text  for timestamptz columns
--     (extract(epoch from timestamptz) is STABLE because it desugars to
--      date_part('epoch', timestamptz); subtracting yields an interval,
--      and extract(epoch from interval) IS IMMUTABLE.)
-- both of which are IMMUTABLE.
--
-- DEDUP NOTE: each DELETE uses exactly the same fingerprint the
-- natural_key column generates, so any rows that would collide on the
-- new UNIQUE index are collapsed first (keeping the lowest id).

BEGIN;

-- ============================================================
-- patient_conditions
-- ============================================================

WITH ranked AS (
    SELECT id,
           ROW_NUMBER() OVER (
               PARTITION BY
                   patient_id,
                   COALESCE(NULLIF(code, ''), 'HASH:' || md5(COALESCE(display, ''))),
                   COALESCE((onset_date - DATE '1970-01-01')::text, 'null')
               ORDER BY id
           ) AS rn
    FROM patient_conditions
)
DELETE FROM patient_conditions
 WHERE id IN (SELECT id FROM ranked WHERE rn > 1);

ALTER TABLE patient_conditions
    ADD COLUMN IF NOT EXISTS natural_key TEXT
    GENERATED ALWAYS AS (
        patient_id::text || ':cond:' ||
        COALESCE(NULLIF(code, ''), 'HASH:' || md5(COALESCE(display, '')))
        || ':' || COALESCE((onset_date - DATE '1970-01-01')::text, 'null')
    ) STORED;

CREATE UNIQUE INDEX IF NOT EXISTS idx_patient_conditions_natural
    ON patient_conditions (natural_key);


-- ============================================================
-- patient_medications
-- ============================================================

WITH ranked AS (
    SELECT id,
           ROW_NUMBER() OVER (
               PARTITION BY
                   patient_id,
                   COALESCE(NULLIF(code, ''), 'HASH:' || md5(COALESCE(display, ''))),
                   COALESCE((authored_on - DATE '1970-01-01')::text, 'null')
               ORDER BY id
           ) AS rn
    FROM patient_medications
)
DELETE FROM patient_medications
 WHERE id IN (SELECT id FROM ranked WHERE rn > 1);

ALTER TABLE patient_medications
    ADD COLUMN IF NOT EXISTS natural_key TEXT
    GENERATED ALWAYS AS (
        patient_id::text || ':med:' ||
        COALESCE(NULLIF(code, ''), 'HASH:' || md5(COALESCE(display, '')))
        || ':' || COALESCE((authored_on - DATE '1970-01-01')::text, 'null')
    ) STORED;

CREATE UNIQUE INDEX IF NOT EXISTS idx_patient_medications_natural
    ON patient_medications (natural_key);


-- ============================================================
-- clinical_events
-- ============================================================

WITH ranked AS (
    SELECT id,
           ROW_NUMBER() OVER (
               PARTITION BY
                   patient_id,
                   COALESCE(NULLIF(event_type, ''), 'NOTYPE'),
                   COALESCE(extract(epoch from (event_date - TIMESTAMPTZ 'epoch'))::bigint::text, 'null'),
                   md5(COALESCE(description, ''))
               ORDER BY id
           ) AS rn
    FROM clinical_events
)
DELETE FROM clinical_events
 WHERE id IN (SELECT id FROM ranked WHERE rn > 1);

ALTER TABLE clinical_events
    ADD COLUMN IF NOT EXISTS natural_key TEXT
    GENERATED ALWAYS AS (
        patient_id::text || ':enc:' ||
        COALESCE(NULLIF(event_type, ''), 'NOTYPE')
        || ':' || COALESCE(extract(epoch from (event_date - TIMESTAMPTZ 'epoch'))::bigint::text, 'null')
        || ':' || md5(COALESCE(description, ''))
    ) STORED;

CREATE UNIQUE INDEX IF NOT EXISTS idx_clinical_events_natural
    ON clinical_events (natural_key);


-- ============================================================
-- behavioral_screenings
-- ============================================================

WITH ranked AS (
    SELECT id,
           ROW_NUMBER() OVER (
               PARTITION BY
                   patient_id,
                   instrument_key,
                   extract(epoch from (administered_at - TIMESTAMPTZ 'epoch'))::bigint
               ORDER BY id
           ) AS rn
    FROM behavioral_screenings
)
DELETE FROM behavioral_screenings
 WHERE id IN (SELECT id FROM ranked WHERE rn > 1);

ALTER TABLE behavioral_screenings
    ADD COLUMN IF NOT EXISTS natural_key TEXT
    GENERATED ALWAYS AS (
        patient_id::text || ':screen:' || instrument_key
        || ':' || extract(epoch from (administered_at - TIMESTAMPTZ 'epoch'))::bigint::text
    ) STORED;

CREATE UNIQUE INDEX IF NOT EXISTS idx_behavioral_screenings_natural
    ON behavioral_screenings (natural_key);


-- biometric_readings already has idx_biometric_readings_unique on
-- (patient_id, metric_type, measured_at) — no change required.

COMMIT;
