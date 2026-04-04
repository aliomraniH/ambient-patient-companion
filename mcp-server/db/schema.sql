-- Ambient Patient Companion — 21-Table PostgreSQL Schema
-- Source of truth for all database tables.
-- Deploy: psql $DATABASE_URL < mcp-server/db/schema.sql
-- All tables include: data_source VARCHAR(50) NOT NULL DEFAULT 'synthea'

-- Enable UUID generation
CREATE EXTENSION IF NOT EXISTS "pgcrypto";

-- ============================================================
-- 1. patients
-- ============================================================
CREATE TABLE IF NOT EXISTS patients (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    mrn             VARCHAR(50) NOT NULL,
    first_name      VARCHAR(100) NOT NULL,
    last_name       VARCHAR(100) NOT NULL,
    birth_date      DATE,
    gender          VARCHAR(20),
    race            VARCHAR(100),
    ethnicity       VARCHAR(100),
    address_line    VARCHAR(255),
    city            VARCHAR(100),
    state           VARCHAR(50),
    zip_code        VARCHAR(20),
    insurance_type  VARCHAR(50),
    is_synthetic    BOOLEAN DEFAULT true,
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    data_source     VARCHAR(50) NOT NULL DEFAULT 'synthea',
    UNIQUE(mrn)
);

-- ============================================================
-- 2. patient_conditions
-- ============================================================
CREATE TABLE IF NOT EXISTS patient_conditions (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    patient_id      UUID NOT NULL REFERENCES patients(id) ON DELETE CASCADE,
    code            VARCHAR(50),
    display         VARCHAR(500),
    system          VARCHAR(200),
    onset_date      DATE,
    clinical_status VARCHAR(50) DEFAULT 'active',
    data_source     VARCHAR(50) NOT NULL DEFAULT 'synthea'
);
CREATE INDEX IF NOT EXISTS idx_conditions_patient ON patient_conditions(patient_id);

-- ============================================================
-- 3. patient_medications
-- ============================================================
CREATE TABLE IF NOT EXISTS patient_medications (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    patient_id      UUID NOT NULL REFERENCES patients(id) ON DELETE CASCADE,
    code            VARCHAR(50),
    display         VARCHAR(500),
    system          VARCHAR(200),
    status          VARCHAR(50) DEFAULT 'active',
    authored_on     DATE,
    data_source     VARCHAR(50) NOT NULL DEFAULT 'synthea'
);
CREATE INDEX IF NOT EXISTS idx_medications_patient ON patient_medications(patient_id);

-- ============================================================
-- 4. patient_sdoh_flags
-- ============================================================
CREATE TABLE IF NOT EXISTS patient_sdoh_flags (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    patient_id      UUID NOT NULL REFERENCES patients(id) ON DELETE CASCADE,
    domain          VARCHAR(100) NOT NULL,
    flag_code       VARCHAR(20),
    description     TEXT,
    severity        VARCHAR(20) DEFAULT 'moderate',
    screening_date  DATE,
    notes           TEXT,
    data_source     VARCHAR(50) NOT NULL DEFAULT 'synthea',
    UNIQUE(patient_id, domain)
);

-- ============================================================
-- 5. biometric_readings
-- ============================================================
CREATE TABLE IF NOT EXISTS biometric_readings (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    patient_id      UUID NOT NULL REFERENCES patients(id) ON DELETE CASCADE,
    metric_type     VARCHAR(50) NOT NULL,
    value           DOUBLE PRECISION NOT NULL,
    unit            VARCHAR(20),
    measured_at     TIMESTAMPTZ NOT NULL,
    device_source   VARCHAR(100),
    context         VARCHAR(50),
    is_abnormal     BOOLEAN DEFAULT false,
    day_of_month    INT,
    data_source     VARCHAR(50) NOT NULL DEFAULT 'synthea'
);
CREATE INDEX IF NOT EXISTS idx_biometric_patient_metric_time
    ON biometric_readings(patient_id, metric_type, measured_at DESC);
CREATE UNIQUE INDEX IF NOT EXISTS idx_biometric_readings_unique
    ON biometric_readings(patient_id, metric_type, measured_at);

-- ============================================================
-- 6. daily_checkins
-- ============================================================
CREATE TABLE IF NOT EXISTS daily_checkins (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    patient_id      UUID NOT NULL REFERENCES patients(id) ON DELETE CASCADE,
    checkin_date    DATE NOT NULL,
    mood            VARCHAR(20),
    mood_numeric    INT,
    energy          VARCHAR(20),
    stress_level    INT,
    sleep_hours     DOUBLE PRECISION,
    sleep_quality   VARCHAR(20),
    notes           TEXT,
    completed_at    TIMESTAMPTZ,
    data_source     VARCHAR(50) NOT NULL DEFAULT 'synthea',
    UNIQUE(patient_id, checkin_date)
);

