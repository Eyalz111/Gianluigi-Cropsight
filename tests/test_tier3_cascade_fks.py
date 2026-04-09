"""
Tier 3.2 — FK CASCADE coverage test.

Verifies that DELETE FROM meetings atomically cascades to every child table
via database-level ON DELETE CASCADE foreign keys (see
scripts/migrate_tier3_cascade_fks.sql), except `embeddings` which is
polymorphic (source_type: 'meeting' | 'document') and keeps a Python-level
delete in delete_meeting_cascade().

Also verifies the tombstone path (keep_tombstone=True) still clears children
explicitly and preserves the meetings row with approval_status='rejected'.

Runs against live Supabase (skips if creds not configured), same pattern as
tests/test_rls_coverage.py.
"""

from datetime import datetime, timezone

import pytest


def _require_supabase():
    """Skip the test if Supabase isn't configured locally."""
    try:
        from services.supabase_client import supabase_client
        from config.settings import settings
    except Exception as e:
        pytest.skip(f"Cannot import Supabase client ({e})")

    if not settings.SUPABASE_URL or not settings.SUPABASE_KEY:
        pytest.skip("SUPABASE_URL / SUPABASE_KEY not configured")

    return supabase_client


def _insert_meeting(sb, title_suffix: str) -> str:
    """Insert a throwaway meeting and return its UUID."""
    meeting = sb.create_meeting(
        date=datetime.now(timezone.utc),
        title=f"[T3.2 TEST] {title_suffix}",
        participants=["tester"],
        raw_transcript="test transcript",
        summary="test summary",
        sensitivity="founders",
        source_file_path=f"test_tier3_{title_suffix}.txt",
    )
    return meeting["id"]


def _count(sb, table: str, fk_col: str, meeting_id: str) -> int:
    """Count rows in `table` where `fk_col` equals `meeting_id`."""
    r = (
        sb.client.table(table)
        .select("id", count="exact")
        .eq(fk_col, meeting_id)
        .execute()
    )
    return r.count or 0


def _meeting_exists(sb, meeting_id: str) -> bool:
    r = sb.client.table("meetings").select("id").eq("id", meeting_id).execute()
    return bool(r.data)


def _cleanup(sb, meeting_id: str):
    """Best-effort cleanup at end of test — delete any lingering rows."""
    try:
        # embeddings has no FK, clear explicitly
        sb.client.table("embeddings").delete().eq("source_id", meeting_id).execute()
    except Exception:
        pass
    try:
        sb.client.table("meetings").delete().eq("id", meeting_id).execute()
    except Exception:
        pass


