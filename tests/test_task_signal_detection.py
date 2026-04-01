"""Tests for Phase 12 A5: Task signal detection and CRUD.

Tests cover:
- create_task_signal() and get_task_signals() in supabase_client
- detect_email_task_signals()
- detect_gantt_task_signals()
- detect_calendar_task_signals()
"""

import pytest
from unittest.mock import MagicMock, patch


def _make_task(task_id="t1", title="Write accuracy abstract for Moldova pilot",
               assignee="Roye", status="pending", category="Product & Tech"):
    return {
        "id": task_id,
        "title": title,
        "assignee": assignee,
        "status": status,
        "category": category,
    }


# =========================================================================
# Supabase CRUD: create_task_signal
# =========================================================================

class TestCreateTaskSignal:

    @patch("services.supabase_client.supabase_client")
    def test_creates_signal(self, mock_sc):
        mock_sc.client.table.return_value.insert.return_value.execute.return_value = MagicMock(
            data=[{"id": "sig-1", "task_id": "t1", "signal_type": "completion"}]
        )

        from services.supabase_client import SupabaseClient
        result = SupabaseClient.create_task_signal(
            mock_sc,
            task_id="t1",
            signal_type="completion",
            signal_source="email",
            confidence="medium",
            details={"msg_id": "abc"},
        )
        assert result["signal_type"] == "completion"

    @patch("services.supabase_client.supabase_client")
    def test_handles_db_error(self, mock_sc):
        mock_sc.client.table.return_value.insert.side_effect = Exception("DB error")

        from services.supabase_client import SupabaseClient
        result = SupabaseClient.create_task_signal(
            mock_sc, task_id="t1", signal_type="completion",
        )
        assert result == {}


# =========================================================================
# Supabase CRUD: get_task_signals
# =========================================================================

class TestGetTaskSignals:

    @patch("services.supabase_client.supabase_client")
    def test_returns_signals(self, mock_sc):
        chain = MagicMock()
        chain.eq.return_value = chain
        chain.order.return_value = chain
        chain.limit.return_value = chain
        chain.execute.return_value = MagicMock(
            data=[{"id": "s1", "signal_type": "completion"}]
        )
        mock_sc.client.table.return_value.select.return_value = chain

        from services.supabase_client import SupabaseClient
        result = SupabaseClient.get_task_signals(mock_sc, task_id="t1")
        assert len(result) == 1

    @patch("services.supabase_client.supabase_client")
    def test_returns_empty(self, mock_sc):
        chain = MagicMock()
        chain.order.return_value = chain
        chain.limit.return_value = chain
        chain.execute.return_value = MagicMock(data=[])
        mock_sc.client.table.return_value.select.return_value = chain

        from services.supabase_client import SupabaseClient
        result = SupabaseClient.get_task_signals(mock_sc)
        assert result == []


# =========================================================================
# detect_email_task_signals
# =========================================================================