-- ============================================================
-- 7. medication_adherence
-- ============================================================
CREATE TABLE IF NOT EXISTS medication_adherence (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    patient_id      UUID NOT NULL REFERENCES patients(id) ON DELETE CASCADE,
    medication_id   UUID NOT NULL,
    adherence_date  DATE NOT NULL,
    taken           BOOLEAN DEFAULT false,
    taken_at        TIMESTAMPTZ,
    notes           TEXT,
    data_source     VARCHAR(50) NOT NULL DEFAULT 'synthea',
    UNIQUE(patient_id, medication_id, adherence_date)
);

-- ============================================================
-- 8. clinical_events
-- ============================================================
CREATE TABLE IF NOT EXISTS clinical_events (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    patient_id      UUID NOT NULL REFERENCES patients(id) ON DELETE CASCADE,
    event_type      VARCHAR(100),
    event_date      TIMESTAMPTZ,
    description     TEXT,
    source_system   VARCHAR(100),
    data_source     VARCHAR(50) NOT NULL DEFAULT 'synthea'
);
CREATE INDEX IF NOT EXISTS idx_clinical_events_patient_date
    ON clinical_events(patient_id, event_date DESC);

-- ============================================================
-- 9. care_gaps
-- ============================================================
CREATE TABLE IF NOT EXISTS care_gaps (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    patient_id      UUID NOT NULL REFERENCES patients(id) ON DELETE CASCADE,
    gap_type        VARCHAR(100),
    description     TEXT,
    status          VARCHAR(20) DEFAULT 'open',
    identified_date DATE,
    resolved_date   DATE,
    data_source     VARCHAR(50) NOT NULL DEFAULT 'synthea'
);
CREATE INDEX IF NOT EXISTS idx_care_gaps_patient_status
    ON care_gaps(patient_id, status);

-- ============================================================
-- 10. obt_scores
-- ============================================================
CREATE TABLE IF NOT EXISTS obt_scores (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    patient_id      UUID NOT NULL REFERENCES patients(id) ON DELETE CASCADE,
    score_date      DATE NOT NULL,
    score           DOUBLE PRECISION NOT NULL,
    primary_driver  VARCHAR(50),
    trend_direction VARCHAR(20),
    confidence      DOUBLE PRECISION DEFAULT 1.0,
    domain_scores   JSONB,
    data_source     VARCHAR(50) NOT NULL DEFAULT 'synthea',
    UNIQUE(patient_id, score_date)
);

-- ============================================================
-- 11. clinical_facts
-- ============================================================
CREATE TABLE IF NOT EXISTS clinical_facts (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    patient_id      UUID NOT NULL REFERENCES patients(id) ON DELETE CASCADE,
    fact_type       VARCHAR(100),
    category        VARCHAR(100),
    summary         TEXT,
    ttl_expires_at  TIMESTAMPTZ,
    source_skill    VARCHAR(100),
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    data_source     VARCHAR(50) NOT NULL DEFAULT 'synthea'
);
CREATE INDEX IF NOT EXISTS idx_clinical_facts_ttl
    ON clinical_facts(ttl_expires_at);

-- ============================================================
-- 12. behavioral_correlations
-- ============================================================
CREATE TABLE IF NOT EXISTS behavioral_correlations (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    patient_id      UUID NOT NULL REFERENCES patients(id) ON DELETE CASCADE,
    correlation_type VARCHAR(100),
    factor_a        VARCHAR(100),
    factor_b        VARCHAR(100),
    correlation_value DOUBLE PRECISION,
    period_start    DATE,
    period_end      DATE,
    data_source     VARCHAR(50) NOT NULL DEFAULT 'synthea'
);

-- ============================================================
-- 13. agent_interventions
-- ============================================================
CREATE TABLE IF NOT EXISTS agent_interventions (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    patient_id      UUID NOT NULL REFERENCES patients(id) ON DELETE CASCADE,
    intervention_type VARCHAR(100),
    channel         VARCHAR(50),
    summary         TEXT,
    content         TEXT,
    delivered_at    TIMESTAMPTZ,
    source_skill    VARCHAR(100),
    data_source     VARCHAR(50) NOT NULL DEFAULT 'synthea'
);
CREATE INDEX IF NOT EXISTS idx_agent_interventions_patient_time
    ON agent_interventions(patient_id, delivered_at DESC);

-- ============================================================
-- 14. agent_memory_episodes
-- ============================================================
CREATE TABLE IF NOT EXISTS agent_memory_episodes (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    patient_id      UUID NOT NULL REFERENCES patients(id) ON DELETE CASCADE,
    episode_type    VARCHAR(100),
    summary         TEXT,
    occurred_at     TIMESTAMPTZ DEFAULT NOW(),
    data_source     VARCHAR(50) NOT NULL DEFAULT 'synthea'
);

