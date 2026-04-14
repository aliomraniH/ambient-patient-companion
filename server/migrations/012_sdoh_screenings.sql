-- Migration 012: SDoH screenings table.
-- Records administered SDoH screener results (PRAPARE, AHC-HRSN, HVS, etc.)
-- routed from ingest_fhir_questionnaire_response / ingest_fhir_observation.
--
-- Safety: all CREATE … IF NOT EXISTS; safe to re-run.

BEGIN;

CREATE TABLE IF NOT EXISTS sdoh_screenings (
    id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    patient_id       UUID NOT NULL REFERENCES patients(id) ON DELETE CASCADE,
    screener_key     TEXT NOT NULL,
    panel_loinc      TEXT,
    domains_flagged  TEXT[] NOT NULL DEFAULT '{}',
    item_answers     JSONB NOT NULL DEFAULT '{}',
    administered_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    source_type      TEXT NOT NULL DEFAULT 'fhir_questionnaire_response',
    source_id        UUID,
    data_source      VARCHAR(50) NOT NULL DEFAULT 'healthex',
    created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_sdoh_screenings_patient
    ON sdoh_screenings(patient_id, administered_at DESC);

CREATE INDEX IF NOT EXISTS idx_sdoh_screenings_screener
    ON sdoh_screenings(screener_key, patient_id);

COMMIT;
