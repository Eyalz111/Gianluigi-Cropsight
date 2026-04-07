-- Migration: Sensitivity Tier Rename (v2.2 Session 2.5)
-- Renames tiers: team→founders, ceo_only→ceo, restricted→ceo
-- Also catches legacy values: normal→founders, sensitive→ceo, legal→ceo
-- Adds sensitivity column to embeddings table with backfill.
--
-- Run: Execute in Supabase SQL Editor
-- Rollback: Reverse the UPDATE statements (founders→team, ceo→ceo_only)
--
-- IMPORTANT: Run in a transaction. Verify counts before committing.
-- Replaces migrate_v2_sensitivity_tiers.sql (which was never run).

BEGIN;

-- ============================================================================
-- Step 1: Rename values across all operational tables
-- ============================================================================

-- meetings
UPDATE meetings SET sensitivity = 'founders' WHERE sensitivity IN ('team', 'normal');
UPDATE meetings SET sensitivity = 'ceo' WHERE sensitivity IN ('ceo_only', 'restricted', 'sensitive', 'legal');

-- tasks
UPDATE tasks SET sensitivity = 'founders' WHERE sensitivity IN ('team', 'normal');
UPDATE tasks SET sensitivity = 'ceo' WHERE sensitivity IN ('ceo_only', 'restricted', 'sensitive', 'legal');

-- decisions
UPDATE decisions SET sensitivity = 'founders' WHERE sensitivity IN ('team', 'normal');
UPDATE decisions SET sensitivity = 'ceo' WHERE sensitivity IN ('ceo_only', 'restricted', 'sensitive', 'legal');

-- open_questions
UPDATE open_questions SET sensitivity = 'founders' WHERE sensitivity IN ('team', 'normal');
UPDATE open_questions SET sensitivity = 'ceo' WHERE sensitivity IN ('ceo_only', 'restricted', 'sensitive', 'legal');

-- ============================================================================
-- Step 2: Add sensitivity column to embeddings table
-- ============================================================================

ALTER TABLE embeddings ADD COLUMN IF NOT EXISTS sensitivity TEXT DEFAULT 'founders';

-- ============================================================================
-- Step 3: Backfill embeddings.sensitivity from source tables
-- ============================================================================

-- From meetings (most common source)
UPDATE embeddings e
SET sensitivity = m.sensitivity
FROM meetings m
WHERE e.source_type = 'meeting' AND e.source_id = m.id;

-- From decisions
UPDATE embeddings e
SET sensitivity = COALESCE(d.sensitivity, 'founders')
FROM decisions d
WHERE e.source_type = 'decision' AND e.source_id = d.id;

-- From tasks (rare but handle)
UPDATE embeddings e
SET sensitivity = COALESCE(t.sensitivity, 'founders')
FROM tasks t
WHERE e.source_type = 'task' AND e.source_id = t.id;

-- Remaining (documents, debriefs) stay as default 'founders' — acceptable,
-- these are operational content without sensitivity classification.

-- ============================================================================
-- Step 4: Create index for filtered queries
-- ============================================================================

CREATE INDEX IF NOT EXISTS idx_embeddings_sensitivity ON embeddings(sensitivity);

-- ============================================================================
-- Step 5: Verify — no legacy values remain
-- ============================================================================

DO $$
DECLARE
    legacy_count INTEGER;
    tbl TEXT;
BEGIN
    FOREACH tbl IN ARRAY ARRAY['meetings', 'tasks', 'decisions', 'open_questions'] LOOP
        EXECUTE format(
            'SELECT COUNT(*) FROM %I WHERE sensitivity IN (''normal'', ''sensitive'', ''legal'', ''team'', ''ceo_only'', ''restricted'')',
            tbl
        ) INTO legacy_count;
        IF legacy_count > 0 THEN
            RAISE NOTICE 'WARNING: % rows in % still have legacy sensitivity values', legacy_count, tbl;
        ELSE
            RAISE NOTICE 'OK: % has no legacy sensitivity values', tbl;
        END IF;
    END LOOP;
END $$;

COMMIT;
