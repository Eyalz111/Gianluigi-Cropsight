-- ============================================================
-- Email ingestion — thread tracking  [2026-07-25]
--
-- Each Gmail thread Gianluigi is looped in on (CC / BCC / forward) becomes ONE
-- evolving "source": a `meetings` row with meeting_type='email_thread', fed into
-- the same extraction -> approval -> DB/Sheet pipeline a transcript uses.
--
-- This table maps a thread to that source and records which Message-IDs we've
-- already extracted from — so a growing thread only ever produces a DELTA, and
-- re-delivery / re-CC is idempotent. It is the backbone of every edge case
-- (growing thread, deleted message, dropped-from-CC gap, BCC).
--
-- Additive + idempotent. New table => mandatory RLS step (CLAUDE.md).
-- ============================================================

BEGIN;

CREATE TABLE IF NOT EXISTS email_threads (
    id                    UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    thread_key            TEXT NOT NULL UNIQUE,                 -- Gmail threadId
    meeting_id            UUID REFERENCES meetings(id) ON DELETE CASCADE,
    processed_message_ids TEXT[] NOT NULL DEFAULT '{}',         -- Message-IDs extracted from
    subject               TEXT,
    created_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at            TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- MANDATORY: RLS on every new table. Service-role key bypasses it (zero
-- functional impact) — it just closes the anon-key public-access hole Supabase
-- flags. Enforced by tests/test_rls_coverage.py + the daily QA scheduler.
ALTER TABLE email_threads ENABLE ROW LEVEL SECURITY;

COMMIT;

-- Self-verifying tail: probes the NEW columns, so a no-op run surfaces (error
-- 42703 if genuinely absent) rather than silently "looking fine". Never checks a
-- value a prior migration could already have set.
SELECT thread_key, meeting_id, processed_message_ids, 'MIGRATION OK' AS status
FROM email_threads
LIMIT 1;
