-- Phase 10 Migration: Dynamic Canonical Projects System
-- Run this in Supabase SQL editor BEFORE deploying Phase 10 code.
--
-- Creates:
--   1. canonical_projects — replaces static config/projects.py
--   2. unmatched_labels — auto-discovery of new project labels
--
-- Safe to re-run: uses IF NOT EXISTS.

-- ============================================================
-- 1. canonical_projects table
-- ============================================================
CREATE TABLE IF NOT EXISTS canonical_projects (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name TEXT NOT NULL UNIQUE,
    description TEXT,
    aliases TEXT[] DEFAULT '{}',
    status TEXT DEFAULT 'active',  -- active, archived
    created_at TIMESTAMPTZ DEFAULT NOW()
);

-- ============================================================
-- 2. unmatched_labels table
-- ============================================================
CREATE TABLE IF NOT EXISTS unmatched_labels (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    label TEXT NOT NULL,
    meeting_id UUID REFERENCES meetings(id),
    meeting_title TEXT,
    context TEXT,  -- brief context of where this label appeared
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_unmatched_labels_created
    ON unmatched_labels(created_at);

-- ============================================================
-- 3. Seed canonical projects (10 projects with aliases)
-- ============================================================
INSERT INTO canonical_projects (name, description, aliases, status) VALUES
    ('Moldova Pilot', 'Wheat yield PoC, Gagauzia region, first client',
     ARRAY['Moldova PoC', 'Gagauzia project', 'Moldova wheat', 'Moldova delivery'], 'active'),
    ('Pre-Seed Fundraising', 'IIA Tnufa program + next funding round',
     ARRAY['fundraising', 'Tnufa', 'investor round'], 'active'),
    ('SatYield Accuracy Model', 'Core ML product, satellite-based yield forecasting',
     ARRAY['the model', 'accuracy model', 'yield model'], 'active'),
    ('Product V1', 'First commercial product version',
     ARRAY['MVP', 'product launch'], 'active'),
    ('Business Plan', 'Financial projections + strategy',
     ARRAY['business model', 'financial plan'], 'active'),
    ('EU Grant', 'European funding programs',
     ARRAY['EU funding', 'European grant', 'Horizon'], 'active'),
    ('Website & Marketing', 'cropsight.io + content strategy',
     ARRAY['website', 'marketing', 'landing page'], 'active'),
    ('Investor Outreach', 'Angel + fund pipeline',
     ARRAY['investor pipeline', 'outreach', 'angel investors'], 'active'),
    ('Operational Tooling', 'Gianluigi AI operations system',
     ARRAY['Gianluigi', 'ops tooling', 'AI assistant'], 'active'),
    ('Team & HR', 'Hiring, roles, team operations',
     ARRAY['hiring', 'team building', 'HR'], 'active')
ON CONFLICT (name) DO NOTHING;
