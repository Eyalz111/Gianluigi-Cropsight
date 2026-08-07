-- ============================================================
-- Projects tab snapshot rail — entity #7 ('project')   [2026-08-07]
--
-- FIXES A LIVE DATA-CORRUPTING LOOP.
--
-- reconcile_projects (processors/sheets_sync.py) decides a project was renamed
-- by comparing the sheet's name cell straight against the DB:
--
--     renamed_this_pass = bool(name) and _normalize(name) != _normalize(dbp["name"])
--
-- There is no merge base, so it cannot tell "a human edited the SHEET" from
-- "the DB moved on and the sheet is stale". Any rename made anywhere other than
-- the Projects tab — MCP rename_canonical_project, a Telegram action, a
-- migration script — is therefore REVERTED by the stale cell on the next
-- 30-minute tick. And because rename_canonical_project backfills `label` across
-- tasks, decisions, open_questions, meetings and topic threads, the revert
-- propagates through all five.
--
-- PROJECTS_RECONCILE_ENABLED is already true with shadow off, so this is live
-- today. It has not bitten only because the 2026-08-07 seed happened to write
-- both surfaces in the same run.
--
-- The fix is the same three-way merge every other editable surface uses. This
-- migration adds the merge base; the engine change is in sheets_sync.py.
--
-- Seventh use of the entity_type recipe (task / decision / gantt_row / meeting /
-- ps_project / ps_action / project). Reuses the existing canonical_project_id
-- and title columns — 'project' snapshots need nothing new, only their own
-- partial unique index so they coexist with 'ps_project' on the same FK.
--
-- Additive + idempotent. No new tables => no new RLS step.
-- ============================================================

BEGIN;

-- The Projects TAB is a different editable surface from the Project Status
-- workbook, so a project legitimately holds two snapshots — one per surface —
-- exactly as a task does ('task' vs 'ps_action'). Both indexes are PARTIAL on
-- the same column, which is what lets them coexist.
CREATE UNIQUE INDEX IF NOT EXISTS uq_sheet_snapshots_project
    ON sheet_snapshots(canonical_project_id)
    WHERE entity_type = 'project';

COMMENT ON INDEX uq_sheet_snapshots_project IS
    'Merge base for the Projects TAB. Distinct from uq_sheet_snapshots_ps_project (Project Status workbook).';

-- ============================================================
-- topic_project_links — what lets PROJECT_LEARNING_ENABLED come back on
--
-- project_learning.propose_new_projects proposes a NEW canonical project for
-- any label recurring in >= 2 meetings. That was right when `label` was the
-- only association we had. It is wrong now: labels officially mean TOPICS (226
-- distinct values against 22 curated projects), so it would propose "AWS Credit
-- Card" and "NCPB Follow-up" as projects and steadily pollute the list Eyal
-- curated. The flag has been held off since 2026-08-07 for exactly this.
--
-- A topic is not a project and must not become one — but it does BELONG to one.
-- This table records that, so a recurring topic becomes a LINK proposal rather
-- than a new project, and future tasks carrying it attach automatically.
--
-- Deliberately NOT modelled as an alias of the project: aliases feed
-- resolve_label, which REWRITES tasks.label to the canonical name, and that
-- would collapse every topic into its project name and destroy the finer grain
-- the topic exists to provide.
-- ============================================================
CREATE TABLE IF NOT EXISTS topic_project_links (
    topic       TEXT PRIMARY KEY,
    project_id  UUID NOT NULL REFERENCES canonical_projects(id) ON DELETE CASCADE,
    source      TEXT,                       -- 'proposal' | 'backfill' | 'manual'
    created_at  TIMESTAMPTZ DEFAULT NOW()
);

ALTER TABLE topic_project_links ENABLE ROW LEVEL SECURITY;

CREATE INDEX IF NOT EXISTS idx_topic_project_links_project
    ON topic_project_links(project_id);

COMMENT ON TABLE topic_project_links IS
    'Topic (tasks.label) -> canonical project. NOT an alias: aliases feed resolve_label, which would rewrite the label and destroy topic granularity.';

COMMIT;

-- ============================================================
-- EXPECTED: every row reads OK.
--   snapshot indexes      7   (task, decision, meeting, gantt_row,
--                              ps_project, ps_action + the new one)
--   project index present 1
--   topic_project_links   1
--   RLS on the new table  1   (mandatory — tests/test_rls_coverage.py enforces)
-- ============================================================
WITH checks AS (
    SELECT 'sheet_snapshots unique indexes' AS item, COUNT(*)::int AS found, 7 AS expected
    FROM pg_indexes
    WHERE tablename = 'sheet_snapshots'
      AND indexname LIKE 'uq_sheet_snapshots_%'
    UNION ALL
    SELECT 'project index present', COUNT(*)::int, 1
    FROM pg_indexes
    WHERE tablename = 'sheet_snapshots'
      AND indexname = 'uq_sheet_snapshots_project'
    UNION ALL
    SELECT 'topic_project_links table', COUNT(*)::int, 1
    FROM information_schema.tables
    WHERE table_schema = 'public' AND table_name = 'topic_project_links'
    UNION ALL
    SELECT 'topic_project_links RLS', COUNT(*)::int, 1
    FROM pg_tables
    WHERE schemaname = 'public' AND tablename = 'topic_project_links'
      AND rowsecurity = true
)
SELECT item, found, expected,
       CASE WHEN found = expected THEN 'OK' ELSE 'MISSING' END AS verdict
FROM checks
ORDER BY item;
