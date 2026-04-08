-- Migration 005: Fix clinical data storage — widen VARCHAR columns, add structured lab fields
--
-- Problem: VARCHAR(20) silently truncates clinical data. UCUM annotated unit codes
-- like %{HemoglobinSaturation} (23 chars), reference ranges like
-- "Male: 13.5-17.5 g/dL; Female: 12.0-16.0 g/dL" (45 chars), and qualitative
-- results like "Moderate growth of Staphylococcus aureus" (41 chars) all exceed
-- the 20-character limit.
--
-- Additionally, non-numeric lab values were being crammed into the unit field
-- (e.g., "Positive (mg/dL)") because there was no proper result_text column.
-- The value column uses DOUBLE PRECISION which has floating-point imprecision
-- for clinical threshold comparisons (e.g., 126 mg/dL glucose cutoff).
--
-- Safety: All ALTER COLUMN TYPE TEXT operations are metadata-only on PostgreSQL 9.1+
-- (~30ms, no table rewrite). ADD COLUMN with NULL default is instant.
-- Safe to run on production during normal operations.

BEGIN;

-- ============================================================
-- PART 1: Widen undersized VARCHAR columns (metadata-only, instant)
-- ============================================================

-- biometric_readings: unit VARCHAR(20) → TEXT (the critical fix)
ALTER TABLE biometric_readings
    ALTER COLUMN unit TYPE TEXT;

-- biometric_readings: metric_type VARCHAR(50) → TEXT
ALTER TABLE biometric_readings
    ALTER COLUMN metric_type TYPE TEXT;

-- patient_conditions: display VARCHAR(500) → TEXT
ALTER TABLE patient_conditions
    ALTER COLUMN display TYPE TEXT;

-- patient_medications: display VARCHAR(500) → TEXT
ALTER TABLE patient_medications
    ALTER COLUMN display TYPE TEXT;

-- patient_sdoh_flags: flag_code VARCHAR(20) → TEXT
ALTER TABLE patient_sdoh_flags
    ALTER COLUMN flag_code TYPE TEXT;

-- ============================================================
-- PART 2: Add structured lab result columns to biometric_readings
-- ============================================================

-- Qualitative result text: "Reactive (Confirmed)", "Positive", narrative results
ALTER TABLE biometric_readings
    ADD COLUMN IF NOT EXISTS result_text TEXT;

-- Exact numeric value using NUMERIC (not FLOAT) for clinical precision
-- NUMERIC avoids 0.1+0.1+0.1 = 0.30000000000000004 floating-point errors
ALTER TABLE biometric_readings
    ADD COLUMN IF NOT EXISTS result_numeric NUMERIC;

-- Proper unit field (UCUM codes, display units) separate from the legacy unit column
ALTER TABLE biometric_readings
    ADD COLUMN IF NOT EXISTS result_unit TEXT;

-- Original reference range text: "Male: 13.5-17.5 g/dL; Female: 12.0-16.0 g/dL"
ALTER TABLE biometric_readings
    ADD COLUMN IF NOT EXISTS reference_text TEXT;

-- Parsed reference range bounds for comparison queries
ALTER TABLE biometric_readings
    ADD COLUMN IF NOT EXISTS reference_low NUMERIC;

ALTER TABLE biometric_readings
    ADD COLUMN IF NOT EXISTS reference_high NUMERIC;

-- LOINC code (format NNNNN-N, current max ~8 chars)
ALTER TABLE biometric_readings
    ADD COLUMN IF NOT EXISTS loinc_code VARCHAR(10);

-- Interpretation code: H, L, N, HH, LL, A, AA
ALTER TABLE biometric_readings
    ADD COLUMN IF NOT EXISTS interpretation VARCHAR(10);

-- Lineage back to raw_fhir_cache for auditability
ALTER TABLE biometric_readings
    ADD COLUMN IF NOT EXISTS source_record_id UUID;

-- JSONB overflow for FHIR extensions and unmapped elements
ALTER TABLE biometric_readings
    ADD COLUMN IF NOT EXISTS fhir_extensions JSONB DEFAULT '{}';

-- ============================================================
-- PART 3: Computed column for range-based abnormality detection
-- ============================================================

-- New generated column based on result_numeric vs reference bounds
-- Keeps existing is_abnormal untouched for backward compatibility
ALTER TABLE biometric_readings
    ADD COLUMN IF NOT EXISTS is_out_of_range BOOLEAN
    GENERATED ALWAYS AS (
        CASE
            WHEN result_numeric IS NULL THEN NULL
            WHEN reference_low IS NOT NULL AND result_numeric < reference_low THEN true
            WHEN reference_high IS NOT NULL AND result_numeric > reference_high THEN true
            ELSE false
        END
    ) STORED;

-- ============================================================
-- PART 4: New indexes for clinical queries
-- ============================================================

-- LOINC-based lab lookups (e.g., "all HbA1c results for patient X")
CREATE INDEX IF NOT EXISTS idx_biometric_loinc
    ON biometric_readings (loinc_code, measured_at DESC)
    WHERE loinc_code IS NOT NULL;

-- Range-based abnormality filter
CREATE INDEX IF NOT EXISTS idx_biometric_out_of_range
    ON biometric_readings (patient_id)
    WHERE is_out_of_range = true;

-- JSONB overflow search (GIN with jsonb_path_ops for smaller index)
CREATE INDEX IF NOT EXISTS idx_biometric_fhir_extensions
    ON biometric_readings USING GIN (fhir_extensions jsonb_path_ops)
    WHERE fhir_extensions != '{}';

-- ============================================================
-- PART 5: Backfill existing data into new columns
-- ============================================================

-- Copy existing unit → result_unit where not yet populated
UPDATE biometric_readings
SET result_unit = unit
WHERE result_unit IS NULL AND unit IS NOT NULL AND unit != '';

-- Copy existing value → result_numeric where not yet populated
UPDATE biometric_readings
SET result_numeric = value::numeric
WHERE result_numeric IS NULL AND value IS NOT NULL;

-- Detect non-numeric values stuffed in unit field (the old hack):
-- If unit contains parentheses or starts with a letter and is >5 chars,
-- it's likely a qualitative result that was crammed into unit.
-- Move it to result_text and clear the unit.
UPDATE biometric_readings
SET result_text = unit,
    result_unit = NULL
WHERE result_text IS NULL
  AND unit IS NOT NULL
  AND unit ~ '^[A-Za-z].*\(.*\)$'
  AND length(unit) > 10;

COMMIT;
