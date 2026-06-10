-- Migration: extensible team roster (Meeting-Summaries upgrade, PR2)
-- A team_members table so people can be added without a deploy. config/team.py
-- loads it when TEAM_ROSTER_DB_ENABLED is on, falling back to the hardcoded
-- roster on any error/empty result. RLS is MANDATORY on the new table (the
-- service-role key bypasses it — zero functional impact, closes the anon-key
-- public-access flag; tests/test_rls_coverage.py enforces this).

CREATE TABLE IF NOT EXISTS team_members (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    member_key TEXT NOT NULL UNIQUE,         -- 'eyal','roye',... stable lookup key
    name TEXT NOT NULL,
    role TEXT,
    role_description TEXT,
    primary_email TEXT,
    identities TEXT[] DEFAULT '{}',          -- every address that is this person (personal + work)
    tier TEXT DEFAULT 'founders',            -- public|team|founders|ceo (maps to the Sensitivity enum)
    telegram_id BIGINT,
    is_admin BOOLEAN DEFAULT FALSE,
    status TEXT DEFAULT 'active',            -- active|inactive
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

ALTER TABLE team_members ENABLE ROW LEVEL SECURITY;   -- REQUIRED for every new public table

CREATE INDEX IF NOT EXISTS idx_team_members_status ON team_members(status);
