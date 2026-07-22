-- ============================================================
-- Meetings tab — reconcile entity #4  [2026-07-22]
--
-- follow_up_meetings has been DB-only since the beginning. Its only Sheet
-- surface was add_follow_ups_as_tasks(), which appended a "Schedule: X" row to
-- the TASKS tab with 9 columns and NO col-J UUID — so every reconcile treated
-- it as a hand-added row and created a duplicate `tasks` row for it, forever.
-- Confirmed live: Tasks row 200 is "Schedule: Virtual Friday sync meeting" with
-- an empty id. This migration gives meetings their own tab and identity, and
-- that smuggling path is retired in the same change.
--
-- Fourth use of the entity_type recipe established by:
--   migrate_phase_v3_reconcile.sql            (task)
--   migrate_decision_reconcile_editable.sql   (decision)
--   migrate_gantt_rows.sql                    (gantt)
--
-- Column reuse is deliberate: title / label / status already exist on
-- sheet_snapshots and mean the same thing here, so only the genuinely new
-- fields are added. Additive + idempotent; no new table => no new RLS step.
--
-- NOTE: label / updated_at / status / scheduled_for on follow_up_meetings were
-- added by migrate_project_area_hierarchy.sql — run that FIRST.
-- ============================================================

BEGIN;

-- ============================================================
-- 1. sheet_snapshots: the meeting identity + its editable fields
-- ============================================================
ALTER TABLE sheet_snapshots
    ADD COLUMN IF NOT EXISTS follow_up_meeting_id UUID
        REFERENCES follow_up_meetings(id) ON DELETE CASCADE,
    ADD COLUMN IF NOT EXISTS led_by TEXT,
    ADD COLUMN IF NOT EXISTS proposed_date TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS participants TEXT;

-- One snapshot per meeting, scoped by entity_type (same partial-index pattern
-- as uq_sheet_snapshots_task / _decision / _gantt).
CREATE UNIQUE INDEX IF NOT EXISTS uq_sheet_snapshots_meeting
    ON sheet_snapshots(follow_up_meeting_id)
    WHERE entity_type = 'meeting';

-- ============================================================
-- 2. follow_up_meetings: sticky flags
--
--    Same six-flag + provenance shape as tasks/decisions. Without these the
--    Rule 4 rail has nothing to consult and a system write would silently
--    revert a human edit — the exact class fixed for tasks on 2026-07-22.
-- ============================================================
ALTER TABLE follow_up_meetings
    ADD COLUMN IF NOT EXISTS manual_title BOOLEAN DEFAULT FALSE,
    ADD COLUMN IF NOT EXISTS manual_label BOOLEAN DEFAULT FALSE,
    ADD COLUMN IF NOT EXISTS manual_led_by BOOLEAN DEFAULT FALSE,
    ADD COLUMN IF NOT EXISTS manual_proposed_date BOOLEAN DEFAULT FALSE,
    ADD COLUMN IF NOT EXISTS manual_participants BOOLEAN DEFAULT FALSE,
    ADD COLUMN IF NOT EXISTS manual_status BOOLEAN DEFAULT FALSE,
    ADD COLUMN IF NOT EXISTS manual_set_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS manual_set_source TEXT;

-- ============================================================
-- 3. Status vocabulary
--
--    not_scheduled -> scheduled -> held, plus dropped. Monotonic like the
--    decision statuses: a stale sheet cell must never un-schedule a meeting
--    that already happened. Plain TEXT (no CHECK) so values stay additive.
-- ============================================================
UPDATE follow_up_meetings
   SET status = 'not_scheduled'
 WHERE status IS NULL OR btrim(status) = '';

COMMIT;

-- ============================================================
-- Post-migration validation:
--   1. SELECT column_name FROM information_schema.columns
--      WHERE table_name='sheet_snapshots'
--        AND column_name IN ('follow_up_meeting_id','led_by','proposed_date','participants');
--   2. SELECT indexname FROM pg_indexes WHERE indexname='uq_sheet_snapshots_meeting';
--   3. SELECT column_name FROM information_schema.columns
--      WHERE table_name='follow_up_meetings' AND column_name LIKE 'manual_%';
--   4. SELECT status, count(*) FROM follow_up_meetings GROUP BY status;
--   5. pytest tests/test_rls_coverage.py   -- expect PASS (no new tables)
--
-- Post-migration:
--   python scripts/rollout_meetings_tab.py            # dry run
--   python scripts/rollout_meetings_tab.py --apply    # create tab + seed + backfill snapshots
--   then set MEETING_RECONCILE_ENABLED=true (shadow first)
-- ============================================================
