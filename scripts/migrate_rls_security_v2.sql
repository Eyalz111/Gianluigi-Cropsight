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

-- =============================================================================
-- Defense-in-depth: get_table_rls_status() function
-- =============================================================================
-- Creates a SECURITY DEFINER function that returns RLS status for every
-- table in the public schema. Used by:
--   - tests/test_rls_coverage.py (pytest assertion — CI/local check)
--   - schedulers/qa_scheduler._check_rls_coverage (daily runtime check)
--
-- Without this function, PostgREST has no way to read pg_tables.rowsecurity,
-- so both defenses would be blind.
--
-- Idempotent: CREATE OR REPLACE makes re-runs safe.
-- =============================================================================

CREATE OR REPLACE FUNCTION public.get_table_rls_status()
RETURNS TABLE(table_name text, rls_enabled boolean)
LANGUAGE sql
SECURITY DEFINER
SET search_path = public
AS $$
    SELECT tablename::text AS table_name, rowsecurity AS rls_enabled
    FROM pg_tables
    WHERE schemaname = 'public'
    ORDER BY tablename;
$$;

-- Grant execute to service_role ONLY (anon must not be able to enumerate)
REVOKE ALL ON FUNCTION public.get_table_rls_status() FROM PUBLIC;
REVOKE ALL ON FUNCTION public.get_table_rls_status() FROM anon;
GRANT EXECUTE ON FUNCTION public.get_table_rls_status() TO service_role;

-- =============================================================================
-- Verification — run after the migration
-- =============================================================================
-- Expected: every row shows rls_enabled = true
/*
SELECT * FROM public.get_table_rls_status() WHERE rls_enabled = false;
*/

-- =============================================================================
-- Template for future migrations — copy this pattern for any new CREATE TABLE
-- =============================================================================
/*
CREATE TABLE IF NOT EXISTS my_new_table (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    created_at TIMESTAMPTZ DEFAULT NOW()
    -- ... columns ...
);

-- REQUIRED: enable RLS on every new table.
-- service_role bypasses RLS automatically (Gianluigi uses service_role),
-- so this locks the table from anon/public access with zero code changes.
ALTER TABLE my_new_table ENABLE ROW LEVEL SECURITY;
*/
