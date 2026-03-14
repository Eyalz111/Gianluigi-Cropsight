-- =============================================================================
-- Gianluigi v1.0 Database Migration
-- Run this against an existing v0.5 Supabase database
-- =============================================================================

-- Gantt schema map
CREATE TABLE IF NOT EXISTS gantt_schema (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    workspace_id TEXT DEFAULT 'cropsight',
    sheet_name TEXT NOT NULL,
    section TEXT NOT NULL,
    subsection TEXT,
    row_number INTEGER NOT NULL,
    owner_column TEXT DEFAULT 'C',
    due_column TEXT DEFAULT 'D',
    first_week_column TEXT DEFAULT 'E',
    week_offset INTEGER DEFAULT 9,
    protected BOOLEAN DEFAULT FALSE,
    notes TEXT,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

-- Gantt update proposals
CREATE TABLE IF NOT EXISTS gantt_proposals (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    workspace_id TEXT DEFAULT 'cropsight',
    status TEXT DEFAULT 'pending',  -- pending, approved, rejected, rolled_back
    source_type TEXT,  -- meeting, email, debrief, weekly_review, manual
    source_id UUID,  -- reference to meeting, debrief session, etc.
    changes JSONB NOT NULL,  -- [{sheet, section, subsection, row, column, old_value, new_value, reason}]
    proposed_at TIMESTAMPTZ DEFAULT NOW(),
    reviewed_at TIMESTAMPTZ,
    reviewed_by TEXT,
    rejection_reason TEXT
);

-- Gantt snapshots (for rollback)
CREATE TABLE IF NOT EXISTS gantt_snapshots (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    workspace_id TEXT DEFAULT 'cropsight',
    proposal_id UUID REFERENCES gantt_proposals(id),
    sheet_name TEXT NOT NULL,
    cell_references TEXT[] NOT NULL,  -- ['B22', 'C22', ...]
    old_values JSONB NOT NULL,
    new_values JSONB NOT NULL,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

-- Debrief sessions
CREATE TABLE IF NOT EXISTS debrief_sessions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    workspace_id TEXT DEFAULT 'cropsight',
    date DATE NOT NULL,
    status TEXT DEFAULT 'in_progress',  -- in_progress, confirming, approved, cancelled
    items_captured JSONB DEFAULT '[]',
    pending_questions JSONB DEFAULT '[]',
    calendar_events_covered TEXT[] DEFAULT '{}',
    calendar_events_remaining TEXT[] DEFAULT '{}',
    raw_messages JSONB DEFAULT '[]',
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

-- Email intelligence
CREATE TABLE IF NOT EXISTS email_scans (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    workspace_id TEXT DEFAULT 'cropsight',
    scan_type TEXT NOT NULL,  -- 'constant' (gianluigi inbox) or 'daily' (eyal gmail)
    email_id TEXT NOT NULL,  -- Gmail message ID
    date TIMESTAMPTZ NOT NULL,
    sender TEXT,
    recipient TEXT,
    subject TEXT,
    classification TEXT,  -- 'relevant', 'borderline', 'false_positive', 'skipped'
    extracted_items JSONB,  -- [{type, text, ...}]
    attachments_processed TEXT[],  -- Drive file IDs of downloaded attachments
    approved BOOLEAN DEFAULT FALSE,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

-- MCP session notes
CREATE TABLE IF NOT EXISTS mcp_sessions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    workspace_id TEXT DEFAULT 'cropsight',
    session_date DATE NOT NULL,
    summary TEXT NOT NULL,
    decisions_made JSONB DEFAULT '[]',
    pending_items JSONB DEFAULT '[]',
    created_at TIMESTAMPTZ DEFAULT NOW()
);

-- Weekly reports
CREATE TABLE IF NOT EXISTS weekly_reports (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    workspace_id TEXT DEFAULT 'cropsight',
    week_number INTEGER NOT NULL,
    year INTEGER NOT NULL,
    report_url TEXT,  -- Cloud Run URL for HTML report
    slide_drive_id TEXT,  -- Google Drive file ID for PPTX
    digest_drive_id TEXT,  -- Google Drive file ID for digest document
    gantt_backup_drive_id TEXT,  -- Google Drive file ID for Gantt backup
    data JSONB,  -- Full compiled data for the report
    created_at TIMESTAMPTZ DEFAULT NOW()
);

-- Meeting prep templates (reference, actual templates in code)
CREATE TABLE IF NOT EXISTS meeting_prep_history (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    workspace_id TEXT DEFAULT 'cropsight',
    meeting_type TEXT NOT NULL,
    calendar_event_id TEXT,
    meeting_date TIMESTAMPTZ NOT NULL,
    prep_content JSONB NOT NULL,
    status TEXT DEFAULT 'pending',  -- pending, approved, distributed, rejected
    approved_at TIMESTAMPTZ,
    distributed_at TIMESTAMPTZ,
    recipients TEXT[],
    created_at TIMESTAMPTZ DEFAULT NOW()
);

-- =============================================================================
-- Triggers: auto-update updated_at
-- =============================================================================

-- Auto-update gantt_schema.updated_at on changes
DROP TRIGGER IF EXISTS update_gantt_schema_updated_at ON gantt_schema;
CREATE TRIGGER update_gantt_schema_updated_at
    BEFORE UPDATE ON gantt_schema
    FOR EACH ROW
    EXECUTE FUNCTION update_updated_at_column();

-- Auto-update debrief_sessions.updated_at on changes
DROP TRIGGER IF EXISTS update_debrief_sessions_updated_at ON debrief_sessions;
CREATE TRIGGER update_debrief_sessions_updated_at
    BEFORE UPDATE ON debrief_sessions
    FOR EACH ROW
    EXECUTE FUNCTION update_updated_at_column();

-- =============================================================================
-- Indexes
-- =============================================================================

-- gantt_schema indexes
CREATE INDEX IF NOT EXISTS idx_gantt_schema_workspace ON gantt_schema(workspace_id);

-- gantt_proposals indexes
CREATE INDEX IF NOT EXISTS idx_gantt_proposals_status ON gantt_proposals(status);
CREATE INDEX IF NOT EXISTS idx_gantt_proposals_workspace ON gantt_proposals(workspace_id);

-- gantt_snapshots indexes
CREATE INDEX IF NOT EXISTS idx_gantt_snapshots_workspace ON gantt_snapshots(workspace_id);

-- debrief_sessions indexes
CREATE INDEX IF NOT EXISTS idx_debrief_sessions_status ON debrief_sessions(status);
CREATE INDEX IF NOT EXISTS idx_debrief_sessions_date ON debrief_sessions(date);
CREATE INDEX IF NOT EXISTS idx_debrief_sessions_workspace ON debrief_sessions(workspace_id);

-- email_scans indexes
CREATE INDEX IF NOT EXISTS idx_email_scans_date ON email_scans(date);
CREATE INDEX IF NOT EXISTS idx_email_scans_workspace ON email_scans(workspace_id);

-- mcp_sessions indexes
CREATE INDEX IF NOT EXISTS idx_mcp_sessions_workspace ON mcp_sessions(workspace_id);

-- weekly_reports indexes
CREATE INDEX IF NOT EXISTS idx_weekly_reports_workspace ON weekly_reports(workspace_id);

-- meeting_prep_history indexes
CREATE INDEX IF NOT EXISTS idx_meeting_prep_history_status ON meeting_prep_history(status);
CREATE INDEX IF NOT EXISTS idx_meeting_prep_history_workspace ON meeting_prep_history(workspace_id);

-- =============================================================================
-- Unique constraints
-- =============================================================================

-- Prevent re-processing the same email
ALTER TABLE email_scans ADD CONSTRAINT uq_email_scans_email_id UNIQUE (email_id);
