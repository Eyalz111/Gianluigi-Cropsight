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

THE BOARD MUST NOT GROW WITH FINISHED WORK — decision 8, after Eyal reviewed the
live tab: *"once we will add more and more projects, it will be too big… in the
previous format we just had the certain weeks that the project is there, and
then once it finished the timeline keeps on going and we kind of get rid of
it."* The old board never grew because its rows were LANES and work flowed
through them; project-per-row makes every project a permanent row, and closing
one ADDS to the pile instead of removing from it.

Option A: projects stay the rows, and finished ones fold into a single collapsed
line per area, so visible height tracks OPEN work rather than cumulative work.
Measured against the live data — 33 rows today and 66 in eighteen months as
Phase 2 was built, against 35 and 42 folded. A month's grace before a project
leaves the main list, the fold collapsed by default, and completed bars keep
their historical span, so "what did we ship this year" is one click away rather
than gone.

STATUS IS DECLARED, NOT COUNTED — decision 9, and the fold cannot exist without
it. `_status_of` used to return 'active' when a project had open tasks and
'planned' when it had none, so a finished project rendered in the same blue as
one that had never started. Counting would also fold a project the moment its
last task closed, which on this team happens constantly: the summary arrives
before anyone decides the next action.

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

# THE DECLARED VOCABULARY (decision 9). A human states which of these a project
# is; nothing infers it. `planned` is deliberately absent: a project that has not
# started is visible from its start_date being in the future, which is a fact
# about a date rather than a claim about intent — and it was the wrong home for
# "finished", which is the whole defect.
PROJECT_STATUSES = ("active", "blocked", "done", "retired")
DEFAULT_PROJECT_STATUS = "active"

# The two that leave the main list. Kept, greyed, and readable inside the fold —
# a timeline that silently drops finished work cannot answer "what did we do
# this year".
FOLD_STATUSES = frozenset({"done", "retired"})

