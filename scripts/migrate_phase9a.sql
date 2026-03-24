-- Phase 9A Migration: Decision Intelligence + Task Linking
-- Date: March 25, 2026
-- Changes:
--   1. Decision lifecycle columns (rationale, confidence, review_date, status, supersession)
--   2. Label column on decisions for topic threading
--   3. source_decision_id on tasks for decision-to-action linking
--   4. Indexes for new query patterns

-- ============================================================
-- 1. Decision lifecycle columns
-- ============================================================
ALTER TABLE decisions ADD COLUMN IF NOT EXISTS rationale TEXT;
ALTER TABLE decisions ADD COLUMN IF NOT EXISTS options_considered TEXT[];
ALTER TABLE decisions ADD COLUMN IF NOT EXISTS confidence INTEGER DEFAULT 3;
ALTER TABLE decisions ADD COLUMN IF NOT EXISTS review_date DATE;
ALTER TABLE decisions ADD COLUMN IF NOT EXISTS decision_status TEXT DEFAULT 'active';
ALTER TABLE decisions ADD COLUMN IF NOT EXISTS superseded_by UUID REFERENCES decisions(id) ON DELETE SET NULL;
ALTER TABLE decisions ADD COLUMN IF NOT EXISTS label TEXT;

-- ============================================================
-- 2. Decision-to-task linking
-- ============================================================
ALTER TABLE tasks ADD COLUMN IF NOT EXISTS source_decision_id UUID REFERENCES decisions(id) ON DELETE SET NULL;

-- ============================================================
-- 3. Indexes
-- ============================================================
CREATE INDEX IF NOT EXISTS idx_decisions_review_date ON decisions(review_date) WHERE review_date IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_decisions_status ON decisions(decision_status);
CREATE INDEX IF NOT EXISTS idx_decisions_label ON decisions(label) WHERE label IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_tasks_source_decision ON tasks(source_decision_id) WHERE source_decision_id IS NOT NULL;
