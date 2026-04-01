-- =============================================================================
-- Gianluigi v2 Phase 12 Migration
-- Date: April 2, 2026
-- Purpose: Decision freshness + chain, task signals
-- =============================================================================
--
-- Run in Supabase SQL Editor before deploying Phase 12 code.
-- All statements are idempotent (safe to run multiple times).
-- =============================================================================

-- A4: Decision freshness — track when decisions are last referenced
ALTER TABLE decisions ADD COLUMN IF NOT EXISTS last_referenced_at TIMESTAMPTZ;

-- A6: Decision chain traversal — parent decision linkage
ALTER TABLE decisions ADD COLUMN IF NOT EXISTS parent_decision_id UUID REFERENCES decisions(id);
ALTER TABLE tasks ADD COLUMN IF NOT EXISTS spawned_from_decision_id UUID REFERENCES decisions(id);

-- A5: Task signals — track completion signals from various sources
CREATE TABLE IF NOT EXISTS task_signals (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    task_id UUID NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    signal_type TEXT NOT NULL,
    signal_source TEXT,
    detected_at TIMESTAMPTZ DEFAULT NOW(),
    confidence TEXT DEFAULT 'medium',
    details JSONB
);
CREATE INDEX IF NOT EXISTS idx_task_signals_task_id ON task_signals(task_id);
ALTER TABLE task_signals ENABLE ROW LEVEL SECURITY;
