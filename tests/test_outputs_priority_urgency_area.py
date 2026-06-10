"""PR6 — flip push outputs to read the priority×urgency×area floor.

Three surfaces, one flag (OUTPUTS_PRIORITY_URGENCY_AREA_ENABLED):
  - morning brief: urgency-first ranking + ASAP (undated H) class + area chip
  - task reminders: an urgency/area suffix on the copy (EXPLICIT-deadline gate
    is untouched — tested elsewhere; here we only assert the copy)
  - weekly digest: per-urgency + per-area rollups + Urgency/Area table columns

Every assertion pairs a flag-OFF (legacy, byte-for-byte) case with a flag-ON
case. The no-invented-dates guardrail shows up as: an undated urgency=H task
renders "ASAP", never a fabricated date.
"""
from unittest.mock import patch

import pytest

from config.settings import settings
from processors import morning_brief as mb
from processors import weekly_digest as wd
from schedulers.task_reminder_scheduler import _urgency_area_suffix


# ---------------------------------------------------------------------------
# morning brief — the _task_urgency_line render helper (pure)
# ---------------------------------------------------------------------------
class TestTaskUrgencyLine:
    def test_undated_high_says_asap_not_a_fake_date(self):
        rank, line = mb._task_urgency_line(
            {"title": "Ship pilot", "assignee": "Roye", "deadline": "",
             "urgency": "H", "area": "Product & Tech"},
            esc=lambda s: s,
        )
        assert rank == 0                 # H floats to the top
        assert "ASAP" in line
        assert "due" not in line         # no invented date
        assert "Product & Tech" in line  # area chip
        assert "🔴" in line

    def test_dated_task_shows_date(self):
        _, line = mb._task_urgency_line(
            {"title": "Send deck", "assignee": "Eyal", "deadline": "2026-06-20",
             "urgency": "M", "area": "BD & Sales", "deadline_confidence": "EXPLICIT"},
            esc=lambda s: s,
        )
        assert "due 2026-06-20" in line
        assert "🟡" in line

    def test_inferred_date_gets_tilde(self):
        _, line = mb._task_urgency_line(
            {"title": "x", "deadline": "2026-07-01", "urgency": "M",
             "area": "non-area", "deadline_confidence": "INFERRED"},
            esc=lambda s: s,
        )
        assert "due ~2026-07-01" in line

    def test_non_area_chip_omitted(self):
        _, line = mb._task_urgency_line(
            {"title": "x", "deadline": "", "urgency": "L", "area": "non-area"},
            esc=lambda s: s,
        )
        assert "·" not in line           # no area chip for non-area
        assert "no date set" in line     # undated, not urgent → neutral copy


# ---------------------------------------------------------------------------
# morning brief — render branch picks the new shape only when 'urgency' present
# ---------------------------------------------------------------------------
class TestBriefRender:
    def _brief(self, item):
        return {"sections": [
            {"type": "task_urgency", "title": "Task Urgency", "items": [item]}
        ]}

    def test_legacy_item_renders_old_line(self):
        # flag-off item shape has no 'urgency' key → legacy "due ?" rendering
        out = mb.format_morning_brief(self._brief(
            {"title": "Legacy task", "assignee": "Roye", "deadline": "2026-06-15"}
        ))
        assert "Legacy task" in out
        assert "due 2026-06-15" in out
        assert "ASAP" not in out

    def test_floor_item_renders_asap(self):
        out = mb.format_morning_brief(self._brief(
            {"title": "Urgent task", "assignee": "Roye", "deadline": "",
             "urgency": "H", "area": "Operations & HR"}
        ))
        assert "Urgent task" in out
        assert "ASAP" in out
        assert "Operations & HR" in out


# ---------------------------------------------------------------------------
# morning brief — gather: surfaces undated H, ranks urgency-first, flag-gated
# ---------------------------------------------------------------------------
def _tasks():
    return [
        {"title": "Overdue low", "priority": "L", "urgency": "L",
         "deadline": "2000-01-01", "area_label": "Finance & Fundraising"},
        {"title": "ASAP no date", "priority": "M", "urgency": "H",
         "deadline": None, "area_label": "Product & Tech"},
        {"title": "Important not urgent", "priority": "H", "urgency": "L",
         "deadline": None, "area_label": "Strategy & Research"},
    ]


class TestBriefGather:
    def test_flag_off_legacy_overdue_high_only(self):
        # legacy: only overdue ∧ priority=H. _tasks() has no such task → empty,
        # and the items carry no 'urgency' key (legacy render shape).
        with patch.object(settings, "OUTPUTS_PRIORITY_URGENCY_AREA_ENABLED", False):
            items = mb._gather_task_urgency_items(_tasks(), "2026-06-10")
        assert items == []

    def test_flag_off_surfaces_overdue_high(self):
        tasks = _tasks() + [{"title": "Overdue high", "priority": "H", "urgency": "L",
                             "deadline": "2000-01-01", "area_label": "x"}]
        with patch.object(settings, "OUTPUTS_PRIORITY_URGENCY_AREA_ENABLED", False):
            items = mb._gather_task_urgency_items(tasks, "2026-06-10")
        assert len(items) == 1
        assert items[0]["title"] == "Overdue high"
        assert "urgency" not in items[0]  # legacy render shape

    def test_flag_on_surfaces_asap_first(self):
        with patch.object(settings, "OUTPUTS_PRIORITY_URGENCY_AREA_ENABLED", True):
            items = mb._gather_task_urgency_items(_tasks(), "2026-06-10")
        assert items, "floor flag should surface tasks"
        # urgency-first: the undated H (ASAP) task ranks ahead of overdue-L
        assert items[0]["title"] == "ASAP no date"
        assert items[0]["urgency"] == "H"
        assert items[0]["area"] == "Product & Tech"
        # the undated H task carries no deadline → renders ASAP, not a date
        assert items[0]["deadline"] in (None, "")


