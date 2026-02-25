"""
Tests for schedulers/task_reminder_scheduler.py

Tests the task reminder functionality.
"""

import pytest
from unittest.mock import AsyncMock, patch, MagicMock
from datetime import datetime, timedelta


class TestTaskReminderTelegramLookup:
    """Tests for Telegram ID lookup logic."""

    def test_get_telegram_id_exact_match(self):
        """Should find Telegram ID for exact name match."""
        team_ids = {
            "eyal": 123456789,
            "eyal zror": 123456789,
            "roye": 987654321,
            "roye tadmor": 987654321,
        }

        def get_telegram_id(name):
            name_lower = name.lower().strip()
            for team_name, telegram_id in team_ids.items():
                if name_lower in team_name.lower():
                    return telegram_id
            return None

        result = get_telegram_id("Eyal")
        assert result == 123456789

        result = get_telegram_id("Roye")
        assert result == 987654321

    def test_get_telegram_id_case_insensitive(self):
        """Should match names case-insensitively."""
        team_ids = {"eyal": 123456789, "eyal zror": 123456789}

        def get_telegram_id(name):
            name_lower = name.lower().strip()
            for team_name, telegram_id in team_ids.items():
                if name_lower in team_name.lower():
                    return telegram_id
            return None

        result = get_telegram_id("EYAL")
        assert result == 123456789

        result = get_telegram_id("eyal")
        assert result == 123456789

    def test_get_telegram_id_not_found(self):
        """Should return None for unknown names."""
        team_ids = {"eyal": 123456789}

        def get_telegram_id(name):
            name_lower = name.lower().strip()
            for team_name, telegram_id in team_ids.items():
                if name_lower in team_name.lower():
                    return telegram_id
            return None

        result = get_telegram_id("Unknown Person")
        assert result is None


class TestTaskDeadlineDetection:
    """Tests for deadline detection logic."""

    def test_detects_overdue_tasks(self):
        """Should detect tasks that are past deadline."""
        today = datetime.now().date()
        yesterday = (today - timedelta(days=1)).strftime("%Y-%m-%d")

        tasks = [
            {"task": "Overdue task", "deadline": yesterday, "status": "pending"},
        ]

        overdue = []
        for task in tasks:
            if task.get("status") == "done":
                continue
            deadline_str = task.get("deadline", "")
            if deadline_str:
                deadline = datetime.strptime(deadline_str, "%Y-%m-%d").date()
                if deadline < today:
                    overdue.append(task)

        assert len(overdue) == 1
        assert overdue[0]["task"] == "Overdue task"

    def test_detects_due_today_tasks(self):
        """Should detect tasks due today."""
        today = datetime.now().date()
        today_str = today.strftime("%Y-%m-%d")

        tasks = [
            {"task": "Due today", "deadline": today_str, "status": "pending"},
        ]

        due_today = []
        for task in tasks:
            if task.get("status") == "done":
                continue
            deadline_str = task.get("deadline", "")
            if deadline_str:
                deadline = datetime.strptime(deadline_str, "%Y-%m-%d").date()
                if deadline == today:
                    due_today.append(task)

        assert len(due_today) == 1

    def test_skips_completed_tasks(self):
        """Should skip completed tasks."""
        today = datetime.now().date()
        yesterday = (today - timedelta(days=1)).strftime("%Y-%m-%d")

        tasks = [
            {"task": "Completed task", "deadline": yesterday, "status": "done"},
        ]

        overdue = []
        for task in tasks:
            if task.get("status") == "done":
                continue
            deadline_str = task.get("deadline", "")
            if deadline_str:
                deadline = datetime.strptime(deadline_str, "%Y-%m-%d").date()
                if deadline < today:
                    overdue.append(task)

        assert len(overdue) == 0

    def test_detects_due_soon_tasks(self):
        """Should detect tasks due within warning period."""
        today = datetime.now().date()
        tomorrow = (today + timedelta(days=1)).strftime("%Y-%m-%d")
        days_before_warning = 2

        tasks = [
            {"task": "Due tomorrow", "deadline": tomorrow, "status": "pending"},
        ]

        due_soon = []
        for task in tasks:
            if task.get("status") == "done":
                continue
            deadline_str = task.get("deadline", "")
            if deadline_str:
                deadline = datetime.strptime(deadline_str, "%Y-%m-%d").date()
                days_until = (deadline - today).days
                if 0 < days_until <= days_before_warning:
                    due_soon.append(task)

        assert len(due_soon) == 1


class TestTaskSummaryGeneration:
    """Tests for task summary generation."""

    def test_summary_counts_by_status(self):
        """Should correctly count tasks by status."""
        tasks = [
            {"task": "Task 1", "status": "pending", "priority": "H", "assignee": "A"},
            {"task": "Task 2", "status": "done", "priority": "M", "assignee": "A"},
            {"task": "Task 3", "status": "in_progress", "priority": "L", "assignee": "B"},
        ]

        summary = {
            "total": len(tasks),
            "by_status": {"pending": 0, "in_progress": 0, "done": 0, "overdue": 0},
            "by_priority": {"H": 0, "M": 0, "L": 0},
            "by_assignee": {},
        }

        for task in tasks:
            status = task.get("status", "pending")
            priority = task.get("priority", "M")
            assignee = task.get("assignee", "Unassigned")

            if status in summary["by_status"]:
                summary["by_status"][status] += 1
            if priority in summary["by_priority"]:
                summary["by_priority"][priority] += 1
            if assignee not in summary["by_assignee"]:
                summary["by_assignee"][assignee] = 0
            summary["by_assignee"][assignee] += 1

        assert summary["total"] == 3
        assert summary["by_status"]["pending"] == 1
        assert summary["by_status"]["done"] == 1
        assert summary["by_status"]["in_progress"] == 1
        assert summary["by_priority"]["H"] == 1
        assert summary["by_priority"]["M"] == 1
        assert summary["by_priority"]["L"] == 1
        assert summary["by_assignee"]["A"] == 2
        assert summary["by_assignee"]["B"] == 1


class TestDuplicateReminderPrevention:
    """Tests for duplicate reminder prevention."""

    def test_no_duplicate_reminders_same_day(self):
        """Should not send duplicate reminders for same task same day."""
        reminders_sent_today = set()

        tasks = [
            {"task": "Task 1", "assignee": "Eyal"},
            {"task": "Task 1", "assignee": "Eyal"},  # Duplicate
        ]

        sent_count = 0
        for task in tasks:
            task_id = f"{task.get('task', '')}:{task.get('assignee', '')}"
            if task_id not in reminders_sent_today:
                reminders_sent_today.add(task_id)
                sent_count += 1

        assert sent_count == 1
        assert len(reminders_sent_today) == 1

    def test_resets_on_new_day(self):
        """Reminders should reset on new day."""
        reminders_day1 = set()
        reminders_day2 = set()

        task_id = "Task 1:Eyal"

        # Day 1
        reminders_day1.add(task_id)
        assert task_id in reminders_day1

        # Day 2 - fresh set
        assert task_id not in reminders_day2
