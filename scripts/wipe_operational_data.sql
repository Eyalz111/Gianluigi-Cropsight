-- Wipe all operational data (preserves schema/structure)
-- Run before each deployment during development phase
-- Date: March 22, 2026

-- Disable FK checks by truncating in dependency order
-- (child tables first, then parents)

-- Children of meetings
TRUNCATE TABLE entity_mentions CASCADE;
TRUNCATE TABLE task_mentions CASCADE;
TRUNCATE TABLE decisions CASCADE;
TRUNCATE TABLE tasks CASCADE;
TRUNCATE TABLE follow_up_meetings CASCADE;
TRUNCATE TABLE open_questions CASCADE;
TRUNCATE TABLE commitments CASCADE;
TRUNCATE TABLE embeddings CASCADE;

-- Standalone operational tables
TRUNCATE TABLE meetings CASCADE;
TRUNCATE TABLE entities CASCADE;
TRUNCATE TABLE documents CASCADE;
TRUNCATE TABLE pending_approvals CASCADE;
TRUNCATE TABLE email_scans CASCADE;
TRUNCATE TABLE debrief_sessions CASCADE;
TRUNCATE TABLE mcp_sessions CASCADE;
TRUNCATE TABLE weekly_reports CASCADE;
TRUNCATE TABLE weekly_review_sessions CASCADE;
TRUNCATE TABLE meeting_prep_history CASCADE;
TRUNCATE TABLE gantt_proposals CASCADE;
TRUNCATE TABLE gantt_snapshots CASCADE;
TRUNCATE TABLE token_usage CASCADE;
TRUNCATE TABLE audit_log CASCADE;

-- Keep calendar_classifications (learned knowledge, not test data)
-- Keep gantt_schema (structural, not operational)
