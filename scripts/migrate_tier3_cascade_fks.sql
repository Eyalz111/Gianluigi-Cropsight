-- scripts/migrate_tier3_cascade_fks.sql
-- Tier 3.2: Ensure every meeting child table has ON DELETE CASCADE.
-- After this migration, DELETE FROM meetings cascades atomically to ALL
-- meeting child tables (except `embeddings`, which is polymorphic by design
-- and keeps its Python-level delete in delete_meeting_cascade).
--
-- The Python-level per-table loop in delete_meeting_cascade() remains for
-- the keep_tombstone=True path (where the parent row is NOT deleted and
-- the DB cascade therefore doesn't fire).
--
-- IMPORTANT — SCHEMA DRIFT DISCOVERED 2026-04-09:
-- Live production DB probe showed that several FKs that setup_supabase.sql
-- claims are CASCADE actually have NO ACTION / RESTRICT behavior in Supabase.
-- Specifically: tasks.meeting_id, decisions.meeting_id, open_questions.meeting_id
-- all BLOCKED a test DELETE FROM meetings, meaning the repo schema has drifted
-- from production. token_usage.meeting_id, which setup_supabase.sql claims is
-- SET NULL, actually already has CASCADE in production.
--
-- To avoid guessing at the real state, this migration DROPs and re-ADDs the
-- FK on EVERY meeting child table unconditionally. The operation is idempotent:
-- if the FK is already CASCADE, the end state is identical.
--
-- embeddings.source_id is polymorphic (source_type: 'meeting' | 'document')
-- so it has no FK on meetings(id) and must keep its Python-level delete.

BEGIN;

-- tasks.meeting_id — nullable for manual tasks; CASCADE only fires when
-- meeting_id is non-null and the parent is deleted.
ALTER TABLE tasks DROP CONSTRAINT IF EXISTS tasks_meeting_id_fkey;
ALTER TABLE tasks
  ADD CONSTRAINT tasks_meeting_id_fkey
  FOREIGN KEY (meeting_id) REFERENCES meetings(id) ON DELETE CASCADE;

-- decisions.meeting_id
ALTER TABLE decisions DROP CONSTRAINT IF EXISTS decisions_meeting_id_fkey;
ALTER TABLE decisions
  ADD CONSTRAINT decisions_meeting_id_fkey
  FOREIGN KEY (meeting_id) REFERENCES meetings(id) ON DELETE CASCADE;

-- open_questions.meeting_id
ALTER TABLE open_questions DROP CONSTRAINT IF EXISTS open_questions_meeting_id_fkey;
ALTER TABLE open_questions
  ADD CONSTRAINT open_questions_meeting_id_fkey
  FOREIGN KEY (meeting_id) REFERENCES meetings(id) ON DELETE CASCADE;

-- follow_up_meetings.source_meeting_id
ALTER TABLE follow_up_meetings DROP CONSTRAINT IF EXISTS follow_up_meetings_source_meeting_id_fkey;
ALTER TABLE follow_up_meetings
  ADD CONSTRAINT follow_up_meetings_source_meeting_id_fkey
  FOREIGN KEY (source_meeting_id) REFERENCES meetings(id) ON DELETE CASCADE;

-- task_mentions.meeting_id
ALTER TABLE task_mentions DROP CONSTRAINT IF EXISTS task_mentions_meeting_id_fkey;
ALTER TABLE task_mentions
  ADD CONSTRAINT task_mentions_meeting_id_fkey
  FOREIGN KEY (meeting_id) REFERENCES meetings(id) ON DELETE CASCADE;

-- entity_mentions.meeting_id
ALTER TABLE entity_mentions DROP CONSTRAINT IF EXISTS entity_mentions_meeting_id_fkey;
ALTER TABLE entity_mentions
  ADD CONSTRAINT entity_mentions_meeting_id_fkey
  FOREIGN KEY (meeting_id) REFERENCES meetings(id) ON DELETE CASCADE;

-- topic_thread_mentions.meeting_id
ALTER TABLE topic_thread_mentions DROP CONSTRAINT IF EXISTS topic_thread_mentions_meeting_id_fkey;
ALTER TABLE topic_thread_mentions
  ADD CONSTRAINT topic_thread_mentions_meeting_id_fkey
  FOREIGN KEY (meeting_id) REFERENCES meetings(id) ON DELETE CASCADE;

-- commitments.meeting_id
ALTER TABLE commitments DROP CONSTRAINT IF EXISTS commitments_meeting_id_fkey;
ALTER TABLE commitments
  ADD CONSTRAINT commitments_meeting_id_fkey
  FOREIGN KEY (meeting_id) REFERENCES meetings(id) ON DELETE CASCADE;

-- token_usage.meeting_id
ALTER TABLE token_usage DROP CONSTRAINT IF EXISTS token_usage_meeting_id_fkey;
ALTER TABLE token_usage
  ADD CONSTRAINT token_usage_meeting_id_fkey
  FOREIGN KEY (meeting_id) REFERENCES meetings(id) ON DELETE CASCADE;

COMMIT;

-- ============================================================================
-- Post-migration validation (run and eyeball):
-- ============================================================================
--
-- Confirms every FK that references meetings(id) now has CASCADE (confdeltype='c').
--
--   SELECT c.conname, c.confdeltype, t.relname AS child_table
--   FROM pg_constraint c
--   JOIN pg_class t ON c.conrelid = t.oid
--   WHERE c.confrelid = 'meetings'::regclass
--   ORDER BY t.relname;
--
-- Expected rows (all with confdeltype='c'):
--   commitments_meeting_id_fkey                 | c | commitments
--   decisions_meeting_id_fkey                   | c | decisions
--   entity_mentions_meeting_id_fkey             | c | entity_mentions
--   follow_up_meetings_source_meeting_id_fkey   | c | follow_up_meetings
--   open_questions_meeting_id_fkey              | c | open_questions
--   task_mentions_meeting_id_fkey               | c | task_mentions
--   tasks_meeting_id_fkey                       | c | tasks                 <-- updated
--   token_usage_meeting_id_fkey                 | c | token_usage           <-- updated
--   topic_thread_mentions_meeting_id_fkey       | c | topic_thread_mentions
--
-- NOTE: `embeddings` is NOT in this list — it's polymorphic (source_type
-- can be 'meeting' or 'document') so it has no FK. Its cleanup stays in
-- delete_meeting_cascade() as an explicit Python-level delete.
