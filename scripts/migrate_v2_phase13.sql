-- =============================================================================
-- Gianluigi v2 Phase 13 Migration
-- Date: April 2, 2026
-- Purpose: Document versioning, Dropbox sync tracking
-- =============================================================================
--
-- Run in Supabase SQL Editor before deploying Phase 13 code.
-- All statements are idempotent (safe to run multiple times).
-- =============================================================================

-- B2: Document versioning
ALTER TABLE documents ADD COLUMN IF NOT EXISTS version INTEGER DEFAULT 1;
ALTER TABLE documents ADD COLUMN IF NOT EXISTS content_hash TEXT;

-- B2: Indexes for dedup lookups
CREATE INDEX IF NOT EXISTS idx_documents_content_hash ON documents(content_hash);
CREATE INDEX IF NOT EXISTS idx_documents_title_source ON documents(title, source);

-- B1: Dropbox sync tracking (for future use)
CREATE TABLE IF NOT EXISTS dropbox_drive_sync (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    dropbox_file_id TEXT NOT NULL UNIQUE,
    drive_file_id TEXT,
    dropbox_path TEXT NOT NULL,
    drive_path TEXT,
    content_hash TEXT,
    last_synced_at TIMESTAMPTZ DEFAULT NOW(),
    sync_status TEXT DEFAULT 'synced',
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_dropbox_sync_status ON dropbox_drive_sync(sync_status);
ALTER TABLE dropbox_drive_sync ENABLE ROW LEVEL SECURITY;