# Lifted from the old board's own legend so the two read alike. Eyal asked for
# retired projects to use the Completed colour "as we had in the gantt".
#
# `planned` survives here as a RENDER state, never a stored one: an active
# project whose start is still in the future. Derived from the date, so it can
# never absorb "finished" the way the stored value did.
STATUS_BG = {
    "blocked": "#E85050",
    "active": "#B7D7B0",
    "planned": "#CCE0F0",
    "done": "#C6CFC4",
    "retired": "#D0D0CC",
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

# Column 4 was "Priority" and rendered the word "retired" into it — the header
# and the content had already disagreed. It is now Status, a dropdown of the four
# declared values, and it is the field's ONLY editable home: nothing else in the
# workspace sets a project's status today, so making the Timeline its writer adds
# no second writer to anything. Eyal declares it where he can see the fold
# happen, which is the same place the consequence shows up.
HEADERS = ["Area / Project", "Owner", "Start", "Target", "Status", "Priority"]
N_LABEL_COLS = len(HEADERS)

# Priority is APPENDED, never inserted before Status. The readback maps sheet
# columns to fields by index, so slotting a column in ahead of an editable one
# would silently re-point every edit onto the wrong field — the exact failure
# the layout gate exists to catch, introduced by our own hand.
#
# It is DERIVED — the most urgent open task on the project — not declared.
# Decision 9 insisted status be declared because the FOLD keys on it, and
# inferring "finished" from an empty task list would hide live work. Priority
# carries no structural consequence: nothing folds, moves or changes colour
# because of it, so a value that tracks the tasks is honest and is always
# current. Eyal changed a task's priority on an area tab and expected to see it
# here, which is exactly what a derived column gives him. [2026-08-13]
_PRIORITY_RANK = {"U": 0, "URGENT": 0, "H": 1, "M": 2, "L": 3}
_PRIORITY_LABEL = {"U": "Urgent", "H": "H", "M": "M", "L": "L"}


def project_priority(tasks: list[dict]) -> str:
    """The most urgent open task's priority, or "" when there are none.

    A blank means "no open work to rank", which is a different statement from
    "low priority" and must not render as one. `M` is NOT assumed: an untouched
    default is not a decision, and stamping it across the board is what told
    Eyal his whole meeting pool had been triaged when none of it had.
    """
    best, label = None, ""
    for t in tasks:
        raw = str(t.get("priority") or "").strip().upper()
        rank = _PRIORITY_RANK.get(raw)
        if rank is None:
            continue
        if best is None or rank < best:
            best, label = rank, _PRIORITY_LABEL.get(raw[0], raw)
    return label

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
# The fold's own header line, one per area. Its own kind so the readback can
# refuse it explicitly rather than by failing to recognise it — a folded project
# row underneath is still ROW_PROJECT and still editable, which is the point:
# reopening something is done by changing its Status, and that has to be
# reachable without hunting for another surface.
ROW_FOLD = "fold"
# One meetings lane per area, and the milestone band. Read-only chrome as far as
# the readback is concerned; the CEO tab stays the milestones' editable home.
ROW_MEETINGS, ROW_MILESTONE = "meetings", "milestone"

_OPEN_TASK = ("pending", "in_progress", "overdue")
_CLOSED_TASK = ("done", "archived", "cancelled", "superseded")


def week_starts(first: date = GRID_START, last: date = GRID_END) -> list[date]:
    """Every Monday on the grid, inclusive."""
    out, cur = [], first
    while cur <= last:
        out.append(cur)
        cur += timedelta(days=7)
    return out


def _today() -> date:
    """Today in Jerusalem, as one named seam.

    A function rather than an inline `datetime.now(...)` so a test can pin it.
    Two of this repo's standing test failures are day-of-week flakes in
    `test_gantt_drift`, and the fold's grace month plus the planned/started
    boundary are both exactly that shape.
    """
    return datetime.now(ISRAEL_TZ).date()


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


def declared_status(project: dict) -> str:
    """The project's own statement of where it is. Never inferred.

    Anything unrecognised falls back to `active` rather than to a fold state:
    the cost of showing a finished project for another day is a row, and the cost
    of folding a live one is that it disappears from the board people work from.
    """
    s = str(project.get("status") or "").strip().lower()
    return s if s in PROJECT_STATUSES else DEFAULT_PROJECT_STATUS


def _status_of(project: dict, start: "date | None", today: date) -> str:
    """Which legend colour this project's bar takes.

    Declared first. `planned` is the one derived reading and it is derived from a
    DATE, not from a task count: an active project whose start has not arrived
    yet has not started. That is observable. "No open tasks means planned" was
    not — it silently claimed that finished work had never begun.
    """
    status = declared_status(project)
    if status == "active" and start is not None and start > today:
        return "planned"
    return status


def _grace_days() -> int:
    return int(getattr(settings, "PROJECT_FOLD_GRACE_DAYS", 30) or 30)


def is_folded(project: dict, today: date) -> bool:
    """Has this project left the main list?

    A MONTH'S GRACE FOR `done`, none for `retired`. Eyal asked that a project
    leave the main list a month after it is marked done, so recent wins are still
    visible where the work happens. Retired is not a win — it is abandoned or
    superseded — so there is nothing to leave visible and it folds at once.

    The grace is measured from `done_at`, stamped by a trigger on the transition
    into done (scripts/migrate_project_declared_status.sql). A MISSING done_at
    keeps the project in the main list: before that migration runs every row
    reads None, and the safe reading of "nobody knows when this finished" is to
    leave it where people can see it. Treating an absent timestamp as infinitely
    old would fold every finished project the first time this ran.
    """
    status = declared_status(project)
    if status not in FOLD_STATUSES:
        return False
    if status == "retired":
        return True
    done_at = _parse(project.get("done_at"))
    if done_at is None:
        return False
    return done_at <= today - timedelta(days=_grace_days())


def nearest_action(tasks: list[dict]) -> str:
    """The project's nearest concrete step, for printing on the bar.

    Decision 8: *bars carry text*, so a row says what is happening rather than
    only that something is. `tasks` arrives already sorted by deadline with the
    undated ones last, so the first entry IS the nearest — an undated task is
    still the nearest concrete step when it is the only one, and printing it
    beats printing nothing.
    """
    return (tasks[0]["title"] if tasks else "")


# Which meetings a lane draws. `recurring` is a CADENCE — permanently live,
# never reaching a terminal state — so it draws as a continuous band rather than
# as events. `scheduled` and `held` are events that are booked or happened, so
# they are single markers. `parked`, `to_schedule` and `dropped` draw nothing:
# they have no agreed date, and a marker is a claim that something occurs in a
# particular week.
_LANE_BAND_STATUS = "recurring"
_LANE_MARKER_STATUSES = frozenset({"scheduled", "held"})


def meetings_lanes(meetings: list[dict], area_of_project: dict,
                   weeks: list[date]) -> dict:
    """One lane per area: a recurring band, and a marker per dated one-off.

    SIX ROWS FOR THE COMPANY, NOT SIX HUNDRED (decision 8). A row per meeting
    would reproduce the growth problem the fold exists to solve, one level down —
    and the question a Gantt answers about meetings is "how often does this area
    meet", not "which Tuesday was the third sync".

    A meeting whose label names no known project contributes to no lane. That is
    deliberate: guessing an area from a free-text label is the same name matching
    that drew 2 correct ghosts out of 27 attempts, and a meeting drawn under the
    wrong area is worse than one drawn nowhere.
    """
    from services.google_sheets import canonical_meeting_status

    lanes: dict[str, dict] = defaultdict(
        lambda: {"recurring": 0, "markers": {}})
    for m in meetings:
        area = area_of_project.get((m.get("label") or "").strip().lower())
        if not area:
            continue
        status = canonical_meeting_status(m.get("status"))
        if status == _LANE_BAND_STATUS:
            lanes[area]["recurring"] += 1
            continue
        if status not in _LANE_MARKER_STATUSES:
            continue
        when = _parse(m.get("proposed_date"))
        if when is None:
            continue
        first, _last, _open = span_columns(when, when, weeks)
        if first < 0:
            continue
        # Several meetings in one week collapse to one marker with a count. Two
        # markers cannot share a cell, and the alternative — dropping all but one
        # — would quietly under-report a busy week.
        cell = lanes[area]["markers"]
        cell[first] = cell.get(first, 0) + 1
    return dict(lanes)


def milestone_band(milestones: list[dict], weeks: list[date]) -> list[dict]:
    """Company milestones as markers on the same week columns.

    READ-ONLY HERE. The CEO tab stays their editable home — this band exists so a
    milestone can be read against the project bars underneath it, which is the
    one thing a separate tab cannot show. Two surfaces rendering the same rows is
    fine; two surfaces WRITING them is the defect family this plan avoids.

    A milestone with no target_date draws nothing rather than landing at the
    edge of the grid: an undated commitment is not a commitment in a week.
    """
    out = []
    for ms in milestones:
        when = _parse(ms.get("target_date"))
        if when is None:
            continue
        col, _last, _open = span_columns(when, when, weeks)
        if col < 0:
            continue
        out.append({
            "col": col,
            "title": (ms.get("title") or "").strip(),
            "status": (ms.get("status") or "open").strip().lower(),
            "moved": bool(ms.get("original_date")
                          and _parse(ms.get("original_date")) != when),
            "original": _parse(ms.get("original_date")),
            "target": when,
        })
    return sorted(out, key=lambda m: m["col"])


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

    Finished projects are INCLUDED but FOLDED — kept, greyed, one collapsed line
    per area. Eyal asked to keep seeing them and also asked that they stop taking
    up the board; the fold is how both are true at once.
    """
    c = supabase_client.client
    weeks = week_starts()
    today = _today()

    areas = {a["id"]: a["name"] for a in
             (c.table("areas").select("id,name").order("name").execute()).data or []}
    projects = (c.table("canonical_projects").select("*")
                .order("name").execute()).data or []
    tasks = (c.table("tasks").select(
        "id,project_id,title,assignee,deadline,status,priority,approval_status")
        .limit(5000).execute()).data or []

    # Both of these are additions to the board, so neither may take it down. A
    # timeline with no meetings lane is a smaller loss than no timeline.
    meetings: list[dict] = []
    try:
        meetings = supabase_client.list_follow_up_meetings(
            limit=2000, include_pending=True) or []
    except Exception as e:                                   # noqa: BLE001
        logger.warning(f"[timeline] meetings unavailable: {e}")
    milestones: list[dict] = []
    try:
        milestones = (c.table("milestones").select("*")
                      .limit(500).execute()).data or []
    except Exception as e:                                   # noqa: BLE001
        logger.warning(f"[timeline] milestones unavailable: {e}")

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
    folded_by_area: dict[str, list] = defaultdict(list)
    area_of_project: dict[str, str] = {}
    stats = {"projects": 0, "folded": 0, "no_start": 0, "open_ended": 0}

    for p in projects:
        area = areas.get(p.get("area_id"), "(no area)")
        area_of_project[(p.get("name") or "").strip().lower()] = area
        start = _parse(p.get("start_date"))
        target = _parse(p.get("target_date"))
        open_tasks = open_by_project.get(p["id"], [])
        first, last, open_ended = span_columns(start, target, weeks)
        folded = is_folded(p, today)

        stats["projects"] += 1
        if folded:
            stats["folded"] += 1
        if start is None:
            stats["no_start"] += 1
        if open_ended:
            stats["open_ended"] += 1

        sorted_tasks = sorted(
            ({"title": t.get("title") or "",
              "assignee": t.get("assignee") or "",
              "deadline": _parse(t.get("deadline")),
              "priority": (t.get("priority") or "M")}
             for t in open_tasks),
            key=lambda t: (t["deadline"] is None, t["deadline"] or date.max))

        row = {
            "project_id": p["id"],
            "name": p["name"],
            "owner": p.get("owner") or "",
            "start": start,
            "target": target,
            "first_col": first,
            "last_col": last,
            # A FOLDED BAR IS NEVER OPEN-ENDED. An open end means "still going,
            # end unknown", which is a contradiction on a project somebody has
            # declared finished — and it would paint the pale tint from its start
            # to the right-hand edge of the grid, so the archive of what we
            # shipped would be mostly a wash of colour claiming ongoing work.
            "open_ended": open_ended and not folded,
            "declared": declared_status(p),
            "status": _status_of(p, start, today),
            "folded": folded,
            "action": nearest_action(sorted_tasks),
            "priority": project_priority(sorted_tasks),
            "tasks": sorted_tasks,
        }
        (folded_by_area if folded else by_area)[area].append(row)

    # Every area with folded work needs its key present in `areas` even if all of
    # its projects are folded, or the whole area silently vanishes from the tab.
    for area in folded_by_area:
        by_area.setdefault(area, [])

    archive = legacy_archive(bars, weeks)
    stats["archive_rows"] = sum(len(s["lanes"]) for s in archive)
    stats["archive_bars"] = sum(len(l["bars"]) for s in archive for l in s["lanes"])

    lanes = meetings_lanes(meetings, area_of_project, weeks)
    band = milestone_band(milestones, weeks)
    stats["meeting_lanes"] = len(lanes)
    stats["milestones"] = len(band)

    return {"weeks": weeks, "areas": dict(by_area),
            "folded": dict(folded_by_area), "lanes": lanes,
            "milestones": band, "archive": archive, "stats": stats}
