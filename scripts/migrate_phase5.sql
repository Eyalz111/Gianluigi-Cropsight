-- Phase 5: Meeting Prep Redesign — Schema Migration
-- Run against Supabase SQL editor

-- Meeting type on calendar classifications (for template matching memory)
ALTER TABLE calendar_classifications ADD COLUMN IF NOT EXISTS meeting_type TEXT;
CREATE INDEX IF NOT EXISTS idx_cal_class_meeting_type ON calendar_classifications(meeting_type);

-- Meeting type on meetings table (for "last meeting of same type" queries)
ALTER TABLE meetings ADD COLUMN IF NOT EXISTS meeting_type TEXT;
CREATE INDEX IF NOT EXISTS idx_meetings_type ON meetings(meeting_type);

-- Outline/focus tracking on meeting_prep_history
ALTER TABLE meeting_prep_history ADD COLUMN IF NOT EXISTS outline_content JSONB;
ALTER TABLE meeting_prep_history ADD COLUMN IF NOT EXISTS focus_instructions TEXT[];
ALTER TABLE meeting_prep_history ADD COLUMN IF NOT EXISTS timeline_mode TEXT DEFAULT 'normal';
