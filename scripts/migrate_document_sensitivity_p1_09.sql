-- Migration: documents.sensitivity  (audit P1-09)
--
-- Gives ingested documents a sensitivity tier so the I3 invariant
-- ("sensitivity follows data") holds for the documents path too — today a
-- FOUNDERS-only term-sheet PDF is ingested with no tier and its chunks land in
-- the same embeddings table as everything else. Additive + backfill-friendly:
-- the column defaults to 'founders' (the safe middle tier) and existing rows
-- are backfilled to 'founders'.
--
-- RLS: the documents table inherits its existing row-level-security posture; a
-- new column needs no new policy (service-role key bypasses RLS; the
-- tests/test_rls_coverage.py check stays green). If documents does NOT yet have
-- RLS enabled, also run:
--     ALTER TABLE documents ENABLE ROW LEVEL SECURITY;
--
-- DEPLOY ORDER: apply this migration BEFORE flipping DOCUMENT_SENSITIVITY_ENABLED=true.
-- The code only classifies/writes the column when that flag is on, so deploying
-- the code first is safe — the feature stays dark until BOTH the column exists
-- and the flag is flipped.

ALTER TABLE documents
    ADD COLUMN IF NOT EXISTS sensitivity TEXT DEFAULT 'founders';

-- Backfill existing rows to the safe default. Idempotent.
UPDATE documents
SET sensitivity = 'founders'
WHERE sensitivity IS NULL;

-- Verify (optional):
--   SELECT sensitivity, count(*) FROM documents GROUP BY sensitivity;
