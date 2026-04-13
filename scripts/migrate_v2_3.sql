-- scripts/migrate_v2_3.sql
-- v2.3 migration: three additive schema changes.
--
-- 1. approval_observations table — fire-and-forget decision log for every
--    approve/edit/reject Eyal makes. Zero LLM cost. Foundation for v2.4
--    thematic-query observation logging and v2.5 graduated autonomy.
--
-- 2. tasks.deadline_confidence column — tag each deadline as EXPLICIT
--    (stated verbatim), INFERRED (LLM-guessed), or NONE (no timing). Lets
--    the reminder + proactive-alert paths suppress noisy INFERRED deadlines
--    without hiding them from the morning brief / MCP get_tasks read path.
--
-- 3. topic_threads.state_json + state_updated_at — structured, continuously
--    updated state blob per topic thread. Populated incrementally by Haiku
--    in PR 4 (update_topic_state()); the existing evolution_summary prose
--    stays untouched and sits alongside. The expression index on the
--    current_status key lets the morning brief filter blocked topics cheaply.
--
-- All three changes are additive and backfill-friendly. No existing reads
-- or writes are affected until application code starts using the columns.
-- Rollback = DROP the new table + columns; old behavior returns.

BEGIN;

-- ============================================================================
-- 1. approval_observations
-- ============================================================================
CREATE TABLE IF NOT EXISTS approval_observations (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    content_type TEXT NOT NULL,
    -- Known values: 'meeting_summary' | 'task_extraction' | 'gantt_proposal'
    -- | 'intelligence_signal' | 'meeting_prep' | 'sheets_sync'
    -- | 'quick_inject' | 'deadline_update'
    -- Intentionally no CHECK constraint — we want forward compatibility
    -- as new content types are added without having to migrate first.
    content_id UUID,                    -- FK to relevant record (optional; no constraint because it's polymorphic)
    action TEXT NOT NULL CHECK (action IN ('approved', 'edited', 'rejected')),
    original_content JSONB,             -- what Gianluigi proposed
    final_content JSONB,                -- what Eyal accepted (null if rejected)
    edit_distance_pct FLOAT,            -- 0.0-1.0, null if not edited
    context JSONB,                      -- free bag: meeting_id, signal_id, etc.
    created_at TIMESTAMPTZ DEFAULT NOW()
);

-- MANDATORY per CLAUDE.md: every new table must enable RLS. service_role
-- bypasses RLS automatically, so this has zero functional impact — it only
-- locks the anon/public path that would otherwise expose the table via
-- the Supabase anon key.
ALTER TABLE approval_observations ENABLE ROW LEVEL SECURITY;

CREATE INDEX IF NOT EXISTS idx_approval_obs_content_type
    ON approval_observations(content_type);
CREATE INDEX IF NOT EXISTS idx_approval_obs_action
    ON approval_observations(action);
CREATE INDEX IF NOT EXISTS idx_approval_obs_created_at
    ON approval_observations(created_at DESC);


-- ============================================================================
-- 2. tasks.deadline_confidence
-- ============================================================================
-- 'EXPLICIT' = user said "by March 15" — trustworthy, notification-worthy
-- 'INFERRED' = LLM guessed from context — suppressed from reminders + alerts
-- 'NONE'     = no deadline mentioned (default)
ALTER TABLE tasks
    ADD COLUMN IF NOT EXISTS deadline_confidence TEXT
    DEFAULT 'NONE'
    CHECK (deadline_confidence IN ('EXPLICIT', 'INFERRED', 'NONE'));

-- Partial index: only reminders + alerts query by this column, and only
-- on approved tasks. Full-table index would be wasted space.
CREATE INDEX IF NOT EXISTS idx_tasks_deadline_confidence
    ON tasks(deadline_confidence)
    WHERE approval_status = 'approved';


-- ============================================================================
-- 3. topic_threads.state_json + state_updated_at
-- ============================================================================
ALTER TABLE topic_threads
    ADD COLUMN IF NOT EXISTS state_json JSONB DEFAULT NULL;

ALTER TABLE topic_threads
    ADD COLUMN IF NOT EXISTS state_updated_at TIMESTAMPTZ DEFAULT NULL;

-- Expression index on the hottest lookup key: morning brief needs to find
-- all topics where current_status = 'blocked' cheaply. Partial WHERE clause
-- skips threads that have no state_json yet (backfill-aware).
CREATE INDEX IF NOT EXISTS idx_topic_threads_state_status
    ON topic_threads ((state_json->>'current_status'))
    WHERE state_json IS NOT NULL;

COMMIT;


-- ============================================================================
-- Post-migration validation (run + eyeball)
-- ============================================================================
--
-- 1. approval_observations table exists with RLS enabled and is empty:
--      SELECT tablename, rowsecurity FROM pg_tables
--      WHERE tablename = 'approval_observations';
--      -- expected: rowsecurity = true
--      SELECT COUNT(*) FROM approval_observations;
--      -- expected: 0
--
-- 2. All three indexes on approval_observations exist:
--      SELECT indexname FROM pg_indexes
--      WHERE tablename = 'approval_observations'
--      ORDER BY indexname;
--      -- expected: idx_approval_obs_action, idx_approval_obs_content_type,
--      --          idx_approval_obs_created_at (+ the PK index)
--
-- 3. tasks.deadline_confidence column + constraint + partial index:
--      SELECT column_name, data_type, column_default
--      FROM information_schema.columns
--      WHERE table_name = 'tasks' AND column_name = 'deadline_confidence';
--      -- expected: deadline_confidence | text | 'NONE'::text
--
--      SELECT constraint_name FROM information_schema.table_constraints
--      WHERE table_name = 'tasks' AND constraint_type = 'CHECK'
--        AND constraint_name LIKE '%deadline_confidence%';
--      -- expected: one CHECK constraint
--
--      SELECT indexname FROM pg_indexes
--      WHERE tablename = 'tasks' AND indexname = 'idx_tasks_deadline_confidence';
--      -- expected: one row
--
--      -- All existing task rows default to 'NONE'. The backfill script
--      -- (scripts/backfill_deadline_confidence.py) promotes rows with a
--      -- non-null deadline to 'INFERRED' so reminders don't immediately
--      -- fire on legacy tasks with possibly-hallucinated deadlines.
--      SELECT deadline_confidence, COUNT(*) FROM tasks GROUP BY deadline_confidence;
--      -- expected immediately after migration: all rows 'NONE'
--      -- expected after backfill: rows with a deadline → 'INFERRED', else 'NONE'
--
-- 4. topic_threads.state_json + state_updated_at + expression index:
--      SELECT column_name, data_type
--      FROM information_schema.columns
--      WHERE table_name = 'topic_threads'
--        AND column_name IN ('state_json', 'state_updated_at')
--      ORDER BY column_name;
--      -- expected: state_json|jsonb and state_updated_at|timestamp with time zone
--
--      SELECT indexname FROM pg_indexes
--      WHERE tablename = 'topic_threads' AND indexname = 'idx_topic_threads_state_status';
--      -- expected: one row
--
-- 5. CHECK constraint is enforced (should FAIL with constraint violation):
--      UPDATE tasks SET deadline_confidence='EXPLCIT' WHERE id = '<any-id>';
--      -- expected: ERROR: new row for relation "tasks" violates check constraint
--
-- 6. RLS coverage test: run tests/test_rls_coverage.py — must pass.
--    It queries Supabase and fails if any public table (including the new
--    approval_observations) is missing RLS.
