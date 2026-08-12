-- A project's status is DECLARED, not counted. [2026-08-13]
--
-- Decision 9 of docs/GANTT_V2_PLAN.md, and decision 8 cannot be built without
-- it. Today `canonical_projects.status` holds only 'active' (23 rows) and
-- 'retired' (4), and the Timeline INFERS the rest: `_status_of` returns 'active'
-- when a project has open tasks and 'planned' when it has none. So a project
-- whose work is simply finished renders in the same blue as one that has never
-- started, and nothing anywhere can tell the two apart.
--
-- That is fatal to the fold. Option A keeps visible height tracking OPEN work by
-- folding completed projects into one collapsed line per area, and "completed"
-- has to be a fact somebody stated. Counting open tasks would fold a project the
-- moment its last task was closed — including the ones where the work is
-- ongoing and nobody has written the next task down yet, which on this team is
-- normal: a new task with no deadline or assignee is how the summary arrives.
--
--   active   work is moving
--   blocked  work is stopped on something external
--   done     finished, successfully
--   retired  abandoned or superseded — kept, never deleted
--
-- 'planned' is deliberately NOT in the vocabulary. A project that has not
-- started yet is visible from its start_date being in the future, which is a
-- fact about a date rather than a claim about intent, and deriving it costs
-- nothing. It was the wrong home for "finished" and that is the whole defect.
--
-- Safe to re-run. Every statement is IF EXISTS / IF NOT EXISTS or idempotent by
-- construction, and both existing values are inside the new CHECK, so the
-- constraint validates without touching a row.
--
-- No new table, so no ENABLE ROW LEVEL SECURITY here (canonical_projects
-- already has RLS on).

BEGIN;

ALTER TABLE canonical_projects
    ADD COLUMN IF NOT EXISTS manual_status BOOLEAN DEFAULT FALSE,
    ADD COLUMN IF NOT EXISTS done_at       TIMESTAMPTZ;

-- NO DEFAULT ON done_at. A DEFAULT backfills every row and the reconcile then
-- announces a decision nobody made — exactly what `priority TEXT DEFAULT 'M'`
-- did to 122 meetings on 2026-08-09.

-- Widen the vocabulary. Dropped first by name so a re-run does not collide, and
-- the name is spelled out rather than left to Postgres's default so the DROP and
-- the ADD are guaranteed to be talking about the same constraint.
ALTER TABLE canonical_projects
    DROP CONSTRAINT IF EXISTS canonical_projects_status_check;
ALTER TABLE canonical_projects
    ADD CONSTRAINT canonical_projects_status_check
    CHECK (status IN ('active', 'blocked', 'done', 'retired'));

-- `done_at` answers "how long has this been finished", which is what the month's
-- grace needs: a project leaves the main list a month after it is marked done,
-- so recent wins are still visible where the work happens.
--
-- A TRIGGER for the same reason held_at gets one (migrate_meeting_held_at.sql):
-- `status` is written from the Projects reconcile, from MCP, from the project
-- learner and from one-off scripts, and a Python choke point would leave the
-- scripts out. `updated_at` cannot stand in — it moves on every edit, so
-- renaming a finished project would restart its grace month.
CREATE OR REPLACE FUNCTION public.stamp_project_done_at()
RETURNS TRIGGER AS $$
BEGIN
    IF NEW.status = 'done'
       AND (TG_OP = 'INSERT' OR OLD.status IS DISTINCT FROM 'done') THEN
        NEW.done_at := COALESCE(NEW.done_at, now());
    ELSIF NEW.status IS DISTINCT FROM 'done' THEN
        -- Reopened. A stale done_at would fold it away a month later even though
        -- the work restarted.
        NEW.done_at := NULL;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_project_done_at ON canonical_projects;
CREATE TRIGGER trg_project_done_at
    BEFORE INSERT OR UPDATE ON canonical_projects
    FOR EACH ROW EXECUTE FUNCTION public.stamp_project_done_at();

COMMIT;

-- NO BACKFILL, deliberately. Nothing in the database knows which of the 23
-- active projects are actually finished — that is precisely the information the
-- column exists to capture, and guessing it from "no open tasks" is the
-- inference this migration removes. The four retired ones stay retired. Eyal
-- marks the finished ones himself, and until he does the board reads exactly as
-- it does today.

-- Verify:
--   SELECT status, count(*) FROM canonical_projects GROUP BY status;
--   -- expect only active/retired until anything is declared
--
--   SELECT column_name FROM information_schema.columns
--    WHERE table_name = 'canonical_projects' AND column_name IN ('manual_status','done_at');
--   -- expect 2 rows (probe the NEW columns, never a value a prior migration could have set)
