-- A project gets a start date, so a bar can have a left edge. [2026-08-12]
--
-- Phase 1 of docs/GANTT_V2_PLAN.md.
--
-- EVERY date in this database is an END: canonical_projects.target_date,
-- tasks.deadline, follow_up_meetings.proposed_date. Nothing records when work
-- BEGAN, which is why the old Gantt is the only place a start exists at all —
-- encoded as "which week column did someone start typing in".
--
-- `start_date` is DERIVED then FROZEN, not recomputed. The obvious
-- implementation — "earliest task's created_at, evaluated on every render" —
-- moves backwards the moment an older task is attached to the project, so a
-- bar silently redraws and reads as a plan change nobody made. Compute once,
-- store, and from then on it is frozen-or-manual.
--
-- `manual_start_date` is the sticky rail every other editable field has, and it
-- joins _PROJECT_MANUAL_FIELDS so Rule 2 stops inference overwriting a date a
-- human chose.
--
-- DELIBERATELY NO DEFAULT. `priority TEXT DEFAULT 'M'` on follow_up_meetings
-- backfilled all 122 rows and the reconcile then stamped "M" across a column
-- nobody had touched, announcing a triage that had not happened. A project
-- whose start nobody has established is NULL, and renders with no left edge.
--
-- Safe to re-run.

ALTER TABLE canonical_projects
    ADD COLUMN IF NOT EXISTS start_date        DATE,
    ADD COLUMN IF NOT EXISTS manual_start_date BOOLEAN DEFAULT FALSE;

COMMENT ON COLUMN canonical_projects.start_date IS
    'When work began. Derived once from the earliest task, then frozen; or set '
    'by a human. Never recomputed — see docs/GANTT_V2_PLAN.md Phase 1.';
COMMENT ON COLUMN canonical_projects.manual_start_date IS
    'A human set this start date. Rule 2 will not overwrite it.';

-- Verify by probing the NEW COLUMNS (42703 = absent), never a value a prior
-- migration could have set:
--   SELECT column_name, data_type, column_default
--     FROM information_schema.columns
--    WHERE table_name = 'canonical_projects'
--      AND column_name IN ('start_date', 'manual_start_date');
-- Expect both listed, and column_default NULL for start_date.
