-- v3 Gantt redesign — lane fields (chunk 2, fixed-template Gantt)
-- Run in Supabase SQL editor. ADDITIVE + idempotent.
--
-- gantt_rows already holds area_id/topic_id/owner/status/week_start-end/manual_*.
-- The fixed-template Gantt is a set of LANES per area (2 Planning, 3 Execution,
-- 1 Meetings, 1 HR) + Milestones/Management bands. These columns capture the
-- lane identity + the workstream content label (which lives in the timeline
-- cells, while the lane label — "Planning #1" — is fixed on the left).

BEGIN;

ALTER TABLE gantt_rows
    ADD COLUMN IF NOT EXISTS lane_type  TEXT,      -- planning|execution|meetings|hr|milestone|management
    ADD COLUMN IF NOT EXISTS lane_index INTEGER,   -- 1,2 (planning) / 1,2,3 (execution) / 1 (others)
    ADD COLUMN IF NOT EXISTS label      TEXT,      -- workstream content shown in the timeline bar
    ADD COLUMN IF NOT EXISTS manual_label BOOLEAN DEFAULT FALSE;  -- Eyal renamed the content (sticky)

-- A lane is unique per (sheet, area, lane_type, lane_index) among current rows.
CREATE UNIQUE INDEX IF NOT EXISTS uq_gantt_rows_lane
    ON gantt_rows(sheet_name, area_id, lane_type, lane_index)
    WHERE valid_to IS NULL AND lane_type IS NOT NULL;

COMMIT;

-- Validation:
--   SELECT column_name FROM information_schema.columns
--   WHERE table_name='gantt_rows' AND column_name IN ('lane_type','lane_index','label','manual_label');
