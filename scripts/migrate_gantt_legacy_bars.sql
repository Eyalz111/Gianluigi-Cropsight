-- Archive the old Gantt's timelines before anything replaces them. [2026-08-12]
--
-- Phase 0 of docs/GANTT_V2_PLAN.md.
--
-- The board at GANTT_SHEET_ID is a WEEKLY-COLUMN board: a bar is the same text
-- repeated across consecutive week cells, and row 5 carries "W9 02/03" style
-- headers, so every column has a real calendar date. That makes ~400 bars
-- recoverable as actual dated spans (2026-03-02 -> 2027-12-27) — and right now
-- that sheet is the ONLY record of them. If a cell is edited or a row dragged,
-- the history is gone with no trace. This table is the copy that survives.
--
-- DELIBERATELY A DUMB ARCHIVE. No foreign keys, no project matching, no
-- interpretation. Matching a bar to a canonical_project is a JUDGEMENT — naive
-- name overlap seeds "Corporate" to 2027-05-31 and maps four different projects
-- onto the same bar — so that judgement belongs in a proposal Eyal approves,
-- not in the record of what the board said. Keep this table faithful to the
-- sheet and let later phases interpret it.
--
-- `lane` is stored raw and unfiltered, including the "Meeting Count (helper)"
-- and "All Meetings (Aggregate)" rows whose cells hold numbers rather than
-- work. Dropping them here would be interpretation too; readers filter by lane.
--
-- Safe to re-run.

CREATE TABLE IF NOT EXISTS gantt_legacy_bars (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),

    -- Where it sat on the board, exactly as written there.
    source_tab    TEXT NOT NULL,          -- '2026-2027'
    section       TEXT,                   -- 'PRODUCT & TECHNOLOGY'
    lane          TEXT,                   -- 'Execution #1', 'Company OKRs'
    row_number    INT,                    -- 1-indexed sheet row, for spot-checks
    label         TEXT NOT NULL,          -- the cell text, tags and all

    -- The recovered span. Both ends are REAL DATES read from the week headers,
    -- not week indexes — the whole point of the extraction.
    start_date    DATE NOT NULL,
    end_date      DATE NOT NULL,
    weeks         INT  NOT NULL,

    -- Owner initials as the board writes them ('[R/E]', '[E/P]'). Left as the
    -- raw prefix rather than resolved to roster names: resolving is a guess
    -- ('[E]' is almost certainly Eyal, but the board never says so) and a guess
    -- does not belong in an archive.
    owner_tag     TEXT,

    extracted_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

ALTER TABLE gantt_legacy_bars ENABLE ROW LEVEL SECURITY;

-- Re-extraction replaces a tab wholesale (delete by source_tab, re-insert), so
-- this index serves both that delete and the "what did we plan in March" reads.
CREATE INDEX IF NOT EXISTS idx_gantt_legacy_bars_tab
    ON gantt_legacy_bars (source_tab, start_date);

COMMENT ON TABLE gantt_legacy_bars IS
    'Faithful archive of the pre-v2 Gantt board''s dated bars. Dumb by design: '
    'no project matching, no filtering. See docs/GANTT_V2_PLAN.md Phase 0.';
COMMENT ON COLUMN gantt_legacy_bars.start_date IS
    'Real calendar date from the week-column header, not a week index.';
COMMENT ON COLUMN gantt_legacy_bars.owner_tag IS
    'Raw initials prefix as written on the board, e.g. [R/E]. Never resolved.';

-- Verify (expect the table listed WITH rls enabled, and 0 rows until the
-- extractor runs). Probe the NEW TABLE, never a value a prior migration
-- could have set:
--   SELECT tablename, rowsecurity FROM pg_tables
--    WHERE schemaname = 'public' AND tablename = 'gantt_legacy_bars';
--   SELECT count(*) FROM gantt_legacy_bars;
