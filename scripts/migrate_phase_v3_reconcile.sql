-- v3 Phase 3 Migration: Outputs reconcile foundation (PR1)
-- Run this in Supabase SQL editor BEFORE deploying PR1 code.
--
-- Adds the substrate for the column-ownership + snapshot reconcile engine:
--   1. tasks: per-field "manually set" flags (Rule 1 — manual wins & sticks)
--   2. sheet_snapshots: the per-sync snapshot that lets reconcile attribute a
--      change to Eyal (Sheet) vs Gianluigi (DB) — the keystone the old sync lacked.
--
-- ALL changes are ADDITIVE and idempotent (IF NOT EXISTS). Never drops/wipes.
-- RLS enabled on the new table (service-role key bypasses; closes anon access).
-- Enforced by tests/test_rls_coverage.py.

BEGIN;

-- ============================================================
-- 1. tasks: per-field manual-override flags (sticky)
-- ============================================================
ALTER TABLE tasks
    ADD COLUMN IF NOT EXISTS manual_status      BOOLEAN DEFAULT FALSE,
    ADD COLUMN IF NOT EXISTS manual_deadline    BOOLEAN DEFAULT FALSE,
    ADD COLUMN IF NOT EXISTS manual_priority    BOOLEAN DEFAULT FALSE,
    ADD COLUMN IF NOT EXISTS manual_assignee    BOOLEAN DEFAULT FALSE,
    ADD COLUMN IF NOT EXISTS manual_set_at       TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS manual_set_source   TEXT;  -- 'sheet_edit' | 'telegram' | 'eyal_mcp'

-- ============================================================
-- 2. sheet_snapshots — last-synced state of the action fields (keystone)
--    One current row per task (entity_type='task'); entity_type discriminator
--    lets the Gantt reuse this table later ('gantt_row').
-- ============================================================
CREATE TABLE IF NOT EXISTS sheet_snapshots (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    workspace_id TEXT DEFAULT 'cropsight',
    entity_type TEXT NOT NULL DEFAULT 'task',
    task_id UUID REFERENCES tasks(id) ON DELETE CASCADE,
    sheet_row INTEGER,
    -- the four action fields as last reconciled/pushed to the Sheet:
    status TEXT,
    deadline DATE,
    priority TEXT,
    assignee TEXT,
    snapshot_at TIMESTAMPTZ DEFAULT NOW()
);

ALTER TABLE sheet_snapshots ENABLE ROW LEVEL SECURITY;

-- one current snapshot per task
CREATE UNIQUE INDEX IF NOT EXISTS uq_sheet_snapshots_task
    ON sheet_snapshots(task_id) WHERE entity_type = 'task';
CREATE INDEX IF NOT EXISTS idx_sheet_snapshots_entity ON sheet_snapshots(entity_type);

COMMIT;

-- ============================================================
-- Post-migration validation (run manually after applying):
--   1. RLS on the new table:
--      SELECT tablename, rowsecurity FROM pg_tables
--      WHERE tablename = 'sheet_snapshots';  -- expect true
--   2. New columns on tasks:
--      SELECT column_name FROM information_schema.columns
--      WHERE table_name='tasks' AND column_name LIKE 'manual_%';
--   3. RLS coverage test: pytest tests/test_rls_coverage.py  -- expect PASS
-- ============================================================
