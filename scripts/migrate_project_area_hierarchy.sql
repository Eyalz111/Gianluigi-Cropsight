-- ============================================================
-- Project -> Area hierarchy + open-question lifecycle  [2026-07-22]
--
-- Context: the office-manager workspace needs ONE organizing axis across
-- tasks, decisions, open questions and follow-up meetings. The knowledge graph
-- already encodes it (topic_threading writes topic->area 'belongs_to' and
-- decision/task->topic 'advances'), but it was dead in practice: 0 of 50
-- topic_threads had an area_id, so the `if area_id:` guard never fired.
--
-- Design decision (Eyal, 2026-07-22): AREA IS STORED ONCE, ON THE PROJECT —
-- never per entity. Decisions/questions/meetings derive their Area through
-- their project. Reclassifying a project then moves everything under it in a
-- single edit, instead of a backfill across four tables that drifts apart.
--
-- Additive + idempotent only. No new tables => no new RLS step.
-- ============================================================

BEGIN;

-- ============================================================
-- 1. canonical_projects.area_id — the single Area anchor
-- ============================================================
ALTER TABLE canonical_projects
    ADD COLUMN IF NOT EXISTS area_id UUID REFERENCES areas(id) ON DELETE SET NULL;

CREATE INDEX IF NOT EXISTS idx_canonical_projects_area
    ON canonical_projects(area_id);

-- ============================================================
-- 2. label (project) on the entities that lacked one
--
--    Tasks and decisions already carry `label`. Open questions and follow-up
--    meetings did not, so they had no way to join the hierarchy at all.
--    Same field, same vocabulary (canonical_projects), same resolve_label().
-- ============================================================
ALTER TABLE open_questions
    ADD COLUMN IF NOT EXISTS label TEXT;

ALTER TABLE follow_up_meetings
    ADD COLUMN IF NOT EXISTS label TEXT;

-- ============================================================
-- 3. Open-question lifecycle
--
--    `status` was open|resolved with no CHECK constraint, and the only exit
--    was a later meeting explicitly answering the question — which almost
--    never fires. Result: 100+ questions open since May, an inbox with no
--    outbox. Adding states is additive; nothing is deleted, ever.
--      open        - live, needs an answer
--      resolved    - answered (existing)
--      stale       - aged out at 60 days untouched (reversible)
--      dropped     - explicitly not pursuing
--      superseded  - overtaken by a decision
-- ============================================================
ALTER TABLE open_questions
    ADD COLUMN IF NOT EXISTS status_changed_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS status_reason TEXT;

CREATE INDEX IF NOT EXISTS idx_open_questions_status_created
    ON open_questions(status, created_at DESC);

-- ============================================================
-- 4. follow_up_meetings: updated_at + status
--
--    Both were entirely absent, and a reconcile needs updated_at to tell
--    "DB changed since snapshot". Required before the Meetings tab becomes
--    editable in Stage 1.
-- ============================================================
ALTER TABLE follow_up_meetings
    ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ DEFAULT NOW(),
    ADD COLUMN IF NOT EXISTS status TEXT DEFAULT 'not_scheduled',
    ADD COLUMN IF NOT EXISTS scheduled_for TIMESTAMPTZ;

CREATE INDEX IF NOT EXISTS idx_follow_up_meetings_status
    ON follow_up_meetings(status);

COMMIT;

-- ============================================================
-- Post-migration validation (run manually after applying):
--   1. Area anchor:
--      SELECT column_name FROM information_schema.columns
--      WHERE table_name='canonical_projects' AND column_name='area_id';
--   2. Labels on the new entities:
--      SELECT table_name, column_name FROM information_schema.columns
--      WHERE column_name='label'
--        AND table_name IN ('open_questions','follow_up_meetings');
--   3. Follow-up reconcile prerequisites:
--      SELECT column_name FROM information_schema.columns
--      WHERE table_name='follow_up_meetings'
--        AND column_name IN ('updated_at','status','scheduled_for');
--   4. RLS coverage unchanged: pytest tests/test_rls_coverage.py  -- expect PASS
--
-- Post-migration backfill:
--   python scripts/backfill_project_areas_2026_07.py           # dry run
--   python scripts/backfill_project_areas_2026_07.py --apply
-- ============================================================
