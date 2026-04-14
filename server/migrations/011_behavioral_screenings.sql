-- Migration 011: Generic behavioral_screenings table.
--
-- Tables:
--   behavioral_screenings   — one row per administered screening instrument,
--                             with item-level answers (JSONB) and triggered
--                             critical items. Replaces the PHQ-9-specific
--                             phq9_observations table from v1 (which was
--                             never deployed to this instance).
--
-- Safety: all CREATE … IF NOT EXISTS; no destructive operations.
-- Safe to run on production during normal operations.

BEGIN;

-- ============================================================
-- behavioral_screenings
-- ============================================================
CREATE TABLE IF NOT EXISTS behavioral_screenings (
    id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    patient_id        UUID NOT NULL REFERENCES patients(id) ON DELETE CASCADE,
    instrument_key    TEXT NOT NULL,         -- SCREENING_REGISTRY key, e.g. 'gad7', 'audit_c'
    domain            TEXT NOT NULL,         -- DOMAINS key, e.g. 'anxiety'
    loinc_code        TEXT,                  -- LOINC code of the questionnaire panel
    score             INTEGER,               -- total numeric score (NULL for qualitative-only)
    band              TEXT,                  -- SeverityBand.label, e.g. 'moderate'
    item_answers      JSONB DEFAULT '{}',    -- {item_number: answer_value, ...} 1-based
    triggered_critical JSONB DEFAULT '[]',   -- [{item_number, alert_text, actual_score}, ...]
    source_type       TEXT,                  -- 'fhir_observation'|'questionnaire_response'|'manual'
    source_id         UUID,                  -- FK to raw_fhir_cache or other source (nullable)
    administered_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    entered_by        TEXT,                  -- clinician name/system
    data_source       VARCHAR(50) NOT NULL DEFAULT 'healthex'
);

CREATE INDEX IF NOT EXISTS idx_bs_patient_instrument_time
    ON behavioral_screenings(patient_id, instrument_key, administered_at DESC);

CREATE INDEX IF NOT EXISTS idx_bs_patient_domain_time
    ON behavioral_screenings(patient_id, domain, administered_at DESC);

CREATE INDEX IF NOT EXISTS idx_bs_loinc
    ON behavioral_screenings(loinc_code, administered_at DESC)
    WHERE loinc_code IS NOT NULL;

-- Partial index for records with critical items triggered (fast SI lookup)
CREATE INDEX IF NOT EXISTS idx_bs_critical
    ON behavioral_screenings(patient_id, administered_at DESC)
    WHERE jsonb_array_length(triggered_critical) > 0;

COMMIT;
