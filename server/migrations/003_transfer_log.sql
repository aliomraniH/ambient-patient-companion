-- Migration 003: transfer_log table
-- Per-record audit trail for every data transfer through the HealthEx ingest pipeline.
-- One row per clinical record, tracks all state transitions with timestamps.

CREATE TABLE IF NOT EXISTS transfer_log (
    -- Primary key — set to TransferRecord.transfer_id from Python so we can UPDATE by it
    id                  UUID PRIMARY KEY,

    -- Patient and resource context
    patient_id          UUID NOT NULL REFERENCES patients(id),
    resource_type       TEXT NOT NULL,   -- labs | conditions | encounters | medications
    source              TEXT NOT NULL,   -- healthex | fhir | synthea | manual

    -- Record identity (natural key — no PHI values, only identifiers)
    record_key          TEXT NOT NULL,   -- e.g. "HbA1c::2025-07-11" or "Prediabetes::2017-04-25"
    record_hash         TEXT,            -- 16-char SHA-256 prefix of sanitized row JSON
    loinc_code          TEXT,            -- for labs: LOINC code if available
    icd10_code          TEXT,            -- for conditions: ICD-10 code if available
    encounter_id        TEXT,            -- for encounters: encounter ID if available

    -- Batch context
    batch_id            UUID,            -- all records from one ingest call share a batch_id
    batch_sequence      INTEGER,         -- position within batch (1-indexed)
    batch_total         INTEGER,         -- total records in this batch
    chunk_id            UUID,            -- for large payloads split into chunks
    chunk_sequence      INTEGER,         -- which chunk within the batch
    chunk_total         INTEGER,

    -- Transfer strategy
    strategy            TEXT NOT NULL DEFAULT 'single',
    -- single | chunked_small | chunked_medium | chunked_large | llm_fallback
    format_detected     TEXT,
    -- plain_text_summary | compressed_table | flat_fhir | fhir_bundle | json_dict | unknown

    -- Timestamp arc — every state transition logged
    planned_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    extracted_at        TIMESTAMPTZ,     -- parser produced the row
    sanitized_at        TIMESTAMPTZ,     -- blob escaping + PHI check applied
    written_at          TIMESTAMPTZ,     -- INSERT executed
    verified_at         TIMESTAMPTZ,     -- SELECT COUNT confirmed write
    failed_at           TIMESTAMPTZ,

    -- Status: planned → sanitized → written → verified  (or → failed at any stage)
    status              TEXT NOT NULL DEFAULT 'planned',
    error_stage         TEXT,            -- which stage failed
    error_message       TEXT,            -- truncated to 500 chars

    -- Metadata
    payload_size_bytes  INTEGER,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT valid_transfer_status CHECK (
        status IN ('planned', 'sanitized', 'written', 'verified',
                   'written_unverified', 'failed')
    )
);

CREATE INDEX IF NOT EXISTS idx_transfer_log_patient
    ON transfer_log (patient_id, resource_type);
CREATE INDEX IF NOT EXISTS idx_transfer_log_batch
    ON transfer_log (batch_id);
CREATE INDEX IF NOT EXISTS idx_transfer_log_status
    ON transfer_log (status, patient_id);
CREATE INDEX IF NOT EXISTS idx_transfer_log_planned_at
    ON transfer_log (planned_at DESC);
