-- Migration 002: ingestion_plans table + raw_fhir_cache columns
-- Supports the two-phase async ingestion architecture:
--   Phase 1: cache raw + LLM planner → ingestion_plans (fast, <500ms)
--   Phase 2: executor reads plan → parse → write rows (async)

CREATE TABLE IF NOT EXISTS ingestion_plans (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    patient_id          UUID NOT NULL REFERENCES patients(id),
    cache_id            TEXT NOT NULL,
    resource_type       VARCHAR(50) NOT NULL,

    -- Plan outputs from LLM Planner
    detected_format     VARCHAR(30) NOT NULL,
    extraction_strategy VARCHAR(50),
    estimated_rows      INT,
    column_map          JSONB,
    sample_rows         JSONB,
    insights_summary    TEXT,
    planner_confidence  FLOAT,

    -- Execution tracking
    status              VARCHAR(20) NOT NULL DEFAULT 'pending',
    rows_written        INT,
    rows_verified       INT,
    extraction_time_ms  INT,
    error_message       TEXT,
    retry_count         INT DEFAULT 0,

    planned_at          TIMESTAMPTZ DEFAULT NOW(),
    executed_at         TIMESTAMPTZ,

    CONSTRAINT valid_plan_status CHECK (
        status IN ('pending', 'running', 'complete', 'failed', 'skipped')
    )
);

CREATE INDEX IF NOT EXISTS idx_ingestion_plans_patient
    ON ingestion_plans(patient_id, status);

CREATE INDEX IF NOT EXISTS idx_ingestion_plans_pending
    ON ingestion_plans(status, planned_at)
    WHERE status = 'pending';

-- Add columns to raw_fhir_cache for raw text storage and format tagging
ALTER TABLE raw_fhir_cache ADD COLUMN IF NOT EXISTS raw_text TEXT;
ALTER TABLE raw_fhir_cache ADD COLUMN IF NOT EXISTS detected_format VARCHAR(30);
