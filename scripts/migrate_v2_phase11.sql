-- =============================================================================
-- Gianluigi v2 Phase 11 Migration
-- Date: April 1, 2026
-- Purpose: Add sensitivity columns + email body storage
-- =============================================================================
--
-- Run in Supabase SQL Editor before deploying Phase 11 code.
-- All statements are idempotent (safe to run multiple times).
-- =============================================================================

-- Sensitivity columns on extracted items
ALTER TABLE tasks ADD COLUMN IF NOT EXISTS sensitivity TEXT DEFAULT 'normal';
ALTER TABLE decisions ADD COLUMN IF NOT EXISTS sensitivity TEXT DEFAULT 'normal';
ALTER TABLE open_questions ADD COLUMN IF NOT EXISTS sensitivity TEXT DEFAULT 'normal';

-- Email body storage (for B4, included early since lightweight)
ALTER TABLE email_scans ADD COLUMN IF NOT EXISTS body_text TEXT;
