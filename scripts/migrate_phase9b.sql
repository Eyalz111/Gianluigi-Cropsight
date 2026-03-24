-- Phase 9B Migration: Memory & Cross-Meeting Intelligence
-- Date: March 25, 2026

-- ============================================================
-- 1. Operational snapshots (daily compressed context)
-- ============================================================
CREATE TABLE IF NOT EXISTS operational_snapshots (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    workspace_id TEXT DEFAULT 'cropsight',
    snapshot_date DATE NOT NULL,
    content TEXT NOT NULL,
    structured_data JSONB,
    generated_at TIMESTAMPTZ DEFAULT NOW(),
    tokens_used INTEGER,
    UNIQUE(workspace_id, snapshot_date)
);
CREATE INDEX IF NOT EXISTS idx_operational_snapshots_date ON operational_snapshots(snapshot_date DESC);

-- ============================================================
-- 2. Topic threads (cross-meeting topic tracking)
-- ============================================================
CREATE TABLE IF NOT EXISTS topic_threads (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    workspace_id TEXT DEFAULT 'cropsight',
    topic_name TEXT NOT NULL,
    topic_name_lower TEXT GENERATED ALWAYS AS (lower(topic_name)) STORED,
    status TEXT DEFAULT 'active',
    first_meeting_id UUID REFERENCES meetings(id) ON DELETE SET NULL,
    last_meeting_id UUID REFERENCES meetings(id) ON DELETE SET NULL,
    meeting_count INTEGER DEFAULT 1,
    evolution_summary TEXT,
    last_updated TIMESTAMPTZ DEFAULT NOW(),
    created_at TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_topic_threads_name ON topic_threads(topic_name_lower);
CREATE INDEX IF NOT EXISTS idx_topic_threads_status ON topic_threads(status);

CREATE TABLE IF NOT EXISTS topic_thread_mentions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    topic_id UUID REFERENCES topic_threads(id) ON DELETE CASCADE,
    meeting_id UUID REFERENCES meetings(id) ON DELETE CASCADE,
    context TEXT,
    decisions_made TEXT[],
    status_at_mention TEXT,
    created_at TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_topic_thread_mentions_topic ON topic_thread_mentions(topic_id);
CREATE INDEX IF NOT EXISTS idx_topic_thread_mentions_meeting ON topic_thread_mentions(meeting_id);
