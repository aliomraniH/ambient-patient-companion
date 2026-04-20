-- Migration 014: Document the atom_pressure_scores refresh schedule.
--
-- atom_pressure_scores is a MATERIALIZED VIEW (created in migration 010)
-- that powers the provider chase-list ranking. It only updates when
-- something issues `REFRESH MATERIALIZED VIEW`. Replit's managed
-- PostgreSQL does not ship with pg_cron, so the schedule is enforced by
-- the long-running daemon `scripts/refresh_atom_pressure_scores.py`,
-- launched by `start.sh`.
--
-- This migration seeds a row in `system_config` so the schedule is
-- documented in-database. The daemon overwrites
-- `atom_pressure_scores_last_refresh` after every successful refresh;
-- `scripts/refresh_atom_pressure_scores.py --check` reads it to verify
-- freshness for monitoring.
--
-- Run: psql $DATABASE_URL -f server/migrations/014_atom_pressure_refresh_schedule.sql

BEGIN;

INSERT INTO system_config (key, value, updated_at) VALUES
    ('atom_pressure_scores_refresh_interval_hours', '24',     NOW()),
    ('atom_pressure_scores_last_refresh',           'never',  NOW())
ON CONFLICT (key) DO NOTHING;

COMMIT;
