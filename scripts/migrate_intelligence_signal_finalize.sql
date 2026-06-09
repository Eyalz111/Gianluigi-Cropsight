-- Migration: restart-safe intelligence-signal distribution (PR1)
-- Additive + nullable — safe to apply on the live DB with no backfill.
--
-- Adds finalize_started_at: the timestamp Eyal's approval kicked off the
-- background finalize→distribute worker. reconstruct_intelligence_finalize_jobs()
-- and the periodic re-pickup read it to compute the bounded Drive-readiness
-- deadline and to detect stale (silently-dead) finalize tasks on boot / each tick.
--
-- No new table (so no RLS change needed). The new intelligence_signals.status
-- value 'approved_finalizing' is a free-TEXT convention and needs no DDL.

ALTER TABLE intelligence_signals
    ADD COLUMN IF NOT EXISTS finalize_started_at TIMESTAMPTZ;

COMMENT ON COLUMN intelligence_signals.finalize_started_at IS
    'When the restart-safe finalize+distribute worker started (set at approval). '
    'NULL until approved with INTELLIGENCE_SIGNAL_SAFE_DISTRIBUTE on.';
