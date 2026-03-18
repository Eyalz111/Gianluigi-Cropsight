-- Phase 6: Weekly Review + Outputs
-- Run after migrate_v1.sql and migrate_phase5.sql

-- Weekly review sessions (follows debrief_sessions pattern)
CREATE TABLE IF NOT EXISTS weekly_review_sessions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    workspace_id TEXT DEFAULT 'cropsight',
    week_number INTEGER NOT NULL,
    year INTEGER NOT NULL,
    status TEXT DEFAULT 'preparing',
    current_part INTEGER DEFAULT 0,
    agenda_data JSONB DEFAULT '{}',
    gantt_proposals JSONB DEFAULT '[]',
    corrections JSONB DEFAULT '[]',
    report_id UUID REFERENCES weekly_reports(id),
    calendar_event_id TEXT,
    trigger_type TEXT DEFAULT 'calendar',
    raw_messages JSONB DEFAULT '[]',
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_wrs_status ON weekly_review_sessions(status);
CREATE INDEX IF NOT EXISTS idx_wrs_week ON weekly_review_sessions(week_number, year);

-- Extend weekly_reports (table already exists from migrate_v1.sql)
ALTER TABLE weekly_reports ADD COLUMN IF NOT EXISTS html_content TEXT;
ALTER TABLE weekly_reports ADD COLUMN IF NOT EXISTS access_token TEXT;
ALTER TABLE weekly_reports ADD COLUMN IF NOT EXISTS session_id UUID;
ALTER TABLE weekly_reports ADD COLUMN IF NOT EXISTS status TEXT DEFAULT 'draft';
ALTER TABLE weekly_reports ADD COLUMN IF NOT EXISTS distributed_at TIMESTAMPTZ;

CREATE INDEX IF NOT EXISTS idx_weekly_reports_token ON weekly_reports(access_token);

-- Phase 6 hardening: report expiry + access logging
ALTER TABLE weekly_reports ADD COLUMN IF NOT EXISTS expires_at TIMESTAMPTZ;
ALTER TABLE weekly_reports ADD COLUMN IF NOT EXISTS last_accessed_at TIMESTAMPTZ;
ALTER TABLE weekly_reports ADD COLUMN IF NOT EXISTS access_count INTEGER DEFAULT 0;
