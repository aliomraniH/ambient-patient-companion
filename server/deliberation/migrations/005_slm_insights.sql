-- Migration 005: Add source_type column to clinical_notes so SLM-generated
-- insights can be stored and automatically picked up by context_compiler.py
-- when assembling deliberation context.

ALTER TABLE clinical_notes ADD COLUMN IF NOT EXISTS source_type VARCHAR(50) DEFAULT 'healthex';
UPDATE clinical_notes SET source_type = 'healthex' WHERE source_type IS NULL;
CREATE INDEX IF NOT EXISTS idx_clinical_notes_source_type
    ON clinical_notes(patient_id, source_type, note_date DESC NULLS LAST);
