-- Migration: v2.2 Phase 4 — Deal & Relationship Intelligence
-- Created: 2026-04-07
-- Tables: deals, deal_interactions, external_commitments

-- Deals table: core CRM-light for tracking commercial relationships
CREATE TABLE IF NOT EXISTS deals (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name TEXT NOT NULL,
    organization TEXT NOT NULL,
    contact_person TEXT,
    stage TEXT DEFAULT 'lead',  -- lead/contacted/meeting_held/proposal/negotiation/pilot/closed_won/closed_lost/on_hold
    value_estimate TEXT,        -- text not numeric (pre-revenue, amounts are fuzzy)
    probability INTEGER,        -- 0-100
    owner TEXT DEFAULT 'Eyal',
    next_action TEXT,
    next_action_date DATE,
    last_interaction_date DATE,
    source TEXT,                -- how we found them
    notes TEXT,
    metadata JSONB DEFAULT '{}',
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

-- Deal interactions: historical record of what happened (meeting, email, call)
CREATE TABLE IF NOT EXISTS deal_interactions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    deal_id UUID REFERENCES deals(id),
    interaction_type TEXT NOT NULL,  -- meeting/email/call/note
    summary TEXT NOT NULL,
    date DATE NOT NULL,
    source_id UUID,           -- meeting_id or email_scan_id (nullable)
    source_type TEXT,         -- 'meeting', 'email', 'manual'
    created_by TEXT DEFAULT 'gianluigi',
    created_at TIMESTAMPTZ DEFAULT NOW()
);

-- External commitments: forward-looking promises to external parties
CREATE TABLE IF NOT EXISTS external_commitments (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    deal_id UUID REFERENCES deals(id),       -- nullable (commitment may not be deal-linked)
    organization TEXT NOT NULL,
    contact_person TEXT,
    commitment TEXT NOT NULL,                 -- what was promised
    promised_by TEXT DEFAULT 'Eyal',          -- who on our side promised
    promised_to TEXT,                         -- who on their side
    deadline DATE,
    status TEXT DEFAULT 'open',              -- open/fulfilled/overdue/cancelled
    source_meeting_id UUID,                  -- where the promise was made
    notes TEXT,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

-- Indexes
CREATE INDEX IF NOT EXISTS idx_deals_stage ON deals(stage);
CREATE INDEX IF NOT EXISTS idx_deals_next_action_date ON deals(next_action_date);
CREATE INDEX IF NOT EXISTS idx_deals_last_interaction ON deals(last_interaction_date);
CREATE INDEX IF NOT EXISTS idx_deal_interactions_deal ON deal_interactions(deal_id);
CREATE INDEX IF NOT EXISTS idx_external_commitments_deadline ON external_commitments(deadline);
CREATE INDEX IF NOT EXISTS idx_external_commitments_status ON external_commitments(status);
CREATE INDEX IF NOT EXISTS idx_external_commitments_deal ON external_commitments(deal_id);
