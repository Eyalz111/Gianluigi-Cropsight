"""The Timeline — one dated bar per project, on a weekly grid.

Phase 2 of docs/GANTT_V2_PLAN.md. Pure data: this module decides WHAT the
timeline contains and performs no sheet I/O, so the shape can be tested without
touching Google.

THE GRID is 96 weekly columns, Monday 2026-03-02 through 2027-12-27 — Eyal's
choice of weeks over months, and end-2027 over end-2028 to keep the width
workable. It is also, not by design, exactly the span the old board covers, so
the archived legacy bars land on the same columns.

BARS END OPEN when a project has no target_date — 11 of 23 have none today. The
alternative, deriving the end from the latest open task deadline, moves the
right edge every time a task is added or re-dated, so the bar would appear to
change plan when nothing was decided. An open end is honest about not knowing;
a derived end is a guess wearing a date.

COLOUR CARRIES STATUS, NOT PRIORITY. The two are different questions and one
cell cannot answer both without becoming unreadable, so the bar takes the old
board's own status palette and priority keeps its own column with the
Urgent/H/M/L colours already used on the Project Status tabs.
"""

import logging
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone

from services.supabase_client import supabase_client

logger = logging.getLogger(__name__)

TIMELINE_TAB = "Timeline"

GRID_START = date(2026, 3, 2)          # Monday, matching the old board's W9
GRID_END = date(2027, 12, 27)
ISRAEL_TZ = timezone(timedelta(hours=3))

# Lifted from the old board's own legend so the two read alike. Eyal asked for
# retired projects to use the Completed colour "as we had in the gantt".
STATUS_BG = {
    "blocked": "#E85050",
    "active": "#B7D7B0",
    "planned": "#CCE0F0",
    "completed": "#D0D0CC",
}
# An open-ended run past today is drawn in a paler active tint: it says "still
# going, end unknown" rather than claiming a finish at the edge of the grid.
OPEN_END_BG = "#E4F0E1"

HEADERS = ["Area / Project", "Owner", "Start", "Target", "Priority"]
N_LABEL_COLS = len(HEADERS)

_OPEN_TASK = ("pending", "in_progress", "overdue")
_CLOSED_TASK = ("done", "archived", "cancelled", "superseded")


def week_starts(first: date = GRID_START, last: date = GRID_END) -> list[date]:
    """Every Monday on the grid, inclusive."""
    out, cur = [], first
    while cur <= last:
        out.append(cur)
        cur += timedelta(days=7)
    return out


def _parse(value) -> "date | None":
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value)[:10]).date()
    except (ValueError, TypeError):
        return None


def span_columns(start: date | None, end: date | None,
                 weeks: list[date]) -> tuple[int, int, bool]:
    """(first_col, last_col, is_open_ended) as indexes into `weeks`.

    A bar occupies the week its start falls IN, not the week after — a project
    that began on a Wednesday started that week. Same for the end. Returns
    (-1, -1, False) when the span misses the grid entirely.
    """
    if start is None:
        return -1, -1, False
    if start > weeks[-1] + timedelta(days=6):
        return -1, -1, False

    def col_of(d: date) -> int:
        if d <= weeks[0]:
            return 0
        for i in range(len(weeks) - 1, -1, -1):
            if weeks[i] <= d:
                return i
        return 0

    first = col_of(start)
    if end is None:
        return first, len(weeks) - 1, True
    if end < weeks[0]:
        return -1, -1, False
    return first, col_of(end), False


def _status_of(project: dict, open_tasks: int, is_retired: bool) -> str:
    """Which legend colour this project's bar takes."""
    if is_retired:
        return "completed"
    if open_tasks:
        return "active"
    # No open work and not retired: it is on the board but nothing is moving.
    return "planned"


def build_timeline() -> dict:
    """Areas -> ordered project rows, each with its span and colour.

    Retired projects are INCLUDED, greyed, because Eyal asked to keep seeing
    them: a timeline that silently drops finished work cannot answer "what did
    we do this year".
    """
    c = supabase_client.client
    weeks = week_starts()

    areas = {a["id"]: a["name"] for a in
             (c.table("areas").select("id,name").order("name").execute()).data or []}
    projects = (c.table("canonical_projects").select("*")
                .order("name").execute()).data or []
    tasks = (c.table("tasks").select(
        "id,project_id,title,assignee,deadline,status,priority,approval_status")
        .limit(5000).execute()).data or []

    open_by_project = defaultdict(list)
    for t in tasks:
        if (t.get("status") or "").lower() in _CLOSED_TASK:
            continue
        if t.get("approval_status") not in (None, "approved"):
            continue
        if t.get("project_id"):
            open_by_project[t["project_id"]].append(t)

    by_area: dict[str, list] = defaultdict(list)
    stats = {"projects": 0, "retired": 0, "no_start": 0, "open_ended": 0}

    for p in projects:
        retired = (p.get("status") or "active") == "retired"
        start = _parse(p.get("start_date"))
        target = _parse(p.get("target_date"))
        open_tasks = open_by_project.get(p["id"], [])
        first, last, open_ended = span_columns(start, target, weeks)

        stats["projects"] += 1
        if retired:
            stats["retired"] += 1
        if start is None:
            stats["no_start"] += 1
        if open_ended:
            stats["open_ended"] += 1

        by_area[areas.get(p.get("area_id"), "(no area)")].append({
            "project_id": p["id"],
            "name": p["name"],
            "owner": p.get("owner") or "",
            "start": start,
            "target": target,
            "first_col": first,
            "last_col": last,
            "open_ended": open_ended,
            "status": _status_of(p, len(open_tasks), retired),
            "retired": retired,
            "tasks": sorted(
                ({"title": t.get("title") or "",
                  "assignee": t.get("assignee") or "",
                  "deadline": _parse(t.get("deadline")),
                  "priority": (t.get("priority") or "M")}
                 for t in open_tasks),
                key=lambda t: (t["deadline"] is None, t["deadline"] or date.max)),
        })

    return {"weeks": weeks, "areas": dict(by_area), "stats": stats}
