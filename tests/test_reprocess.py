"""
Tests for the reprocess feature.

Covers:
- delete_meeting_cascade in supabase_client
- reprocess_file in transcript_watcher
- /reprocess Telegram command handler
"""

import pytest
from unittest.mock import patch, MagicMock, AsyncMock


class TestDeleteMeetingCascade:
    """Tests for supabase_client.delete_meeting_cascade."""

    def test_hard_delete_uses_fk_cascade_not_per_table_loop(self):
        """
        Post-Tier-3.2: the hard-delete path (keep_tombstone=False) relies on
        DB-level FK CASCADE. It should:
          1. Pre-count children for reporting (SELECT count=exact on 5 tables)
          2. Explicitly delete embeddings (polymorphic, no FK)
          3. Delete pending_approvals (keyed by approval_id, no FK to meetings)
          4. Run a single DELETE on meetings (FK cascade handles children)
        The old per-table delete loop is GONE from this path.
        """
        from services.supabase_client import SupabaseClient

        client = SupabaseClient()
        mock_supabase = MagicMock()

        # Track every .table(name) call to assert overall shape
        touched_tables: list[str] = []
        delete_calls: list[str] = []

        def table_router(name):
            touched_tables.append(name)
            chain = MagicMock()

            # Pre-count path: .select("id", count="exact").eq(...).execute()
            select_chain = MagicMock()
            select_chain.eq.return_value = select_chain
            # .execute() returns an object with .count == 1 so pre-counts propagate
            select_chain.execute.return_value = MagicMock(count=1)
            chain.select.return_value = select_chain

            # Delete path: .delete().eq(...).execute()
            delete_chain = MagicMock()
            delete_chain.eq.return_value = delete_chain

            def _execute_delete():
                delete_calls.append(name)
                return MagicMock(data=[{"id": f"{name}-row"}])

            delete_chain.execute.side_effect = _execute_delete
            chain.delete.return_value = delete_chain

            return chain

        mock_supabase.table.side_effect = table_router
        object.__setattr__(client, "_client", mock_supabase)

        result = client.delete_meeting_cascade("test-meeting-id", keep_tombstone=False)

        # Hard-delete path should only .delete() on 3 things:
        #   - embeddings (polymorphic, explicit Python delete)
        #   - pending_approvals (no FK to meetings, explicit delete)
        #   - meetings (the parent — FK CASCADE handles the rest)
        assert "embeddings" in delete_calls
        assert "pending_approvals" in delete_calls
        assert "meetings" in delete_calls
        # These should NOT be in the delete list (FK CASCADE handles them)
        assert "tasks" not in delete_calls
        assert "decisions" not in delete_calls
        assert "topic_thread_mentions" not in delete_calls
        assert "open_questions" not in delete_calls
        assert "follow_up_meetings" not in delete_calls

        # Pre-count should have run on the 5 reporting tables
        # (we can verify they were at least touched via .table(...))
        for pre_count_table in (
            "tasks", "decisions", "open_questions",
            "follow_up_meetings", "topic_thread_mentions",
        ):
            assert pre_count_table in touched_tables

        # Counts dict shape is preserved for callers that log it
        assert "tasks" in result
        assert "decisions" in result
        assert "meetings" in result
        assert "embeddings" in result

    def test_tombstone_path_still_deletes_children_explicitly(self):
        """
        keep_tombstone=True path is unchanged — it MUST explicitly delete every
        child table because the parent row is kept alive (so DB cascade can't fire).
        """
        from services.supabase_client import SupabaseClient

        client = SupabaseClient()
        mock_supabase = MagicMock()
        delete_calls: list[str] = []

        def table_router(name):
            chain = MagicMock()

            select_chain = MagicMock()
            select_chain.eq.return_value = select_chain
            select_chain.execute.return_value = MagicMock(count=0)
            chain.select.return_value = select_chain

            delete_chain = MagicMock()
            delete_chain.eq.return_value = delete_chain

            def _execute_delete():
                delete_calls.append(name)
                return MagicMock(data=[])

            delete_chain.execute.side_effect = _execute_delete
            chain.delete.return_value = delete_chain

            # tombstone also calls .update(...).eq(...).execute() on meetings
            update_chain = MagicMock()
            update_chain.eq.return_value = update_chain
            update_chain.execute.return_value = MagicMock(data=[{"id": "m1"}])
            chain.update.return_value = update_chain

            return chain

        mock_supabase.table.side_effect = table_router
        object.__setattr__(client, "_client", mock_supabase)

        result = client.delete_meeting_cascade("test-id", keep_tombstone=True)

        # Tombstone path: every child table must be explicitly deleted because
        # the parent row is preserved (FK cascade won't fire).
        for child in (
            "task_mentions", "entity_mentions", "topic_thread_mentions",
            "commitments", "decisions", "follow_up_meetings",
            "open_questions", "tasks", "embeddings", "pending_approvals",
        ):
            assert child in delete_calls, f"tombstone path must delete {child}"

        # meetings table is NOT deleted in tombstone path (it's updated instead)
        assert "meetings" not in delete_calls

        # Tombstone marker is set
        assert result["tombstone"] == 1
        assert result["meetings"] == 0

    def test_handles_empty_results(self):
        """Counts are 0 when tables have no matching records."""
        from services.supabase_client import SupabaseClient

        client = SupabaseClient()
        mock_supabase = MagicMock()

        def table_router(name):
            chain = MagicMock()
            # Pre-count returns 0
            select_chain = MagicMock()
            select_chain.eq.return_value = select_chain
            select_chain.execute.return_value = MagicMock(count=0)
            chain.select.return_value = select_chain
            # Delete returns empty data
            delete_chain = MagicMock()
            delete_chain.eq.return_value = delete_chain
            delete_chain.execute.return_value = MagicMock(data=[])
            chain.delete.return_value = delete_chain
            return chain

        mock_supabase.table.side_effect = table_router
        object.__setattr__(client, "_client", mock_supabase)

        result = client.delete_meeting_cascade("nonexistent-id")

        assert result["embeddings"] == 0
        assert result["tasks"] == 0
        assert result["meetings"] == 0

    def test_handles_exception(self):
        """Returns partial counts on error."""
        from services.supabase_client import SupabaseClient

        client = SupabaseClient()
        mock_supabase = MagicMock()
        mock_supabase.table.side_effect = Exception("DB error")
        object.__setattr__(client, "_client", mock_supabase)

        result = client.delete_meeting_cascade("test-id")

        assert result["embeddings"] == 0
        assert result["tasks"] == 0
        assert result["meetings"] == 0


