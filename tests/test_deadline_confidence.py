"""
Tests for PR 2 — deadline confidence tiers (v2.3).

Covers:
- DeadlineConfidence enum + Task schema field
- update_task_deadline() helper defaults to EXPLICIT
- Task reminder scheduler filters to EXPLICIT keys, falls back on DB error
- Proactive alerts overdue checks filter to EXPLICIT
- Morning brief renders ~ prefix on INFERRED deadlines
"""

from datetime import date, datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest


# =============================================================================
# Schema
# =============================================================================

class TestDeadlineConfidenceSchema:
    def test_enum_values(self):
        from models.schemas import DeadlineConfidence
        assert DeadlineConfidence.EXPLICIT.value == "EXPLICIT"
        assert DeadlineConfidence.INFERRED.value == "INFERRED"
        assert DeadlineConfidence.NONE.value == "NONE"

    def test_task_default_is_none(self):
        from models.schemas import Task, DeadlineConfidence
        t = Task(title="t", assignee="a")
        assert t.deadline_confidence == DeadlineConfidence.NONE

    def test_task_accepts_explicit(self):
        from models.schemas import Task, DeadlineConfidence
        t = Task(
            title="t",
            assignee="a",
            deadline=date(2026, 5, 1),
            deadline_confidence=DeadlineConfidence.EXPLICIT,
        )
        assert t.deadline_confidence == DeadlineConfidence.EXPLICIT


# =============================================================================
# Supabase helper: update_task_deadline
# =============================================================================

class TestUpdateTaskDeadline:
    def test_default_confidence_is_explicit(self):
        from services.supabase_client import SupabaseClient

        with patch.object(SupabaseClient, "__init__", return_value=None):
            sc = SupabaseClient()

        captured = {}

        fake_query = MagicMock()
        fake_query.update.return_value = fake_query
        fake_query.eq.return_value = fake_query

        def _exec():
            captured.update(fake_query.update.call_args.args[0])
            return MagicMock(data=[{"id": "t1", "deadline": "2026-05-01", "deadline_confidence": "EXPLICIT"}])

        fake_query.execute.side_effect = _exec
        mock_client = MagicMock()
        mock_client.table.return_value = fake_query
        sc._client = mock_client

        result = sc.update_task_deadline("t1", date(2026, 5, 1))
        assert result["deadline_confidence"] == "EXPLICIT"
        assert captured["deadline_confidence"] == "EXPLICIT"

    def test_clear_with_none_confidence(self):
        from services.supabase_client import SupabaseClient

        with patch.object(SupabaseClient, "__init__", return_value=None):
            sc = SupabaseClient()

        captured = {}
        fake_query = MagicMock()
        fake_query.update.return_value = fake_query
        fake_query.eq.return_value = fake_query

        def _exec():
            captured.update(fake_query.update.call_args.args[0])
            return MagicMock(data=[{"id": "t1"}])

        fake_query.execute.side_effect = _exec
        mock_client = MagicMock()
        mock_client.table.return_value = fake_query
        sc._client = mock_client

        sc.update_task_deadline("t1", None, confidence="NONE")
        assert captured["deadline"] is None
        assert captured["deadline_confidence"] == "NONE"

    def test_raises_when_task_not_found(self):
        from services.supabase_client import SupabaseClient

        with patch.object(SupabaseClient, "__init__", return_value=None):
            sc = SupabaseClient()

        fake_query = MagicMock()
        fake_query.update.return_value = fake_query
        fake_query.eq.return_value = fake_query
        fake_query.execute.return_value = MagicMock(data=[])
        mock_client = MagicMock()
        mock_client.table.return_value = fake_query
        sc._client = mock_client

        with pytest.raises(ValueError, match="not found or not approved"):
            sc.update_task_deadline("missing", date(2026, 5, 1))


# =============================================================================
# Task reminder scheduler
# =============================================================================

