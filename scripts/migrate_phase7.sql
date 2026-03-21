-- Phase 7: MCP Core + Read Tools
-- Minimal migration — mcp_sessions table already exists from v1.0 migration.
-- This adds a date index for faster "get latest session" queries.

-- Index for ordering by session_date (get_latest_mcp_session uses ORDER BY session_date DESC)
CREATE INDEX IF NOT EXISTS idx_mcp_sessions_date ON mcp_sessions(session_date DESC);
