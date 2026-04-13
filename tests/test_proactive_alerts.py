"""
Tests for proactive alerts (v0.3 Tier 2).

Tests cover:
- Overdue task clusters
- Stale commitments
- Recurring discussions
- Open question pileup
- Alert formatting
- Scheduler behavior
"""

import pytest
from datetime import datetime, timedelta
from unittest.mock import patch, MagicMock, AsyncMock

from processors.proactive_alerts import (
    generate_alerts,
    generate_post_meeting_alerts,
    format_alerts_message,
    _check_overdue_clusters,
    _check_stale_commitments,
    _check_recurring_discussions,
    _check_question_pileup,
)


# =========================================================================
# Test Overdue Clusters
# =========================================================================

class TestOverdueClusters:
    """Tests for overdue task cluster detection."""

    def test_no_overdue_tasks(self):
        """No overdue tasks should produce no alerts."""
        with patch("processors.proactive_alerts.supabase_client") as mock_db:
            mock_db.get_tasks.return_value = []
            result = _check_overdue_clusters()
            assert result == []

    def test_three_or_more_triggers_alert(self):
        """3+ overdue tasks for one assignee should trigger alert.

        v2.3: deadline_confidence='EXPLICIT' is required to pass the filter
        that was added to `_check_overdue_clusters` — INFERRED and NONE
        deadlines are LLM guesses and suppressed from cluster alerts.
        """
        recent = datetime.now().isoformat()
        with patch("processors.proactive_alerts.supabase_client") as mock_db:
            mock_db.get_tasks.return_value = [
                {"assignee": "Eyal", "title": "Task 1", "status": "overdue", "created_at": recent, "deadline_confidence": "EXPLICIT"},
                {"assignee": "Eyal", "title": "Task 2", "status": "overdue", "created_at": recent, "deadline_confidence": "EXPLICIT"},
                {"assignee": "Eyal", "title": "Task 3", "status": "overdue", "created_at": recent, "deadline_confidence": "EXPLICIT"},
            ]
            result = _check_overdue_clusters()
            assert len(result) == 1
            assert result[0]["severity"] == "high"
            assert "Eyal" in result[0]["title"]
            assert "3" in result[0]["title"]

    def test_two_does_not_trigger(self):
        """2 overdue tasks should not trigger alert."""
        recent = datetime.now().isoformat()
        with patch("processors.proactive_alerts.supabase_client") as mock_db:
            mock_db.get_tasks.return_value = [
                {"assignee": "Eyal", "title": "Task 1", "status": "overdue", "created_at": recent},
                {"assignee": "Eyal", "title": "Task 2", "status": "overdue", "created_at": recent},
            ]
            result = _check_overdue_clusters()
            assert result == []

    def test_groups_by_assignee(self):
        """Should alert separately for each assignee with 3+.

        v2.3: all fixture tasks carry deadline_confidence='EXPLICIT' to pass
        the PR 2 filter — see test_three_or_more_triggers_alert for rationale.
        """
        recent = datetime.now().isoformat()
        with patch("processors.proactive_alerts.supabase_client") as mock_db:
            mock_db.get_tasks.return_value = [
                {"assignee": "Eyal", "title": "T1", "status": "overdue", "created_at": recent, "deadline_confidence": "EXPLICIT"},
                {"assignee": "Eyal", "title": "T2", "status": "overdue", "created_at": recent, "deadline_confidence": "EXPLICIT"},
                {"assignee": "Eyal", "title": "T3", "status": "overdue", "created_at": recent, "deadline_confidence": "EXPLICIT"},
                {"assignee": "Paolo", "title": "T4", "status": "overdue", "created_at": recent, "deadline_confidence": "EXPLICIT"},
                {"assignee": "Paolo", "title": "T5", "status": "overdue", "created_at": recent, "deadline_confidence": "EXPLICIT"},
                {"assignee": "Paolo", "title": "T6", "status": "overdue", "created_at": recent, "deadline_confidence": "EXPLICIT"},
            ]
            result = _check_overdue_clusters()
            assert len(result) == 2
            names = [a["title"] for a in result]
            assert any("Eyal" in n for n in names)
            assert any("Paolo" in n for n in names)


# =========================================================================
# Test Stale Commitments
# =========================================================================

class TestStaleCommitments:
    """Tests for stale commitment detection."""

    def test_no_commitments(self):
        """No open commitments should produce no alerts."""
        with patch("processors.proactive_alerts.supabase_client") as mock_db:
            mock_db.get_commitments.return_value = []
            result = _check_stale_commitments()
            assert result == []

    def test_old_commitment_triggers(self):
        """Commitment older than 2 weeks should trigger alert."""
        three_weeks_ago = (datetime.now() - timedelta(days=21)).isoformat()
        with patch("processors.proactive_alerts.supabase_client") as mock_db:
            mock_db.get_commitments.return_value = [
                {"id": "c1", "speaker": "Eyal", "commitment_text": "Send deck", "created_at": three_weeks_ago},
            ]
            result = _check_stale_commitments()
            assert len(result) == 1
            assert result[0]["severity"] == "medium"

    def test_recent_commitment_does_not_trigger(self):
        """Commitment from 3 days ago should not trigger alert."""
        recent = (datetime.now() - timedelta(days=3)).isoformat()
        with patch("processors.proactive_alerts.supabase_client") as mock_db:
            mock_db.get_commitments.return_value = [
                {"id": "c1", "speaker": "Eyal", "commitment_text": "Send deck", "created_at": recent},
            ]
            result = _check_stale_commitments()
            assert result == []


# =========================================================================
# Test Recurring Discussions
# =========================================================================

