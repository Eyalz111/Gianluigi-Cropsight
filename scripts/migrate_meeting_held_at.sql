-- Held meetings need to know WHEN they became held. [2026-08-13]
--
-- Eyal: held meetings should sink to the bottom of the Meetings tab and then
-- disappear after two weeks. The sinking already works (MEETING_DISPLAY_ORDER
-- puts held second-from-last). The two weeks did not, because nothing in the
-- schema could answer "two weeks since WHAT".
--
-- `updated_at` cannot answer it. It moves on every edit, and the reconcile edits
-- rows: both held meetings on the live tab carry `updated_at` of 2026-08-12
-- 21:03 — the timestamp of last night's sync, not of the day the meeting
-- happened. Keying the timer on it means renaming a held meeting silently
-- restarts its two weeks, and a tab-wide write restarts everyone's at once. The
-- dropped-meetings timer keys on `updated_at` today and carries exactly that
-- flaw; it is left alone here because 60 days absorbs it and changing two timers
-- at once would make a regression ambiguous.
--
-- So: a column that is written ONCE, on the transition into `held`, and is not
-- touched by any later edit.
--
-- A TRIGGER, not Python. `status` is written from the meetings reconcile, from
-- create_follow_up_meeting_manual, from MCP, and from one-off repair scripts.
-- Stamping it at a Python choke point would leave the scripts out, and a repair
-- script bypassing a guard the engine has is precisely how 30 meetings got
-- dropped instead of 8 on 2026-08-09. At the database there is no path around it.
--
-- Safe to re-run. Idempotent by construction: IF NOT EXISTS on the column,
-- CREATE OR REPLACE on the function, DROP ... IF EXISTS before the trigger, and
-- the backfill matches no rows on a second run.
--
-- No new table, so no ENABLE ROW LEVEL SECURITY is required here (the mandate in
-- CLAUDE.md is per CREATE TABLE; follow_up_meetings already has RLS on).

BEGIN;

ALTER TABLE follow_up_meetings
    ADD COLUMN IF NOT EXISTS held_at TIMESTAMPTZ;

-- Set on the way IN, cleared on the way OUT, untouched while it stays held.
--
-- The middle case is the one that matters and it is the case with no branch:
-- when OLD.status and NEW.status are both 'held' neither arm fires, so an edit
-- to the title or the participants leaves the clock exactly where it was.
--
-- COALESCE rather than a bare now(): it lets the backfill below, and any future
-- correction, state a held_at explicitly without the trigger overwriting it with
-- the moment the correction happened.
CREATE OR REPLACE FUNCTION public.stamp_meeting_held_at()
RETURNS TRIGGER AS $$
BEGIN
    IF NEW.status = 'held'
       AND (TG_OP = 'INSERT' OR OLD.status IS DISTINCT FROM 'held') THEN
        NEW.held_at := COALESCE(NEW.held_at, now());
    ELSIF NEW.status IS DISTINCT FROM 'held' THEN
        -- A meeting walked back out of held starts again from nothing. Leaving a
        -- stale timestamp behind would archive it two weeks after a hold that
        -- was undone.
        NEW.held_at := NULL;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_meeting_held_at ON follow_up_meetings;
CREATE TRIGGER trg_meeting_held_at
    BEFORE INSERT OR UPDATE ON follow_up_meetings
    FOR EACH ROW EXECUTE FUNCTION public.stamp_meeting_held_at();

-- Backfill: 48 rows are already held and none of them recorded the moment.
--
-- `updated_at` is an UPPER BOUND, not the answer — a meeting became held at or
-- before its last edit, never after. Using it therefore errs toward keeping a
-- row visible longer than strictly needed, never toward vanishing one early,
-- which is the right direction for a timer whose failure mode is "history
-- disappeared before anyone read it".
--
-- In practice this touches two rows that anyone will see: 46 of the 48 are
-- already on Past Meetings, where held_at is only bookkeeping.
UPDATE follow_up_meetings
   SET held_at = updated_at
 WHERE status = 'held'
   AND held_at IS NULL;

COMMIT;

-- Verify:
--   SELECT count(*) FROM follow_up_meetings WHERE status='held' AND held_at IS NULL;
--   -- expect 0
--
--   -- the trigger holds the clock still across an unrelated edit:
--   SELECT held_at FROM follow_up_meetings WHERE status='held' LIMIT 1;
--   -- note the value, touch the row's title, re-select: held_at must be identical
--   -- while updated_at has moved.
