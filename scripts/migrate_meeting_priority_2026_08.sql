-- Meetings get a priority, like tasks. [2026-08-09]
--
-- Eyal: "i think we should have priorities also for the meetings".
--
-- Same vocabulary as tasks.priority so one scale means one thing everywhere:
--   U = Urgent, H = High, M = Medium, L = Low
-- Plain TEXT with no CHECK, matching tasks.priority — that is what let 'U' be
-- added there without a migration, and the sheet refuses anything off-vocabulary
-- before it can reach the column.
--
-- `manual_priority` is the sticky rail every other editable field already has:
-- it records that a HUMAN set this, so the system stops overwriting it.
--
-- Safe to re-run.

ALTER TABLE follow_up_meetings
    ADD COLUMN IF NOT EXISTS priority        TEXT    DEFAULT 'M',
    ADD COLUMN IF NOT EXISTS manual_priority BOOLEAN DEFAULT FALSE;

-- Recurring meetings are a STATUS, not a priority — see the note in
-- services/google_sheets.py. `status` is TEXT with no CHECK, so 'recurring'
-- needs no migration; this comment exists so the next person looking for one
-- knows it was a decision rather than an omission.

COMMENT ON COLUMN follow_up_meetings.priority IS
    'U/H/M/L — same scale as tasks.priority. Set from the Meetings tab.';

-- Verify (expect: priority + manual_priority listed):
--   SELECT column_name, data_type, column_default
--     FROM information_schema.columns
--    WHERE table_name = 'follow_up_meetings'
--      AND column_name IN ('priority', 'manual_priority');
