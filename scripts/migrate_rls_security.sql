-- =============================================================================
-- Gianluigi RLS Security Migration
-- Date: April 1, 2026
-- Purpose: Enable Row-Level Security on ALL tables to prevent unauthorized access
-- =============================================================================
--
-- CONTEXT:
-- Supabase flagged critical security issues: tables are publicly accessible
-- without RLS. This migration locks down all tables so ONLY the service_role
-- key (used by Gianluigi server-side) can access data.
--
-- The anon/public key will have ZERO access after this migration.
-- This is correct — Gianluigi is a server-side app, not a client-side app.
--
-- HOW IT WORKS:
-- 1. ALTER TABLE ... ENABLE ROW LEVEL SECURITY → locks the table
-- 2. By default, service_role key BYPASSES RLS (Supabase built-in behavior)
-- 3. No explicit policies needed for service_role — it always has full access
-- 4. The anon key gets blocked because there are no permissive policies for it
--
-- SAFE TO RUN:
-- - If using service_role key: zero impact, Gianluigi works exactly as before
-- - If using anon key: Gianluigi will BREAK → switch to service_role key first
-- - Idempotent: running this twice is safe (ENABLE on already-enabled is a no-op)
--
-- ROLLBACK (if needed):
-- ALTER TABLE <table_name> DISABLE ROW LEVEL SECURITY;
-- =============================================================================

-- Core Operational Tables
ALTER TABLE meetings ENABLE ROW LEVEL SECURITY;
ALTER TABLE decisions ENABLE ROW LEVEL SECURITY;
ALTER TABLE tasks ENABLE ROW LEVEL SECURITY;
ALTER TABLE follow_up_meetings ENABLE ROW LEVEL SECURITY;
ALTER TABLE open_questions ENABLE ROW LEVEL SECURITY;
ALTER TABLE commitments ENABLE ROW LEVEL SECURITY;

-- Knowledge Base & Documents
ALTER TABLE documents ENABLE ROW LEVEL SECURITY;
ALTER TABLE embeddings ENABLE ROW LEVEL SECURITY;

-- Entity & Relationship Tracking
ALTER TABLE entities ENABLE ROW LEVEL SECURITY;
ALTER TABLE entity_mentions ENABLE ROW LEVEL SECURITY;
ALTER TABLE task_mentions ENABLE ROW LEVEL SECURITY;
ALTER TABLE topic_threads ENABLE ROW LEVEL SECURITY;
ALTER TABLE topic_thread_mentions ENABLE ROW LEVEL SECURITY;

-- Approval & Persistence
ALTER TABLE pending_approvals ENABLE ROW LEVEL SECURITY;
ALTER TABLE calendar_classifications ENABLE ROW LEVEL SECURITY;

-- Gantt Chart & Planning
ALTER TABLE gantt_schema ENABLE ROW LEVEL SECURITY;
ALTER TABLE gantt_proposals ENABLE ROW LEVEL SECURITY;
ALTER TABLE gantt_snapshots ENABLE ROW LEVEL SECURITY;

-- Session & Workflow Management
ALTER TABLE debrief_sessions ENABLE ROW LEVEL SECURITY;
ALTER TABLE email_scans ENABLE ROW LEVEL SECURITY;
ALTER TABLE mcp_sessions ENABLE ROW LEVEL SECURITY;
ALTER TABLE meeting_prep_history ENABLE ROW LEVEL SECURITY;
ALTER TABLE weekly_review_sessions ENABLE ROW LEVEL SECURITY;
ALTER TABLE weekly_reports ENABLE ROW LEVEL SECURITY;

-- Operational Intelligence
ALTER TABLE operational_snapshots ENABLE ROW LEVEL SECURITY;
ALTER TABLE unmatched_labels ENABLE ROW LEVEL SECURITY;
ALTER TABLE canonical_projects ENABLE ROW LEVEL SECURITY;

-- System Infrastructure
ALTER TABLE token_usage ENABLE ROW LEVEL SECURITY;
ALTER TABLE scheduler_heartbeats ENABLE ROW LEVEL SECURITY;
ALTER TABLE audit_log ENABLE ROW LEVEL SECURITY;

-- =============================================================================
-- Verification query — run after migration to confirm all tables have RLS
-- =============================================================================
-- SELECT schemaname, tablename, rowsecurity
-- FROM pg_tables
-- WHERE schemaname = 'public'
-- ORDER BY tablename;
--
-- All tables should show rowsecurity = true
-- =============================================================================