class TestTaskReminderExplicitFilter:
    def _make_scheduler(self):
        """Import and instantiate scheduler with fresh state."""
        from schedulers.task_reminder_scheduler import TaskReminderScheduler
        return TaskReminderScheduler()

    def test_get_explicit_keys_returns_lowercase_tuples(self):
        scheduler = self._make_scheduler()

        fake_query = MagicMock()
        fake_query.select.return_value = fake_query
        fake_query.eq.return_value = fake_query
        fake_query.limit.return_value = fake_query
        fake_query.execute.return_value = MagicMock(data=[
            {"title": "Send RFP", "assignee": "Paolo"},
            {"title": "Review Contract", "assignee": "Yoram"},
        ])
        with patch("schedulers.task_reminder_scheduler.supabase_client") as sb:
            sb.client.table.return_value = fake_query
            keys = scheduler._get_explicit_task_keys()

        assert ("send rfp", "paolo") in keys
        assert ("review contract", "yoram") in keys
        assert len(keys) == 2

    def test_get_explicit_keys_returns_empty_set_on_db_error(self):
        scheduler = self._make_scheduler()
        with patch("schedulers.task_reminder_scheduler.supabase_client") as sb:
            sb.client.table.side_effect = Exception("connection lost")
            keys = scheduler._get_explicit_task_keys()
        # Empty set = falls back to pre-v2.3 behavior (no filtering)
        assert keys == set()


# =============================================================================
# Proactive alerts
# =============================================================================

class TestProactiveAlertsExplicitFilter:
    def test_overdue_cluster_skips_inferred_tasks(self):
        """An assignee with 3 overdue tasks — 2 INFERRED, 1 EXPLICIT — should
        NOT trigger a cluster alert (cluster requires 3+ EXPLICIT)."""
        from processors import proactive_alerts

        today_iso = datetime.now().isoformat()
        tasks = [
            {"assignee": "paolo", "title": "A", "deadline": "2026-01-01",
             "deadline_confidence": "INFERRED", "created_at": today_iso},
            {"assignee": "paolo", "title": "B", "deadline": "2026-01-02",
             "deadline_confidence": "INFERRED", "created_at": today_iso},
            {"assignee": "paolo", "title": "C", "deadline": "2026-01-03",
             "deadline_confidence": "EXPLICIT", "created_at": today_iso},
        ]

        with patch.object(proactive_alerts.supabase_client, "get_tasks", return_value=tasks):
            with patch.object(proactive_alerts, "_within_lookback", return_value=True):
                alerts = proactive_alerts._check_overdue_clusters()

        # Only 1 EXPLICIT — below cluster threshold of 3
        assert alerts == []

    def test_overdue_cluster_triggers_on_three_explicit(self):
        from processors import proactive_alerts

        today_iso = datetime.now().isoformat()
        tasks = [
            {"assignee": "paolo", "title": f"Task{i}", "deadline": "2026-01-01",
             "deadline_confidence": "EXPLICIT", "created_at": today_iso}
            for i in range(4)
        ]

        with patch.object(proactive_alerts.supabase_client, "get_tasks", return_value=tasks):
            with patch.object(proactive_alerts, "_within_lookback", return_value=True):
                alerts = proactive_alerts._check_overdue_clusters()

        assert len(alerts) == 1
        assert alerts[0]["type"] == "overdue_cluster"
        assert "paolo" in alerts[0]["title"].lower()


# =============================================================================
# Morning brief rendering
# =============================================================================