class TestFkCascade:
    def test_hard_delete_cascades_via_fk(self):
        """
        Insert a meeting with child rows in every FK-referenced table, then
        call delete_meeting_cascade(keep_tombstone=False). All children must
        be gone afterwards.
        """
        sb = _require_supabase()
        meeting_id = _insert_meeting(sb, "hard_delete_cascade")

        try:
            # Seed children across the FK-referenced tables.
            sb.create_tasks_batch(meeting_id, [
                {"title": "T3.2 test task", "assignee": "tester", "priority": "M"},
            ])
            sb.create_decisions_batch(meeting_id, [
                {"description": "T3.2 test decision"},
            ])
            sb.create_open_questions_batch(meeting_id, [
                {"question": "T3.2 test question", "raised_by": "tester"},
            ])

            # Sanity: children should exist before delete.
            assert _count(sb, "tasks", "meeting_id", meeting_id) >= 1
            assert _count(sb, "decisions", "meeting_id", meeting_id) >= 1
            assert _count(sb, "open_questions", "meeting_id", meeting_id) >= 1

            # Act — hard delete.
            counts = sb.delete_meeting_cascade(meeting_id, keep_tombstone=False)

            # Assert — every child is gone, meetings row is gone.
            assert _count(sb, "tasks", "meeting_id", meeting_id) == 0
            assert _count(sb, "decisions", "meeting_id", meeting_id) == 0
            assert _count(sb, "open_questions", "meeting_id", meeting_id) == 0
            assert _count(sb, "follow_up_meetings", "source_meeting_id", meeting_id) == 0
            assert _count(sb, "task_mentions", "meeting_id", meeting_id) == 0
            assert _count(sb, "entity_mentions", "meeting_id", meeting_id) == 0
            assert _count(sb, "topic_thread_mentions", "meeting_id", meeting_id) == 0
            assert _count(sb, "commitments", "meeting_id", meeting_id) == 0
            assert not _meeting_exists(sb, meeting_id), "meetings row should be gone"

            # The returned counts dict should reflect what was deleted.
            assert counts.get("tasks", 0) >= 1
            assert counts.get("decisions", 0) >= 1
            assert counts.get("open_questions", 0) >= 1
            assert counts.get("meetings", 0) == 1
        finally:
            _cleanup(sb, meeting_id)

    def test_tombstone_path_preserves_meeting_row(self):
        """
        keep_tombstone=True must delete children but KEEP the meetings row
        with approval_status='rejected' and a cleared summary/transcript.
        """
        sb = _require_supabase()
        meeting_id = _insert_meeting(sb, "tombstone_preserve")

        try:
            sb.create_tasks_batch(meeting_id, [
                {"title": "T3.2 tombstone task", "assignee": "tester", "priority": "M"},
            ])
            sb.create_decisions_batch(meeting_id, [
                {"description": "T3.2 tombstone decision"},
            ])

            counts = sb.delete_meeting_cascade(meeting_id, keep_tombstone=True)

            # Children are gone.
            assert _count(sb, "tasks", "meeting_id", meeting_id) == 0
            assert _count(sb, "decisions", "meeting_id", meeting_id) == 0

            # meetings row still exists with tombstone markers.
            assert _meeting_exists(sb, meeting_id), "meetings row must survive"
            r = (
                sb.client.table("meetings")
                .select("approval_status, raw_transcript, summary")
                .eq("id", meeting_id)
                .limit(1)
                .execute()
            )
            row = r.data[0]
            assert row["approval_status"] == "rejected"
            assert row["raw_transcript"] is None
            assert row["summary"].startswith("[REJECTED"), (
                f"summary should be tombstone marker, got: {row['summary']!r}"
            )
            # counts dict should note the tombstone.
            assert counts.get("tombstone") == 1
            assert counts.get("meetings", 0) == 0
        finally:
            _cleanup(sb, meeting_id)

    def test_direct_db_delete_cascades_tasks(self):
        """
        The T3.2 payoff: a raw DELETE FROM meetings (simulating pgAdmin,
        Supabase UI, or a future code path that doesn't go through
        delete_meeting_cascade) must atomically cascade to child tables
        via the FK. Before T3.2 this would have left orphan rows behind.
        """
        sb = _require_supabase()
        meeting_id = _insert_meeting(sb, "direct_delete")

        try:
            sb.create_tasks_batch(meeting_id, [
                {"title": "T3.2 direct-delete task 1", "assignee": "tester", "priority": "M"},
                {"title": "T3.2 direct-delete task 2", "assignee": "tester", "priority": "L"},
            ])
            sb.create_decisions_batch(meeting_id, [
                {"description": "T3.2 direct-delete decision"},
            ])

            assert _count(sb, "tasks", "meeting_id", meeting_id) == 2
            assert _count(sb, "decisions", "meeting_id", meeting_id) == 1

            # Raw delete — bypass the Python wrapper entirely.
            sb.client.table("meetings").delete().eq("id", meeting_id).execute()

            # The FK CASCADE must have cleaned up children atomically.
            assert _count(sb, "tasks", "meeting_id", meeting_id) == 0, (
                "tasks_meeting_id_fkey should CASCADE — "
                "run scripts/migrate_tier3_cascade_fks.sql on Supabase"
            )
            assert _count(sb, "decisions", "meeting_id", meeting_id) == 0
            assert not _meeting_exists(sb, meeting_id)
        finally:
            _cleanup(sb, meeting_id)

    def test_token_usage_cascades(self):
        """
        token_usage.meeting_id was ON DELETE SET NULL pre-T3.2. After the
        migration it must CASCADE so cost-tracking rows are cleaned up when
        a meeting is deleted.
        """
        sb = _require_supabase()
        meeting_id = _insert_meeting(sb, "token_usage_cascade")

        try:
            # Insert a token_usage row directly (no helper for this).
            sb.client.table("token_usage").insert({
                "call_site": "test_tier3_cascade_fks",
                "model": "claude-opus-4-6",
                "input_tokens": 100,
                "output_tokens": 50,
                "meeting_id": meeting_id,
            }).execute()

            before = (
                sb.client.table("token_usage")
                .select("id", count="exact")
                .eq("meeting_id", meeting_id)
                .execute()
            )
            assert (before.count or 0) >= 1

            # Raw delete — bypass the wrapper.
            sb.client.table("meetings").delete().eq("id", meeting_id).execute()

            after = (
                sb.client.table("token_usage")
                .select("id", count="exact")
                .eq("meeting_id", meeting_id)
                .execute()
            )
            assert (after.count or 0) == 0, (
                "token_usage_meeting_id_fkey should CASCADE — "
                "run scripts/migrate_tier3_cascade_fks.sql on Supabase"
            )
        finally:
            _cleanup(sb, meeting_id)

    def test_embeddings_still_require_python_delete(self):
        """
        embeddings.source_id is polymorphic (source_type: 'meeting' OR
        'document') so it has no FK on meetings(id). A raw DELETE FROM
        meetings must NOT touch embeddings — the Python delete in
        delete_meeting_cascade() is the only thing that cleans them up.

        Part 1: raw delete leaves embeddings rows in place.
        Part 2: delete_meeting_cascade() wrapper does clean them up.
        """
        sb = _require_supabase()

        # Part 1 — raw delete test.
        m1 = _insert_meeting(sb, "embeddings_raw")
        try:
            sb.client.table("embeddings").insert({
                "source_type": "meeting",
                "source_id": m1,
                "chunk_index": 0,
                "chunk_text": "T3.2 embedding test chunk",
                "embedding": [0.0] * 1536,
            }).execute()

            before = (
                sb.client.table("embeddings")
                .select("id", count="exact")
                .eq("source_id", m1)
                .execute()
            )
            assert (before.count or 0) >= 1

            # Raw delete — FK cascade doesn't touch embeddings.
            sb.client.table("meetings").delete().eq("id", m1).execute()

            after = (
                sb.client.table("embeddings")
                .select("id", count="exact")
                .eq("source_id", m1)
                .execute()
            )
            assert (after.count or 0) >= 1, (
                "embeddings should SURVIVE a raw meeting delete (polymorphic source_id, no FK)"
            )
        finally:
            # Manually clean up the orphan embeddings from Part 1.
            try:
                sb.client.table("embeddings").delete().eq("source_id", m1).execute()
            except Exception:
                pass

        # Part 2 — delete_meeting_cascade() wrapper test.
        m2 = _insert_meeting(sb, "embeddings_wrapper")
        try:
            sb.client.table("embeddings").insert({
                "source_type": "meeting",
                "source_id": m2,
                "chunk_index": 0,
                "chunk_text": "T3.2 wrapper embedding test chunk",
                "embedding": [0.0] * 1536,
            }).execute()

            sb.delete_meeting_cascade(m2, keep_tombstone=False)

            after = (
                sb.client.table("embeddings")
                .select("id", count="exact")
                .eq("source_id", m2)
                .execute()
            )
            assert (after.count or 0) == 0, (
                "delete_meeting_cascade() wrapper should clean up embeddings"
            )
        finally:
            _cleanup(sb, m2)
