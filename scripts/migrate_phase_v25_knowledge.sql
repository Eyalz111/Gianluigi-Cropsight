-- v2.5 Phase 1 Migration: Knowledge Foundation (PR1)
-- Run this in Supabase SQL editor BEFORE deploying PR1 code.
--
-- Creates the knowledge spine (graph-lite on Postgres):
--   1. areas              — Layer 3.5 sphere briefs (seeded from gantt_schema by backfill)
--   2. topic_threads      — new cols: area_id, parent_topic_id (sub-topics),
--                           brief_json (richer than state_json), bi-temporal validity
--   3. knowledge_links    — typed links between entities (the "graph", no graph DB)
--   4. decisions / tasks  — bi-temporal validity cols (valid_from/valid_to/superseded_*)
--
-- ALL changes are ADDITIVE and idempotent (IF NOT EXISTS). Never drops or wipes.
-- Existing read paths are unaffected: new layer lives in separate columns/tables.
-- RLS is enabled on every new table (service-role key bypasses it; closes anon access).
-- Enforced by tests/test_rls_coverage.py.

BEGIN;

-- ============================================================
-- 1. areas (Layer 3.5 — sphere briefs)
-- ============================================================
CREATE TABLE IF NOT EXISTS areas (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    workspace_id TEXT DEFAULT 'cropsight',
    name TEXT NOT NULL UNIQUE,
    description TEXT,
    gantt_section TEXT,          -- source gantt_schema.section it was seeded from (one-way link)
    brief_json JSONB,            -- AreaBrief
    brief_updated_at TIMESTAMPTZ,
    status TEXT DEFAULT 'active', -- active, archived
    valid_from TIMESTAMPTZ DEFAULT NOW(),
    valid_to TIMESTAMPTZ,        -- NULL = currently valid (bi-temporal)
    superseded_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

ALTER TABLE areas ENABLE ROW LEVEL SECURITY;

CREATE INDEX IF NOT EXISTS idx_areas_status ON areas(status);
CREATE INDEX IF NOT EXISTS idx_areas_gantt_section ON areas(gantt_section);

-- ============================================================
-- 2. topic_threads — additive columns
-- ============================================================
ALTER TABLE topic_threads
    ADD COLUMN IF NOT EXISTS area_id UUID REFERENCES areas(id) ON DELETE SET NULL,
    ADD COLUMN IF NOT EXISTS parent_topic_id UUID REFERENCES topic_threads(id) ON DELETE SET NULL,
    ADD COLUMN IF NOT EXISTS brief_json JSONB,        -- TopicBrief (richer than legacy state_json)
    ADD COLUMN IF NOT EXISTS brief_updated_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS valid_from TIMESTAMPTZ DEFAULT NOW(),
    ADD COLUMN IF NOT EXISTS valid_to TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS superseded_at TIMESTAMPTZ;

CREATE INDEX IF NOT EXISTS idx_topic_threads_area ON topic_threads(area_id);
CREATE INDEX IF NOT EXISTS idx_topic_threads_parent ON topic_threads(parent_topic_id);

-- ============================================================
-- 3. knowledge_links (typed links = the graph-lite layer)
-- ============================================================
CREATE TABLE IF NOT EXISTS knowledge_links (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    workspace_id TEXT DEFAULT 'cropsight',
    from_type TEXT NOT NULL,     -- 'topic' | 'area' | 'decision' | 'task' | 'meeting' | 'milestone'
    from_id UUID NOT NULL,
    to_type TEXT NOT NULL,
    to_id UUID NOT NULL,
    link_type TEXT NOT NULL,     -- belongs_to | supersedes | advances | blocks | relates_to | derived_from
    confidence REAL,             -- 0..1 (LLM-inferred); NULL for deterministic links
    source_meeting_id UUID REFERENCES meetings(id) ON DELETE SET NULL,
    created_by TEXT DEFAULT 'auto',  -- 'auto' | 'eyal' | 'backfill'
    valid_from TIMESTAMPTZ DEFAULT NOW(),
    valid_to TIMESTAMPTZ,        -- NULL = currently valid
    superseded_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

ALTER TABLE knowledge_links ENABLE ROW LEVEL SECURITY;

CREATE INDEX IF NOT EXISTS idx_knowledge_links_from ON knowledge_links(from_type, from_id);
CREATE INDEX IF NOT EXISTS idx_knowledge_links_to ON knowledge_links(to_type, to_id);
CREATE INDEX IF NOT EXISTS idx_knowledge_links_type ON knowledge_links(link_type);
-- "current links only" is the hot query path
CREATE INDEX IF NOT EXISTS idx_knowledge_links_current ON knowledge_links(from_type, from_id)
    WHERE valid_to IS NULL;

-- ============================================================
-- 4. Bi-temporal validity on atomic items (additive)
--    DEFAULT-open so existing reads are unaffected until a writer sets valid_to.
--    The central read helpers (get_tasks / list_decisions) filter valid_to IS NULL
--    by default — shipped in the same PR (see services/supabase_client.py).
-- ============================================================
ALTER TABLE decisions
    ADD COLUMN IF NOT EXISTS valid_from TIMESTAMPTZ DEFAULT NOW(),
    ADD COLUMN IF NOT EXISTS valid_to TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS superseded_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS superseded_by UUID REFERENCES decisions(id) ON DELETE SET NULL;

ALTER TABLE tasks
    ADD COLUMN IF NOT EXISTS valid_from TIMESTAMPTZ DEFAULT NOW(),
    ADD COLUMN IF NOT EXISTS valid_to TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS superseded_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS superseded_by UUID REFERENCES tasks(id) ON DELETE SET NULL;

CREATE INDEX IF NOT EXISTS idx_decisions_valid ON decisions(meeting_id) WHERE valid_to IS NULL;
CREATE INDEX IF NOT EXISTS idx_tasks_valid ON tasks(status) WHERE valid_to IS NULL;

COMMIT;

-- ============================================================
-- Post-migration validation (run manually after applying):
--   1. RLS on new tables:
--      SELECT tablename, rowsecurity FROM pg_tables
--      WHERE tablename IN ('areas','knowledge_links') ORDER BY tablename;  -- expect both true
--   2. New columns present:
--      SELECT column_name FROM information_schema.columns
--      WHERE table_name='topic_threads' AND column_name IN
--        ('area_id','parent_topic_id','brief_json','valid_from','valid_to');
--   3. RLS coverage test: pytest tests/test_rls_coverage.py  -- expect PASS
-- ============================================================