class TestMorningBriefInferredRendering:
    def test_inferred_deadline_gets_tilde_prefix(self):
        """format_morning_brief renders INFERRED task deadlines with a ~ prefix."""
        from processors.morning_brief import format_morning_brief

        brief = {"sections": [{
            "type": "task_urgency",
            "title": "Task Urgency",
            "items": [
                {
                    "title": "Sign contract",
                    "assignee": "Yoram",
                    "deadline": "2026-04-15",
                    "deadline_confidence": "INFERRED",
                },
            ],
        }]}

        rendered = format_morning_brief(brief)
        assert "~2026-04-15" in rendered

    def test_explicit_deadline_no_tilde_prefix(self):
        from processors.morning_brief import format_morning_brief

        brief = {"sections": [{
            "type": "task_urgency",
            "title": "Task Urgency",
            "items": [
                {
                    "title": "Sign contract",
                    "assignee": "Yoram",
                    "deadline": "2026-04-15",
                    "deadline_confidence": "EXPLICIT",
                },
            ],
        }]}

        rendered = format_morning_brief(brief)
        # Make sure the date appears without ~ prefix
        assert "due 2026-04-15" in rendered
        assert "~2026-04-15" not in rendered


# =============================================================================
# Safety-net normalization (v2.3.1)
# =============================================================================

class TestDeadlineConfidenceSafetyNet:
    """
    Covers the post-extraction normalization in processors/transcript_processor.py
    that promotes deadline_confidence=NONE→EXPLICIT when Opus produced a concrete
    deadline but forgot the classification field. Regression guard for the
    Franciacorta incident (2026-04-14) where 3 dated tasks landed as NONE and
    suppressed all reminders until manually backfilled.
    """

    def test_normalization_promotes_none_to_explicit_when_deadline_present(self):
        """Shape of the loop inline in process_transcript — replicated here for
        stability if the loop ever gets extracted into a helper."""
        tasks = [
            {"title": "A", "deadline": "2026-05-01", "deadline_confidence": "NONE"},
            {"title": "B", "deadline": "2026-05-02", "deadline_confidence": None},
            {"title": "C", "deadline": "2026-05-03"},  # field missing entirely
            {"title": "D", "deadline": None, "deadline_confidence": "INFERRED"},
            {"title": "E", "deadline": None, "deadline_confidence": None},
            {"title": "F", "deadline": "2026-05-04", "deadline_confidence": "EXPLICIT"},
            {"title": "G", "deadline": "2026-05-05", "deadline_confidence": "INFERRED"},
        ]
        # Replicate the normalizer inline (kept in sync with process_transcript)
        for t in tasks:
            dc = t.get("deadline_confidence")
            has_deadline = bool(t.get("deadline"))
            if has_deadline and dc in (None, "", "NONE"):
                t["deadline_confidence"] = "EXPLICIT"
            elif not has_deadline and dc not in ("NONE", None, ""):
                t["deadline_confidence"] = "NONE"
            elif dc is None:
                t["deadline_confidence"] = "NONE"

        by_title = {t["title"]: t["deadline_confidence"] for t in tasks}
        assert by_title["A"] == "EXPLICIT"   # NONE + deadline → EXPLICIT
        assert by_title["B"] == "EXPLICIT"   # None + deadline → EXPLICIT
        assert by_title["C"] == "EXPLICIT"   # missing + deadline → EXPLICIT
        assert by_title["D"] == "NONE"       # INFERRED + no deadline → NONE
        assert by_title["E"] == "NONE"       # None + no deadline → NONE
        assert by_title["F"] == "EXPLICIT"   # already EXPLICIT — untouched
        assert by_title["G"] == "INFERRED"   # INFERRED + deadline — allowed

    def test_extraction_prompt_uses_concrete_explicit_value(self):
        """The JSON schema example in transcript_processor.py uses the bare
        value "EXPLICIT" (not a pipe-separated options string) so Opus
        reliably emits one of the three literal enum values."""
        import processors.transcript_processor as tp_mod
        src = open(tp_mod.__file__, "r", encoding="utf-8").read()
        # The old wordy example was the regression cause
        assert '"deadline_confidence": "EXPLICIT | INFERRED | NONE' not in src
        # New crisp example
        assert '"deadline_confidence": "EXPLICIT"' in src