class TestDetectEmailTaskSignals:

    @patch("processors.task_signal_detection.supabase_client")
    def test_no_items_returns_empty(self, mock_sc):
        from processors.task_signal_detection import detect_email_task_signals

        result = detect_email_task_signals([], "msg1", "test@test.com", "Hello")
        assert result == []

    @patch("processors.task_signal_detection.supabase_client")
    def test_no_open_tasks_returns_empty(self, mock_sc):
        mock_sc.get_tasks.return_value = []

        from processors.task_signal_detection import detect_email_task_signals

        result = detect_email_task_signals(
            [{"text": "I finished the abstract", "type": "task"}],
            "msg1", "roye@test.com", "Re: abstract",
        )
        assert result == []

    @patch("processors.task_signal_detection.supabase_client")
    def test_matching_email_creates_signal(self, mock_sc):
        mock_sc.get_tasks.side_effect = [
            [_make_task("t1", "Write accuracy abstract for Moldova pilot")],  # pending
            [],  # in_progress
        ]
        mock_sc.create_task_signal.return_value = {"id": "sig-1", "signal_type": "completion"}

        from processors.task_signal_detection import detect_email_task_signals

        result = detect_email_task_signals(
            [{"text": "I completed the accuracy abstract for Moldova pilot documentation", "type": "task"}],
            "msg1", "roye@test.com", "abstract done",
        )
        assert len(result) == 1
        mock_sc.create_task_signal.assert_called_once()
        call_kwargs = mock_sc.create_task_signal.call_args[1]
        assert call_kwargs["task_id"] == "t1"
        assert call_kwargs["signal_type"] == "completion"
        assert call_kwargs["signal_source"] == "email"

    @patch("processors.task_signal_detection.supabase_client")
    def test_impediment_detection(self, mock_sc):
        mock_sc.get_tasks.side_effect = [
            [_make_task("t1", "Send capability deck to Lavazza partner")],
            [],
        ]
        mock_sc.create_task_signal.return_value = {"id": "sig-1", "signal_type": "impediment"}

        from processors.task_signal_detection import detect_email_task_signals

        result = detect_email_task_signals(
            [{"text": "The capability deck for Lavazza partner is blocked waiting for legal review", "type": "info"}],
            "msg2", "paolo@test.com", "deck update",
        )
        assert len(result) == 1
        call_kwargs = mock_sc.create_task_signal.call_args[1]
        assert call_kwargs["signal_type"] == "impediment"

    @patch("processors.task_signal_detection.supabase_client")
    def test_no_match_below_threshold(self, mock_sc):
        """Items with < 3 keyword overlap should not create signals."""
        mock_sc.get_tasks.side_effect = [
            [_make_task("t1", "Write accuracy abstract for Moldova pilot")],
            [],
        ]

        from processors.task_signal_detection import detect_email_task_signals

        result = detect_email_task_signals(
            [{"text": "unrelated content about weather", "type": "info"}],
            "msg3", "test@test.com", "weather",
        )
        assert result == []
        mock_sc.create_task_signal.assert_not_called()

    @patch("processors.task_signal_detection.supabase_client")
    def test_db_fetch_failure_returns_empty(self, mock_sc):
        mock_sc.get_tasks.side_effect = Exception("DB down")

        from processors.task_signal_detection import detect_email_task_signals

        result = detect_email_task_signals(
            [{"text": "completed the task", "type": "task"}],
            "msg4", "test@test.com", "update",
        )
        assert result == []


# =========================================================================
# detect_gantt_task_signals
# =========================================================================

class TestDetectGanttTaskSignals:

    @patch("processors.task_signal_detection.supabase_client")
    def test_no_changes_returns_empty(self, mock_sc):
        from processors.task_signal_detection import detect_gantt_task_signals

        result = detect_gantt_task_signals([])
        assert result == []

    @patch("processors.task_signal_detection.supabase_client")
    def test_completion_transition_creates_signal(self, mock_sc):
        mock_sc.get_tasks.side_effect = [
            [_make_task("t1", "Deploy product infrastructure", category="Product & Tech")],
            [],
        ]
        mock_sc.create_task_signal.return_value = {"id": "sig-1"}

        from processors.task_signal_detection import detect_gantt_task_signals

        changes = [
            {
                "section": "Product & Technology",
                "subsection": "Infrastructure",
                "old_value": "[R] Active",
                "new_value": "[R] Completed",
                "row": 5,
                "column": "G",
            },
        ]
        result = detect_gantt_task_signals(changes, proposal_id="prop-1")
        assert len(result) == 1
        call_kwargs = mock_sc.create_task_signal.call_args[1]
        assert call_kwargs["signal_type"] == "completion"
        assert call_kwargs["signal_source"] == "gantt"
        assert call_kwargs["confidence"] == "medium"

    @patch("processors.task_signal_detection.supabase_client")
    def test_blocked_transition_creates_impediment(self, mock_sc):
        mock_sc.get_tasks.side_effect = [
            [_make_task("t1", "Sign legal agreement", category="Legal & Compliance")],
            [],
        ]
        mock_sc.create_task_signal.return_value = {"id": "sig-1"}

        from processors.task_signal_detection import detect_gantt_task_signals

        changes = [
            {
                "section": "Legal",
                "subsection": "Contracts",
                "old_value": "[E] Active",
                "new_value": "[E] Blocked",
            },
        ]
        result = detect_gantt_task_signals(changes)
        assert len(result) == 1
        call_kwargs = mock_sc.create_task_signal.call_args[1]
        assert call_kwargs["signal_type"] == "impediment"

    @patch("processors.task_signal_detection.supabase_client")
    def test_no_status_change_skipped(self, mock_sc):
        """Changes without meaningful status transitions should be skipped."""
        mock_sc.get_tasks.side_effect = [
            [_make_task()],
            [],
        ]

        from processors.task_signal_detection import detect_gantt_task_signals

        changes = [
            {
                "section": "Product",
                "subsection": "Dev",
                "old_value": "Active",
                "new_value": "Active",
            },
        ]
        result = detect_gantt_task_signals(changes)
        assert result == []


