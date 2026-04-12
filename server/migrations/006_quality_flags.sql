-- Migration 006: Add quality flag columns to transfer_log
-- Part of F1/F3 validation wiring. Flagged records still get written,
-- but downstream consumers can filter by quality_status.

BEGIN;

ALTER TABLE transfer_log
    ADD COLUMN IF NOT EXISTS quality_flag TEXT,
    ADD COLUMN IF NOT EXISTS quality_status VARCHAR(20) DEFAULT 'ok';

CREATE INDEX IF NOT EXISTS idx_transfer_log_quality_status
    ON transfer_log(quality_status)
    WHERE quality_status != 'ok';

COMMENT ON COLUMN transfer_log.quality_flag IS
    'Human-readable note about validation failures (e.g., plausibility, anchoring)';
COMMENT ON COLUMN transfer_log.quality_status IS
    'Validation outcome: ok | flagged_plausibility | flagged_anchoring | flagged_fhir';

COMMIT;
