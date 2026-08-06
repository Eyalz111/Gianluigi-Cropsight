"""Weekly Projects Status pack — row assembly and the fire-once scheduler.

Built 2026-08-06 for the Paolo/Nechama area-by-area review. Grouped by AREA, not
canonical_projects, because no task->project link exists in the schema.
"""
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest


def _task(**kw):
    base = {
        "title": "Do the thing", "assignee": "Roye Tadmor", "deadline": "2026-08-30",
        "status": "pending", "category": "PRODUCT & TECHNOLOGY", "label": None,
        "urgency": "M", "meeting_id": "m1", "meetings": {"title": "Weekly", "date": "2026-08-01"},
    }
    base.update(kw)
    return base


class TestManualFlagsAreNotValues:
    """manual_<field> columns are BOOLEAN FLAGS ('a human edited this'), NOT
    override values — the value always stays in the plain column. Reading them as
    values rendered every cell as the literal 'False'."""

    def test_plain_value_is_used_even_when_flag_set(self):
        from processors.project_status import _effective
        t = _task(title="Real title", manual_title=True)
        assert _effective(t, "title") == "Real title"

    def test_false_flag_does_not_leak_into_the_cell(self):
        from processors.project_status import _effective
        t = _task(assignee="Paolo Vailetti", manual_assignee=False)
        assert _effective(t, "assignee") == "Paolo Vailetti"

    def test_hand_edit_is_surfaced_in_comments(self):
        from processors.project_status import _comment_for
        note = _comment_for(_task(manual_deadline=True, manual_assignee=True))
        assert "edited:" in note and "deadline" in note and "assignee" in note


class TestSubjectFallback:
    def test_label_wins(self):
        from processors.project_status import _subject_for
        assert _subject_for(_task(label="AWS Credits")) == "AWS Credits"

    def test_untagged_shows_a_dash_not_the_meeting_title(self):
        """The meeting already appears in Comments; repeating it as Subject makes
        the column useless. A dash makes untagged work visibly untagged."""
        from processors.project_status import _subject_for
        assert _subject_for(_task(title="Some unmatched work", label=None)) == "—"


class TestSortOrder:
    def test_overdue_first_then_in_progress_then_pending(self):
        from processors.project_status import _sort_key
        rows = [_task(status="pending"), _task(status="overdue"), _task(status="in_progress")]
        assert [t["status"] for t in sorted(rows, key=_sort_key)] == \
            ["overdue", "in_progress", "pending"]

    def test_undated_sorts_last_within_its_band(self):
        """An empty date string would otherwise win an ascending sort and push
        genuinely dated work below it."""
        from processors.project_status import _sort_key
        rows = [_task(status="pending", deadline=None), _task(status="pending", deadline="2026-09-01")]
        assert sorted(rows, key=_sort_key)[0]["deadline"] == "2026-09-01"


class TestTabNames:
    def test_forbidden_sheets_characters_stripped(self):
        from processors.project_status import tab_name_for
        assert ":" not in tab_name_for("LEGAL: CORPORATE/FINANCE")
        assert "/" not in tab_name_for("LEGAL: CORPORATE/FINANCE")

    def test_blank_falls_back_to_general(self):
        from processors.project_status import tab_name_for
        assert tab_name_for("") == "General"


class TestSchedulerFireOnce:
    def test_week_key_is_stable_within_an_iso_week(self):
        from schedulers.project_status_scheduler import ProjectStatusScheduler as S
        assert S._week_key(datetime(2026, 8, 3)) == S._week_key(datetime(2026, 8, 6))

    def test_week_key_changes_across_weeks(self):
        from schedulers.project_status_scheduler import ProjectStatusScheduler as S
        assert S._week_key(datetime(2026, 8, 6)) != S._week_key(datetime(2026, 8, 13))

    async def test_failed_refresh_does_not_consume_the_week(self, monkeypatch):
        """A failed refresh must retry on the next tick, not silently skip the
        review for a whole week."""
        from schedulers import project_status_scheduler as mod
        s = mod.ProjectStatusScheduler()
        monkeypatch.setattr(mod.settings, "PROJECT_STATUS_DAY", datetime.now(mod._ISRAEL_TZ).weekday())
        monkeypatch.setattr(mod.settings, "PROJECT_STATUS_HOUR", 0)
        monkeypatch.setattr(mod.settings, "PROJECT_STATUS_WINDOW_HOURS", 24)
        monkeypatch.setattr(s, "refresh", AsyncMock(return_value={"error": "sheets down"}))
        assert await s._check_and_refresh() is False
        assert s._sent_weeks == set()

    async def test_successful_refresh_marks_the_week(self, monkeypatch):
        from schedulers import project_status_scheduler as mod
        s = mod.ProjectStatusScheduler()
        monkeypatch.setattr(mod.settings, "PROJECT_STATUS_DAY", datetime.now(mod._ISRAEL_TZ).weekday())
        monkeypatch.setattr(mod.settings, "PROJECT_STATUS_HOUR", 0)
        monkeypatch.setattr(mod.settings, "PROJECT_STATUS_WINDOW_HOURS", 24)
        monkeypatch.setattr(s, "refresh", AsyncMock(return_value={"error": None, "rows": 12, "tabs": ["a"]}))
        monkeypatch.setattr(mod.supabase_client, "log_action", lambda **kw: None)
        assert await s._check_and_refresh() is True
        assert len(s._sent_weeks) == 1
        # second tick inside the same window must not re-fire
        assert await s._check_and_refresh() is False

    async def test_outside_the_window_does_nothing(self, monkeypatch):
        from schedulers import project_status_scheduler as mod
        s = mod.ProjectStatusScheduler()
        wrong_day = (datetime.now(mod._ISRAEL_TZ).weekday() + 3) % 7
        monkeypatch.setattr(mod.settings, "PROJECT_STATUS_DAY", wrong_day)
        monkeypatch.setattr(s, "refresh", AsyncMock())
        assert await s._check_and_refresh() is False
        s.refresh.assert_not_awaited()
