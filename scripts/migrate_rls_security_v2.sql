-- =============================================================================
-- Gianluigi RLS Security Migration v2
-- Date: April 9, 2026
-- Purpose: Enable RLS on tables added AFTER the April 1 migration.
-- Supabase flagged these as publicly accessible (rls_disabled_in_public).
-- =============================================================================
--
-- CONTEXT:
-- The original migrate_rls_security.sql (April 1, 2026) covered 30 tables.
-- Between then and now, these tables were added without RLS:
--
--   1. intelligence_signals    (Intelligence Signal feature, v2.1)
--   2. competitor_watchlist    (Intelligence Signal watchlist)
--   3. task_signals            (Phase 12 — task signal detection)
--   4. deals                   (v2.2 Session 3 — deal intelligence)
--   5. deal_interactions       (v2.2 Session 3)
--   6. external_commitments    (v2.2 Session 3)
--
-- HOW IT WORKS (same pattern as the original migration):
-- 1. ALTER TABLE ... ENABLE ROW LEVEL SECURITY → locks the table
-- 2. service_role key BYPASSES RLS by default (Supabase built-in)
-- 3. anon/public key gets blocked (no permissive policies)
-- 4. Gianluigi uses service_role, so zero functional impact
--
-- IDEMPOTENT: running this twice is safe (ENABLE on already-enabled is a no-op)
--
-- ROLLBACK (if needed):
-- ALTER TABLE <table_name> DISABLE ROW LEVEL SECURITY;
-- =============================================================================

-- Intelligence Signal feature (v2.1, April 5, 2026)
ALTER TABLE intelligence_signals ENABLE ROW LEVEL SECURITY;
ALTER TABLE competitor_watchlist ENABLE ROW LEVEL SECURITY;

-- Phase 12 — Task signal detection
ALTER TABLE task_signals ENABLE ROW LEVEL SECURITY;

-- v2.2 Session 3 — Deal intelligence (April 8, 2026)
ALTER TABLE deals ENABLE ROW LEVEL SECURITY;
ALTER TABLE deal_interactions ENABLE ROW LEVEL SECURITY;
ALTER TABLE external_commitments ENABLE ROW LEVEL SECURITY;

-- Verification query — run after the migration to confirm all tables are locked
-- Expected: every row shows rowsecurity = true
/*
SELECT schemaname, tablename, rowsecurity
FROM pg_tables
WHERE schemaname = 'public'
  AND tablename IN (
    'intelligence_signals', 'competitor_watchlist', 'task_signals',
    'deals', 'deal_interactions', 'external_commitments'
  )
ORDER BY tablename;
*/
