-- ============================================================
-- Project Status v2 — reconcile entities #5 and #6  [2026-08-07]
--
-- The Project Status sheet shipped 2026-08-06 as a GENERATED SNAPSHOT: it
-- cleared each tab and rewrote it on every run. Nechama is about to own that
-- file and run the weekly project review from it, so as it stands her Monday
-- edits are destroyed by Monday morning's refresh. This migration gives the
-- sheet the same three-way-merge machinery the Tasks/Meetings/Decisions tabs
-- already have, so it can become bidirectional.
--
-- Two things the schema simply could not express before:
--
--   1. PROJECT-LEVEL FIELDS. canonical_projects had name/description/aliases/
--      area_id and nothing else. The sheet's project row carries an objective
--      ("To do" = where we want to take it), a target date, an owner and notes
--      — none of which had anywhere to live.
--
--   2. TASK -> PROJECT. There has never been a project_id on tasks. Association
--      was the free-text `label`, and only 12 of 355 approved tasks carried a
--      label matching a canonical project; the other ~226 label values are
--      TOPICS, not projects. A project-centric sheet needs a real FK.
--
-- Fifth and sixth use of the entity_type recipe established by:
--   migrate_phase_v3_reconcile.sql            (task)
--   migrate_decision_reconcile_editable.sql   (decision)
--   migrate_gantt_rows.sql                    (gantt)
--   migrate_meeting_reconcile.sql             (meeting)
--
-- Column reuse is deliberate: sheet_row / snapshot_at / status already exist on
-- sheet_snapshots and mean the same thing here, so only genuinely new fields
-- are added.
--
-- THE KEYSTONE is uq_sheet_snapshots_ps_action. uq_sheet_snapshots_task is
-- PARTIAL on entity_type='task', so one task can legitimately hold two snapshot
-- rows — one per editable surface. Without a separate merge base per surface,
-- the Tasks tab and Project Status would share one and corrupt each other:
-- every edit on either would look like a divergence to the other.
--
-- Additive + idempotent; no new tables => no new RLS step (canonical_projects,
-- tasks and sheet_snapshots are already covered by migrate_rls_security*.sql).
-- ============================================================

BEGIN;

-- ============================================================
-- 1. canonical_projects: the fields the Project Status row owns
--
--    objective / target_date / owner / notes are edited by Nechama in the
--    sheet and are hers — the manual_* rail below is what stops a system
--    write from reverting them (Rule 2).
--
--    display_order drives the '#' column and the order blocks appear in a tab.
--    Gaps are fine and expected; it is a sort key, not a sequence.
--
--    retired_at / retired_reason implement RETIRE-NOT-DELETE. Deleting a
--    project orphans the provenance of every task/decision that referenced it,
--    so a retired project keeps its row and its aliases and simply stops
--    appearing in new work. NOTE resolve_label() must keep matching retired
--    names, or their historical labels become "unmatched" and
--    project_learning.propose_new_projects re-proposes them as brand-new
--    projects within days.
-- ============================================================
ALTER TABLE canonical_projects
    ADD COLUMN IF NOT EXISTS objective          TEXT,
    ADD COLUMN IF NOT EXISTS target_date        DATE,
    ADD COLUMN IF NOT EXISTS owner              TEXT,
    ADD COLUMN IF NOT EXISTS notes              TEXT,
    ADD COLUMN IF NOT EXISTS display_order      INTEGER,
    ADD COLUMN IF NOT EXISTS updated_at         TIMESTAMPTZ DEFAULT NOW(),
    ADD COLUMN IF NOT EXISTS retired_at         TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS retired_reason     TEXT,
    -- Sticky flags — same shape as tasks/decisions/meetings.
    ADD COLUMN IF NOT EXISTS manual_objective   BOOLEAN DEFAULT FALSE,
    ADD COLUMN IF NOT EXISTS manual_target_date BOOLEAN DEFAULT FALSE,
    ADD COLUMN IF NOT EXISTS manual_owner       BOOLEAN DEFAULT FALSE,
    ADD COLUMN IF NOT EXISTS manual_notes       BOOLEAN DEFAULT FALSE,
    ADD COLUMN IF NOT EXISTS manual_set_at      TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS manual_set_source  TEXT;

COMMENT ON COLUMN canonical_projects.objective IS
    'The "To do" cell on the project row: where we want to take this, the eventual aim. Nechama-owned.';
COMMENT ON COLUMN canonical_projects.owner IS
    'Project-level owner (account owner). Distinct from an action row''s Resp. (activity owner).';
COMMENT ON COLUMN canonical_projects.display_order IS
    'Sort key for blocks within an area tab. Gaps are expected; not a sequence.';
COMMENT ON COLUMN canonical_projects.retired_at IS
    'Retire-not-delete. status=''retired'' keeps the row + aliases so provenance and label matching survive.';
COMMENT ON COLUMN canonical_projects.manual_set_source IS
    'Provenance of the last human edit: ''sheet_edit'' | ''telegram'' | ''eyal_mcp''. Audit only.';

-- status vocabulary widens to 'active' | 'retired'. Deliberately no CHECK
-- constraint: the column is plain TEXT today and adding one would be a
-- non-additive change that could fail on unexpected legacy values.
CREATE INDEX IF NOT EXISTS idx_canonical_projects_status ON canonical_projects(status);
CREATE INDEX IF NOT EXISTS idx_canonical_projects_order  ON canonical_projects(display_order);

