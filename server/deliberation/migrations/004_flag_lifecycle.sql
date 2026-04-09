-- ============================================================
-- Migration 4: Flag Lifecycle & Retroactive Correction System
-- Adds: deliberation_flags, flag_review_runs, flag_corrections
-- Run: psql $DATABASE_URL -f migrations/004_flag_lifecycle.sql
-- ============================================================

-- ── ENUM types ───────────────────────────────────────────────

DO $$ BEGIN
    CREATE TYPE flag_lifecycle_state AS ENUM (
        'open',              -- active, not yet reviewed
        'retracted',         -- auto-retracted: data that caused it is now correct
        'superseded',        -- replaced by a newer flag on the same clinical topic
        'human_verified',    -- clinician confirmed this flag is still valid
        'human_dismissed',   -- clinician confirmed this flag was a false alarm
        'resolved'           -- the underlying clinical issue was addressed
    );
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

DO $$ BEGIN
    CREATE TYPE flag_basis AS ENUM (
        'data_corrupt',      -- caused by wrong/truncated/0.0 data
        'data_missing',      -- caused by absence of a field that now exists
        'data_stale',        -- caused by data older than clinical threshold
        'data_conflict',     -- two sources disagree
        'clinical_finding',  -- genuine clinical concern (not a data artifact)
        'derived_inference'  -- inferred from pattern, not a single field
    );
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

DO $$ BEGIN
    CREATE TYPE flag_priority AS ENUM (
        'low', 'medium', 'medium-high', 'high', 'critical',
        'retracted',    -- soft-delete: flag existed but was retracted
        'superseded'    -- replaced by newer flag
    );
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

DO $$ BEGIN
    CREATE TYPE correction_action AS ENUM (
        'auto_retract',       -- safe to retract without human
        'auto_supersede',     -- replaced by a better flag
        'escalate_human',     -- needs clinician decision
        'confirm_valid',      -- new data confirms flag is still correct
        'upgrade_priority',   -- new data makes flag more urgent
        'downgrade_priority'  -- new data makes flag less urgent
    );
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;


-- ── Table 1: deliberation_flags ──────────────────────────────

CREATE TABLE IF NOT EXISTS deliberation_flags (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    patient_id          UUID NOT NULL,
    deliberation_id     UUID NOT NULL,
    flag_type           VARCHAR(50) NOT NULL,

    -- Flag content
    title               TEXT NOT NULL,
    description         TEXT NOT NULL,
    priority            flag_priority NOT NULL DEFAULT 'medium',
    flag_basis          flag_basis NOT NULL DEFAULT 'clinical_finding',

    -- Data provenance: what records caused this flag
    data_provenance     JSONB NOT NULL DEFAULT '[]',

    -- Data quality snapshot at time of flag
    data_quality_score  NUMERIC(3,2),
    had_zero_values     BOOLEAN DEFAULT false,
    had_missing_fields  BOOLEAN DEFAULT false,

    -- Lifecycle
    lifecycle_state     flag_lifecycle_state NOT NULL DEFAULT 'open',
    flagged_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    reviewed_at         TIMESTAMPTZ,
    resolved_at         TIMESTAMPTZ,

    -- Review outcome
    retraction_reason   TEXT,
    retraction_trigger  VARCHAR(50),
    retracted_by        UUID,

    -- Human escalation
    requires_human      BOOLEAN DEFAULT false,
    human_reviewer_id   VARCHAR(100),
    human_review_note   TEXT,
    human_reviewed_at   TIMESTAMPTZ,

    -- Nudge linkage
    linked_nudge_ids    UUID[] DEFAULT '{}',
    nudge_was_sent      BOOLEAN DEFAULT false,

    -- Dedup fingerprint
    flag_fingerprint    VARCHAR(64) NOT NULL,
    UNIQUE(patient_id, flag_fingerprint, lifecycle_state)
);

CREATE INDEX IF NOT EXISTS idx_flags_patient_open
    ON deliberation_flags(patient_id, lifecycle_state)
    WHERE lifecycle_state = 'open';
CREATE INDEX IF NOT EXISTS idx_flags_patient_all
    ON deliberation_flags(patient_id, flagged_at DESC);
CREATE INDEX IF NOT EXISTS idx_flags_basis
    ON deliberation_flags(flag_basis, lifecycle_state);
