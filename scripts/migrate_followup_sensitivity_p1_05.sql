-- Migration: follow_up_meetings.sensitivity  (audit P1-01 / P1-05)
--
-- Gives the 4th extraction child table a sensitivity tier so the I3 invariant
-- ("sensitivity follows data") holds for follow-ups too — today they carry no
-- tier and default to team-visible. Additive + backfill-friendly: the column
-- defaults to 'founders' (the safe middle tier) and existing rows are backfilled
-- from their parent meeting's tier.
--
-- RLS: follow_up_meetings already has ROW LEVEL SECURITY enabled
-- (setup_supabase.sql:237). A new column inherits the table's RLS, so no new
-- policy is required (service-role key bypasses it; tests/test_rls_coverage.py
-- stays green).
--
-- DEPLOY ORDER: apply this migration BEFORE flipping
-- FOLLOW_UP_SENSITIVITY_ENABLED=true. The code only writes/propagates the column
-- when that flag is on, so deploying the code first is safe — the feature stays
-- dark until BOTH the column exists and the flag is flipped.

ALTER TABLE follow_up_meetings
    ADD COLUMN IF NOT EXISTS sensitivity TEXT DEFAULT 'founders';

-- Backfill existing rows from their source meeting's tier (founders where the
-- meeting has none). Idempotent — re-running yields the same result.
UPDATE follow_up_meetings fum
SET sensitivity = COALESCE(m.sensitivity, 'founders')
FROM meetings m
WHERE fum.source_meeting_id = m.id;

-- Verify (optional):
--   SELECT sensitivity, count(*) FROM follow_up_meetings GROUP BY sensitivity;