-- ============================================================
-- 15. skill_executions
-- ============================================================
CREATE TABLE IF NOT EXISTS skill_executions (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    skill_name      VARCHAR(100) NOT NULL,
    patient_id      UUID,
    status          VARCHAR(20) NOT NULL,
    output_data     JSONB,
    error_message   TEXT,
    execution_date  TIMESTAMPTZ DEFAULT NOW(),
    data_source     VARCHAR(50) NOT NULL DEFAULT 'synthea'
);
CREATE INDEX IF NOT EXISTS idx_skill_executions_date_name
    ON skill_executions(execution_date, skill_name);

-- ============================================================
-- 16. provider_risk_scores
-- ============================================================
CREATE TABLE IF NOT EXISTS provider_risk_scores (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    patient_id      UUID NOT NULL,
    score_date      DATE NOT NULL,
    risk_score      DOUBLE PRECISION NOT NULL,
    risk_tier       VARCHAR(20),
    chase_list_rank INT,
    risk_factors    JSONB,
    data_source     VARCHAR(50) NOT NULL DEFAULT 'synthea',
    UNIQUE(patient_id, score_date)
);

-- ============================================================
-- 17. pipeline_runs
-- ============================================================
CREATE TABLE IF NOT EXISTS pipeline_runs (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    run_date        TIMESTAMPTZ DEFAULT NOW(),
    patients_processed INT DEFAULT 0,
    skills_succeeded INT DEFAULT 0,
    skills_failed    INT DEFAULT 0,
    escalations      INT DEFAULT 0,
    stale_sources    INT DEFAULT 0,
    summary         JSONB,
    data_source     VARCHAR(50) NOT NULL DEFAULT 'synthea'
);

-- ============================================================
-- 18. data_sources (ingestion management)
-- ============================================================
CREATE TABLE IF NOT EXISTS data_sources (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    patient_id      UUID NOT NULL REFERENCES patients(id) ON DELETE CASCADE,
    source_name     VARCHAR(50) NOT NULL,
    is_active       BOOLEAN DEFAULT true,
    auth_token_ref  VARCHAR(200),
    connected_at    TIMESTAMPTZ DEFAULT NOW(),
    data_source     VARCHAR(50) NOT NULL DEFAULT 'synthea',
    UNIQUE(patient_id, source_name)
);

-- ============================================================
-- 19. source_freshness (ingestion management)
-- ============================================================
CREATE TABLE IF NOT EXISTS source_freshness (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    patient_id      UUID NOT NULL REFERENCES patients(id) ON DELETE CASCADE,
    source_name     VARCHAR(50) NOT NULL,
    last_ingested_at TIMESTAMPTZ,
    records_count   INT DEFAULT 0,
    ttl_hours       INT DEFAULT 24,
    is_stale        BOOLEAN DEFAULT true,
    data_source     VARCHAR(50) NOT NULL DEFAULT 'synthea',
    UNIQUE(patient_id, source_name)
);

-- ============================================================
-- 20. ingestion_log (ingestion management)
-- ============================================================
CREATE TABLE IF NOT EXISTS ingestion_log (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    patient_id      UUID NOT NULL REFERENCES patients(id) ON DELETE CASCADE,
    source_name     VARCHAR(50),
    status          VARCHAR(20),
    records_upserted INT DEFAULT 0,
    conflicts_detected INT DEFAULT 0,
    duration_ms     INT DEFAULT 0,
    error_message   TEXT,
    retry_count     INT DEFAULT 0,
    triggered_by    VARCHAR(50),
    started_at      TIMESTAMPTZ DEFAULT NOW(),
    data_source     VARCHAR(50) NOT NULL DEFAULT 'synthea'
);

-- ============================================================
-- 21. raw_fhir_cache (ingestion management)
-- ============================================================
CREATE TABLE IF NOT EXISTS raw_fhir_cache (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    patient_id      UUID NOT NULL,
    source_name     VARCHAR(50),
    resource_type   VARCHAR(50),
    raw_json        JSONB,
    fhir_resource_id VARCHAR(100),
    retrieved_at    TIMESTAMPTZ DEFAULT NOW(),
    processed       BOOLEAN DEFAULT false,
    data_source     VARCHAR(50) NOT NULL DEFAULT 'synthea',
    UNIQUE(patient_id, source_name, fhir_resource_id)
);

-- ============================================================
-- 22. system_config (runtime key-value store)
-- ============================================================
CREATE TABLE IF NOT EXISTS system_config (
    key         VARCHAR(100) PRIMARY KEY,
    value       TEXT NOT NULL,
    updated_at  TIMESTAMPTZ DEFAULT NOW()
);

INSERT INTO system_config (key, value)
VALUES ('DATA_TRACK', 'synthea')
ON CONFLICT (key) DO NOTHING;