CREATE INDEX IF NOT EXISTS idx_flags_requires_human
    ON deliberation_flags(patient_id)
    WHERE requires_human = true AND lifecycle_state = 'open';
CREATE INDEX IF NOT EXISTS idx_flags_delib
    ON deliberation_flags(deliberation_id);


-- ── Table 2: flag_review_runs ────────────────────────────────

CREATE TABLE IF NOT EXISTS flag_review_runs (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    patient_id          UUID NOT NULL,
    trigger_type        VARCHAR(50) NOT NULL,
    trigger_ref_id      UUID,

    -- Review scope
    flags_reviewed      INT DEFAULT 0,
    flags_retracted     INT DEFAULT 0,
    flags_superseded    INT DEFAULT 0,
    flags_escalated     INT DEFAULT 0,
    flags_unchanged     INT DEFAULT 0,

    -- LLM reviewer output
    review_model        VARCHAR(50),
    review_prompt_chars INT,
    review_summary      TEXT,

    status              VARCHAR(20) DEFAULT 'complete',
    error_message       TEXT,
    started_at          TIMESTAMPTZ DEFAULT NOW(),
    completed_at        TIMESTAMPTZ,
    duration_ms         INT
);

CREATE INDEX IF NOT EXISTS idx_review_runs_patient
    ON flag_review_runs(patient_id, started_at DESC);


-- ── Table 3: flag_corrections ────────────────────────────────

CREATE TABLE IF NOT EXISTS flag_corrections (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    flag_id             UUID NOT NULL REFERENCES deliberation_flags(id),
    review_run_id       UUID NOT NULL REFERENCES flag_review_runs(id),
    patient_id          UUID NOT NULL,

    -- What the reviewer decided
    action              correction_action NOT NULL,
    confidence          NUMERIC(3,2) NOT NULL,
    reasoning           TEXT NOT NULL,

    -- Evidence
    old_data_snapshot   JSONB,
    new_data_snapshot   JSONB,
    data_changed        BOOLEAN DEFAULT false,

    -- For escalate_human
    clarification_question  TEXT,
    clarification_options   JSONB,
    clarification_urgency   VARCHAR(20),

    -- For priority changes
    old_priority        flag_priority,
    new_priority        flag_priority,

    -- Execution
    applied             BOOLEAN DEFAULT false,
    applied_at          TIMESTAMPTZ,
    applied_by          VARCHAR(50),

    created_at          TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_corrections_flag
    ON flag_corrections(flag_id);
CREATE INDEX IF NOT EXISTS idx_corrections_review
    ON flag_corrections(review_run_id);
CREATE INDEX IF NOT EXISTS idx_corrections_patient
    ON flag_corrections(patient_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_corrections_pending
    ON flag_corrections(patient_id)
    WHERE applied = false;


-- ── Fix deliberation_outputs priority constraint ─────────────

ALTER TABLE deliberation_outputs
    DROP CONSTRAINT IF EXISTS deliberation_outputs_priority_check;
ALTER TABLE deliberation_outputs
    ADD CONSTRAINT deliberation_outputs_priority_check
    CHECK (priority IN ('low','medium','medium-high','high','critical'));


-- ── Backfill historic flags from deliberation_outputs ────────

INSERT INTO deliberation_flags (
    patient_id, deliberation_id, flag_type,
    title, description, priority, flag_basis,
    flag_fingerprint, lifecycle_state,
    had_zero_values
)
SELECT
    d.patient_id::uuid,
    o.deliberation_id,
    o.output_type,
    COALESCE(o.output_data::jsonb->>'flag', o.output_data::jsonb->>'title', 'Historic flag'),
    COALESCE(o.output_data::jsonb->>'description', o.output_data::jsonb->>'detail', ''),
    COALESCE(o.priority, 'medium')::flag_priority,
    CASE
        WHEN o.output_data::text ILIKE '%0.0%' THEN 'data_corrupt'::flag_basis
        WHEN o.output_data::text ILIKE '%missing%' THEN 'data_missing'::flag_basis
        ELSE 'clinical_finding'::flag_basis
    END,
    MD5(d.patient_id::text || COALESCE(o.output_data::jsonb->>'flag', 'historic')),
    'open'::flag_lifecycle_state,
    o.output_data::text ILIKE '%0.0%'
FROM deliberation_outputs o
JOIN deliberations d ON d.id = o.deliberation_id
WHERE o.output_type = 'missing_data_flag'
ON CONFLICT DO NOTHING;
