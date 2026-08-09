-- An ACTION gets its own objective, like a project has one. [2026-08-09]
--
-- Eyal: "i want to have 'to do' for both the actions and the projects - each one
-- of them should have it".
--
-- `To do` already means the objective on a PROJECT row (canonical_projects.
-- objective). On an ACTION row nothing read it, so text typed there was
-- silently swallowed — Nechama lost "Search for investors" that way.
--
-- This is NOT the Subject/Project ambiguity coming back. That column DECIDED
-- what kind of row it was. This one only reads a kind already declared, and the
-- sheet has the same shape already: `Resp.` is the project's owner on a project
-- row and the step's owner on an action row, and nobody finds it confusing.
-- Same concept at two levels.
--
-- `manual_objective` is the sticky rail every other editable field has: it
-- records that a HUMAN set this, so Rule 2 stops the system overwriting it.
-- Adding the column without the rail is what left meeting priorities
-- unprotected earlier today.
--
-- NOTE: no migration is needed for the snapshot. `sheet_snapshots` is shared
-- across entity types and ALREADY carries an `objective` column (ps_project
-- uses it), so the ps_action rail reuses it as-is.
--
-- Safe to re-run.

ALTER TABLE tasks
    ADD COLUMN IF NOT EXISTS objective        TEXT,
    ADD COLUMN IF NOT EXISTS manual_objective BOOLEAN DEFAULT FALSE;

-- DELIBERATELY NO DEFAULT. `priority TEXT DEFAULT 'M'` on follow_up_meetings
-- backfilled all 122 rows this morning and the reconcile then stamped "M"
-- across a column nobody had touched, announcing a triage that had not
-- happened. An objective nobody has written is NULL, and renders blank.

COMMENT ON COLUMN tasks.objective IS
    'What this step is meant to achieve. The `To do` cell on its Project Status row.';

-- Verify (expect both listed, column_default NULL for objective):
--   SELECT column_name, data_type, column_default
--     FROM information_schema.columns
--    WHERE table_name = 'tasks'
--      AND column_name IN ('objective', 'manual_objective');
