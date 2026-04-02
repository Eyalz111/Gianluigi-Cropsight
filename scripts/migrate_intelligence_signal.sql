-- Intelligence Signal migration
-- Run once before first deployment. Safe to re-run (all IF NOT EXISTS).
--
-- Creates:
--   intelligence_signals  — weekly signal records, content, approval state, Drive links
--   competitor_watchlist  — known and auto-discovered competitors for signal research

-- ==========================================================================
-- intelligence_signals
-- ==========================================================================
CREATE TABLE IF NOT EXISTS intelligence_signals (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    signal_id TEXT UNIQUE NOT NULL,              -- "signal-w14-2026" format
    week_number INTEGER NOT NULL,
    year INTEGER NOT NULL,
    status TEXT DEFAULT 'generating',            -- generating | pending_approval | approved | distributed | error

    -- Context and research
    context_snapshot JSONB,                      -- input data used for generation
    research_results JSONB,                      -- Perplexity/Claude search results (truncated to 3KB/result)
    research_source TEXT,                        -- perplexity | perplexity_retry | claude_search
    perplexity_queries_run INTEGER DEFAULT 0,

    -- Content
    signal_content TEXT,                         -- full written report (markdown)
    flags JSONB,                                 -- [{flag: str, urgency: "high"|"medium"}] max 3
    script_text TEXT,                            -- video narration script (when video enabled)

    -- Drive outputs
    drive_doc_id TEXT,                           -- Google Doc file ID
    drive_doc_url TEXT,                          -- Google Doc webViewLink
    drive_video_id TEXT,                         -- Video file ID (null if video disabled)
    drive_video_url TEXT,                        -- Video webViewLink

    -- Approval and distribution
    approval_id TEXT,                            -- FK into pending_approvals.approval_id
    recipients TEXT[],                           -- who received the email
    distributed_at TIMESTAMPTZ,

    -- Cost tracking
    generation_cost_usd NUMERIC(10,4),
    token_usage JSONB,                           -- aggregated LLM token costs

    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_intelligence_signals_week_year
    ON intelligence_signals(week_number, year);

CREATE INDEX IF NOT EXISTS idx_intelligence_signals_status
    ON intelligence_signals(status);

-- ==========================================================================
-- competitor_watchlist
-- ==========================================================================
CREATE TABLE IF NOT EXISTS competitor_watchlist (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name TEXT UNIQUE NOT NULL,
    category TEXT DEFAULT 'known',               -- known | discovered | watching
    funding TEXT,
    target_customer TEXT,
    key_limitation TEXT,
    notes TEXT,
    appearance_count INTEGER DEFAULT 0,          -- times seen in Perplexity results
    last_seen_week INTEGER,                      -- week_number of last appearance
    last_seen_year INTEGER,                      -- year of last appearance
    added_by TEXT DEFAULT 'system',              -- system | eyal | auto_discovered
    is_active BOOLEAN DEFAULT TRUE,
    metadata JSONB,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_competitor_watchlist_active
    ON competitor_watchlist(is_active);

-- ==========================================================================
-- updated_at triggers (reuse existing function if available)
-- ==========================================================================
DO $$
BEGIN
    -- Create trigger function if it doesn't exist
    IF NOT EXISTS (
        SELECT 1 FROM pg_proc WHERE proname = 'update_updated_at_column'
    ) THEN
        CREATE FUNCTION update_updated_at_column()
        RETURNS TRIGGER AS $func$
        BEGIN
            NEW.updated_at = NOW();
            RETURN NEW;
        END;
        $func$ LANGUAGE plpgsql;
    END IF;
END $$;

CREATE OR REPLACE TRIGGER update_intelligence_signals_updated_at
    BEFORE UPDATE ON intelligence_signals
    FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();

CREATE OR REPLACE TRIGGER update_competitor_watchlist_updated_at
    BEFORE UPDATE ON competitor_watchlist
    FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();
