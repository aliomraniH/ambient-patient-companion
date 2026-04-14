-- Migration 011: Instrument-agnostic behavioral + SDoH screenings (v2)
--
-- Replaces the PHQ-9-specific phq9_observations table with a generic
-- behavioral_screenings table keyed by instrument + domain. Adds a
-- sibling sdoh_screenings table for social-determinants instruments.
-- Adds triggered_domains to behavioral_screening_gaps so multiple
-- simultaneous domain gaps per patient can be tracked.
--
-- Idempotent — all statements use IF [NOT] EXISTS where possible.
-- Wrapped in a single transaction so the PHQ-9 migration is atomic.

BEGIN;

-- ── behavioral_screenings ────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS behavioral_screenings (
    id                    UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    patient_id            UUID NOT NULL REFERENCES patients(id) ON DELETE CASCADE,
    instrument_key        TEXT NOT NULL,             -- registry key, e.g. 'phq9'
    instrument_name       TEXT NOT NULL,             -- 'PHQ-9'
    domain                TEXT NOT NULL,             -- 'depression'
    observation_date      DATE NOT NULL,
    total_score           INT,                       -- may be NULL for panels scored by item
    severity_band         TEXT,                      -- resolved by registry helper
    is_positive           BOOLEAN,                   -- gender-aware for AUDIT-C etc.
    item_scores           JSONB,                     -- {item_number: numeric_score}
    item_answers          JSONB,                     -- {item_number: raw_answer}
    triggered_critical    JSONB DEFAULT '[]'::jsonb, -- list[{instrument, item_number, alert_text, actual_score, priority}]
    source                TEXT,                      -- 'healthex' | 'manual' | 'fhir'
    fhir_resource_type    TEXT,                      -- 'Observation' | 'QuestionnaireResponse'
    fhir_resource_id      TEXT,
    raw_payload           JSONB,                     -- full FHIR resource (audit)
    created_at            TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_bs_patient_date
    ON behavioral_screenings (patient_id, observation_date DESC);
CREATE INDEX IF NOT EXISTS idx_bs_instrument
    ON behavioral_screenings (instrument_key);
CREATE INDEX IF NOT EXISTS idx_bs_domain
    ON behavioral_screenings (patient_id, domain, observation_date DESC);
-- Idempotency: same patient + instrument + date = single row.
CREATE UNIQUE INDEX IF NOT EXISTS uq_bs_patient_instrument_date
    ON behavioral_screenings (patient_id, instrument_key, observation_date);

-- ── sdoh_screenings ──────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS sdoh_screenings (
    id                    UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    patient_id            UUID NOT NULL REFERENCES patients(id) ON DELETE CASCADE,
    instrument_key        TEXT NOT NULL,
    instrument_name       TEXT NOT NULL,
    observation_date      DATE NOT NULL,
    positive_domains      TEXT[] DEFAULT '{}',
    item_answers          JSONB,
    item_scores           JSONB,
    source                TEXT,
    fhir_resource_type    TEXT,
    fhir_resource_id      TEXT,
    raw_payload           JSONB,
    created_at            TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_ss_patient_date
    ON sdoh_screenings (patient_id, observation_date DESC);
CREATE INDEX IF NOT EXISTS idx_ss_instrument
    ON sdoh_screenings (instrument_key);
CREATE UNIQUE INDEX IF NOT EXISTS uq_ss_patient_instrument_date
    ON sdoh_screenings (patient_id, instrument_key, observation_date);

-- ── behavioral_screening_gaps: add triggered_domains ─────────────────────
ALTER TABLE behavioral_screening_gaps
    ADD COLUMN IF NOT EXISTS triggered_domains TEXT[] DEFAULT '{}';

CREATE INDEX IF NOT EXISTS idx_bsg_triggered_domains
    ON behavioral_screening_gaps USING GIN (triggered_domains);

-- ── Migrate phq9_observations → behavioral_screenings ────────────────────
-- Copy any existing PHQ-9 rows as instrument='phq9', domain='depression'.
-- item_scores is {9: item_9_score} where available (raw_items JSONB is
-- preserved in raw_payload).
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.tables
         WHERE table_name = 'phq9_observations'
    ) THEN
        INSERT INTO behavioral_screenings (
            patient_id, instrument_key, instrument_name, domain,
            observation_date, total_score, item_scores, item_answers,
            triggered_critical, source, fhir_resource_type,
            fhir_resource_id, raw_payload, created_at
        )
        SELECT
            patient_id,
            'phq9'::TEXT,
            'PHQ-9'::TEXT,
            'depression'::TEXT,
            observation_date,
            total_score,
            CASE WHEN item_9_score IS NOT NULL
                 THEN jsonb_build_object('9', item_9_score)
                 ELSE NULL END,
            NULL::jsonb,
            CASE WHEN COALESCE(item_9_score, 0) >= 1
                 THEN jsonb_build_array(jsonb_build_object(
                     'instrument', 'PHQ-9',
                     'item_number', 9,
                     'alert_text', 'PHQ-9 item 9 (passive SI) elevated',
                     'actual_score', item_9_score,
                     'priority', 'critical'
                 ))
                 ELSE '[]'::jsonb END,
            COALESCE(source, 'phq9_observations_migration'),
            'Observation'::TEXT,
            fhir_resource_id,
            raw_items,
            COALESCE(created_at, NOW())
        FROM phq9_observations
        ON CONFLICT (patient_id, instrument_key, observation_date) DO NOTHING;

        -- Drop the legacy table now that rows have been migrated.
        DROP TABLE phq9_observations;
    END IF;
END$$;

COMMIT;
