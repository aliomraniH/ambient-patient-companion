-- Migration 006: Gap-Aware Reasoning Tables
-- Adds: reasoning_gaps, clarification_requests, gap_triggers, knowledge_search_cache
-- Run: psql $DATABASE_URL -f server/migrations/006_gap_aware_tables.sql

BEGIN;

-- ============================================================
-- 1. reasoning_gaps — gap artifacts emitted by agents
-- ============================================================
CREATE TABLE IF NOT EXISTS reasoning_gaps (
    id                      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    deliberation_id         VARCHAR(255) NOT NULL,
    patient_mrn             VARCHAR(50) NOT NULL,
    emitting_agent          VARCHAR(20) NOT NULL CHECK (emitting_agent IN ('ARIA','MIRA','THEO')),
    gap_id                  VARCHAR(255) NOT NULL,
    gap_type                VARCHAR(50) NOT NULL CHECK (gap_type IN (
                                'missing_data','stale_data','conflicting_evidence',
                                'ambiguous_context','guideline_uncertainty',
                                'drug_interaction_unknown','patient_preference_unknown',
                                'social_determinant_unknown')),
    severity                VARCHAR(20) NOT NULL CHECK (severity IN ('critical','high','medium','low')),
    description             TEXT NOT NULL,
    impact_statement        TEXT,
    confidence_without_res  FLOAT,
    confidence_with_res     FLOAT,
    attempted_resolutions   JSONB DEFAULT '[]',
    recommended_action      VARCHAR(50),
    caveat_text             TEXT,
    status                  VARCHAR(20) DEFAULT 'open' CHECK (status IN ('open','resolved','expired','superseded')),
    resolution_method       VARCHAR(100),
    expires_at              TIMESTAMPTZ,
    created_at              TIMESTAMPTZ DEFAULT NOW(),
    resolved_at             TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_reasoning_gaps_deliberation ON reasoning_gaps(deliberation_id);
CREATE INDEX IF NOT EXISTS idx_reasoning_gaps_patient      ON reasoning_gaps(patient_mrn, status);
CREATE INDEX IF NOT EXISTS idx_reasoning_gaps_severity     ON reasoning_gaps(severity, status);


-- ============================================================
-- 2. clarification_requests — structured clarification queue
-- ============================================================
CREATE TABLE IF NOT EXISTS clarification_requests (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    clarification_id    VARCHAR(255) UNIQUE NOT NULL,
    deliberation_id     VARCHAR(255) NOT NULL,
    gap_id              VARCHAR(255) NOT NULL,
    requesting_agent    VARCHAR(20) NOT NULL,
    recipient           VARCHAR(30) NOT NULL CHECK (recipient IN ('provider','patient','peer_agent','synthesis')),
    recipient_agent_id  VARCHAR(20),
    urgency             VARCHAR(20) NOT NULL CHECK (urgency IN ('blocking','preferred','optional')),
    question_text       TEXT NOT NULL,
    clinical_rationale  TEXT,
    response_schema     JSONB,
    suggested_options   JSONB DEFAULT '[]',
    default_if_unanswered TEXT,
    fallback_behavior   VARCHAR(50) DEFAULT 'escalate_to_synthesis',
    status              VARCHAR(20) DEFAULT 'pending' CHECK (status IN ('pending','answered','timeout','declined')),
    response            JSONB,
    respondent          VARCHAR(255),
    timeout_at          TIMESTAMPTZ NOT NULL,
    created_at          TIMESTAMPTZ DEFAULT NOW(),
    responded_at        TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_clarification_deliberation ON clarification_requests(deliberation_id);
CREATE INDEX IF NOT EXISTS idx_clarification_status       ON clarification_requests(status, timeout_at);


-- ============================================================
-- 3. gap_triggers — watches for gap-resolving data arrival
-- ============================================================
CREATE TABLE IF NOT EXISTS gap_triggers (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    trigger_id          VARCHAR(255) UNIQUE NOT NULL,
    patient_mrn         VARCHAR(50) NOT NULL,
    gap_id              VARCHAR(255) NOT NULL,
    watch_for           VARCHAR(50) NOT NULL,
    loinc_code          VARCHAR(20),
    snomed_code         VARCHAR(30),
    custom_condition    TEXT,
    trigger_type        VARCHAR(50) DEFAULT 'gap_resolution_received',
    on_fire_action      VARCHAR(50) NOT NULL,
    deliberation_scope  JSONB DEFAULT '["full_council"]',
    status              VARCHAR(20) DEFAULT 'active' CHECK (status IN ('active','fired','expired','cancelled')),
    expires_at          TIMESTAMPTZ NOT NULL,
    created_at          TIMESTAMPTZ DEFAULT NOW(),
    fired_at            TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_gap_triggers_patient ON gap_triggers(patient_mrn, status);
CREATE INDEX IF NOT EXISTS idx_gap_triggers_watch   ON gap_triggers(loinc_code, status) WHERE loinc_code IS NOT NULL;


-- ============================================================
-- 4. knowledge_search_cache — TTL-based external API cache
-- ============================================================
CREATE TABLE IF NOT EXISTS knowledge_search_cache (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    cache_key       VARCHAR(512) UNIQUE NOT NULL,
    query_type      VARCHAR(50) NOT NULL,
    source          VARCHAR(50) NOT NULL,
    results         JSONB NOT NULL,
    ttl_hours       INTEGER NOT NULL DEFAULT 720,
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    expires_at      TIMESTAMPTZ NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_knowledge_cache_key     ON knowledge_search_cache(cache_key);
CREATE INDEX IF NOT EXISTS idx_knowledge_cache_expires ON knowledge_search_cache(expires_at);

COMMIT;
