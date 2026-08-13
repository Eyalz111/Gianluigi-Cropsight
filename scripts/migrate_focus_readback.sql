-- Focus becomes an editable surface, and needs its own merge base. [2026-08-13]
--
-- Eyal: *"this tab i believe will be the one we are working on in our meetings
-- in the end … lets say i want to delete or sign as done in this focus tab."*
--
-- Focus is the only view that shows everything needing action across all six
-- areas, so it is where a weekly review actually happens. A review that cannot
-- record its own outcome is a meeting that has to be repeated.
--
-- A SEPARATE entity_type, not a shared one. `sheet_snapshots` already holds one
-- merge base per SURFACE per entity: `uq_sheet_snapshots_task` for the Tasks
-- tab, `_ps_action` for the Project Status area tabs, `_gantt_project` for the
-- Timeline. They are partial unique indexes over the same underlying id and they
-- coexist precisely so an edit on one surface does not read as divergence on
-- another. Focus sharing the area tabs' base would make every edit on either one
-- look like a human change on the other — the cross-surface defect family of
-- 2026-08, reproduced deliberately.
--
-- WHAT IS EDITABLE THERE: Done, Due, Priority. NOT the assignee. `tasks.deadline`
-- and `tasks.assignee` are already edited daily by Nechama on the area tabs, and
-- Focus makes a FOURTH writer on those rows; the narrower surface keeps the two
-- fields that actually move in a weekly review and leaves reassignment — rarer,
-- and the likeliest to be done in two places in one week — with its existing
-- owner. Eyal's call, 2026-08-13, after being shown the risk.
--
-- SAFE TO RUN BEFORE THE CODE SHIPS. It creates two indexes and writes no data,
-- so on today's deploy it changes nothing observable. The readback that uses it
-- is flag-gated and ships shadow-on.
--
-- Safe to re-run: both indexes are IF NOT EXISTS.
--
-- No new table, so no ENABLE ROW LEVEL SECURITY here — sheet_snapshots already
-- has RLS on.

BEGIN;

-- One current snapshot per task ON THE FOCUS TAB. Partial, so it constrains only
-- Focus rows and leaves every other surface's snapshot for the same task
-- untouched — that coexistence is the entire point.
CREATE UNIQUE INDEX IF NOT EXISTS uq_sheet_snapshots_focus_task
    ON sheet_snapshots (task_id)
    WHERE entity_type = 'focus_task' AND task_id IS NOT NULL;

-- Meetings appear on Focus too (scheduled + to_schedule), and their Due maps to
-- follow_up_meetings.proposed_date rather than tasks.deadline. Same reasoning,
-- its own base.
CREATE UNIQUE INDEX IF NOT EXISTS uq_sheet_snapshots_focus_meeting
    ON sheet_snapshots (follow_up_meeting_id)
    WHERE entity_type = 'focus_meeting' AND follow_up_meeting_id IS NOT NULL;

COMMENT ON INDEX uq_sheet_snapshots_focus_task IS
    'One Focus-tab merge base per task. Partial by entity_type so it coexists '
    'with uq_sheet_snapshots_task (Tasks tab) and _ps_action (area tabs): an '
    'edit on one surface must not read as divergence on another.';

COMMIT;

-- Verify:
--   SELECT indexname FROM pg_indexes
--    WHERE indexname IN ('uq_sheet_snapshots_focus_task',
--                        'uq_sheet_snapshots_focus_meeting');
--   -- expect exactly 2 rows
--
--   -- and that the existing surfaces are untouched:
--   SELECT entity_type, count(*) FROM sheet_snapshots GROUP BY 1 ORDER BY 2 DESC;
--   -- expect task / decision / meeting / ps_action / ps_project / project /
--   -- gantt_project at their existing counts, and no focus_* rows yet
