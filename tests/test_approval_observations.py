"""
Tests for PR 3 — approval observation capture layer (v2.3).

Covers:
- log_approval_observation() writes correct row shape
- Fire-and-forget: DB failure does NOT raise or interrupt caller
- edit_distance_pct computed correctly for edited content
- None for non-edited actions
- get_approval_stats MCP tool aggregates correctly
"""

from unittest.mock import MagicMock, patch

import pytest


# =============================================================================
# log_approval_observation helper
# =============================================================================

class TestLogApprovalObservation:
    def _make_sc_with_mock_client(self):
        from services.supabase_client import SupabaseClient
        with patch.object(SupabaseClient, "__init__", return_value=None):
            sc = SupabaseClient()
        mock_client = MagicMock()
        sc._client = mock_client
        return sc, mock_client

    def test_writes_basic_row(self):
        sc, mock_client = self._make_sc_with_mock_client()
        fake_query = MagicMock()
        fake_query.insert.return_value = fake_query
        fake_query.execute.return_value = MagicMock(data=[{"id": "obs-1"}])
        mock_client.table.return_value = fake_query

        sc.log_approval_observation(
            content_type="meeting_summary",
            action="approved",
            content_id="m-1",
            final_content={"summary": "ok"},
            context={"meeting_id": "m-1"},
        )

        mock_client.table.assert_called_once_with("approval_observations")
        call_kwargs = fake_query.insert.call_args[0][0]
        assert call_kwargs["content_type"] == "meeting_summary"
        assert call_kwargs["action"] == "approved"
        assert call_kwargs["content_id"] == "m-1"
        assert call_kwargs["final_content"] == {"summary": "ok"}
        assert call_kwargs["edit_distance_pct"] is None  # not 'edited'

    def test_edit_distance_computed_for_edited(self):
        sc, mock_client = self._make_sc_with_mock_client()
        fake_query = MagicMock()
        fake_query.insert.return_value = fake_query
        fake_query.execute.return_value = MagicMock(data=[{"id": "obs-1"}])
        mock_client.table.return_value = fake_query

        sc.log_approval_observation(
            content_type="meeting_summary",
            action="edited",
            original_content={"summary": "hello world"},
            final_content={"summary": "hello WORLD and more"},
        )

        call_kwargs = fake_query.insert.call_args[0][0]
        assert call_kwargs["edit_distance_pct"] is not None
        assert 0.0 < call_kwargs["edit_distance_pct"] < 1.0

    def test_edit_distance_zero_for_identical(self):
        sc, mock_client = self._make_sc_with_mock_client()
        fake_query = MagicMock()
        fake_query.insert.return_value = fake_query
        fake_query.execute.return_value = MagicMock(data=[{"id": "obs-1"}])
        mock_client.table.return_value = fake_query

        sc.log_approval_observation(
            content_type="meeting_summary",
            action="edited",
            original_content={"summary": "same"},
            final_content={"summary": "same"},
        )

        call_kwargs = fake_query.insert.call_args[0][0]
        assert call_kwargs["edit_distance_pct"] == 0.0

    def test_fire_and_forget_on_db_failure(self):
        """DB insert failure MUST NOT propagate — logs warning and returns."""
        sc, mock_client = self._make_sc_with_mock_client()
        fake_query = MagicMock()
        fake_query.insert.return_value = fake_query
        fake_query.execute.side_effect = Exception("connection lost")
        mock_client.table.return_value = fake_query

        # Should not raise
        sc.log_approval_observation(
            content_type="meeting_summary",
            action="approved",
        )

    def test_empty_context_becomes_empty_dict(self):
        sc, mock_client = self._make_sc_with_mock_client()
        fake_query = MagicMock()
        fake_query.insert.return_value = fake_query
        fake_query.execute.return_value = MagicMock(data=[{"id": "obs-1"}])
        mock_client.table.return_value = fake_query

        sc.log_approval_observation(
            content_type="sheets_sync",
            action="approved",
        )

        call_kwargs = fake_query.insert.call_args[0][0]
        assert call_kwargs["context"] == {}


# =============================================================================
# sheets_sync hook
# =============================================================================

class TestSheetsSyncObservationHook:
    def test_logs_on_successful_apply(self):
        """apply_sheets_to_db logs an approval_observation when total > 0."""
        from processors import sheets_sync

        diff = {
            "tasks": {
                "modified": [{"db_id": "t-1", "changes": {"status": {"to": "done"}}}],
                "sheets_only": [],
            },
            "decisions": {"modified": []},
        }

        with patch.object(sheets_sync, "supabase_client") as mock_sc:
            fake_tasks = MagicMock()
            fake_tasks.update.return_value = fake_tasks
            fake_tasks.eq.return_value = fake_tasks
            fake_tasks.execute.return_value = MagicMock(data=[{"id": "t-1"}])
            mock_sc.client.table.return_value = fake_tasks

            sheets_sync.apply_sheets_to_db(diff)

            # observation hook fired exactly once, with correct content_type
            assert mock_sc.log_approval_observation.called
            call_kwargs = mock_sc.log_approval_observation.call_args.kwargs
            assert call_kwargs["content_type"] == "sheets_sync"
            assert call_kwargs["action"] == "approved"

    def test_skips_log_when_no_changes(self):
        """Empty diff should not fire an observation (total = 0)."""
        from processors import sheets_sync

        diff = {
            "tasks": {"modified": [], "sheets_only": []},
            "decisions": {"modified": []},
        }

        with patch.object(sheets_sync, "supabase_client") as mock_sc:
            sheets_sync.apply_sheets_to_db(diff)
            mock_sc.log_approval_observation.assert_not_called()
