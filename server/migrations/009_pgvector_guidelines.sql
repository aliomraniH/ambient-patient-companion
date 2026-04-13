-- Migration 006: pgvector guidelines retrieval schema (Phase 2 dependency).
--
-- Replaces the _VectorStorePlaceholder in server/mcp_server.py with a real
-- backing table for semantic + BM25 hybrid search over clinical guidelines.
--
-- Prerequisites:
--   - PostgreSQL 15+ (for pgvector + text search)
--   - pgvector extension available
--   - MedCPT-Article-Encoder embeddings (768-dim) produced by the
--     sibling module server/guidelines/ingestion/ (chunk_guidelines.py
--     produces rows; embedding runner fills in `embedding`).
--
-- After this migration is applied and the `guidelines` table is populated:
--   1. Update server/mcp_server.py — replace _VectorStorePlaceholder with
--      a real store that runs a hybrid query (ivfflat cosine + BM25).
--   2. Swap the stub `search_guidelines` MCP tool (Tier 3.5) with the real
--      implementation.
--
-- Safety: CREATE EXTENSION is idempotent; CREATE TABLE IF NOT EXISTS is safe.

BEGIN;

CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pg_trgm;

-- ============================================================
-- guidelines — one row per recommendation-level chunk
-- ============================================================
CREATE TABLE IF NOT EXISTS guidelines (
    id                       UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    recommendation_id        TEXT UNIQUE,
    guideline_source         TEXT,                    -- 'ADA' | 'USPSTF' | 'ACC' | 'AHA'
    version                  TEXT,
    chapter                  TEXT,
    section                  TEXT,
    text                     TEXT,
    evidence_grade           CHAR(1),                 -- 'A' | 'B' | 'C' | 'D' | 'I'
    recommendation_strength  TEXT,
    patient_population       TEXT[],
    contraindications        TEXT[],
    medications_mentioned    TEXT[],
    last_reviewed            DATE,
    is_current               BOOLEAN DEFAULT true,
    embedding                VECTOR(768),             -- ncbi/MedCPT-Article-Encoder
    bm25_tokens              TSVECTOR,
    created_at               TIMESTAMPTZ DEFAULT NOW()
);

-- ANN index for cosine similarity over embeddings.
CREATE INDEX IF NOT EXISTS idx_guidelines_embedding_ivfflat
    ON guidelines USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100);

-- Full-text index for BM25 keyword side of the hybrid retriever.
CREATE INDEX IF NOT EXISTS idx_guidelines_bm25
    ON guidelines USING GIN (bm25_tokens);

-- Metadata filter indexes.
CREATE INDEX IF NOT EXISTS idx_guidelines_source
    ON guidelines(guideline_source) WHERE is_current;
CREATE INDEX IF NOT EXISTS idx_guidelines_grade
    ON guidelines(evidence_grade) WHERE is_current;

-- Auto-update bm25_tokens from text (single source of truth).
CREATE OR REPLACE FUNCTION guidelines_bm25_tokens_trg() RETURNS trigger AS $$
BEGIN
    NEW.bm25_tokens := to_tsvector('english', COALESCE(NEW.text, ''));
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS guidelines_bm25_tokens_trigger ON guidelines;
CREATE TRIGGER guidelines_bm25_tokens_trigger
    BEFORE INSERT OR UPDATE OF text ON guidelines
    FOR EACH ROW EXECUTE FUNCTION guidelines_bm25_tokens_trg();

COMMIT;
