-- Migration 007: Provenance Audit Log
-- Shared table used by verify_output_provenance on all three MCP servers.
-- Metadata only — no patient content, no raw PHI.
-- Run: psql $DATABASE_URL -f server/migrations/007_provenance_audit_log.sql

BEGIN;

CREATE TABLE IF NOT EXISTS provenance_audit_log (
    id                   UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    provenance_report_id UUID        NOT NULL,
    deliberation_id      UUID,
    output_id            TEXT,
    -- SHA-256(mrn) only — never raw PHI.
    patient_mrn_hash     TEXT,
    -- Which server generated the assembled output.
    source_server        TEXT        CHECK (source_server IN (
                             'ambient-clinical-intelligence',
                             'ambient-skills-companion',
                             'ambient-ingestion',
                             'unknown'
                         )),
    assembled_by         TEXT,
    gate_decision        TEXT        NOT NULL CHECK (gate_decision IN (
                             'APPROVED',
                             'APPROVED_WITH_WARNINGS',
                             'BLOCKED'
                         )),
    block_reason         TEXT,
    total_sections       INT         DEFAULT 0,
    blocked_count        INT         DEFAULT 0,
    warned_count         INT         DEFAULT 0,
    approved_count       INT         DEFAULT 0,
    pending_tools_needed JSONB       DEFAULT '[]'::jsonb,
    -- Section metadata only — no content, no PHI.
    section_results      JSONB       DEFAULT '[]'::jsonb,
    assessed_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    strict_mode          BOOLEAN     DEFAULT TRUE
);

CREATE INDEX IF NOT EXISTS idx_prov_deliberation
    ON provenance_audit_log (deliberation_id);
CREATE INDEX IF NOT EXISTS idx_prov_gate
    ON provenance_audit_log (gate_decision, assessed_at DESC);
CREATE INDEX IF NOT EXISTS idx_prov_server
    ON provenance_audit_log (source_server, assessed_at DESC);
CREATE INDEX IF NOT EXISTS idx_prov_assessed
    ON provenance_audit_log (assessed_at DESC);

COMMENT ON TABLE provenance_audit_log IS
    'Audit trail for verify_output_provenance. '
    'Metadata only — no patient content, no raw PHI anywhere. '
    'patient_mrn_hash = SHA-256(mrn). '
    'Populated by all three MCP servers via '
    'shared/provenance/audit_writer.py.';

COMMIT;
