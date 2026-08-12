-- Milestones: the company's dated commitments, and their slippage. [2026-08-12]
--
-- Phase 3 of docs/GANTT_V2_PLAN.md.
--
-- WHY A TABLE OF THEIR OWN. A milestone is not a project: no owner, no task
-- tree, not necessarily an area. It is not a task either: tasks are actions
-- somebody performs, milestones are outcomes that either arrive or do not.
-- Forcing them into canonical_projects would give every one of them an owner
-- field to leave blank, and Eyal has been explicit: "milestone has no owner
-- because it is a company thing."
--
-- WHAT `original_date` IS FOR. The old board carries its own slippage history:
-- `Signing #1 MVP client` sits at 2026-06-01, and a second bar seven weeks later
-- reads `Signing #1 MVP client - postponed here`. A model that simply moved
-- target_date would lose the fact that this was ever committed to June. So
-- original_date is written ONCE, at creation, and never updated; target_date
-- moves; and each move leaves a row in milestone_moves.
--
-- NO `reason` COLUMN, AND THAT IS DELIBERATE. An earlier draft proposed
-- recording WHY a milestone moved — slip vs. deliberate re-plan vs. added
-- scope. Eyal rejected it: "I don't see a reason to have a 'reason per move'
-- that will force me to add explanations or even worse, will try to guess it
-- automatically." A required reason is friction on every move; an optional one
-- is an empty slot something will eventually fill by inference, which would be
-- the system asserting intent it cannot observe. The record holds the FACT and
-- nothing else: the dates, and when they changed. Any explanation lives in the
-- free-text title, exactly as the old board already does it — written by a
-- human who felt like writing it, never prompted for and never inferred.
--
-- So the CEO tab shows "moved 1 Jun -> 6 Jul", never "SLIPPED". Whether that
-- was a slip or a re-plan is Eyal's read, and the board must not put a word in
-- his mouth.
--
-- Safe to re-run.

CREATE TABLE IF NOT EXISTS milestones (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),

    title           TEXT NOT NULL,
    kind            TEXT,           -- funding | product | commercial | corporate

    -- The dates. `target_date` is the current commitment and moves;
    -- `original_date` is the first one and never does.
    target_date     DATE,
    original_date   DATE,

    -- open | hit | missed | dropped. No DEFAULT beyond 'open' because 'open' is
    -- genuinely what a new milestone is, not a decision being backfilled — the
    -- distinction that `priority TEXT DEFAULT 'M'` got wrong for 122 meetings.
    status          TEXT NOT NULL DEFAULT 'open',

    -- Optional anchors. NO owner column: a milestone belongs to the company.
    area_id         UUID REFERENCES areas(id) ON DELETE SET NULL,
    project_id      UUID REFERENCES canonical_projects(id) ON DELETE SET NULL,

    notes           TEXT,
    source_label    TEXT,           -- the archived bar this was seeded from

    -- The manual rails, same shape as every other editable field here: a human
    -- decision must not be overwritten by later inference.
    manual_target_date BOOLEAN NOT NULL DEFAULT FALSE,
    manual_status      BOOLEAN NOT NULL DEFAULT FALSE,
    manual_set_at      TIMESTAMPTZ,
    manual_set_source  TEXT,

    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

ALTER TABLE milestones ENABLE ROW LEVEL SECURITY;

-- One row per move. The child table is what makes "moved 1 Jun -> 6 Jul"
-- expressible more than once; original_date alone only remembers the first
-- commitment, not the path.
CREATE TABLE IF NOT EXISTS milestone_moves (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    milestone_id  UUID NOT NULL REFERENCES milestones(id) ON DELETE CASCADE,
    from_date     DATE,
    to_date       DATE,
    moved_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    source        TEXT            -- 'legacy_board' | 'sheet' | 'mcp' | 'telegram'
);

ALTER TABLE milestone_moves ENABLE ROW LEVEL SECURITY;

CREATE INDEX IF NOT EXISTS idx_milestones_target
    ON milestones (target_date);
CREATE INDEX IF NOT EXISTS idx_milestone_moves_milestone
    ON milestone_moves (milestone_id, moved_at);

-- A milestone is identified by its title. Seeding runs repeatedly (it rides the
-- daily QA), and without this a re-run would insert "Signing #1 MVP client" a
-- second time rather than recognising it.
CREATE UNIQUE INDEX IF NOT EXISTS uq_milestones_title
    ON milestones (lower(btrim(title)));

COMMENT ON TABLE milestones IS
    'Company-level dated commitments. No owner by design — see '
    'docs/GANTT_V2_PLAN.md decision 5. original_date never changes.';
COMMENT ON COLUMN milestones.original_date IS
    'The FIRST committed date. Written once at creation, never updated — it is '
    'what makes a move visible instead of silent.';
COMMENT ON TABLE milestone_moves IS
    'One row per target_date change. Records the fact only: no reason column, '
    'deliberately — see docs/GANTT_V2_PLAN.md decision on milestone moves.';

-- Verify by probing the NEW TABLES, never a value a prior migration could have
-- set:
--   SELECT tablename, rowsecurity FROM pg_tables
--    WHERE schemaname = 'public' AND tablename IN ('milestones','milestone_moves');
--   SELECT count(*) FROM milestones;      -- 0 until the seeder proposes and
--   SELECT count(*) FROM milestone_moves; -- Eyal approves