# =========================================================================
# detect_calendar_task_signals
# =========================================================================

class TestDetectCalendarTaskSignals:

    @patch("processors.task_signal_detection.supabase_client")
    def test_no_events_returns_empty(self, mock_sc):
        from processors.task_signal_detection import detect_calendar_task_signals

        result = detect_calendar_task_signals([])
        assert result == []

    @patch("processors.task_signal_detection.supabase_client")
    def test_matching_event_creates_signal(self, mock_sc):
        mock_sc.get_tasks.side_effect = [
            [_make_task("t1", "Review accuracy abstract Moldova pilot results")],
            [],
        ]
        mock_sc.create_task_signal.return_value = {"id": "sig-1"}

        from processors.task_signal_detection import detect_calendar_task_signals

        events = [
            {
                "id": "ev-1",
                "title": "Review accuracy abstract Moldova pilot results",
                "description": "Review the abstract for Moldova pilot accuracy benchmarks",
            },
        ]
        result = detect_calendar_task_signals(events)
        assert len(result) == 1
        call_kwargs = mock_sc.create_task_signal.call_args[1]
        assert call_kwargs["signal_source"] == "calendar"

    @patch("processors.task_signal_detection.supabase_client")
    def test_completion_keyword_in_description(self, mock_sc):
        mock_sc.get_tasks.side_effect = [
            [_make_task("t1", "Prepare investor deck for funding round")],
            [],
        ]
        mock_sc.create_task_signal.return_value = {"id": "sig-1"}

        from processors.task_signal_detection import detect_calendar_task_signals

        events = [
            {
                "id": "ev-2",
                "title": "Investor deck funding round review",
                "description": "Completed the investor deck preparation for funding round",
            },
        ]
        result = detect_calendar_task_signals(events)
        assert len(result) == 1
        call_kwargs = mock_sc.create_task_signal.call_args[1]
        assert call_kwargs["signal_type"] == "completion"

    @patch("processors.task_signal_detection.supabase_client")
    def test_no_match_returns_empty(self, mock_sc):
        mock_sc.get_tasks.side_effect = [
            [_make_task("t1", "Write accuracy abstract")],
            [],
        ]

        from processors.task_signal_detection import detect_calendar_task_signals

        events = [
            {"id": "ev-3", "title": "Lunch with team", "description": ""},
        ]
        result = detect_calendar_task_signals(events)
        assert result == []

    @patch("processors.task_signal_detection.supabase_client")
    def test_db_failure_returns_empty(self, mock_sc):
        mock_sc.get_tasks.side_effect = Exception("DB down")

        from processors.task_signal_detection import detect_calendar_task_signals

        result = detect_calendar_task_signals([{"id": "e1", "title": "Test", "description": ""}])
        assert result == []
