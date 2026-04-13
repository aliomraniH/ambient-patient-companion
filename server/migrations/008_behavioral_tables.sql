-- Migration 008: Behavioral science tables for Tier 2.b tool stack.
--
-- Tables:
--   patient_llm_interactions    — longitudinal LLM-interaction health tracking
--                                 (score_llm_interaction_health + history)
--   patient_com_b_assessments   — COM-B barrier classifications per target
--                                 behavior for audit + re-evaluation
--   nis_score_audits            — complete NIS decomposition per decision
--                                 (components: ITE, receptivity, COM-B, LLM health)
--   jitai_triggers              — registered conversation → JITAI bridges
--                                 (register_conversation_trigger)
--
-- Safety: all CREATE TABLE IF NOT EXISTS; no destructive operations.
-- Safe to run on production during normal operations.

BEGIN;

-- ============================================================
-- patient_llm_interactions — daily LLM interaction health scores
-- ============================================================
CREATE TABLE IF NOT EXISTS patient_llm_interactions (
    id                   UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    patient_id           UUID NOT NULL REFERENCES patients(id) ON DELETE CASCADE,
    session_date         DATE NOT NULL,
    health_score         DOUBLE PRECISION NOT NULL,    -- 0.0 (unhealthy) – 1.0 (healthy)
    session_duration_min INTEGER,
    session_count        INTEGER DEFAULT 1,
    patterns_detected    TEXT[] DEFAULT '{}',          -- over_reliance, reassurance_seeking, ...
    referral_made        BOOLEAN DEFAULT false,
    referral_type        TEXT,                         -- e.g. 'clinician_handoff', 'crisis_line'
    notes                TEXT,
    created_at           TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE (patient_id, session_date)
);
CREATE INDEX IF NOT EXISTS idx_llm_interactions_patient_date
    ON patient_llm_interactions(patient_id, session_date DESC);


-- ============================================================
-- patient_com_b_assessments — COM-B barrier map per target behavior
-- ============================================================
CREATE TABLE IF NOT EXISTS patient_com_b_assessments (
    id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    patient_id        UUID NOT NULL REFERENCES patients(id) ON DELETE CASCADE,
    target_behavior   TEXT NOT NULL,                   -- e.g. 'statin_adherence', 'bp_monitoring'
    com_b_component   TEXT NOT NULL,                   -- Capability|Opportunity|Motivation
    sub_component     TEXT,                            -- Physical|Psychological|Social|Reflective|...
    primary_barrier   TEXT,
    confidence        DOUBLE PRECISION DEFAULT 0.0,
    supporting_evidence TEXT[] DEFAULT '{}',
    assessed_at       TIMESTAMPTZ DEFAULT NOW(),
    assessed_by       TEXT DEFAULT 'MIRA',
    valid_until       TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_com_b_patient_behavior
    ON patient_com_b_assessments(patient_id, target_behavior);


-- ============================================================
-- nis_score_audits — full Nudge Impactability Score decomposition
-- ============================================================
CREATE TABLE IF NOT EXISTS nis_score_audits (
    id                   UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    patient_id           UUID NOT NULL REFERENCES patients(id) ON DELETE CASCADE,
    deliberation_id      UUID,                          -- nullable; not all calls come from a deliberation
    computed_at          TIMESTAMPTZ DEFAULT NOW(),
    compound_score       DOUBLE PRECISION NOT NULL,     -- α·ITE + β·receptivity + γ·COM-B + δ·LLM health
    ite_score            DOUBLE PRECISION,
    receptivity_score    DOUBLE PRECISION,
    com_b_score          DOUBLE PRECISION,
    llm_health_score     DOUBLE PRECISION,
    weights              JSONB,                         -- {α, β, γ, δ} for traceability
    recommendation       TEXT NOT NULL,                 -- fire|hold|suppress
    rationale            TEXT,
    calling_agent        TEXT                           -- ARIA|THEO|MIRA|SYNTHESIS
);
CREATE INDEX IF NOT EXISTS idx_nis_audits_patient_time
    ON nis_score_audits(patient_id, computed_at DESC);


-- ============================================================
-- jitai_triggers — registered conversation→JITAI bridges
-- ============================================================
CREATE TABLE IF NOT EXISTS jitai_triggers (
    id                 UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    patient_id         UUID NOT NULL REFERENCES patients(id) ON DELETE CASCADE,
    signal_type        TEXT NOT NULL,                   -- change_talk, emotional_disclosure, ...
    trigger_jitai_type TEXT NOT NULL,                   -- food_access, medication_pickup, ...
    min_signal_strength DOUBLE PRECISION DEFAULT 0.6,
    status             TEXT NOT NULL DEFAULT 'active',  -- active|fired|expired|cancelled
    registered_at      TIMESTAMPTZ DEFAULT NOW(),
    expires_at         TIMESTAMPTZ,
    fired_at           TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_jitai_triggers_patient_status
    ON jitai_triggers(patient_id, status);


COMMIT;
