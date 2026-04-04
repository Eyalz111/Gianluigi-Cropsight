-- Add drive_audio columns to intelligence_signals
-- Run once in Supabase SQL Editor. Safe to re-run.

ALTER TABLE intelligence_signals ADD COLUMN IF NOT EXISTS drive_audio_id TEXT;
ALTER TABLE intelligence_signals ADD COLUMN IF NOT EXISTS drive_audio_url TEXT;