-- ============================================================
-- 2. tasks: the project FK that has never existed, + PS-owned fields
--
--    project_id is ON DELETE SET NULL rather than CASCADE — losing a project
--    must never delete the work done under it.
--
--    ps_suppressed records "Nechama deleted this row from the review".
--    Without it the next auto-injection pass re-adds the row and she is in a
--    resurrection loop. It is a VIEW flag: the task stays live, stays on the
--    Tasks tab, and is still chased.
-- ============================================================
ALTER TABLE tasks
    ADD COLUMN IF NOT EXISTS project_id        UUID REFERENCES canonical_projects(id) ON DELETE SET NULL,
    ADD COLUMN IF NOT EXISTS notes             TEXT,
    ADD COLUMN IF NOT EXISTS ps_suppressed     BOOLEAN DEFAULT FALSE,
    ADD COLUMN IF NOT EXISTS manual_project_id BOOLEAN DEFAULT FALSE,
    ADD COLUMN IF NOT EXISTS manual_notes      BOOLEAN DEFAULT FALSE;

COMMENT ON COLUMN tasks.project_id IS
    'Canonical project this task belongs to. tasks.label remains the TOPIC (finer grain), not the project.';
COMMENT ON COLUMN tasks.notes IS
    'The "Comments" cell on a Project Status action row. Nechama-owned; not written by extraction.';
COMMENT ON COLUMN tasks.ps_suppressed IS
    'Row was deliberately removed from the Project Status view. Task stays live; just never re-injected there.';

CREATE INDEX IF NOT EXISTS idx_tasks_project_id ON tasks(project_id);
-- Partial index: the auto-injection candidate query is exactly this shape.
CREATE INDEX IF NOT EXISTS idx_tasks_ps_candidates
    ON tasks(project_id, status)
    WHERE valid_to IS NULL AND ps_suppressed = FALSE;

-- ============================================================
-- 3. sheet_snapshots: entity #5 (ps_project) and #6 (ps_action)
--
--    sheet_tab is new and applies to both: the Project Status workbook has one
--    tab per area, so a row's tab is part of its address in a way it never was
--    for the single-tab entities.
-- ============================================================
ALTER TABLE sheet_snapshots
    ADD COLUMN IF NOT EXISTS canonical_project_id UUID
        REFERENCES canonical_projects(id) ON DELETE CASCADE,
    ADD COLUMN IF NOT EXISTS objective   TEXT,
    ADD COLUMN IF NOT EXISTS target_date DATE,
    ADD COLUMN IF NOT EXISTS owner       TEXT,
    ADD COLUMN IF NOT EXISTS notes       TEXT,
    ADD COLUMN IF NOT EXISTS sheet_tab   TEXT;

COMMENT ON COLUMN sheet_snapshots.sheet_tab IS
    'Area tab the row lived in at snapshot time (Project Status is multi-tab). Advisory, like sheet_row.';

-- One snapshot per project row on the Project Status sheet.
CREATE UNIQUE INDEX IF NOT EXISTS uq_sheet_snapshots_ps_project
    ON sheet_snapshots(canonical_project_id)
    WHERE entity_type = 'ps_project';

-- One snapshot per task PER SURFACE. This coexists with uq_sheet_snapshots_task
-- (entity_type='task') precisely because both are partial — a task edited on
-- the Tasks tab and shown on Project Status needs an independent merge base for
-- each, or an edit on one surface reads as a divergence on the other.
CREATE UNIQUE INDEX IF NOT EXISTS uq_sheet_snapshots_ps_action
    ON sheet_snapshots(task_id)
    WHERE entity_type = 'ps_action';

COMMIT;

-- ============================================================
-- Self-verifying tail. ONE query so the editor's single result pane shows the
-- whole verdict. Counts the NEW columns by name (information_schema, so a
-- missing one reads as a low count rather than erroring mid-way) and the two
-- new partial indexes.
--
-- EXPECTED: every row reads OK.
--   canonical_projects  14
--   tasks                5
--   sheet_snapshots      6
--   snapshot indexes     6   (task, decision, meeting, gantt_row + the 2 new)
-- ============================================================
WITH checks AS (
    SELECT 'canonical_projects new columns' AS item, COUNT(*)::int AS found, 14 AS expected
    FROM information_schema.columns
    WHERE table_name = 'canonical_projects'
      AND column_name IN ('objective','target_date','owner','notes','display_order',
                          'updated_at','retired_at','retired_reason','manual_objective',
                          'manual_target_date','manual_owner','manual_notes',
                          'manual_set_at','manual_set_source')
    UNION ALL
    SELECT 'tasks new columns', COUNT(*)::int, 5
    FROM information_schema.columns
    WHERE table_name = 'tasks'
      AND column_name IN ('project_id','notes','ps_suppressed',
                          'manual_project_id','manual_notes')
    UNION ALL
    SELECT 'sheet_snapshots new columns', COUNT(*)::int, 6
    FROM information_schema.columns
    WHERE table_name = 'sheet_snapshots'
      AND column_name IN ('canonical_project_id','objective','target_date',
                          'owner','notes','sheet_tab')
    UNION ALL
    SELECT 'sheet_snapshots unique indexes', COUNT(*)::int, 6
    FROM pg_indexes
    WHERE tablename = 'sheet_snapshots'
      AND indexname LIKE 'uq_sheet_snapshots_%'
)
SELECT item, found, expected,
       CASE WHEN found = expected THEN 'OK' ELSE 'MISSING' END AS verdict
FROM checks
ORDER BY item;

-- Post-migration follow-ups (NOT run here):
--   1. scripts/seed_canonical_projects_2026_08.py --apply   (Eyal's project list)
--   2. scripts/backfill_task_projects_2026_08.py --apply    (label -> project_id)
--   3. pytest tests/test_rls_coverage.py                    (expect PASS)
