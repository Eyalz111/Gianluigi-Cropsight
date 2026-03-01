-- Gianluigi Database Schema
-- Run this script in Supabase SQL Editor to create all tables
--
-- Prerequisites:
-- 1. Create a new Supabase project in EU region (Frankfurt)
-- 2. Enable the pgvector extension (see below)
--
-- This schema matches Section 3 of GIANLUIGI_PROJECT_PLAN.md

-- =============================================================================
-- Enable pgvector extension for embeddings
-- =============================================================================

CREATE EXTENSION IF NOT EXISTS vector;

-- =============================================================================
-- Core Tables
-- =============================================================================

-- Meetings table: Stores processed meeting records
CREATE TABLE IF NOT EXISTS meetings (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    date TIMESTAMPTZ NOT NULL,
    title TEXT NOT NULL,
    participants TEXT[] NOT NULL,
    duration_minutes INTEGER,
    raw_transcript TEXT,
    summary TEXT,
    sensitivity TEXT DEFAULT 'normal',  -- 'normal', 'sensitive', 'legal'
    source_file_path TEXT,              -- Google Drive path to original Tactiq export
    approval_status TEXT DEFAULT 'pending',  -- 'pending', 'approved', 'rejected'
    approved_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

-- Add index for date-based queries
CREATE INDEX IF NOT EXISTS idx_meetings_date ON meetings(date DESC);
CREATE INDEX IF NOT EXISTS idx_meetings_approval_status ON meetings(approval_status);


-- Decisions table: Key decisions extracted from meetings
CREATE TABLE IF NOT EXISTS decisions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    meeting_id UUID REFERENCES meetings(id) ON DELETE CASCADE,
    description TEXT NOT NULL,
    context TEXT,                        -- surrounding discussion context
    participants_involved TEXT[],
    transcript_timestamp TEXT,           -- source citation, e.g., "43:28"
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_decisions_meeting_id ON decisions(meeting_id);


-- Tasks table: Action items from meetings or manually created
CREATE TABLE IF NOT EXISTS tasks (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    meeting_id UUID REFERENCES meetings(id) ON DELETE SET NULL,  -- nullable for manual tasks
    title TEXT NOT NULL,
    assignee TEXT NOT NULL,
    deadline DATE,
    status TEXT DEFAULT 'pending',       -- 'pending', 'in_progress', 'done', 'overdue'
    priority TEXT DEFAULT 'M',           -- 'H', 'M', 'L'
    transcript_timestamp TEXT,           -- source citation
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_tasks_assignee ON tasks(assignee);
CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);
CREATE INDEX IF NOT EXISTS idx_tasks_deadline ON tasks(deadline);

-- v0.2.1: Add category column to tasks
ALTER TABLE tasks ADD COLUMN IF NOT EXISTS category TEXT;
CREATE INDEX IF NOT EXISTS idx_tasks_category ON tasks(category);


-- Follow-up meetings: Scheduled follow-ups identified from meetings
CREATE TABLE IF NOT EXISTS follow_up_meetings (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    source_meeting_id UUID REFERENCES meetings(id) ON DELETE CASCADE,
    title TEXT NOT NULL,
    proposed_date TIMESTAMPTZ,
    led_by TEXT NOT NULL,
    participants TEXT[],
    agenda_items TEXT[],
    prep_needed TEXT,                    -- what needs to happen before this meeting
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_follow_up_meetings_source ON follow_up_meetings(source_meeting_id);


-- Documents table: Ingested documents for knowledge base
CREATE TABLE IF NOT EXISTS documents (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    title TEXT NOT NULL,
    source TEXT,                         -- 'upload', 'email', 'drive'
    file_type TEXT,
    summary TEXT,
    drive_path TEXT,
    ingested_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_documents_source ON documents(source);


-- Open questions: Unresolved issues from meetings
CREATE TABLE IF NOT EXISTS open_questions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    meeting_id UUID REFERENCES meetings(id) ON DELETE CASCADE,
    question TEXT NOT NULL,
    raised_by TEXT,
    status TEXT DEFAULT 'open',          -- 'open', 'resolved'
    resolved_in_meeting_id UUID REFERENCES meetings(id) ON DELETE SET NULL,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_open_questions_status ON open_questions(status);
CREATE INDEX IF NOT EXISTS idx_open_questions_meeting_id ON open_questions(meeting_id);


-- =============================================================================
-- Vector Embeddings (pgvector)
-- =============================================================================

-- Embeddings table: Stores text chunks with their vector embeddings
CREATE TABLE IF NOT EXISTS embeddings (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    source_type TEXT NOT NULL,           -- 'meeting', 'document'
    source_id UUID NOT NULL,
    chunk_text TEXT NOT NULL,
    chunk_index INTEGER,
    speaker TEXT,                        -- who said this (for meeting chunks)
    timestamp_range TEXT,                -- e.g., "43:00-45:30"
    embedding VECTOR(1536),              -- dimension for text-embedding-3-small
    metadata JSONB,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

-- Create index for vector similarity search
CREATE INDEX IF NOT EXISTS idx_embeddings_source ON embeddings(source_type, source_id);
CREATE INDEX IF NOT EXISTS idx_embeddings_vector ON embeddings
    USING ivfflat (embedding vector_cosine_ops)
    WITH (lists = 100);


-- =============================================================================
-- Audit Log
-- =============================================================================

-- Audit log: Tracks all Gianluigi actions for transparency
CREATE TABLE IF NOT EXISTS audit_log (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    action TEXT NOT NULL,                -- 'meeting_processed', 'task_created', etc.
    details JSONB,
    triggered_by TEXT,                   -- 'auto', 'eyal', 'roye', 'paolo', 'yoram'
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_audit_log_action ON audit_log(action);
CREATE INDEX IF NOT EXISTS idx_audit_log_created_at ON audit_log(created_at DESC);


-- =============================================================================
-- Helper Functions
-- =============================================================================

-- Function to automatically update updated_at timestamp
CREATE OR REPLACE FUNCTION update_updated_at_column()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ language 'plpgsql';

-- Trigger to auto-update tasks.updated_at
DROP TRIGGER IF EXISTS update_tasks_updated_at ON tasks;
CREATE TRIGGER update_tasks_updated_at
    BEFORE UPDATE ON tasks
    FOR EACH ROW
    EXECUTE FUNCTION update_updated_at_column();


-- =============================================================================
-- Row Level Security (RLS) - Optional
-- =============================================================================

-- Enable RLS on all tables (uncomment when ready to implement auth)
-- ALTER TABLE meetings ENABLE ROW LEVEL SECURITY;
-- ALTER TABLE decisions ENABLE ROW LEVEL SECURITY;
-- ALTER TABLE tasks ENABLE ROW LEVEL SECURITY;
-- ALTER TABLE follow_up_meetings ENABLE ROW LEVEL SECURITY;
-- ALTER TABLE documents ENABLE ROW LEVEL SECURITY;
-- ALTER TABLE open_questions ENABLE ROW LEVEL SECURITY;
-- ALTER TABLE embeddings ENABLE ROW LEVEL SECURITY;
-- ALTER TABLE audit_log ENABLE ROW LEVEL SECURITY;


-- =============================================================================
-- Vector Search RPC Function
-- =============================================================================

-- Function for semantic similarity search
-- Called via: supabase.rpc('match_embeddings', {...})
CREATE OR REPLACE FUNCTION match_embeddings(
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
        1 - (e.embedding <=> query_embedding) AS similarity
    FROM embeddings e
    WHERE
        (filter_source_type IS NULL OR e.source_type = filter_source_type)
        AND 1 - (e.embedding <=> query_embedding) > match_threshold
    ORDER BY e.embedding <=> query_embedding
    LIMIT match_count;
END;
$$;


-- =============================================================================
-- Sample Queries for Testing
-- =============================================================================

-- Semantic search for similar content:
-- SELECT chunk_text, 1 - (embedding <=> '[your_query_vector]') AS similarity
-- FROM embeddings
-- WHERE source_type = 'meeting'
-- ORDER BY embedding <=> '[your_query_vector]'
-- LIMIT 10;

-- Get all open tasks for a user:
-- SELECT * FROM tasks
-- WHERE assignee = 'roye'
--   AND status IN ('pending', 'in_progress')
-- ORDER BY deadline ASC;

-- Get recent meetings with their decisions:
-- SELECT m.title, m.date, d.description
-- FROM meetings m
-- LEFT JOIN decisions d ON d.meeting_id = m.id
-- WHERE m.date > NOW() - INTERVAL '30 days'
-- ORDER BY m.date DESC;


-- =============================================================================
-- Full-Text Search (v0.2 — RAG Foundation Upgrade)
-- =============================================================================

-- Add generated tsvector columns for full-text search on embeddings.chunk_text
ALTER TABLE embeddings ADD COLUMN IF NOT EXISTS chunk_text_tsv tsvector
    GENERATED ALWAYS AS (to_tsvector('english', coalesce(chunk_text, ''))) STORED;
CREATE INDEX IF NOT EXISTS idx_embeddings_chunk_text_tsv ON embeddings USING GIN(chunk_text_tsv);

-- Add generated tsvector column for full-text search on decisions.description
ALTER TABLE decisions ADD COLUMN IF NOT EXISTS description_tsv tsvector
    GENERATED ALWAYS AS (to_tsvector('english', coalesce(description, ''))) STORED;
CREATE INDEX IF NOT EXISTS idx_decisions_description_tsv ON decisions USING GIN(description_tsv);


-- =============================================================================
-- Task Mentions (v0.3 — Cross-Reference Intelligence)
-- =============================================================================

-- Track when tasks are mentioned across different meetings.
-- Used for task deduplication, status inference, and cross-meeting tracking.
CREATE TABLE IF NOT EXISTS task_mentions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    task_id UUID REFERENCES tasks(id) ON DELETE CASCADE,
    meeting_id UUID REFERENCES meetings(id) ON DELETE CASCADE,
    mention_text TEXT NOT NULL,
    implied_status TEXT,              -- 'done', 'in_progress', or NULL
    confidence TEXT DEFAULT 'medium', -- 'high', 'medium', 'low'
    evidence TEXT,                    -- exact quote from transcript
    transcript_timestamp TEXT,        -- e.g., "23:45"
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_task_mentions_task ON task_mentions(task_id);
CREATE INDEX IF NOT EXISTS idx_task_mentions_meeting ON task_mentions(meeting_id);


-- =============================================================================
-- Entity Registry (v0.3 Tier 2 — Stakeholder Context)
-- =============================================================================

-- Canonical entity records: people, organizations, projects, etc.
CREATE TABLE IF NOT EXISTS entities (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    canonical_name TEXT NOT NULL,
    entity_type TEXT NOT NULL,  -- person, organization, project, technology, location
    aliases TEXT[] DEFAULT '{}',
    metadata JSONB DEFAULT '{}',
    first_seen_meeting_id UUID REFERENCES meetings(id) ON DELETE SET NULL,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_entities_canonical_name ON entities(canonical_name);
CREATE INDEX IF NOT EXISTS idx_entities_entity_type ON entities(entity_type);
CREATE INDEX IF NOT EXISTS idx_entities_aliases ON entities USING GIN(aliases);

-- Track when/where entities are mentioned across meetings.
CREATE TABLE IF NOT EXISTS entity_mentions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    entity_id UUID REFERENCES entities(id) ON DELETE CASCADE,
    meeting_id UUID REFERENCES meetings(id) ON DELETE CASCADE,
    mention_text TEXT NOT NULL,
    context TEXT,
    speaker TEXT,
    sentiment TEXT,  -- positive, neutral, negative, mixed
    transcript_timestamp TEXT,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_entity_mentions_entity ON entity_mentions(entity_id);
CREATE INDEX IF NOT EXISTS idx_entity_mentions_meeting ON entity_mentions(meeting_id);


-- =============================================================================
-- Commitment Tracking (v0.3 Tier 2 — Verbal Promises)
-- =============================================================================

-- Track verbal commitments ("I'll send that by Friday") across meetings.
CREATE TABLE IF NOT EXISTS commitments (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    meeting_id UUID REFERENCES meetings(id) ON DELETE CASCADE,
    speaker TEXT NOT NULL,
    commitment_text TEXT NOT NULL,
    context TEXT,
    implied_deadline TEXT,
    status TEXT DEFAULT 'open',  -- open, fulfilled, overdue, withdrawn
    fulfilled_in_meeting_id UUID REFERENCES meetings(id) ON DELETE SET NULL,
    evidence TEXT,
    linked_task_id UUID REFERENCES tasks(id) ON DELETE SET NULL,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_commitments_meeting ON commitments(meeting_id);
CREATE INDEX IF NOT EXISTS idx_commitments_speaker ON commitments(speaker);
CREATE INDEX IF NOT EXISTS idx_commitments_status ON commitments(status);

-- Auto-update commitments.updated_at on changes
DROP TRIGGER IF EXISTS update_commitments_updated_at ON commitments;
CREATE TRIGGER update_commitments_updated_at
    BEFORE UPDATE ON commitments
    FOR EACH ROW
    EXECUTE FUNCTION update_updated_at_column();


-- =============================================================================
-- Pending Approvals (v0.4 — Persistent Approval State)
-- =============================================================================

-- Persist approval state so approve/reject survives process restarts.
CREATE TABLE IF NOT EXISTS pending_approvals (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    approval_id TEXT UNIQUE NOT NULL,       -- meeting UUID or "prep-YYYY-MM-DD"
    content_type TEXT NOT NULL,             -- meeting_summary / meeting_prep / weekly_digest
    content JSONB NOT NULL,                 -- full content dict
    status TEXT DEFAULT 'pending',          -- pending / approved / rejected / editing
    auto_publish_at TIMESTAMPTZ,            -- NULL if manual mode
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_pending_approvals_status ON pending_approvals(status);

-- Auto-update pending_approvals.updated_at on changes
DROP TRIGGER IF EXISTS update_pending_approvals_updated_at ON pending_approvals;
CREATE TRIGGER update_pending_approvals_updated_at
    BEFORE UPDATE ON pending_approvals
    FOR EACH ROW
    EXECUTE FUNCTION update_updated_at_column();


-- =============================================================================
-- Calendar Classifications (v0.4.1 — Meeting Classification Memory)
-- =============================================================================

-- Remember Eyal's meeting classification answers so similar meetings
-- can be auto-classified without asking again.
CREATE TABLE IF NOT EXISTS calendar_classifications (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    title TEXT NOT NULL,
    title_lower TEXT GENERATED ALWAYS AS (lower(title)) STORED,
    is_cropsight BOOLEAN NOT NULL,
    classified_by TEXT DEFAULT 'eyal',
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_cal_class_title_lower ON calendar_classifications(title_lower);


-- RPC function for full-text search on embeddings
-- Called via: supabase.rpc('search_embeddings_fulltext', {...})
CREATE OR REPLACE FUNCTION search_embeddings_fulltext(
    search_query TEXT,
    match_count INT DEFAULT 20,
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
        e.chunk_index,
        e.speaker,
        e.timestamp_range,
        e.metadata,
        ts_rank(e.chunk_text_tsv, plainto_tsquery('english', search_query))::FLOAT AS rank
    FROM embeddings e
    WHERE
        e.chunk_text_tsv @@ plainto_tsquery('english', search_query)
        AND (filter_source_type IS NULL OR e.source_type = filter_source_type)
    ORDER BY rank DESC
    LIMIT match_count;
END;
$$;
