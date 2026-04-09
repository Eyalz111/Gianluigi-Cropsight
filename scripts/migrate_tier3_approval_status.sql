-- scripts/migrate_tier3_approval_status.sql
-- Tier 3.1 (narrow): add approval_status column to the 4 extraction tables
-- + backfill existing rows to 'approved' so current read behavior is preserved
-- on day 1. Includes a CHECK constraint so stray UPDATEs can't plant a typo,
-- and uses partial indexes on 'pending' only (95%+ of rows will be 'approved',
-- so a full index has poor selectivity; the partial index is tiny and covers
-- exactly the rows we're trying to filter out).
--
-- How this works end-to-end:
--  1. Extraction writes child rows with approval_status='pending' (via the
--     DB column default — no Python code changes to the batch-insert paths).
--  2. Central read helpers (get_tasks, list_decisions, get_open_questions,
--     list_follow_up_meetings) gain an include_pending=False default that
--     filters to approval_status='approved'. Most callers get filtered
--     automatically without any code change.
--  3. On approve, guardrails/approval_flow._promote_children_to_approved()
--     bulk-flips the 4 child tables to 'approved' for the meeting.
--  4. On reject, T1.9 cascade-clears children entirely (so the column is
--     irrelevant for rejected data — it exists for pending/editing states).
--
-- The QA scheduler safety-net check catches any meeting where the parent
-- is 'approved' but children are still 'pending' (indicates a partial
-- promote failure) and surfaces it in the morning brief.

BEGIN;

-- Add the column with DEFAULT 'pending'. New inserts get 'pending' automatically.
ALTER TABLE tasks              ADD COLUMN IF NOT EXISTS approval_status TEXT DEFAULT 'pending';
ALTER TABLE decisions          ADD COLUMN IF NOT EXISTS approval_status TEXT DEFAULT 'pending';
ALTER TABLE open_questions     ADD COLUMN IF NOT EXISTS approval_status TEXT DEFAULT 'pending';
ALTER TABLE follow_up_meetings ADD COLUMN IF NOT EXISTS approval_status TEXT DEFAULT 'pending';

-- Backfill: preserve current read behavior by marking all existing rows 'approved'.
-- Any row that pre-dates this migration is assumed to be approved (that's the
-- behavior the system had before the column existed).
UPDATE tasks              SET approval_status='approved' WHERE approval_status IS NULL OR approval_status='pending';
UPDATE decisions          SET approval_status='approved' WHERE approval_status IS NULL OR approval_status='pending';
UPDATE open_questions     SET approval_status='approved' WHERE approval_status IS NULL OR approval_status='pending';
UPDATE follow_up_meetings SET approval_status='approved' WHERE approval_status IS NULL OR approval_status='pending';

-- CHECK constraint: only 'pending' and 'approved' are valid values.
-- Rejected children don't exist (T1.9 cascade-deletes them on reject), so the
-- enum is binary. Any stray UPDATE with a typo or unknown value will fail
-- loudly at write time instead of silently planting corrupt data.
ALTER TABLE tasks              ADD CONSTRAINT tasks_approval_status_check              CHECK (approval_status IN ('pending', 'approved'));
ALTER TABLE decisions          ADD CONSTRAINT decisions_approval_status_check          CHECK (approval_status IN ('pending', 'approved'));
ALTER TABLE open_questions     ADD CONSTRAINT open_questions_approval_status_check     CHECK (approval_status IN ('pending', 'approved'));
ALTER TABLE follow_up_meetings ADD CONSTRAINT follow_up_meetings_approval_status_check CHECK (approval_status IN ('pending', 'approved'));

-- Partial indexes on 'pending' only (high-selectivity, tiny footprint).
-- The default read path filters WHERE approval_status = 'approved', which
-- eventually becomes ~100% of rows — a full index on that column is wasted.
-- A partial index covering only 'pending' rows is what we actually care about
-- (orphan detection, mid-approval reads, the QA safety-net check).
CREATE INDEX IF NOT EXISTS idx_tasks_approval_status_pending
    ON tasks(meeting_id) WHERE approval_status = 'pending';
CREATE INDEX IF NOT EXISTS idx_decisions_approval_status_pending
    ON decisions(meeting_id) WHERE approval_status = 'pending';
CREATE INDEX IF NOT EXISTS idx_open_questions_approval_status_pending
    ON open_questions(meeting_id) WHERE approval_status = 'pending';
CREATE INDEX IF NOT EXISTS idx_follow_up_meetings_approval_status_pending
    ON follow_up_meetings(source_meeting_id) WHERE approval_status = 'pending';

COMMIT;

-- ============================================================================
-- Post-migration validation (run and eyeball):
-- ============================================================================
--
-- 1. Column exists on all 4 tables with default 'pending':
--      SELECT table_name, column_name, column_default
--      FROM information_schema.columns
--      WHERE column_name = 'approval_status'
--        AND table_name IN ('tasks', 'decisions', 'open_questions', 'follow_up_meetings')
--      ORDER BY table_name;
--
-- 2. All existing rows are 'approved' (backfill worked):
--      SELECT 'tasks' t, approval_status, count(*) FROM tasks GROUP BY approval_status
--      UNION ALL
--      SELECT 'decisions', approval_status, count(*) FROM decisions GROUP BY approval_status
--      UNION ALL
--      SELECT 'open_questions', approval_status, count(*) FROM open_questions GROUP BY approval_status
--      UNION ALL
--      SELECT 'follow_up_meetings', approval_status, count(*) FROM follow_up_meetings GROUP BY approval_status;
--    Expected: all existing rows are 'approved'; 'pending' count is 0 until new transcripts arrive.
--
-- 3. Partial indexes exist:
--      SELECT indexname, tablename
--      FROM pg_indexes
--      WHERE indexname LIKE 'idx_%_approval_status_pending'
--      ORDER BY tablename;
--
-- 4. CHECK constraints are enforced:
--      -- This should FAIL with a constraint violation:
--      UPDATE tasks SET approval_status='rejectd' WHERE id = '<any-id>';
