-- ============================================================================
-- Migration 012: SLM Companion Inference Layer
-- File: server/migrations/012_slm_companion.sql
-- Apply after: 011_behavioral_atoms_v2.sql
-- ----------------------------------------------------------------------------
-- Creates three new tables:
--   slm_adapter_registry  — tracks HF Hub adapters (cohort + per-patient)
--   slm_training_queue    — schedules Modal retraining jobs
--   slm_inference_log     — PHI-safe audit log of every SLM inference call
--
-- Also adds deliberation_outputs.vera_gate column if not present,
-- and deliberation_outputs.synthesis_summary if not present.
-- Both are referenced by the training corpus assembly query.
-- ============================================================================

BEGIN;

-- ─────────────────────────────────────────────────────────────────────────────
-- Guard: add columns to deliberation_outputs if they don't exist
-- ─────────────────────────────────────────────────────────────────────────────
ALTER TABLE deliberation_outputs
  ADD COLUMN IF NOT EXISTS vera_gate          TEXT,         -- 'allow' | 'flag' | 'block' | NULL (audit mode)
  ADD COLUMN IF NOT EXISTS synthesis_summary  TEXT,         -- SYNTHESIS-arbitrated companion response
  ADD COLUMN IF NOT EXISTS clinical_findings  TEXT,         -- raw clinical findings from Phase 3
  ADD COLUMN IF NOT EXISTS patient_id         UUID REFERENCES patients(id) ON DELETE CASCADE,
  ADD COLUMN IF NOT EXISTS convergence_score  FLOAT;        -- per-output score; mirrors deliberations.convergence_score

CREATE INDEX IF NOT EXISTS idx_delibout_patient
  ON deliberation_outputs (patient_id, created_at DESC)
  WHERE patient_id IS NOT NULL;

-- ─────────────────────────────────────────────────────────────────────────────
-- Table 1: slm_adapter_registry
-- ─────────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS slm_adapter_registry (
  id                   BIGSERIAL       PRIMARY KEY,

  -- Scope: NULL patient_id = cohort adapter; non-NULL = per-patient adapter
  patient_id           UUID            REFERENCES patients(id) ON DELETE CASCADE,

  adapter_type         TEXT            NOT NULL
                                       CHECK (adapter_type IN ('cohort','patient')),
  cohort_name          TEXT,           -- 'diabetes_bh' | 'diabetes' | NULL for patient adapters

  -- HF Hub location
  hf_repo              TEXT            NOT NULL,   -- e.g. 'yourorg/cohort-diabetes-bh-adapter'
  hf_revision          TEXT            NOT NULL DEFAULT 'main',

  -- Lifecycle
  status               TEXT            NOT NULL DEFAULT 'active'
                                       CHECK (status IN (
                                         'active','training','pending_review',
                                         'flagged','superseded','rolled_back'
                                       )),
  previous_revision    TEXT,           -- stores last revision before promotion (for rollback)

  -- Training metadata
  training_examples    INT,
  base_model           TEXT            NOT NULL DEFAULT 'Qwen/Qwen2.5-3B-Instruct',
  lora_rank            INT             NOT NULL DEFAULT 16,
  trained_at           TIMESTAMPTZ,
  eval_score           REAL,           -- optional post-training eval metric (0–1)

  -- Promotion audit
  promoted_at          TIMESTAMPTZ,
  promoted_by          TEXT,           -- 'system' | 'claude_mcp' | 'claude_mcp_rollback'

  -- Flagging
  flagged_reason       TEXT,
  flagged_at           TIMESTAMPTZ,

  created_at           TIMESTAMPTZ     DEFAULT NOW()
);

-- One active adapter per patient per type
CREATE UNIQUE INDEX IF NOT EXISTS idx_slm_adapter_patient_active
  ON slm_adapter_registry (patient_id, adapter_type)
  WHERE status = 'active' AND patient_id IS NOT NULL;

-- One active cohort adapter per cohort name
CREATE UNIQUE INDEX IF NOT EXISTS idx_slm_adapter_cohort_active
  ON slm_adapter_registry (cohort_name)
  WHERE adapter_type = 'cohort' AND status = 'active' AND cohort_name IS NOT NULL;

-- General lookup indexes
CREATE INDEX IF NOT EXISTS idx_slm_adapter_cohort
  ON slm_adapter_registry (cohort_name, status);
CREATE INDEX IF NOT EXISTS idx_slm_adapter_patient
  ON slm_adapter_registry (patient_id, status);
CREATE INDEX IF NOT EXISTS idx_slm_adapter_hf_repo
  ON slm_adapter_registry (hf_repo);


-- ─────────────────────────────────────────────────────────────────────────────
-- Table 2: slm_training_queue
-- ─────────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS slm_training_queue (
  id                   BIGSERIAL       PRIMARY KEY,

  -- Scope: either patient or cohort (not both)
  patient_id           UUID            REFERENCES patients(id) ON DELETE SET NULL,
  cohort_name          TEXT,

  -- Training job details
  adapter_name         TEXT            NOT NULL,   -- target HF Hub repo
  status               TEXT            NOT NULL DEFAULT 'pending'
                                       CHECK (status IN (
                                         'pending','submitted','completed','failed'
                                       )),
  priority             TEXT            NOT NULL DEFAULT 'normal'
                                       CHECK (priority IN ('urgent','normal','low')),
  reason               TEXT,

  -- External job tracking
  training_corpus_url  TEXT,           -- signed URL to JSONL (for patient-specific jobs)
  modal_job_id         TEXT,

  -- Timing
  scheduled_for        TIMESTAMPTZ     NOT NULL DEFAULT NOW(),
  submitted_at         TIMESTAMPTZ,
  completed_at         TIMESTAMPTZ,

  -- Error handling
  failed_reason        TEXT,

  -- Audit
  created_at           TIMESTAMPTZ     DEFAULT NOW(),
  created_by           TEXT            NOT NULL DEFAULT 'system'
                                       CHECK (created_by IN (
                                         'system','claude_mcp','watcher','scheduled_task'
                                       ))
);

