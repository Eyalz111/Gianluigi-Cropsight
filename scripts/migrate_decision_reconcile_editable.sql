-- Phase 2 (Decision Sheet editable, 2026-07): make the Decisions sheet editable
-- like tasks — FULL PARITY (content + label + rationale + confidence + status,
-- propose-don't-clobber, DecisionBrief groundwork). Eyal's Q4 (2026-07-10): the
-- Decisions sheet is an EDITABLE mirror, not a read-only view.
--
-- Today decisions are one-way DB->Sheet: there is NO identity column on the sheet,
-- NO snapshot, NO sticky flags, and the only Sheet->DB decision path is dead code
-- (0 callers). This migration adds the substrate to reconcile decisions like tasks
-- (snapshot-based "manual wins & sticks", UUID-keyed):
--   1. sheet_snapshots: reuse the entity_type discriminator (built for exactly this)
--      to hold a decision snapshot — add a parallel decision_id FK + the last-synced
--      editable decision columns. task_id is already nullable, so decision rows just
--      leave it empty (entity_type='decision').
--   2. decisions: per-field manual flags (sticky) mirroring tasks.manual_*, so
--      inference proposes-not-clobbers a field Eyal set (Step 5), plus manual_set_*
--      provenance.
--   3. decisions: brief_json (DecisionBrief, living-decision groundwork) + updated_at
--      (so reconcile can tell "DB changed since snapshot" for the DB->Sheet refresh).
--
-- ALL changes are ADDITIVE and idempotent (IF NOT EXISTS). Never drops/wipes.
-- sheet_snapshots + decisions already have RLS — no new table, so no new RLS step.
-- Enforced by tests/test_rls_coverage.py.

BEGIN;

-- ============================================================
-- 1. sheet_snapshots: hold a decision snapshot alongside task snapshots.
--    entity_type ('task' default) already discriminates; add decision_id + the
--    editable decision columns. label (col A) reuses the existing sheet_snapshots
--    .label column (added in the Phase 1 task migration). task_id stays nullable.
-- ============================================================
ALTER TABLE sheet_snapshots
    ADD COLUMN IF NOT EXISTS decision_id     UUID REFERENCES decisions(id) ON DELETE CASCADE,
    ADD COLUMN IF NOT EXISTS description     TEXT,      -- decision text (Decisions col B)
    ADD COLUMN IF NOT EXISTS rationale       TEXT,      -- rationale       (col C)
    ADD COLUMN IF NOT EXISTS confidence      INTEGER,   -- confidence      (col D)
    ADD COLUMN IF NOT EXISTS decision_status TEXT;      -- status          (col G)

-- one current snapshot per decision (mirrors uq_sheet_snapshots_task)
CREATE UNIQUE INDEX IF NOT EXISTS uq_sheet_snapshots_decision
    ON sheet_snapshots(decision_id) WHERE entity_type = 'decision';

-- ============================================================
-- 2. decisions: per-field manual-override flags (sticky), mirroring tasks.manual_*.
--    A Sheet edit to one of these sets the flag; inference must then propose, not
--    clobber (Step 5). manual_set_* records provenance (mirror tasks).
-- ============================================================
ALTER TABLE decisions
    ADD COLUMN IF NOT EXISTS manual_description BOOLEAN DEFAULT FALSE,
    ADD COLUMN IF NOT EXISTS manual_label       BOOLEAN DEFAULT FALSE,
    ADD COLUMN IF NOT EXISTS manual_rationale   BOOLEAN DEFAULT FALSE,
    ADD COLUMN IF NOT EXISTS manual_confidence  BOOLEAN DEFAULT FALSE,
    ADD COLUMN IF NOT EXISTS manual_status      BOOLEAN DEFAULT FALSE,
    ADD COLUMN IF NOT EXISTS manual_set_at      TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS manual_set_source  TEXT;  -- 'sheet_edit' | 'telegram' | 'eyal_mcp'

-- ============================================================
-- 3. decisions: DecisionBrief (living-decision groundwork, full parity) + updated_at.
--    updated_at defaults NOW() and is bumped on every update_decision so the
--    reconcile's "DB != snapshot" refresh path has a cheap change signal.
-- ============================================================
ALTER TABLE decisions
    ADD COLUMN IF NOT EXISTS brief_json JSONB,
    ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ DEFAULT NOW();

COMMIT;

-- ============================================================
-- Post-migration backfill (run scripts/backfill_decision_snapshots.py --apply):
--   Seeds a sheet_snapshots row (entity_type='decision') for every existing
--   approved decision from the current DB values, so the first reconcile after
--   deploy sees snap == db == sheet and does NOT mistake an untouched cell for an
--   Eyal edit (phantom-pull). Mirrors backfill_snapshot_content.py for tasks.
--
-- Post-migration validation (run manually after applying):
--   1. New columns on sheet_snapshots:
--      SELECT column_name FROM information_schema.columns
--      WHERE table_name='sheet_snapshots'
--        AND column_name IN ('decision_id','description','rationale','confidence','decision_status');
--   2. New manual flags + brief on decisions:
--      SELECT column_name FROM information_schema.columns
--      WHERE table_name='decisions'
--        AND column_name IN ('manual_description','manual_label','manual_rationale',
--                            'manual_confidence','manual_status','brief_json','updated_at');
--   3. Decision snapshot uniqueness index exists:
--      SELECT indexname FROM pg_indexes WHERE indexname='uq_sheet_snapshots_decision';
--   4. RLS coverage test: pytest tests/test_rls_coverage.py  -- expect PASS
-- ============================================================
