-- ============================================================
-- DELIBERATION ENGINE TABLES
-- Adds to existing 22-table schema
-- Run: psql $DATABASE_URL -f migrations/001_deliberation_tables.sql
-- ============================================================

-- Main deliberation session record
CREATE TABLE IF NOT EXISTS deliberations (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    patient_id          TEXT NOT NULL,
    trigger_type        TEXT NOT NULL CHECK (trigger_type IN (
                            'scheduled_pre_encounter',
                            'lab_result_received',
                            'medication_change',
                            'missed_appointment',
                            'temporal_threshold',
                            'manual'
                        )),
    triggered_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    completed_at        TIMESTAMPTZ,
    status              TEXT NOT NULL DEFAULT 'pending' CHECK (status IN (
                            'pending','running','complete','failed','cancelled'
                        )),
    rounds_completed    INTEGER DEFAULT 0,
    convergence_score   FLOAT,
    model_claude        TEXT NOT NULL DEFAULT 'claude-sonnet-4-20250514',
    model_gpt4          TEXT NOT NULL DEFAULT 'gpt-4o',
    synthesizer_model   TEXT NOT NULL,
    total_tokens        INTEGER,
    total_latency_ms    INTEGER,
    error_message       TEXT,
    -- Full transcript stored as JSONB for developer inspection
    transcript          JSONB,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_deliberations_patient_id ON deliberations(patient_id);
CREATE INDEX idx_deliberations_status ON deliberations(status);
CREATE INDEX idx_deliberations_triggered_at ON deliberations(triggered_at DESC);

-- Five structured output categories per deliberation
CREATE TABLE IF NOT EXISTS deliberation_outputs (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    deliberation_id     UUID NOT NULL REFERENCES deliberations(id) ON DELETE CASCADE,
    output_type         TEXT NOT NULL CHECK (output_type IN (
                            'anticipatory_scenario',
                            'predicted_patient_question',
                            'missing_data_flag',
                            'patient_nudge',
                            'care_team_nudge'
                        )),
    output_data         JSONB NOT NULL,
    priority            TEXT CHECK (priority IN ('critical','high','medium','low')),
    confidence          FLOAT CHECK (confidence >= 0 AND confidence <= 1),
    timeframe           TEXT,                  -- e.g. 'next_30_days'
    trigger_condition   TEXT,                  -- for nudges: when to fire
    -- Delivery tracking for nudges
    delivered_at        TIMESTAMPTZ,
    delivery_channel    TEXT,
    patient_responded   BOOLEAN,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_deliberation_outputs_deliberation_id
    ON deliberation_outputs(deliberation_id);
CREATE INDEX idx_deliberation_outputs_output_type
    ON deliberation_outputs(output_type);
CREATE INDEX idx_deliberation_outputs_pending_nudges
    ON deliberation_outputs(trigger_condition, delivered_at)
    WHERE output_type IN ('patient_nudge', 'care_team_nudge')
      AND delivered_at IS NULL;

-- Patient-specific knowledge accumulated across deliberations
CREATE TABLE IF NOT EXISTS patient_knowledge (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    patient_id          TEXT NOT NULL,
    knowledge_type      TEXT NOT NULL CHECK (knowledge_type IN (
                            'clinical_inference',     -- e.g. "patient shows BP drift"
                            'behavioral_pattern',     -- e.g. "engagement drops on Mondays"
                            'preference',             -- e.g. "prefers SMS over portal"
                            'risk_trajectory',        -- forward-looking risk
                            'care_gap',               -- identified gap
                            'data_quality_flag'       -- missing or conflicting data
                        )),
    entry_text          TEXT NOT NULL,
    confidence          FLOAT CHECK (confidence >= 0 AND confidence <= 1),
    -- Temporal validity window (Zep/Graphiti pattern)
    valid_from          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    valid_until         TIMESTAMPTZ,           -- NULL means open-ended
    -- Provenance chain
    source_deliberation_id UUID REFERENCES deliberations(id),
    contributing_models TEXT[],                -- ['claude-sonnet-4','gpt-4o']
    identified_in_round INTEGER,
    evidence_refs       TEXT[],                -- EHR field names / lab IDs cited
    -- Lifecycle
    superseded_by       UUID REFERENCES patient_knowledge(id),
    is_current          BOOLEAN NOT NULL DEFAULT true,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_patient_knowledge_patient_current
    ON patient_knowledge(patient_id, is_current)
    WHERE is_current = true;
CREATE INDEX idx_patient_knowledge_type
    ON patient_knowledge(patient_id, knowledge_type);

-- Core clinical knowledge reinforced/updated by deliberations
-- (shared across patients — e.g. "metformin takes 4-6 weeks for full HbA1c effect")
CREATE TABLE IF NOT EXISTS core_knowledge_updates (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    knowledge_entry     TEXT NOT NULL,
    update_type         TEXT NOT NULL CHECK (update_type IN (
                            'reinforcement',  -- existing fact confirmed again
                            'revision',       -- existing fact corrected
                            'new_fact'        -- novel clinical insight
                        )),
    confidence_delta    FLOAT,
    source              TEXT,                  -- free-text justification
    source_deliberation_id UUID REFERENCES deliberations(id),
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