class TestFindMeetingBySource:
    """Tests for find_meeting_by_source."""

    def test_finds_by_partial_path(self):
        """Finds meeting by partial source file path."""
        from services.supabase_client import SupabaseClient

        client = SupabaseClient()
        mock_supabase = MagicMock()
        mock_chain = MagicMock()
        mock_chain.select.return_value = mock_chain
        mock_chain.ilike.return_value = mock_chain
        mock_chain.limit.return_value = mock_chain
        mock_chain.execute.return_value = MagicMock(
            data=[{"id": "m1", "title": "Test Meeting"}]
        )
        mock_supabase.table.return_value = mock_chain
        object.__setattr__(client, "_client", mock_supabase)

        result = client.find_meeting_by_source("test_transcript.txt")

        assert result is not None
        assert result["title"] == "Test Meeting"

    def test_returns_none_when_not_found(self):
        """Returns None when no matching source file."""
        from services.supabase_client import SupabaseClient

        client = SupabaseClient()
        mock_supabase = MagicMock()
        mock_chain = MagicMock()
        mock_chain.select.return_value = mock_chain
        mock_chain.ilike.return_value = mock_chain
        mock_chain.limit.return_value = mock_chain
        mock_chain.execute.return_value = MagicMock(data=[])
        mock_supabase.table.return_value = mock_chain
        object.__setattr__(client, "_client", mock_supabase)

        result = client.find_meeting_by_source("nonexistent.txt")
        assert result is None


class TestSearchMeetingsByTitle:
    """Tests for search_meetings_by_title."""

    def test_returns_matching_meetings(self):
        """Returns meetings matching the title query."""
        from services.supabase_client import SupabaseClient

        client = SupabaseClient()
        mock_supabase = MagicMock()
        mock_chain = MagicMock()
        mock_chain.select.return_value = mock_chain
        mock_chain.ilike.return_value = mock_chain
        mock_chain.order.return_value = mock_chain
        mock_chain.limit.return_value = mock_chain
        mock_chain.execute.return_value = MagicMock(
            data=[
                {"id": "m1", "title": "Strategy Review", "date": "2026-03-01"},
                {"id": "m2", "title": "Strategy Planning", "date": "2026-02-28"},
            ]
        )
        mock_supabase.table.return_value = mock_chain
        object.__setattr__(client, "_client", mock_supabase)

        result = client.search_meetings_by_title("Strategy")
        assert len(result) == 2


