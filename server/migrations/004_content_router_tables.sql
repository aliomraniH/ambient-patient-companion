-- clinical_notes: text extracted from Binary/Observation resources, safe for context
CREATE TABLE IF NOT EXISTS clinical_notes (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    patient_id      UUID NOT NULL REFERENCES patients(id),
    binary_id       VARCHAR(200),
    content_type    VARCHAR(100),
    note_text       TEXT NOT NULL,
    note_text_raw   TEXT,
    note_type       VARCHAR(200),
    note_date       TIMESTAMPTZ,
    author          VARCHAR(200),
    encounter_id    VARCHAR(200),
    source          VARCHAR(50) DEFAULT 'healthex',
    ingested_at     TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(patient_id, binary_id)
);

-- media_references: URL pointers to non-text assets (images, PDFs, ECG waveforms)
CREATE TABLE IF NOT EXISTS media_references (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    patient_id      UUID NOT NULL REFERENCES patients(id),
    resource_id     VARCHAR(200),
    resource_type   VARCHAR(50),
    content_type    VARCHAR(100),
    reference_url   TEXT,
    doc_ref_id      VARCHAR(200),
    doc_type        VARCHAR(200),
    author          VARCHAR(200),
    doc_date        TIMESTAMPTZ,
    attachments     JSONB,
    note            VARCHAR(200),
    source          VARCHAR(50) DEFAULT 'healthex',
    ingested_at     TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(patient_id, resource_id)
);

-- Index: context compiler fetches recent notes ordered by date
CREATE INDEX IF NOT EXISTS idx_clinical_notes_patient_date
    ON clinical_notes(patient_id, note_date DESC NULLS LAST);

-- Index: media inventory lookup by patient + content_type
CREATE INDEX IF NOT EXISTS idx_media_refs_patient
    ON media_references(patient_id, content_type);
