-- Phase 4: Email Intelligence + Morning Brief
-- Adds direction column to email_scans table

ALTER TABLE email_scans ADD COLUMN IF NOT EXISTS direction TEXT DEFAULT 'inbound';
