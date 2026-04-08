-- ============================================================
-- DELIBERATION DATA REQUESTS TABLE
-- Tracks what data each deliberation round requested and
-- whether it was fulfilled (progressive context loading).
-- Run: psql $DATABASE_URL -f server/deliberation/migrations/002_data_requests.sql
-- ============================================================

CREATE TABLE IF NOT EXISTS deliberation_data_requests (
    id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    deliberation_id  UUID NOT NULL,
    round_number     INT NOT NULL,
    request_type     VARCHAR(50) NOT NULL,
    resource_id      VARCHAR(200),
    date_from        DATE,
    date_to          DATE,
    reason           TEXT,
    fulfilled        BOOLEAN DEFAULT false,
    fulfilled_chars  INT,
    requested_at     TIMESTAMPTZ DEFAULT NOW(),
    fulfilled_at     TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_delib_requests_delib
    ON deliberation_data_requests(deliberation_id, round_number);
