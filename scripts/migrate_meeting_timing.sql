-- A meeting's timing, in Eyal's own words. [2026-08-12]
--
-- FIXES LIVE DATA LOSS. `proposed_date` is a timestamptz, and the Proposed Date
-- cell went straight into it. Eyal writes things like "Once a week" there — he
-- is telling Nechama roughly when he wants it, not booking a slot — so the
-- insert raised 22007, a broad `except` returned None, and the caller did
-- `if not created: continue`. The WHOLE ROW was discarded, retried and
-- discarded again every thirty minutes:
--
--   Error creating manual follow-up meeting 'CropSight Weekly Managment Meeting':
--   invalid input syntax for type timestamp with time zone: "Once a week"
--
-- Six meetings existed only in the sheet. Nothing else in the system knew they
-- were there, and a rebuild of that tab from the database would have erased
-- them.
--
-- `timing_text` holds the words verbatim and is never parsed away — it is what
-- Nechama reads, and the system has no business rewriting it. The parsed
-- structure sits ALONGSIDE, never instead: round-tripping the text through a
-- parser would eventually turn "end of August, before Paolo travels" into
-- "31/08/2026" and lose the reason.
--
-- Additive and idempotent. No new tables => no new RLS step.

BEGIN;

ALTER TABLE follow_up_meetings
    ADD COLUMN IF NOT EXISTS timing_text  TEXT,
    ADD COLUMN IF NOT EXISTS recurrence   TEXT,
    ADD COLUMN IF NOT EXISTS window_start DATE,
    ADD COLUMN IF NOT EXISTS window_end   DATE;

-- NO DEFAULT and NO BACKFILL. Writing anything into timing_text for the 125
-- existing rows would be inventing an intention nobody expressed — the mistake
-- `priority TEXT DEFAULT 'M'` made across 122 meetings on 2026-08-09. A row
-- that has never carried a timing phrase simply has none.

COMMENT ON COLUMN follow_up_meetings.timing_text IS
    'When Eyal wants this, in his words: "Once a week", "end of August". '
    'Verbatim, never parsed away. proposed_date/recurrence/window_* are the '
    'machine-readable reading of it, and may all be null while this is set.';
COMMENT ON COLUMN follow_up_meetings.recurrence IS
    'Normalised cadence when timing_text expresses one: weekly | biweekly | '
    'monthly | quarterly | annual | daily | twice_weekly | bimonthly.';
COMMENT ON COLUMN follow_up_meetings.window_start IS
    'Start of the range when timing_text expresses one ("week of 15/9", '
    '"end of August") rather than a single date.';

COMMIT;

-- Verify by probing the NEW COLUMNS, never a value a prior migration could
-- have set:
--   SELECT timing_text, recurrence, window_start FROM follow_up_meetings LIMIT 1;
--   -- 42703 = not applied
--   SELECT count(*) FROM follow_up_meetings WHERE timing_text IS NOT NULL;
--   -- 0 until the sheet is next read; the six dropped rows appear then
