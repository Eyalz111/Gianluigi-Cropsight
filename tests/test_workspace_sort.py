"""
Daily default order for the Tasks + Meetings tabs. [2026-07-23]

Eyal's call: Tasks are grouped by Area (primary), and within an area ordered
active-before-done, then by combined priority+urgency with overdue boosted.
Meetings: to-schedule first, then by proposed date. The reorder is in place (no
clear, no DB read) so it can never wipe.
"""

import pytest

from services.google_sheets import _task_sort_key


TODAY = "2026-07-23"

# The full prod A:L layout (urgency+last_update flags are on in prod; the
# module-level TASK_COL_INDEX omits them when the flags default off in tests).
COL_INDEX = {
    "priority": 0, "label": 1, "task": 2, "owner": 3, "deadline": 4,
    "status": 5, "category": 6, "source_meeting": 7, "created": 8, "id": 9,
    "urgency": 10, "last_update": 11,
}


def _row(**kw):
    """Build a Tasks raw row (A:L) from named fields."""
    r = [""] * (max(COL_INDEX.values()) + 1)
    for k, v in kw.items():
        r[COL_INDEX[k]] = v
    return r


def _order(rows):
    return [r for r in sorted(rows, key=lambda x: _task_sort_key(x, COL_INDEX, TODAY))]


class TestTaskSort:
    def test_area_is_primary(self):
        a = _row(task="a", category="PRODUCT & TECHNOLOGY", priority="L", urgency="L")
        b = _row(task="b", category="FUNDRAISING & INVESTOR RELATIONS", priority="H", urgency="H")
        # Fundraising < Product alphabetically, so b's area comes first even
        # though a would outrank it on priority — area is PRIMARY.
        assert [r[COL_INDEX["task"]] for r in _order([a, b])] == ["b", "a"]

    def test_within_area_priority_urgency_orders(self):
        hi = _row(task="hi", category="PRODUCT & TECHNOLOGY", priority="H", urgency="H")
        lo = _row(task="lo", category="PRODUCT & TECHNOLOGY", priority="L", urgency="L")
        mid = _row(task="mid", category="PRODUCT & TECHNOLOGY", priority="M", urgency="M")
        assert [r[COL_INDEX["task"]] for r in _order([lo, hi, mid])] == ["hi", "mid", "lo"]

    def test_overdue_is_boosted_within_area(self):
        normal_high = _row(task="nh", category="SALES & BUSINESS DEVELOPMENT",
                           priority="H", urgency="M", status="pending")
        overdue_mid = _row(task="od", category="SALES & BUSINESS DEVELOPMENT",
                           priority="M", urgency="M", status="pending",
                           deadline="2026-07-01")  # past
        # overdue adds +3, lifting the M/M overdue above the H/M non-overdue.
        assert [r[COL_INDEX["task"]] for r in _order([normal_high, overdue_mid])] == ["od", "nh"]

    def test_done_sinks_within_its_area(self):
        active = _row(task="act", category="TEAM & HUMAN RESOURCES",
                      priority="L", urgency="L", status="pending")
        done = _row(task="dn", category="TEAM & HUMAN RESOURCES",
                    priority="H", urgency="H", status="done")
        # done goes below active in the same area, regardless of priority.
        assert [r[COL_INDEX["task"]] for r in _order([done, active])] == ["act", "dn"]

    def test_general_sinks_to_the_bottom(self):
        real = _row(task="real", category="PRODUCT & TECHNOLOGY", priority="L", urgency="L")
        gen = _row(task="gen", category="General", priority="H", urgency="H")
        blank = _row(task="blank", category="", priority="H", urgency="H")
        out = [r[COL_INDEX["task"]] for r in _order([gen, real, blank])]
        assert out[0] == "real", "a real area outranks General even at lower priority"
        assert set(out[1:]) == {"gen", "blank"}, "General and blank sink to the bottom"

    def test_overdue_ignored_for_done_tasks(self):
        # a done task with a past deadline must NOT get the overdue boost.
        done_past = _row(task="dp", category="X", priority="L", urgency="L",
                         status="done", deadline="2026-01-01")
        active = _row(task="ac", category="X", priority="L", urgency="L", status="pending")
        assert [r[COL_INDEX["task"]] for r in _order([done_past, active])] == ["ac", "dp"]
