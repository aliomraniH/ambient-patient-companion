-- Migration: SLM adapter registry + inference log
-- Apply: psql $DATABASE_URL < mcp-server/db/migrations/001_slm_tables.sql

-- ── adapter_registry ────────────────────────────────────────────────────────
-- Tracks every LoRA adapter available for inference (cohort, patient, base).
CREATE TABLE IF NOT EXISTS adapter_registry (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    cohort_name     VARCHAR(100) NOT NULL UNIQUE,
    hf_repo         VARCHAR(200),
    adapter_type    VARCHAR(20)  NOT NULL DEFAULT 'cohort'
                    CHECK (adapter_type IN ('cohort', 'patient', 'base')),
    status          VARCHAR(20)  NOT NULL DEFAULT 'active'
                    CHECK (status IN ('active', 'training', 'inactive', 'failed')),
    last_trained_at TIMESTAMPTZ,
    created_at      TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    metadata        JSONB        DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_adapter_registry_status ON adapter_registry(status);

-- Seed: the diabetes behavioural-health cohort adapter shipped with the system
INSERT INTO adapter_registry (cohort_name, hf_repo, adapter_type, status, last_trained_at, metadata)
VALUES (
    'cohort-diabetes-bh-adapter',
    'Aliomrani6/companion-lora-sim-lora',
    'cohort',
    'active',
    NOW() - INTERVAL '7 days',
    '{"base_model":"Qwen/Qwen2.5-3B-Instruct","epochs":3,"loss":0.142,"source":"sim-lora-1778289173101"}'
)
ON CONFLICT (cohort_name) DO NOTHING;

-- ── slm_inference_log ───────────────────────────────────────────────────────
-- Per-call audit log for all SLM inferences.
-- Never stores raw prompt text — only a truncated SHA-256 hash (PHI safety).
CREATE TABLE IF NOT EXISTS slm_inference_log (
    id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    called_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    adapter_type     VARCHAR(20) NOT NULL DEFAULT 'base'
                     CHECK (adapter_type IN ('cohort', 'patient', 'base')),
    prompt_hash      VARCHAR(16) NOT NULL,   -- first 16 hex chars of SHA-256(prompt)
    patient_id       UUID,                   -- NULL when not patient-specific
    latency_ms       INT,
    prompt_tokens    INT,
    completion_tokens INT,
    total_tokens     INT,
    multimodal       BOOLEAN NOT NULL DEFAULT FALSE,
    status           VARCHAR(20) NOT NULL DEFAULT 'ok',
    endpoint_url     TEXT
);
CREATE INDEX IF NOT EXISTS idx_slm_log_called_at   ON slm_inference_log(called_at DESC);
CREATE INDEX IF NOT EXISTS idx_slm_log_adapter_type ON slm_inference_log(adapter_type);
CREATE INDEX IF NOT EXISTS idx_slm_log_patient      ON slm_inference_log(patient_id)
    WHERE patient_id IS NOT NULL;