class TestRecurringDiscussions:
    """Tests for recurring discussion detection."""

    def test_entity_in_three_meetings_triggers(self):
        """Entity mentioned in 3+ meetings should trigger alert."""
        with patch("processors.proactive_alerts.supabase_client") as mock_db:
            mock_db.list_entities.return_value = [
                {"id": "e1", "canonical_name": "Lavazza", "entity_type": "organization"},
            ]
            recent = datetime.now().isoformat()
            mock_db.get_entity_mentions.return_value = [
                {"entity_id": "e1", "meeting_id": "m1", "entities": {"canonical_name": "Lavazza"}, "created_at": recent},
                {"entity_id": "e1", "meeting_id": "m2", "entities": {"canonical_name": "Lavazza"}, "created_at": recent},
                {"entity_id": "e1", "meeting_id": "m3", "entities": {"canonical_name": "Lavazza"}, "created_at": recent},
            ]
            result = _check_recurring_discussions()
            assert len(result) == 1
            assert "Lavazza" in result[0]["title"]
            assert result[0]["severity"] == "low"

    def test_entity_in_two_meetings_does_not_trigger(self):
        """Entity in only 2 meetings should not trigger."""
        with patch("processors.proactive_alerts.supabase_client") as mock_db:
            mock_db.list_entities.return_value = [
                {"id": "e1", "canonical_name": "Ferrero", "entity_type": "organization"},
            ]
            mock_db.get_entity_mentions.return_value = [
                {"entity_id": "e1", "meeting_id": "m1"},
                {"entity_id": "e1", "meeting_id": "m2"},
            ]
            result = _check_recurring_discussions()
            assert result == []


# =========================================================================
# Test Question Pileup
# =========================================================================

class TestQuestionPileup:
    """Tests for open question pileup detection."""

    def test_five_or_more_triggers(self):
        """5+ open questions should trigger alert."""
        recent = datetime.now().isoformat()
        with patch("processors.proactive_alerts.supabase_client") as mock_db:
            mock_db.get_open_questions.return_value = [
                {"question": f"Question {i}", "raised_by": "Eyal", "created_at": recent} for i in range(6)
            ]
            result = _check_question_pileup()
            assert len(result) == 1
            assert result[0]["severity"] == "medium"
            assert "6" in result[0]["title"]

    def test_few_questions_does_not_trigger(self):
        """Fewer than 5 questions should not trigger."""
        recent = datetime.now().isoformat()
        with patch("processors.proactive_alerts.supabase_client") as mock_db:
            mock_db.get_open_questions.return_value = [
                {"question": "Q1", "raised_by": "Eyal", "created_at": recent},
                {"question": "Q2", "raised_by": "Roye", "created_at": recent},
            ]
            result = _check_question_pileup()
            assert result == []


# =========================================================================
# Test format_alerts_message
# =========================================================================

class TestFormatAlerts:
    """Tests for alert message formatting."""

    def test_severity_grouping(self):
        """Alerts should be grouped by severity in the message."""
        alerts = [
            {"type": "overdue_cluster", "severity": "high", "title": "3 overdue tasks", "details": ""},
            {"type": "question_pileup", "severity": "medium", "title": "6 open questions", "details": ""},
            {"type": "recurring_discussion", "severity": "low", "title": "Lavazza in 4 meetings", "details": ""},
        ]
        message = format_alerts_message(alerts)
        assert "Heads up" in message
        assert "🔴" in message  # high severity emoji
        assert "🟡" in message  # medium severity emoji
        assert "3 overdue tasks" in message
        assert "6 open questions" in message
        assert "Lavazza in 4 meetings" in message
        # High should appear before medium in the output
        assert message.index("3 overdue tasks") < message.index("6 open questions")

    def test_empty_alerts(self):
        """Empty alerts should return empty string."""
        message = format_alerts_message([])
        assert message == ""


# =========================================================================
# Test Scheduler
# =========================================================================

class TestAlertScheduler:
    """Tests for the alert scheduler."""

    @pytest.mark.asyncio
    async def test_sends_once_per_day(self):
        """Scheduler should only send alerts once per day."""
        from schedulers.alert_scheduler import AlertScheduler

        scheduler = AlertScheduler(check_interval=1)

        with patch("schedulers.alert_scheduler.generate_alerts") as mock_gen, \
             patch("schedulers.alert_scheduler.format_alerts_message") as mock_fmt, \
             patch("schedulers.alert_scheduler.telegram_bot") as mock_tg, \
             patch("schedulers.alert_scheduler.supabase_client") as mock_db:

            mock_gen.return_value = [{"type": "test", "severity": "low", "title": "Test"}]
            mock_fmt.return_value = "Test alert"
            mock_tg.send_to_eyal = AsyncMock()

            count = await scheduler._check_and_send_alerts()
            assert count == 1
            mock_tg.send_to_eyal.assert_called_once()

    @pytest.mark.asyncio
    async def test_sends_to_eyal(self):
        """Alerts should be sent to Eyal."""
        from schedulers.alert_scheduler import AlertScheduler

        scheduler = AlertScheduler()

        with patch("schedulers.alert_scheduler.generate_alerts") as mock_gen, \
             patch("schedulers.alert_scheduler.format_alerts_message") as mock_fmt, \
             patch("schedulers.alert_scheduler.telegram_bot") as mock_tg, \
             patch("schedulers.alert_scheduler.supabase_client") as mock_db:

            mock_gen.return_value = [{"type": "test", "severity": "high", "title": "Alert!"}]
            mock_fmt.return_value = "Formatted alert"
            mock_tg.send_to_eyal = AsyncMock()

            await scheduler._check_and_send_alerts()
            mock_tg.send_to_eyal.assert_called_once_with("Formatted alert", parse_mode="HTML")
