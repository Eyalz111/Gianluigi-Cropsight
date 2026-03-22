-- Phase 8b Migration
-- Date: March 22, 2026
-- Changes:
--   1. tsvector: 'english' → 'simple' for Hebrew support
--   2. Composite indexes on tasks table
--   3. Indexes on token_usage table for cost queries
--   4. scheduler_heartbeats table for health monitoring
--   5. sheet_row column on tasks table for Sheets sync

-- ============================================================
-- 1. tsvector Hebrew fix: switch from 'english' to 'simple'
-- 'simple' tokenizes on whitespace without language-specific stemming.
-- Works for both English and Hebrew. Semantic search (pgvector)
-- handles Hebrew morphological variants.
-- ============================================================

-- embeddings table
ALTER TABLE embeddings DROP COLUMN IF EXISTS chunk_text_tsv;
ALTER TABLE embeddings ADD COLUMN chunk_text_tsv tsvector
    GENERATED ALWAYS AS (to_tsvector('simple', coalesce(chunk_text, ''))) STORED;
CREATE INDEX IF NOT EXISTS idx_embeddings_chunk_text_tsv ON embeddings USING GIN(chunk_text_tsv);

-- decisions table
ALTER TABLE decisions DROP COLUMN IF EXISTS description_tsv;
ALTER TABLE decisions ADD COLUMN description_tsv tsvector
    GENERATED ALWAYS AS (to_tsvector('simple', coalesce(description, ''))) STORED;
CREATE INDEX IF NOT EXISTS idx_decisions_description_tsv ON decisions USING GIN(description_tsv);

-- Update the RPC function to use 'simple' instead of 'english'
-- Drop first to allow return type change
DROP FUNCTION IF EXISTS search_embeddings_fulltext(text, integer, text);

CREATE FUNCTION search_embeddings_fulltext(
    search_query TEXT,
    match_count INT DEFAULT 20,
    source_filter TEXT DEFAULT NULL
)
RETURNS TABLE (
    id UUID,
    source_type TEXT,
    source_id UUID,
    chunk_text TEXT,
    speaker TEXT,
    timestamp_range TEXT,
    metadata JSONB,
    rank FLOAT
)
LANGUAGE plpgsql
AS $$
BEGIN
    RETURN QUERY
    SELECT
        e.id,
        e.source_type,
        e.source_id,
        e.chunk_text,
        e.speaker,
        e.timestamp_range,
        e.metadata,
        ts_rank(e.chunk_text_tsv, plainto_tsquery('simple', search_query))::FLOAT AS rank
    FROM embeddings e
    WHERE
        e.chunk_text_tsv @@ plainto_tsquery('simple', search_query)
        AND (source_filter IS NULL OR e.source_type = source_filter)
    ORDER BY rank DESC
    LIMIT match_count;
END;
$$;

-- ============================================================
-- 2. Composite indexes on tasks table
-- ============================================================
CREATE INDEX IF NOT EXISTS idx_tasks_status_deadline ON tasks(status, deadline);
CREATE INDEX IF NOT EXISTS idx_tasks_status_assignee ON tasks(status, assignee);

-- ============================================================
-- 3. Indexes on token_usage for cost queries
-- ============================================================
CREATE INDEX IF NOT EXISTS idx_token_usage_created_at ON token_usage(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_token_usage_call_site ON token_usage(call_site);

-- ============================================================
-- 4. Scheduler heartbeats table
-- ============================================================
CREATE TABLE IF NOT EXISTS scheduler_heartbeats (
    scheduler_name TEXT PRIMARY KEY,
    last_run_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    status TEXT DEFAULT 'ok',
    details JSONB,
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

-- ============================================================
-- 5. sheet_row column on tasks for Sheets sync
-- ============================================================
ALTER TABLE tasks ADD COLUMN IF NOT EXISTS sheet_row INTEGER;