# ---------------------------------------------------------------------------
# task reminders — urgency/area suffix
# ---------------------------------------------------------------------------
class TestReminderSuffix:
    def test_off_is_empty(self):
        with patch.object(settings, "OUTPUTS_PRIORITY_URGENCY_AREA_ENABLED", False):
            assert _urgency_area_suffix({"urgency": "H", "area": "Product & Tech"}) == ""

    def test_on_high_and_area(self):
        with patch.object(settings, "OUTPUTS_PRIORITY_URGENCY_AREA_ENABLED", True):
            s = _urgency_area_suffix({"urgency": "H", "area": "Product & Tech"})
        assert s.startswith("\n")
        assert "Urgent" in s and "Product & Tech" in s

    def test_on_but_unremarkable_is_empty(self):
        # non-urgent, non-area → nothing added, reads like today's reminder
        with patch.object(settings, "OUTPUTS_PRIORITY_URGENCY_AREA_ENABLED", True):
            assert _urgency_area_suffix({"urgency": "M", "area": "non-area"}) == ""

    def test_area_label_fallback(self):
        with patch.object(settings, "OUTPUTS_PRIORITY_URGENCY_AREA_ENABLED", True):
            s = _urgency_area_suffix({"urgency": "L", "area_label": "BD & Sales"})
        assert "BD & Sales" in s


# ---------------------------------------------------------------------------
# weekly digest — get_task_summary rollups
# ---------------------------------------------------------------------------
class TestDigestRollups:
    def _mock_tasks(self, status):
        if status == "overdue":
            return [{"urgency": "H", "area_label": "Product & Tech"}]
        if status == "pending":
            return [
                {"urgency": "M", "area_label": "Product & Tech", "deadline": None},
                {"urgency": "L", "area_label": "non-area", "deadline": None},
            ]
        return []

    @patch.object(wd, "supabase_client")
    async def test_flag_off_no_rollup_keys(self, mock_sb):
        mock_sb.get_tasks.side_effect = lambda **k: self._mock_tasks(k.get("status"))
        with patch.object(settings, "OUTPUTS_PRIORITY_URGENCY_AREA_ENABLED", False):
            res = await wd.get_task_summary()
        assert "by_area" not in res
        assert "by_urgency" not in res

    @patch.object(wd, "supabase_client")
    async def test_flag_on_rollups_present(self, mock_sb):
        mock_sb.get_tasks.side_effect = lambda **k: self._mock_tasks(k.get("status"))
        with patch.object(settings, "OUTPUTS_PRIORITY_URGENCY_AREA_ENABLED", True):
            res = await wd.get_task_summary()
        # overdue(1 H) + pending(1 M, 1 L)
        assert res["by_urgency"] == {"H": 1, "M": 1, "L": 1}
        # busiest area first
        assert list(res["by_area"].keys())[0] == "Product & Tech"
        assert res["by_area"]["Product & Tech"] == 2
        assert res["by_area"]["non-area"] == 1


# ---------------------------------------------------------------------------
# weekly digest — format_digest_document table + rollup block
# ---------------------------------------------------------------------------
class TestDigestDocument:
    def _doc(self, **extra):
        return wd.format_digest_document(
            week_of="2026-06-08",
            meetings=[], decisions=[], tasks_completed=[], tasks_overdue=[],
            tasks_upcoming=[{"title": "T", "assignee": "Roye", "deadline": "2026-06-12",
                             "priority": "H", "urgency": "H", "category": "Product & Tech",
                             "area_label": "Product & Tech"}],
            open_questions=[], upcoming_meetings=[], **extra,
        )

    def test_off_legacy_table(self):
        with patch.object(settings, "OUTPUTS_PRIORITY_URGENCY_AREA_ENABLED", False):
            doc = self._doc()
        assert "| Task | Category | Assignee | Deadline | Priority |" in doc
        assert "Urgency" not in doc.split("Due Next Week")[1]
        assert "Open by urgency" not in doc

    def test_on_adds_columns_and_rollup(self):
        with patch.object(settings, "OUTPUTS_PRIORITY_URGENCY_AREA_ENABLED", True):
            doc = self._doc(
                task_urgency_rollup={"H": 2, "M": 1, "L": 0},
                task_area_rollup={"Product & Tech": 3, "non-area": 1},
            )
        assert "| Task | Area | Assignee | Deadline | Priority | Urgency |" in doc
        assert "Open by urgency" in doc
        assert "Open by area" in doc
        assert "Product & Tech: 3" in doc
