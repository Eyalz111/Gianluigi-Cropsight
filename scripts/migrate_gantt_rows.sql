-- v3 Phase 3 (chunk 2) — Gantt redesign migration (PR1)
-- Run in Supabase SQL editor BEFORE deploying Gantt code.
--
-- Adds the DB representation of curated Gantt rows (gantt_rows) and extends
-- sheet_snapshots for gantt_row timeframe reconcile. ADDITIVE + idempotent.
-- RLS enabled on the new table (service-role key bypasses; closes anon access).
-- Enforced by tests/test_rls_coverage.py.

BEGIN;

-- ============================================================
-- 1. gantt_rows — curated roadmap rows (topic-tagged); DB view of the Gantt.
--    Eyal arranges the sheet rows; this table mirrors them by topic-id tag.
-- ============================================================
CREATE TABLE IF NOT EXISTS gantt_rows (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    workspace_id TEXT DEFAULT 'cropsight',
    area_id UUID REFERENCES areas(id) ON DELETE SET NULL,
    topic_id UUID REFERENCES topic_threads(id) ON DELETE SET NULL,  -- NULL = Area header / untagged
    sheet_name TEXT NOT NULL,
    owner TEXT,
    status TEXT,
    week_start INTEGER,
    week_end INTEGER,
    manual_status BOOLEAN DEFAULT FALSE,
    manual_timeframe BOOLEAN DEFAULT FALSE,
    manual_set_at TIMESTAMPTZ,
    manual_set_source TEXT,            -- 'sheet_edit' | 'eyal_mcp'
    display_order INTEGER,             -- advisory only; never used to move rows
    valid_from TIMESTAMPTZ DEFAULT NOW(),
    valid_to TIMESTAMPTZ,
    superseded_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

ALTER TABLE gantt_rows ENABLE ROW LEVEL SECURITY;

-- A topic appears at most once per sheet (but can appear on multiple year-sheets)
CREATE UNIQUE INDEX IF NOT EXISTS uq_gantt_rows_topic
    ON gantt_rows(sheet_name, topic_id) WHERE valid_to IS NULL AND topic_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_gantt_rows_area ON gantt_rows(area_id);

-- ============================================================
-- 2. Extend sheet_snapshots for gantt_row reuse (tasks untouched).
--    The task index stays scoped WHERE entity_type='task', so reconcile_tasks
--    is unaffected; gantt rows get their own partial unique index.
-- ============================================================
ALTER TABLE sheet_snapshots
    ADD COLUMN IF NOT EXISTS gantt_row_id UUID REFERENCES gantt_rows(id) ON DELETE CASCADE,
    ADD COLUMN IF NOT EXISTS week_start INTEGER,
    ADD COLUMN IF NOT EXISTS week_end INTEGER;

CREATE UNIQUE INDEX IF NOT EXISTS uq_sheet_snapshots_gantt
    ON sheet_snapshots(gantt_row_id) WHERE entity_type = 'gantt_row';

COMMIT;

-- ============================================================
-- Post-migration validation (run manually after applying):
--   1. RLS:  SELECT tablename, rowsecurity FROM pg_tables WHERE tablename='gantt_rows';  -- true
--   2. Cols: SELECT column_name FROM information_schema.columns
--            WHERE table_name='sheet_snapshots' AND column_name IN ('gantt_row_id','week_start','week_end');
--   3. pytest tests/test_rls_coverage.py  -- expect PASS
-- ============================================================
