-- ============================================================
-- DELIBERATION DATA REQUESTS TABLE
-- Tracks what data each deliberation round requested and
-- whether it was fulfilled (progressive context loading).
-- Run: psql $DATABASE_URL -f migrations/002_data_requests.sql
-- ============================================================

CREATE TABLE IF NOT EXISTS deliberation_data_requests (
    id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    deliberation_id  UUID NOT NULL,
    round_number     INT NOT NULL,
    request_type     VARCHAR(50) NOT NULL,  -- 'lab_trend'|'clinical_note'|'encounter_detail'|'condition_history'|'tier2'
    resource_id      VARCHAR(200),          -- specific Binary.id, encounter_id, or lab test name
    date_from        DATE,
    date_to          DATE,
    reason           TEXT,                  -- agent's stated reason for the request
    fulfilled        BOOLEAN DEFAULT false,
    fulfilled_chars  INT,                   -- how many chars were added to context
    requested_at     TIMESTAMPTZ DEFAULT NOW(),
    fulfilled_at     TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_delib_requests_delib
    ON deliberation_data_requests(deliberation_id, round_number);
