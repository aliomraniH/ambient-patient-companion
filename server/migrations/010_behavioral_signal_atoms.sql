-- Migration 010: Behavioral signal atoms + atom pressure materialized view.
--
-- Tables:
--   behavioral_signal_atoms      — one row per extracted behavioral signal,
--                                  with 768-dim embedding for pgvector retrieval
--   behavioral_screening_gaps    — open gaps when domain has signals but no
--                                  recent qualifying screening
--   behavioral_phenotypes        — upserted per-patient phenotype summary
--
-- Views:
--   atom_pressure_scores         — materialized view of current atom pressure
--                                  per patient × signal_type
--
-- Indexes:
--   idx_bsa_embedding            — HNSW cosine index (pgvector)
--   idx_bsa_patient_signal       — fast per-patient + signal_type lookups
--
-- Prerequisites: migration 009 (pgvector extension already enabled).
-- Safety: all CREATE … IF NOT EXISTS; safe to re-run.

BEGIN;

-- ============================================================
-- behavioral_signal_atoms
-- ============================================================
CREATE TABLE IF NOT EXISTS behavioral_signal_atoms (
    id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    patient_id       UUID NOT NULL REFERENCES patients(id) ON DELETE CASCADE,
    signal_type      TEXT NOT NULL,          -- e.g. 'anxiety_markers', 'depression_markers'
    signal_value     TEXT NOT NULL,          -- raw text extracted from conversation/notes
    confidence       DOUBLE PRECISION DEFAULT 0.5 CHECK (confidence BETWEEN 0 AND 1),
    source_type      TEXT,                   -- 'conversation'|'clinical_note'|'checkin'
    source_id        UUID,                   -- FK to source row (nullable)
    extracted_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    embedding        VECTOR(768),            -- populated by atom_embedder at insert time
    data_source      VARCHAR(50) NOT NULL DEFAULT 'healthex'
);

CREATE INDEX IF NOT EXISTS idx_bsa_patient_signal
    ON behavioral_signal_atoms(patient_id, signal_type, extracted_at DESC);

CREATE INDEX IF NOT EXISTS idx_bsa_embedding
    ON behavioral_signal_atoms
    USING hnsw (embedding vector_cosine_ops)
    WITH (m = 16, ef_construction = 64);

-- ============================================================
-- behavioral_screening_gaps
-- ============================================================
CREATE TABLE IF NOT EXISTS behavioral_screening_gaps (
    id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    patient_id       UUID NOT NULL REFERENCES patients(id) ON DELETE CASCADE,
    domain           TEXT NOT NULL,          -- DOMAINS key
    gap_type         TEXT NOT NULL DEFAULT 'no_screening'
                     CHECK (gap_type IN ('no_screening', 'stale_screening')),
    triggered_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    resolved_at      TIMESTAMPTZ,
    resolved_by      UUID,                   -- FK to behavioral_screenings.id
    pressure_score   DOUBLE PRECISION,       -- atom pressure at time of gap detection
    suggested_instruments TEXT[],            -- instrument keys from registry
    phenotype_label  TEXT,
    temporal_confidence TEXT,
    status           TEXT NOT NULL DEFAULT 'open'
                     CHECK (status IN ('open', 'resolved', 'dismissed')),
    data_source      VARCHAR(50) NOT NULL DEFAULT 'healthex'
);

CREATE INDEX IF NOT EXISTS idx_bsg_patient_domain_status
    ON behavioral_screening_gaps(patient_id, domain, status);

-- ============================================================
-- behavioral_phenotypes — upserted per-patient phenotype summary
-- ============================================================
CREATE TABLE IF NOT EXISTS behavioral_phenotypes (
    id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    patient_id       UUID NOT NULL REFERENCES patients(id) ON DELETE CASCADE,
    domain           TEXT NOT NULL,
    phenotype_label  TEXT NOT NULL,
    confidence       DOUBLE PRECISION DEFAULT 0.5,
    last_updated     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (patient_id, domain)
);

-- ============================================================
-- atom_pressure_scores — materialized view
-- ============================================================
DROP MATERIALIZED VIEW IF EXISTS atom_pressure_scores;

CREATE MATERIALIZED VIEW atom_pressure_scores AS
SELECT
    patient_id,
    signal_type,
    COUNT(*)                                                   AS present_atom_count,
    AVG(confidence)                                            AS pressure_score,
    MAX(extracted_at)                                          AS last_atom_at,
    MIN(extracted_at)                                          AS first_atom_at
FROM behavioral_signal_atoms
WHERE extracted_at >= NOW() - INTERVAL '90 days'
GROUP BY patient_id, signal_type;

CREATE UNIQUE INDEX IF NOT EXISTS idx_aps_patient_signal
    ON atom_pressure_scores(patient_id, signal_type);

CREATE INDEX IF NOT EXISTS idx_aps_pressure
    ON atom_pressure_scores(pressure_score DESC);

COMMIT;