-- Hot path: pick up pending jobs ordered by priority + scheduled time
CREATE INDEX IF NOT EXISTS idx_slm_queue_pending
  ON slm_training_queue (priority, scheduled_for)
  WHERE status = 'pending';

-- Poll submitted jobs
CREATE INDEX IF NOT EXISTS idx_slm_queue_submitted
  ON slm_training_queue (submitted_at)
  WHERE status = 'submitted';

-- History lookup
CREATE INDEX IF NOT EXISTS idx_slm_queue_cohort
  ON slm_training_queue (cohort_name, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_slm_queue_patient
  ON slm_training_queue (patient_id, created_at DESC);


-- ─────────────────────────────────────────────────────────────────────────────
-- Table 3: slm_inference_log
-- ─────────────────────────────────────────────────────────────────────────────
-- PHI SAFETY: raw prompts are NEVER stored here — only prompt_hash (sha256 prefix).
-- Patient identifiers are stored only as UUIDs (never names, DOBs, MRNs).
-- ─────────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS slm_inference_log (
  id                   BIGSERIAL       PRIMARY KEY,

  -- Who
  patient_id           UUID            REFERENCES patients(id) ON DELETE SET NULL,

  -- Which adapter served this request
  adapter_used         TEXT,           -- HF repo, or 'tgi' for base model
  adapter_type         TEXT            CHECK (adapter_type IN ('cohort','patient','base')),

  -- Prompt fingerprint (never the raw text)
  prompt_hash          TEXT            NOT NULL,   -- sha256(prompt)[:24]

  -- Token counts (no prompt text)
  prompt_tokens        INT,
  response_tokens      INT,

  -- Performance
  latency_ms           INT,
  endpoint_status      TEXT            CHECK (endpoint_status IN (
                                         'success','error','timeout','cold_start'
                                       )),
  error_message        TEXT,

  -- Session linkage (ties to mcp_call_log.session_id)
  mcp_session_id       TEXT,

  called_at            TIMESTAMPTZ     DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_slm_log_patient
  ON slm_inference_log (patient_id, called_at DESC);
CREATE INDEX IF NOT EXISTS idx_slm_log_called
  ON slm_inference_log (called_at DESC);
CREATE INDEX IF NOT EXISTS idx_slm_log_adapter
  ON slm_inference_log (adapter_used, called_at DESC);


-- ─────────────────────────────────────────────────────────────────────────────
-- Seed: initial cohort adapter row
-- (Update hf_repo to your actual HF org/repo before applying)
-- ─────────────────────────────────────────────────────────────────────────────
INSERT INTO slm_adapter_registry
  (adapter_type, cohort_name, hf_repo, hf_revision, status,
   base_model, lora_rank, training_examples)
VALUES
  ('cohort', 'diabetes_bh',
   'yourorg/cohort-diabetes-bh-adapter',  -- ← update to your HF org
   'main', 'active',
   'Qwen/Qwen2.5-3B-Instruct', 16, 0)
ON CONFLICT DO NOTHING;


-- ─────────────────────────────────────────────────────────────────────────────
-- View: active adapter per patient (useful for debugging)
-- ─────────────────────────────────────────────────────────────────────────────
CREATE OR REPLACE VIEW v_patient_active_adapters AS
SELECT
  p.id                            AS patient_id,
  p.first_name,
  p.last_name,
  COALESCE(pa.hf_repo, ca.hf_repo, 'tgi')   AS effective_adapter,
  COALESCE(pa.adapter_type, ca.adapter_type, 'base') AS adapter_type,
  COALESCE(pa.hf_repo, ca.hf_repo) IS NOT NULL        AS has_adapter,
  pa.hf_repo                      AS patient_adapter,
  ca.hf_repo                      AS cohort_adapter,
  ca.cohort_name
FROM patients p
LEFT JOIN slm_adapter_registry pa
  ON pa.patient_id = p.id AND pa.adapter_type = 'patient' AND pa.status = 'active'
LEFT JOIN patient_conditions pc
  ON pc.patient_id = p.id AND pc.status = 'active' AND pc.code LIKE 'E11%'
LEFT JOIN slm_adapter_registry ca
  ON ca.cohort_name = 'diabetes_bh'
  AND ca.adapter_type = 'cohort'
  AND ca.status = 'active'
  AND pc.patient_id IS NOT NULL;

COMMIT;

-- ─────────────────────────────────────────────────────────────────────────────
-- Verification queries (run after applying to confirm success):
-- ─────────────────────────────────────────────────────────────────────────────
-- SELECT table_name FROM information_schema.tables
--   WHERE table_schema='public' AND table_name LIKE 'slm_%';
-- Expected: slm_adapter_registry, slm_training_queue, slm_inference_log
--
-- SELECT * FROM slm_adapter_registry;
-- Expected: 1 row — cohort-diabetes-bh-adapter, status=active
--
-- SELECT column_name FROM information_schema.columns
--   WHERE table_name='deliberation_outputs' AND column_name IN
--   ('vera_gate','synthesis_summary','clinical_findings');
-- Expected: 3 rows
