-- The Timeline becomes an editable surface. [2026-08-12]
--
-- Phase 4a of docs/GANTT_V2_PLAN.md. Additive and idempotent; no new tables,
-- so no new RLS step.
--
-- TWO THINGS ARE MISSING BEFORE THE TIMELINE CAN BE READ BACK.
--
-- 1. `sheet_snapshots` has no `start_date`. Every other editable field on the
--    Timeline already has a column here — target_date, owner — because the
--    Project Status surfaces edit them too. `start_date` is new in this plan and
--    the Timeline is its only home, so the merge base has nowhere to record what
--    the sheet last said. Without it, a start date typed into the sheet would
--    compare against NULL on every cycle and read as a fresh human edit forever.
--
-- 2. The Timeline is a DIFFERENT SURFACE from the Projects tab and from the
--    Project Status workbook, and a project legitimately appears on all three.
--    Each needs its own merge base, or an edit on one reads as a divergence on
--    the others — which is exactly the cross-surface defect family of 2026-08:
--    the rename-revert loop, the per-task manual_set_at recency bug, and three
--    labels left permanently divergent because the Tasks tab pulled a stale cell
--    over a value Project Status had just written.
--
--    `uq_sheet_snapshots_project` (Projects tab) and `uq_sheet_snapshots_ps_project`
--    (Project Status workbook) already coexist as PARTIAL indexes on the same
--    `canonical_project_id`. This adds the third.

BEGIN;

ALTER TABLE sheet_snapshots
    ADD COLUMN IF NOT EXISTS start_date DATE;

CREATE UNIQUE INDEX IF NOT EXISTS uq_sheet_snapshots_gantt_project
    ON sheet_snapshots(canonical_project_id)
    WHERE entity_type = 'gantt_project';

COMMENT ON INDEX uq_sheet_snapshots_gantt_project IS
    'Merge base for the TIMELINE tab. Distinct from uq_sheet_snapshots_project '
    '(Projects tab) and uq_sheet_snapshots_ps_project (Project Status workbook) '
    '— one project, three editable surfaces, three independent bases.';
COMMENT ON COLUMN sheet_snapshots.start_date IS
    'What the sheet last showed for canonical_projects.start_date. The Timeline '
    'is currently the only surface that edits it.';

COMMIT;

-- Verify by probing the NEW COLUMN and the NEW INDEX, never a value a prior
-- migration could have set:
--   SELECT start_date FROM sheet_snapshots LIMIT 1;   -- 42703 = not applied
--   SELECT indexname FROM pg_indexes
--    WHERE indexname = 'uq_sheet_snapshots_gantt_project';
--   -- all three project bases should now be listed:
--   SELECT indexname FROM pg_indexes
--    WHERE indexname LIKE 'uq_sheet_snapshots_%project%';
