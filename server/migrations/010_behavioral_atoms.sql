-- Migration 010: ATOM-first behavioral detection
-- Adds behavioral_signal_atoms, behavioral_screening_gaps, phq9_observations,
-- behavioral_phenotypes, and the atom_pressure_scores materialized view.
-- All statements are idempotent (IF NOT EXISTS).

-- pgvector already enabled by 009_pgvector_guidelines.sql; keep idempotent safety.
CREATE EXTENSION IF NOT EXISTS vector;

-- ── behavioral_signal_atoms ──────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS behavioral_signal_atoms (
    id                    UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    patient_id            UUID NOT NULL REFERENCES patients(id) ON DELETE CASCADE,
    source_note_id        TEXT NOT NULL,
    clinical_date         DATE NOT NULL,
    note_section          TEXT,
    signal_type           TEXT NOT NULL,
    signal_value          TEXT NOT NULL,
    assertion             TEXT NOT NULL
                          CHECK (assertion IN ('present','absent','historical')),
    confidence            FLOAT NOT NULL
                          CHECK (confidence >= 0 AND confidence <= 1),
    snomed_concept        TEXT,
    embedding             vector(768),
    pressure_weight       FLOAT GENERATED ALWAYS AS (
                            confidence * EXP(-0.5 *
                              EXTRACT(EPOCH FROM (NOW() - clinical_date::timestamptz))
                              / (10368000.0))
                          ) STORED,
    contributed_to_gap_id UUID,
    extraction_model      TEXT,
    extraction_prompt_ver TEXT,
    created_at            TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_bsa_patient_date
    ON behavioral_signal_atoms (patient_id, clinical_date DESC);
CREATE INDEX IF NOT EXISTS idx_bsa_signal_type
    ON behavioral_signal_atoms (signal_type);
CREATE INDEX IF NOT EXISTS idx_bsa_assertion
    ON behavioral_signal_atoms (patient_id, assertion);
CREATE INDEX IF NOT EXISTS idx_bsa_embedding
    ON behavioral_signal_atoms USING hnsw (embedding vector_cosine_ops)
    WITH (m=16, ef_construction=64);

-- ── behavioral_screening_gaps ────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS behavioral_screening_gaps (
    id                        UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    patient_id                UUID NOT NULL REFERENCES patients(id) ON DELETE CASCADE,
    detected_at               TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    gap_type                  TEXT NOT NULL
                              CHECK (gap_type IN (
                                'no_screening',
                                'stale_screening',
                                'item9_no_followup'
                              )),
    atom_count                INT NOT NULL,
    atom_date_range           DATERANGE NOT NULL,
    atom_ids                  UUID[] NOT NULL,
    pressure_score            FLOAT NOT NULL,
    last_screening_date       DATE,
    last_screening_score      INT,
    last_screening_item9      INT,
    status                    TEXT NOT NULL DEFAULT 'open'
                              CHECK (status IN ('open','acknowledged','resolved')),
    surfaced_to               TEXT[] DEFAULT '{}',
    output_mode               TEXT NOT NULL DEFAULT 'primary_evidence'
                              CHECK (output_mode IN ('primary_evidence','contextual')),
    resolved_by_screening_id  UUID,
    resolved_at               TIMESTAMPTZ,
    temporal_confidence       TEXT
                              CHECK (temporal_confidence IN (
                                'high','moderate','low','very_low'
                              )),
    recommended_instruments   TEXT[] DEFAULT '{}',
    created_at                TIMESTAMPTZ DEFAULT NOW(),
    updated_at                TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_bsg_patient_status
    ON behavioral_screening_gaps (patient_id, status);
CREATE INDEX IF NOT EXISTS idx_bsg_detected_at
    ON behavioral_screening_gaps (detected_at DESC);

-- FK from atoms → gaps (added after gaps table exists)
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'fk_atom_gap'
    ) THEN
        ALTER TABLE behavioral_signal_atoms
            ADD CONSTRAINT fk_atom_gap
            FOREIGN KEY (contributed_to_gap_id)
            REFERENCES behavioral_screening_gaps(id)
            ON DELETE SET NULL;
    END IF;
END$$;

-- ── phq9_observations ────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS phq9_observations (
    id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    patient_id       UUID NOT NULL REFERENCES patients(id) ON DELETE CASCADE,
    observation_date DATE NOT NULL,
    total_score      INT  NOT NULL CHECK (total_score >= 0 AND total_score <= 27),
    item_9_score     INT  CHECK (item_9_score >= 0 AND item_9_score <= 3),
    phq2_score       INT  CHECK (phq2_score >= 0 AND phq2_score <= 6),
    source           TEXT,
    fhir_resource_id TEXT,
    raw_items        JSONB,
    created_at       TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_phq9_patient_date
    ON phq9_observations (patient_id, observation_date DESC);

-- ── behavioral_phenotypes (new in this repo) ─────────────────────────────
CREATE TABLE IF NOT EXISTS behavioral_phenotypes (
    patient_id              UUID PRIMARY KEY REFERENCES patients(id) ON DELETE CASCADE,
    evidence_mode           TEXT DEFAULT 'contextual'
                            CHECK (evidence_mode IN ('primary_evidence','contextual')),
    last_formal_screening   DATE,
    screening_gap_id        UUID REFERENCES behavioral_screening_gaps(id) ON DELETE SET NULL,
    atom_pressure_score     FLOAT DEFAULT 0.0,
    temporal_confidence     TEXT
                            CHECK (temporal_confidence IN ('high','moderate','low','very_low')),
    trajectory_status       TEXT,
    updated_at              TIMESTAMPTZ DEFAULT NOW()
);

-- ── atom_pressure_scores materialized view ───────────────────────────────
CREATE MATERIALIZED VIEW IF NOT EXISTS atom_pressure_scores AS
SELECT
    patient_id,
    SUM(pressure_weight) AS pressure_score,
    COUNT(*) FILTER (WHERE assertion = 'present') AS present_atom_count,
    COUNT(*) AS total_atom_count,
    MAX(clinical_date) AS latest_atom_date,
    MIN(clinical_date) AS earliest_atom_date,
    NOW() AS computed_at
FROM behavioral_signal_atoms
WHERE assertion IN ('present', 'historical')
GROUP BY patient_id;

CREATE UNIQUE INDEX IF NOT EXISTS idx_aps_patient
    ON atom_pressure_scores (patient_id);
