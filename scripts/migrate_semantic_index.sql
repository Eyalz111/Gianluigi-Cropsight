-- =============================================================================
-- migrate_semantic_index.sql  (2026-07-13)
-- Semantic Index for Decisions & Topics — Phase 0.
--
-- Add the `sensitivity` column to the match_embeddings RPC output so retrieval
-- can tier-filter DIRECTLY. Before this, the RPC returned no sensitivity, so
-- meeting consumers back-resolved tier from the source meeting, and the decision
-- semantic branch of find_relevant_decisions applied NO tier filter at all — a
-- latent leak the moment decisions/topics get embedded.
--
-- Run this in Supabase BEFORE deploying the semantic-index code.
--
-- Postgres cannot change a function's RETURNS TABLE via CREATE OR REPLACE, so we
-- DROP then CREATE, wrapped in a transaction so there is no window where the
-- function is missing. The `embeddings.sensitivity` column already exists
-- (added by migrate_v3_sensitivity_rename.sql); this only surfaces it.
-- =============================================================================

BEGIN;

DROP FUNCTION IF EXISTS match_embeddings(VECTOR(1536), FLOAT, INT, TEXT);

CREATE FUNCTION match_embeddings(
    query_embedding VECTOR(1536),
    match_threshold FLOAT DEFAULT 0.7,
    match_count INT DEFAULT 10,
    filter_source_type TEXT DEFAULT NULL
)
RETURNS TABLE (
    id UUID,
    source_type TEXT,
    source_id UUID,
    chunk_text TEXT,
    chunk_index INT,
    speaker TEXT,
    timestamp_range TEXT,
    metadata JSONB,
    sensitivity TEXT,
    similarity FLOAT
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
        e.chunk_index,
        e.speaker,
        e.timestamp_range,
        e.metadata,
        e.sensitivity,
        1 - (e.embedding <=> query_embedding) AS similarity
    FROM embeddings e
    WHERE
        (filter_source_type IS NULL OR e.source_type = filter_source_type)
        AND 1 - (e.embedding <=> query_embedding) > match_threshold
    ORDER BY e.embedding <=> query_embedding
    LIMIT match_count;
END;
$$;

COMMIT;
