-- Migration: operational-task floor (Meeting-Summaries upgrade, PR1)
-- ADDITIVE + nullable/defaulted — existing rows and every current code path are
-- unaffected (no output reads these until the later "flip" PR). Safe to apply on
-- the live DB with no backfill.
--
-- Adds the URGENCY axis (the priority×urgency matrix) and a HARD area linkage to
-- tasks. `tasks` and `areas` already exist and already have RLS — no NEW table,
-- so no new RLS grant is required here.

ALTER TABLE tasks
    -- Time-pressure, SEPARATE from priority (importance). H|M|L, default M.
    -- Derived from deadline proximity + explicit signals; never fabricates a date
    -- ("ASAP" -> urgency H, deadline NULL).
    ADD COLUMN IF NOT EXISTS urgency TEXT DEFAULT 'M',
    -- Structural link to a Gantt Area (one-way, nullable). ON DELETE SET NULL so
    -- removing an area never cascades into tasks.
    ADD COLUMN IF NOT EXISTS area_id UUID REFERENCES areas(id) ON DELETE SET NULL,
    -- The HARD field: always present. One of the 6 Gantt area names, or the
    -- 'non-area' sentinel for genuine misfits. Denormalized so outputs/sheet can
    -- group by area without a join even when area_id is unresolved.
    ADD COLUMN IF NOT EXISTS area_label TEXT DEFAULT 'non-area';

CREATE INDEX IF NOT EXISTS idx_tasks_urgency ON tasks(urgency);
CREATE INDEX IF NOT EXISTS idx_tasks_area ON tasks(area_id) WHERE area_id IS NOT NULL;

COMMENT ON COLUMN tasks.urgency IS
    'Time-pressure H/M/L, separate from priority(importance) — the urgency axis of the priority x urgency matrix. Never implies a deadline.';
COMMENT ON COLUMN tasks.area_label IS
    'Hard area linkage: one of the 6 Gantt area names or the ''non-area'' sentinel.';