class TestReprocessFile:
    """Tests for transcript_watcher.reprocess_file."""

    @pytest.mark.asyncio
    async def test_reprocess_deletes_then_processes(self):
        """Reprocess deletes old meeting then processes fresh."""
        from schedulers.transcript_watcher import TranscriptWatcher

        watcher = TranscriptWatcher()

        mock_file = {"name": "test_transcript.txt", "id": "file-123"}

        with patch("schedulers.transcript_watcher.drive_service") as mock_drive, \
             patch("services.supabase_client.SupabaseClient.find_meeting_by_source") as mock_find, \
             patch("services.supabase_client.SupabaseClient.delete_meeting_cascade") as mock_delete:

            mock_drive.get_file_metadata = AsyncMock(return_value=mock_file)

            mock_find.return_value = {
                "id": "old-meeting-id",
                "title": "Old Meeting",
            }
            mock_delete.return_value = {"embeddings": 5, "tasks": 2, "meetings": 1}

            # Mock process_file_manually
            watcher.process_file_manually = AsyncMock(
                return_value={"status": "processed", "meeting_id": "new-id"}
            )

            result = await watcher.reprocess_file("file-123")

        assert result["reprocessed"] is True
        assert result["deleted_old"]["embeddings"] == 5
        assert result["status"] == "processed"
        mock_delete.assert_called_once_with("old-meeting-id")

    @pytest.mark.asyncio
    async def test_reprocess_no_existing_just_processes(self):
        """When no existing meeting, just processes normally."""
        from schedulers.transcript_watcher import TranscriptWatcher

        watcher = TranscriptWatcher()

        with patch("schedulers.transcript_watcher.drive_service") as mock_drive, \
             patch("services.supabase_client.SupabaseClient.find_meeting_by_source") as mock_find:

            mock_drive.get_file_metadata = AsyncMock(
                return_value={"name": "new_transcript.txt", "id": "file-456"}
            )
            mock_find.return_value = None

            watcher.process_file_manually = AsyncMock(
                return_value={"status": "processed", "meeting_id": "new-id"}
            )

            result = await watcher.reprocess_file("file-456")

        assert result["reprocessed"] is True
        assert "deleted_old" not in result

    @pytest.mark.asyncio
    async def test_reprocess_file_not_found(self):
        """Returns error when Drive file not found."""
        from schedulers.transcript_watcher import TranscriptWatcher

        watcher = TranscriptWatcher()

        with patch("schedulers.transcript_watcher.drive_service") as mock_drive:
            mock_drive.get_file_metadata = AsyncMock(return_value=None)

            result = await watcher.reprocess_file("nonexistent")

        assert result["status"] == "error"


class TestReprocessCommand:
    """Tests for /reprocess Telegram command handler."""

    @pytest.mark.asyncio
    async def test_admin_only(self):
        """Non-admin users are rejected."""
        from services.telegram_bot import TelegramBot

        bot = TelegramBot.__new__(TelegramBot)
        bot.eyal_chat_id = "12345"
        bot._is_admin = MagicMock(return_value=False)
        bot.send_message = AsyncMock()

        update = MagicMock()
        update.effective_user.id = 99999
        update.effective_chat.id = 99999
        context = MagicMock()
        context.args = []

        await bot._handle_reprocess(update, context)

        bot.send_message.assert_called_once()
        msg = bot.send_message.call_args[0][1]
        assert "Only Eyal" in msg

    @pytest.mark.asyncio
    async def test_no_args_lists_meetings(self):
        """No arguments lists recent meetings."""
        from services.telegram_bot import TelegramBot

        bot = TelegramBot.__new__(TelegramBot)
        bot.eyal_chat_id = "12345"
        bot._is_admin = MagicMock(return_value=True)
        bot.send_message = AsyncMock()

        update = MagicMock()
        update.effective_user.id = 12345
        update.effective_chat.id = 12345
        context = MagicMock()
        context.args = []

        mock_module = MagicMock()
        mock_module.supabase_client.list_meetings.return_value = [
            {"title": "Meeting A", "date": "2026-03-01T10:00:00"},
            {"title": "Meeting B", "date": "2026-02-28T14:00:00"},
        ]

        with patch.dict("sys.modules", {"services.supabase_client": mock_module}):
            await bot._handle_reprocess(update, context)

        bot.send_message.assert_called_once()
        msg = bot.send_message.call_args[0][1]
        assert "Recent Meetings" in msg

    @pytest.mark.asyncio
    async def test_ambiguous_title_asks_specificity(self):
        """Multiple matches asks user to be more specific."""
        from services.telegram_bot import TelegramBot

        bot = TelegramBot.__new__(TelegramBot)
        bot.eyal_chat_id = "12345"
        bot._is_admin = MagicMock(return_value=True)
        bot.send_message = AsyncMock()

        update = MagicMock()
        update.effective_user.id = 12345
        update.effective_chat.id = 12345
        context = MagicMock()
        context.args = ["Strategy"]

        mock_module = MagicMock()
        mock_module.supabase_client.search_meetings_by_title.return_value = [
            {"title": "Strategy Review", "date": "2026-03-01T10:00:00"},
            {"title": "Strategy Planning", "date": "2026-02-28T14:00:00"},
        ]

        with patch.dict("sys.modules", {"services.supabase_client": mock_module}):
            await bot._handle_reprocess(update, context)

        msg = bot.send_message.call_args[0][1]
        assert "more specific" in msg
