-- Phase 1 (Task/Decision Flow finalize, 2026-07): make Task text + label editable.
--
-- The reconcile previously treated Task text (col C) and Label (col B) as one-way
-- DB->Sheet content — so a manual edit to either was silently overwritten on the
-- next reconcile (Eyal's /sync incident, 2026-07-06). This migration adds the
-- substrate to reconcile them like the action fields (snapshot-based "manual wins
-- & sticks"):
--   1. sheet_snapshots: carry the last-synced title/label so a Sheet edit can be
--      attributed to Eyal (Sheet-now != snapshot) vs an untouched cell.
--   2. tasks: per-field manual flags for title/label (mirror manual_status/etc.)
--      so inference can propose-not-clobber a text Eyal set (Phase 1 Step 3).
--
-- ALL changes are ADDITIVE and idempotent (IF NOT EXISTS). Never drops/wipes.
-- sheet_snapshots already has RLS enabled (migrate_phase_v3_reconcile.sql) — no
-- new table, so no new RLS step needed. Enforced by tests/test_rls_coverage.py.

BEGIN;

-- 1. sheet_snapshots: last-synced content columns (mirror the action columns).
ALTER TABLE sheet_snapshots
    ADD COLUMN IF NOT EXISTS title TEXT,
    ADD COLUMN IF NOT EXISTS label TEXT;

-- 2. tasks: per-field manual-override flags for the content columns (sticky),
--    mirroring manual_status/manual_deadline/manual_priority/manual_assignee.
ALTER TABLE tasks
    ADD COLUMN IF NOT EXISTS manual_title BOOLEAN DEFAULT FALSE,
    ADD COLUMN IF NOT EXISTS manual_label BOOLEAN DEFAULT FALSE;

COMMIT;

-- ============================================================
-- Post-migration backfill (run scripts/backfill_snapshot_content.py):
--   Seeds title/label on every existing sheet_snapshots row from the current DB
--   task, so the first reconcile after deploy sees snap == db == sheet and does
--   NOT mistake an untouched cell for an Eyal edit (phantom-pull, audit P1-04).
--   (The reconcile also guards this with a sheet != db check, but the backfill
--   keeps snapshots honest.)
--
-- Post-migration validation (run manually after applying):
--   1. New columns on sheet_snapshots:
--      SELECT column_name FROM information_schema.columns
--      WHERE table_name='sheet_snapshots' AND column_name IN ('title','label');
--   2. New manual flags on tasks:
--      SELECT column_name FROM information_schema.columns
--      WHERE table_name='tasks' AND column_name IN ('manual_title','manual_label');
--   3. RLS coverage test: pytest tests/test_rls_coverage.py  -- expect PASS
-- ============================================================
