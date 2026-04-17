-- Migration 013: Staleness band configuration.
--
-- Before P-3, generate_previsit_brief hardcoded a 24h cutoff for
-- deliberation reuse, while get_deliberation_results read
-- deliberation_staleness_hours from system_config (default 72). The
-- mismatch meant a 30-hour-old deliberation was flagged PRIOR_SESSION
-- by the deliberation reader but dropped entirely by the brief — the
-- brief then rendered empty patient_questions / recent_deliberation
-- fields while the reader returned rich content.
--
-- This migration introduces two shared thresholds:
--   deliberation_staleness_fresh_hours  — deliberations newer than this
--                                         are treated as TOOL (full trust).
--   deliberation_staleness_recent_days  — deliberations newer than this
--                                         (but older than fresh) carry a
--                                         PRIOR_SESSION provenance tag.
-- Anything older is PRIOR_SESSION_STALE (surfaced with a warning).
--
-- The existing deliberation_staleness_hours key (default 72) remains
-- unchanged — it is still read by get_deliberation_results. New keys
-- are additive.

BEGIN;

INSERT INTO system_config (key, value, updated_at) VALUES
    ('deliberation_staleness_fresh_hours', '24', NOW()),
    ('deliberation_staleness_recent_days', '7',  NOW())
ON CONFLICT (key) DO NOTHING;

COMMIT;
