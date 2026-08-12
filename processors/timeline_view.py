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

THE GHOST LAYER answers "what did we plan in March?" on the same columns, as a
collapsed block below the live rows: the old board's own sections and lanes,
redrawn on the new grid. It is nearly free because the grid already spans
exactly what the old board spans, so no dates are recomputed. It attaches no
bar to any project — see `legacy_archive` for why that was tried and dropped.
Approved by Eyal 2026-08-12; gated on TIMELINE_LEGACY_OVERLAY_ENABLED because
the cost is screen density, and that is a judgement only he can make once he
sees it.
"""

import logging
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone

from config.settings import settings
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

# The legacy ghost. Deliberately OUTSIDE the status palette — a lilac that
# matches none of the four status colours, so the archive can never be misread
# as current truth at a glance.
GHOST_BG = "#D9CFE8"

# Lanes and sections that carry no plan. The Meetings lanes are attendance
# cadence, and OPERATIONAL RULES is the old board's own legend — neither is
# something anyone planned, and together they are 99 of the 244 bars.
_ARCHIVE_SKIP_LANES = ("Meeting Count", "All Meetings", "Availability",
                       "Meetings", "Management Meetings")
_ARCHIVE_SKIP_SECTIONS = ("OPERATIONAL RULES",)

HEADERS = ["Area / Project", "Owner", "Start", "Target", "Priority"]
N_LABEL_COLS = len(HEADERS)

# Hidden identity columns, written to the right of the week grid and hidden the
# same way the Project Status tabs hide theirs. Phase 4 reads this tab back, and
# a row must be identifiable without depending on WHERE it sits or WHAT it is
# called: rows move whenever a project is added to an area, and matching by name
# is the untrusted step this whole plan works around.
#
# `_kind` is stored rather than inferred from indentation, so the readback never
# has to guess whether "    Legal" is a project or a task whose title happens to
# be indented.
HIDDEN_HEADERS = ["_uid", "_kind"]
N_HIDDEN = len(HIDDEN_HEADERS)

ROW_PROJECT, ROW_TASK, ROW_AREA = "project", "task", "area"
ROW_ARCHIVE, ROW_CHROME = "archive", "chrome"

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


def legacy_archive(bars: list[dict], weeks: list[date]) -> list[dict]:
    """The old board's own rows, on the new grid. Sections -> lanes -> spans.

    NO PROJECT MATCHING. An earlier build of this drew each project's matched
    legacy bar directly beneath it, which is the more useful shape and is not
    supportable: measured on live data it drew 2 ghosts across 27 projects, and
    the matches it suppressed included obviously-correct ones ("Legal" ->
    "Legal entity Establishment") scoring identically to obvious junk
    ("Corporate" -> "Monthly close — send docs to Shimony"). Name overlap does
    not separate the two, and no threshold exists that admits one without the
    other.

    So the archive asserts nothing about which project a bar belongs to. It
    reproduces the old board's own structure — its sections, its lanes — on the
    new columns, and lets Eyal read the correspondence himself. That is the same
    instinct as the rest of this plan: show the evidence, never infer the link.
    """
    lanes: dict[tuple, list] = defaultdict(list)
    for b in bars:
        section = (b.get("section") or "").strip() or "(unsectioned)"
        lane = (b.get("lane") or "").strip() or "(unnamed)"
        if section in _ARCHIVE_SKIP_SECTIONS:
            continue
        if any(skip in lane for skip in _ARCHIVE_SKIP_LANES):
            continue
        start = _parse(b.get("start_date"))
        if start is None:
            continue
        # A bar with no recorded end is ONE week, never open-ended: these are
        # contiguous runs of filled cells, so a blank end means the run was one
        # cell long. Treating it as open would paint 90-odd weeks of lilac.
        end = _parse(b.get("end_date")) or start
        first, last, _ = span_columns(start, end, weeks)
        if first < 0:
            continue
        lanes[(section, lane)].append({
            "first_col": first, "last_col": last,
            "label": (b.get("label") or "").strip(), "start": start, "end": end})

    out: dict[str, list] = defaultdict(list)
    for (section, lane), spans in lanes.items():
        out[section].append({"lane": lane,
                             "bars": sorted(spans, key=lambda s: s["first_col"])})
    return [{"section": s, "lanes": sorted(out[s], key=lambda l: l["lane"])}
            for s in sorted(out)]


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

    bars = []
    if settings.TIMELINE_LEGACY_OVERLAY_ENABLED:
        try:
            bars = (c.table("gantt_legacy_bars").select("*")
                    .limit(1000).execute()).data or []
        except Exception as e:                               # noqa: BLE001
            # The archive missing is not a reason to drop the whole timeline.
            logger.warning(f"[timeline] legacy bars unavailable: {e}")

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

    archive = legacy_archive(bars, weeks)
    stats["archive_rows"] = sum(len(s["lanes"]) for s in archive)
    stats["archive_bars"] = sum(len(l["bars"]) for s in archive for l in s["lanes"])

    return {"weeks": weeks, "areas": dict(by_area), "archive": archive,
            "stats": stats}
