-- Phase 7 QA Hardening Migration
-- Date: March 22, 2026
-- Fixes: Missing thread_id column in email_scans (should have been in Phase 4)

-- Add thread_id column for email thread deduplication
ALTER TABLE email_scans ADD COLUMN IF NOT EXISTS thread_id TEXT;

-- Index for efficient thread_id lookups
CREATE INDEX IF NOT EXISTS idx_email_scans_thread_id ON email_scans(thread_id) WHERE thread_id IS NOT NULL;
