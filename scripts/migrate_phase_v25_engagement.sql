-- v2.5 Phase 3 Migration: Morning-brief engagement feedback (PR3)
-- Run this in Supabase SQL editor BEFORE enabling BRIEF_FEEDBACK_ENABLED.
--
-- Captures whole-brief 👍/👎 + an optional "what felt like noise?" follow-up so
-- push-relevance and the corrections trend become measurable (V2.5_STRATEGY §12).
--
-- ALL changes are ADDITIVE and idempotent (IF NOT EXISTS). Never drops/wipes.
-- RLS enabled (service-role key bypasses; closes anon access).
-- Enforced by tests/test_rls_coverage.py.

BEGIN;

CREATE TABLE IF NOT EXISTS morning_brief_feedback (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    workspace_id TEXT DEFAULT 'cropsight',
    brief_id TEXT NOT NULL,                       -- 'brief-YYYY-MM-DD' (matches audit_log morning_brief_sent); '-2' suffix on same-day regenerate
    brief_date DATE,
    variant TEXT DEFAULT 'primary',               -- 'primary' (authoritative send) | 'preview' (v2 shadow); trend reads 'primary' only
    vote TEXT,                                     -- 'up' | 'down' | NULL (sent, no vote yet)
    noise_category TEXT,                           -- optional one-tap "what felt like noise?" selection
    noise_note TEXT,                               -- optional free-text follow-up
    section_count INTEGER,                         -- brief size snapshot for trend analysis
    pending_noise_reply BOOLEAN DEFAULT FALSE,     -- restart-safe flag for the follow-up reply flow (expires 24h)
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

ALTER TABLE morning_brief_feedback ENABLE ROW LEVEL SECURITY;  -- MANDATORY per project RLS rule

-- One row per brief id => the vote upsert is idempotent (revoting updates).
CREATE UNIQUE INDEX IF NOT EXISTS idx_mbf_brief_id ON morning_brief_feedback(brief_id);
CREATE INDEX IF NOT EXISTS idx_mbf_date ON morning_brief_feedback(brief_date);

COMMIT;

-- Post-migration validation:
--   SELECT tablename, rowsecurity FROM pg_tables WHERE tablename='morning_brief_feedback';  -- expect rowsecurity = true
--   python -m pytest tests/test_rls_coverage.py
