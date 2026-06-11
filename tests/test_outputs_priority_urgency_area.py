"""PR6 — flip push outputs to read the priority×urgency×category floor.

Since the 2026-06 category realignment, `tasks.category` carries the
Gantt-area taxonomy — outputs read `category` (fallback "General"); the
per-task area_label/area_id concept is gone.

Three surfaces, one flag (OUTPUTS_PRIORITY_URGENCY_AREA_ENABLED):
  - morning brief: urgency-first ranking + ASAP (undated H) class + category chip
  - task reminders: an urgency/category suffix on the copy (EXPLICIT-deadline
    gate is untouched — tested elsewhere; here we only assert the copy)
  - weekly digest: per-urgency + per-category rollups + Urgency table column

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
         "deadline": "2000-01-01", "category": "FUNDRAISING & INVESTOR RELATIONS"},
        {"title": "ASAP no date", "priority": "M", "urgency": "H",
         "deadline": None, "category": "PRODUCT & TECHNOLOGY"},
        {"title": "Important not urgent", "priority": "H", "urgency": "L",
         "deadline": None, "category": "TEAM & HUMAN RESOURCES"},
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
                             "deadline": "2000-01-01", "category": "x"}]
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
        # render key stays 'area' (it IS the chip) but the value is task.category
        assert items[0]["area"] == "PRODUCT & TECHNOLOGY"
        # the undated H task carries no deadline → renders ASAP, not a date
        assert items[0]["deadline"] in (None, "")


# ---------------------------------------------------------------------------
# task reminders — urgency/category suffix
# ---------------------------------------------------------------------------
class TestReminderSuffix:
    def test_off_is_empty(self):
        with patch.object(settings, "OUTPUTS_PRIORITY_URGENCY_AREA_ENABLED", False):
            assert _urgency_area_suffix(
                {"urgency": "H", "category": "PRODUCT & TECHNOLOGY"}) == ""

    def test_on_high_and_category(self):
        with patch.object(settings, "OUTPUTS_PRIORITY_URGENCY_AREA_ENABLED", True):
            s = _urgency_area_suffix(
                {"urgency": "H", "category": "PRODUCT & TECHNOLOGY"})
        assert s.startswith("\n")
        assert "Urgent" in s and "PRODUCT & TECHNOLOGY" in s

    def test_on_but_unremarkable_is_empty(self):
        # non-urgent, no real category → nothing added, reads like today's
        # reminder ("non-area" and "General" are both hidden)
        with patch.object(settings, "OUTPUTS_PRIORITY_URGENCY_AREA_ENABLED", True):
            assert _urgency_area_suffix({"urgency": "M", "category": "non-area"}) == ""
            assert _urgency_area_suffix({"urgency": "M", "category": "General"}) == ""

    def test_category_without_urgency(self):
        with patch.object(settings, "OUTPUTS_PRIORITY_URGENCY_AREA_ENABLED", True):
            s = _urgency_area_suffix(
                {"urgency": "L", "category": "SALES & BUSINESS DEVELOPMENT"})
        assert "SALES & BUSINESS DEVELOPMENT" in s


# ---------------------------------------------------------------------------
# weekly digest — get_task_summary rollups
# ---------------------------------------------------------------------------
class TestDigestRollups:
    def _mock_tasks(self, status):
        if status == "overdue":
            return [{"urgency": "H", "category": "PRODUCT & TECHNOLOGY"}]
        if status == "pending":
            return [
                {"urgency": "M", "category": "PRODUCT & TECHNOLOGY", "deadline": None},
                {"urgency": "L", "category": None, "deadline": None},  # → "General"
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
        # busiest category first; uncategorized rolls up under "General"
        assert list(res["by_area"].keys())[0] == "PRODUCT & TECHNOLOGY"
        assert res["by_area"]["PRODUCT & TECHNOLOGY"] == 2
        assert res["by_area"]["General"] == 1


# ---------------------------------------------------------------------------
# weekly digest — format_digest_document table + rollup block
# ---------------------------------------------------------------------------
class TestDigestDocument:
    def _doc(self, **extra):
        return wd.format_digest_document(
            week_of="2026-06-08",
            meetings=[], decisions=[], tasks_completed=[], tasks_overdue=[],
            tasks_upcoming=[{"title": "T", "assignee": "Roye", "deadline": "2026-06-12",
                             "priority": "H", "urgency": "H",
                             "category": "PRODUCT & TECHNOLOGY"}],
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
                task_area_rollup={"PRODUCT & TECHNOLOGY": 3, "General": 1},
            )
        assert "| Task | Category | Assignee | Deadline | Priority | Urgency |" in doc
        assert "Open by urgency" in doc
        assert "Open by category" in doc
        assert "PRODUCT & TECHNOLOGY: 3" in doc
