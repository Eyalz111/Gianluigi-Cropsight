-- `not_scheduled` becomes `to_schedule`. [2026-08-12]
--
-- Eyal: *"we are starting working on a concept of what is 'parked' nechama is
-- not going to schedule yet, what is not schedule she is going to schedule and
-- i think i want to change the terminology to 'to schedule'."*
--
-- The two words describe different things and the old one described the wrong
-- one. `not_scheduled` states a fact about the meeting — nobody has booked it.
-- `to_schedule` states an instruction to Nechama — book this. Beside `parked`,
-- which means "deliberately not yet", the pair finally reads as the decision it
-- is. The tab's own banner has said "Scheduled first, then to schedule, parked,
-- and held" since the pool was built; the stored value was the last thing still
-- using the old word.
--
-- THE CODE ACCEPTS BOTH REGARDLESS. `canonical_meeting_status()` maps the old
-- spelling forward, so nothing breaks if this is never run and nothing breaks
-- for a cell somebody typed last week. This migration only stops the old value
-- being re-read and re-canonicalised forever.
--
-- Safe to re-run. Idempotent by construction: the second run matches no rows.

BEGIN;

UPDATE follow_up_meetings
   SET status = 'to_schedule'
 WHERE status = 'not_scheduled';

-- The snapshot rail holds its own copy of the status as the merge base. Left
-- alone, every migrated row would read as "the database changed, the sheet did
-- not" on the next cycle and get pushed back over — 8 cells rewritten for a
-- change nobody made on the sheet.
UPDATE sheet_snapshots
   SET status = 'to_schedule'
 WHERE status = 'not_scheduled'
   AND entity_type = 'meeting';

COMMIT;

-- Verify:
--   SELECT status, count(*) FROM follow_up_meetings GROUP BY status ORDER BY 2 DESC;
--   -- expect to_schedule where not_scheduled was; expect NO not_scheduled left
--   SELECT count(*) FROM sheet_snapshots
--    WHERE entity_type = 'meeting' AND status = 'not_scheduled';   -- expect 0
