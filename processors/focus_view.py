"""The Focus tab — one re-sliceable view of everything still open.

Nechama works the list daily and had no way to ask "what is late?" without
reading six area tabs. This is that question, and four others, on ONE tab.

WHY TWO TABS. `Focus data` is written by the server and hidden; `Focus` is the
visible tab and holds nothing but formulas over it. That split is what makes
the dropdowns instant: changing "group by" is a recalculation in the browser,
not a round trip that waits up to 30 minutes for the next reconcile cycle.

WHY THE SERVER COMPUTES THE BUCKETS. "Overdue" could be an in-sheet formula
against TODAY(), but the rebuild clears and rewrites the data rows every cycle,
so those formulas would have to be re-written as formulas each time and survive
`valueInputOption`. Computing `Bucket`/`BucketSort` here keeps the data tab
pure values. The cost is that bucket boundaries are as fresh as the last
refresh — which matters only in the half hour after midnight, when nobody is
reading it.

WHY IT CANNOT BE FORMULAS OVER THE AREA TABS DIRECTLY. Those carry the v2 block
layout (a project row then its action rows), hidden identity columns, and a row
count that moves whenever a block is added. A formula addressing them by range
would break the first time the structural pass ran. This reads the DATABASE,
which is the source of truth anyway.

BOTH TABS ARE DERIVED. They are registered in `NON_AREA_TABS`, without which
the reconcile would try to parse them as area tabs and the formatting pass
would strip them — exactly what happened to the meetings pool in the 2026-08-09
review. The visible tab is additionally protected: Nechama edits this workbook
every day, and an edit onto a generated tab would vanish at the next refresh
with nothing to show she had made it.
"""

import logging
from datetime import datetime, timedelta, timezone

from config.settings import settings
from services.supabase_client import supabase_client

logger = logging.getLogger(__name__)

FOCUS_TAB = "Focus"
FOCUS_DATA_TAB = "Focus data"

ISRAEL_TZ = timezone(timedelta(hours=3))

# Hidden data tab. Column order is load-bearing — the visible tab's QUERY
# addresses these by letter, so a column inserted here silently re-points every
# selector on the other tab. Add at the END.
#        A       B      C       D      E          F           G          H       I         J         K
DATA_HEADERS = ["Kind", "Due", "What", "Who", "PriSort", "Priority", "Project", "Area",
                "Status", "Bucket", "BucketSort", "Uid"]

# Index of each field in a `build_rows` row. The renderer, the filter and the
# readback all index these, and three sets of literals would drift the first
# time a column moved.
DCOL_KIND, DCOL_DUE, DCOL_WHAT, DCOL_WHO = 0, 1, 2, 3
DCOL_PRISORT, DCOL_PRIORITY, DCOL_PROJECT, DCOL_AREA = 4, 5, 6, 7
DCOL_STATUS, DCOL_BUCKET, DCOL_BUCKETSORT, DCOL_UID = 8, 9, 10, 11

_PRI_SORT = {"U": 1, "Urgent": 1, "H": 2, "M": 3, "L": 4}
_PRI_LABEL = {"U": "Urgent", "H": "H", "M": "M", "L": "L"}

# Bucket -> sort key. "NO DATE" sorts LAST but is never filtered out: undated
# work is the backlog that ages silently, and a view that hides it is how a
# to-do list quietly stops being true.
BUCKETS = [
    ("OVERDUE", 1),
    ("TODAY", 2),
    ("THIS WEEK", 3),
    ("THIS MONTH", 4),
    ("LATER", 5),
    ("NO DATE", 6),
]
_BUCKET_SORT = dict(BUCKETS)

_CLOSED_TASK = {"done", "archived", "cancelled", "superseded"}
# A meeting that has happened or been dropped is not outstanding work.
_CLOSED_MEETING = {"held", "dropped", "cancelled"}

# Focus answers "what needs attention", so a meeting earns its place here only
# if somebody is expected to act on it. Eyal, 2026-08-12: *"only the scheduled
# and to schedule ones, not the park and held."*
#
# `parked` is the one that had to go, and it is 17 of the 22 meetings on the
# pool. Parked means deliberately not being pursued — a decision already taken —
# so listing them on the attention tab inverts the point: the shortest list is
# the one people read, and burying 5 live meetings under 17 dormant ones is how
# a focus view stops being looked at.
FOCUS_MEETING_STATUSES = {"scheduled", "to_schedule"}


def _parse_date(value) -> "datetime.date | None":
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value)[:10]).date()
    except (ValueError, TypeError):
        return None


def bucket_for(due, today) -> tuple[str, int]:
    """Which urgency bucket a due date falls in.

    Computed from the DATE, never from `tasks.status`. On 2026-08-11 seventeen
    open tasks were past their deadline while only five carried
    `status='overdue'` — the status field lags reality by 3x, and a view built
    on it would have under-reported the backlog by the same factor.
    """
    if due is None:
        return "NO DATE", _BUCKET_SORT["NO DATE"]
    delta = (due - today).days
    if delta < 0:
        return "OVERDUE", _BUCKET_SORT["OVERDUE"]
    if delta == 0:
        return "TODAY", _BUCKET_SORT["TODAY"]
    if delta <= 7:
        return "THIS WEEK", _BUCKET_SORT["THIS WEEK"]
    if delta <= 30:
        return "THIS MONTH", _BUCKET_SORT["THIS MONTH"]
    return "LATER", _BUCKET_SORT["LATER"]


def _area_and_project_maps() -> tuple[dict, dict]:
    """project_id -> (project name, area name)."""
    areas = {a["id"]: a.get("name") or ""
             for a in (supabase_client.client.table("areas")
                       .select("id,name").execute().data or [])}
    projects = {}
    for p in (supabase_client.client.table("canonical_projects")
              .select("id,name,area_id").execute().data or []):
        projects[p["id"]] = (p.get("name") or "", areas.get(p.get("area_id"), ""))
    return projects, areas


def _what(label, title, project_name) -> str:
    """The action, prefixed by the thread it belongs to.

    THE ACTION ALONE IS OFTEN MEANINGLESS. Eyal, reading the live tab: *"some
    are 'context missing' such as 'Yoram to reach out - Eyal to articulate a
    message for him' … cannot understand exactly what is the task without moving
    to the right tab."* On the area tab that row reads `Eitan Zemel` in Topic and
    the action beside it, and the Topic is what names the person, the grant or
    the counterparty. Focus was showing Project and dropping Topic, so a whole
    class of rows arrived without the noun they are about.

    `tasks.label` IS the topic — the 226 label values are topics, not projects;
    the project is `project_id`. Suppressed when it merely repeats the project
    name, which would add a word and no information.
    """
    t = (title or "").strip()
    lab = (label or "").strip()
    if not lab or _norm_name(lab) == _norm_name(project_name):
        return t
    if not t:
        return lab
    # Already self-describing — don't stutter "Eitan Zemel — Eitan Zemel call".
    if _norm_name(lab) in _norm_name(t):
        return t
    return f"{lab} — {t}"


def _norm_name(v) -> str:
    return " ".join(str(v or "").lower().split())


def _meeting_area(label, projects: dict) -> str:
    """The area a meeting rolls up to, via its project name.

    Looked up by NAME because `follow_up_meetings` carries a label, not a
    project_id. An unmatched label yields "" rather than a guess — a meeting
    filed under the wrong area is worse than one filed under none.
    """
    want = _norm_name(label)
    if not want:
        return ""
    for _pid, (pname, parea) in projects.items():
        if _norm_name(pname) == want:
            return parea or ""
    return ""


def build_rows(today=None) -> list[list]:
    """Every open task and every open meeting, one row each.

    Returns [] only when there genuinely is nothing open — the caller must
    treat [] as "do not clear the tab" (see `_rebuild_readonly_tab`), because a
    transient read failure returning [] is indistinguishable from real
    emptiness and would blank the view.
    """
    today = today or datetime.now(ISRAEL_TZ).date()
    projects, areas = _area_and_project_maps()
    rows: list[list] = []

    tasks = (supabase_client.client.table("tasks").select("*")
             .eq("approval_status", "approved").limit(5000).execute().data or [])
    for t in tasks:
        if (t.get("status") or "").lower() in _CLOSED_TASK:
            continue
        if t.get("ps_suppressed") or t.get("valid_to"):
            continue
        due = _parse_date(t.get("deadline"))
        bucket, bsort = bucket_for(due, today)
        pname, parea = projects.get(t.get("project_id"), ("", ""))
        if not parea:
            parea = areas.get(t.get("area_id"), "") or t.get("area_label") or ""
        pri = (t.get("priority") or "M").strip()
        rows.append([
            "Task",
            due.isoformat() if due else "",
            _what(t.get("label"), t.get("title"), pname),
            t.get("assignee") or "(unassigned)",
            _PRI_SORT.get(pri, 3),
            _PRI_LABEL.get(pri, pri),
            pname,
            parea,
            t.get("status") or "",
            bucket,
            bsort,
                t.get("id") or "",
        ])

    meetings = (supabase_client.client.table("follow_up_meetings").select("*")
                .eq("approval_status", "approved").limit(2000).execute().data or [])
    for m in meetings:
        from services.google_sheets import canonical_meeting_status

        # Canonicalised so a row written before the 2026-08-12 rename still
        # matches — otherwise every pre-rename `not_scheduled` meeting silently
        # vanishes from the tab that exists to stop things being missed.
        status = canonical_meeting_status(m.get("status"))
        # `recurring` has no single date by definition — it would sit in NO DATE
        # forever and train the eye to ignore that bucket. `parked` and the
        # closed states are excluded by the allowlist above.
        if status not in FOCUS_MEETING_STATUSES:
            continue
        due = _parse_date(m.get("proposed_date"))
        bucket, bsort = bucket_for(due, today)
        pri = (m.get("priority") or "M").strip()
        rows.append([
            "Meeting",
            due.isoformat() if due else "",
            m.get("title") or "",
            m.get("led_by") or "(no lead)",
            _PRI_SORT.get(pri, 3),
            _PRI_LABEL.get(pri, pri),
            # A meeting's project was rendered blank, so every meeting row sat
            # under "(no project)" and the Project filter could never find one.
            # `follow_up_meetings.label` is already the canonical project name.
            # [2026-08-13]
            (m.get("label") or "").strip(),
            _meeting_area(m.get("label"), projects),
            m.get("status") or "",
            bucket,
            bsort,
                m.get("id") or "",
        ])

    rows.sort(key=lambda r: (r[10], r[1] or "9999-99-99", r[4]))
    return rows


# --------------------------------------------------------------------------
# The visible tab. Everything below builds formulas — no data is written here,
# so a stale Focus tab is impossible: it always reflects the data tab as of the
# last refresh.
# --------------------------------------------------------------------------

GROUP_CHOICES = ["Due date", "Owner", "Project", "Area", "Priority"]
SHOW_CHOICES = ["Everything open", "Overdue only", "Due this week", "No date only"]

# Which QUERY column each grouping sorts on FIRST. Always followed by K (bucket)
# and B (date) so that within any grouping the order is still "soonest first".

CTL_GROUP = "B2"
CTL_OWNER = "D2"
CTL_AREA = "F2"
CTL_SHOW = "H2"

HEADERS = ["✓", "When", "Due", "What", "Who", "Priority", "Project", "Area", "Kind"]

# Hidden identity, written to the right of the visible block and masked
# white-on-white the way every other editable tab masks its own. A readback has
# to find a row without depending on WHERE it sits, and Focus re-sorts and
# re-filters on every dropdown change, so row position means even less here than
# on the Timeline — where at least the order is stable between renders.
#
# `_kind` is STORED, not inferred. A task and a meeting write to different
# tables, and reading that off the visible Kind column would mean trusting a
# cell a person can retype.
FOCUS_HIDDEN_HEADERS = ["_uid", "_kind"]
N_FOCUS_HIDDEN = len(FOCUS_HIDDEN_HEADERS)

# Column indexes into a rendered Focus row. Named because the readback, the
# renderer and the conditional formats all have to agree.
FCOL_DONE, FCOL_WHEN, FCOL_DUE = 0, 1, 2
FCOL_WHAT, FCOL_WHO, FCOL_PRIORITY = 3, 4, 5
FCOL_PROJECT, FCOL_AREA, FCOL_KIND = 6, 7, 8

ROW_TASK, ROW_MEETING = "task", "meeting"

# EDITABLE ON FOCUS: Done, Due, Priority. Deliberately NOT Who.
#
# `tasks.deadline` and `tasks.assignee` are edited daily by Nechama on the area
# tabs and are read-only on the Timeline for exactly that reason; Focus makes a
# FOURTH writer on those rows, and every cross-surface defect of 2026-08 came
# from two writers on one field. Done is a status transition nothing else
# contends for, and Due and Priority are what actually move in a weekly review.
# Reassignment is rarer and the likeliest of the three to happen in two places
# in one week, so it stays with its existing owner. Eyal's call, 2026-08-13,
# after being shown the trade.
FOCUS_EDITABLE = {FCOL_DONE: "done", FCOL_DUE: "due", FCOL_PRIORITY: "priority"}


_SHOW_BUCKETS = {
    "Overdue only": {1},
    "Due this week": {1, 2, 3},
    "No date only": {6},
}

# Which raw column each grouping sorts on FIRST, then always bucket and date, so
# that within any grouping the order is still "soonest first". Mirrors the old
# QUERY's letters exactly — the behaviour Nechama already knows.
_GROUP_KEY = {
    "Due date": (DCOL_BUCKETSORT, DCOL_DUE, DCOL_PRISORT),
    "Owner": (DCOL_WHO, DCOL_BUCKETSORT, DCOL_DUE),
    "Project": (DCOL_PROJECT, DCOL_BUCKETSORT, DCOL_DUE),
    "Area": (DCOL_AREA, DCOL_BUCKETSORT, DCOL_DUE),
    "Priority": (DCOL_PRISORT, DCOL_BUCKETSORT, DCOL_DUE),
}


def display_rows(raw: list[list], group="Due date", owner="All", area="All",
                 show="Everything open") -> list[list]:
    """The filtered, sorted, display-shaped rows for the visible tab.

    THIS REPLACES THE QUERY FORMULA. The tab used to be a single QUERY over the
    data tab, which made the dropdowns instant — and made the rows unwritable,
    because Sheets owns a formula's whole spill range. Worse, an edit placed
    BESIDE a query is anchored to a row POSITION, so changing any dropdown
    re-points it at a different task and a Done tick lands on whatever now sits
    on that line. Materialising is what makes Focus editable at all, and it is
    why the filter had to move to the server.

    The filter semantics are carried over unchanged, including the one that
    looks like a bug and is not: MEETINGS SURVIVE THE AREA FILTER. They carry no
    area of their own, so a strict match would hide every one of them the moment
    an area was chosen — and a meeting disappearing because you narrowed to your
    own area is the failure this tab exists to prevent. [2026-08-11, Eyal's call]
    """
    out = []
    for r in raw:
        if not str(r[DCOL_WHAT] or "").strip():
            continue
        if owner and owner != "All" and str(r[DCOL_WHO]) != owner:
            continue
        if (area and area != "All" and str(r[DCOL_AREA]) != area
                and str(r[DCOL_KIND]) != "Meeting"):
            continue
        wanted = _SHOW_BUCKETS.get(show)
        if wanted is not None and int(r[DCOL_BUCKETSORT]) not in wanted:
            continue
        out.append(r)

    keys = _GROUP_KEY.get(group, _GROUP_KEY["Due date"])

    def _sort_key(r):
        vals = []
        for k in keys:
            v = r[k]
            # Dates and blanks share a column; an empty due date must sort LAST
            # rather than first, or the undated backlog would head the list.
            if k == DCOL_DUE:
                vals.append(str(v) or "9999-99-99")
            elif isinstance(v, int):
                vals.append(f"{v:04d}")
            else:
                vals.append(str(v).lower())
        return tuple(vals)

    out.sort(key=_sort_key)

    return [[
        False,                                   # the Done tick, always unticked
        r[DCOL_BUCKET], r[DCOL_DUE], r[DCOL_WHAT], r[DCOL_WHO],
        r[DCOL_PRIORITY], r[DCOL_PROJECT], r[DCOL_AREA], r[DCOL_KIND],
        r[DCOL_UID],
        ROW_MEETING if str(r[DCOL_KIND]) == "Meeting" else ROW_TASK,
    ] for r in out]


def valid_control(kind: str, value, owners=(), areas=()) -> str:
    """A control cell's value, or its default if the cell says something unknown.

    A dropdown can hold a stale option — an owner with no open work left, an
    area renamed since the last refresh. Filtering on it would return zero rows
    and read as "nothing is open", which is the one wrong answer this tab must
    never give.
    """
    v = str(value or "").strip()
    if kind == "group":
        return v if v in GROUP_CHOICES else GROUP_CHOICES[0]
    if kind == "show":
        return v if v in SHOW_CHOICES else SHOW_CHOICES[0]
    if kind == "owner":
        return v if (v == "All" or v in owners) else "All"
    if kind == "area":
        return v if (v == "All" or v in areas) else "All"
    return v


def _counts_formula(bucket_sort: int, label: str) -> str:
    return (f'="{label}: "&COUNTIF(\'{FOCUS_DATA_TAB}\'!K2:K,{bucket_sort})')


def focus_layout(controls: dict | None = None) -> list[list]:
    """Rows 1-4 of the visible tab. The rows themselves are written separately.

    THE CONTROLS ARE PRESERVED, NOT RESET. They used to be written as constants
    because a QUERY read them and nothing else did; now the SERVER reads them to
    filter, and re-writing the defaults every refresh would silently drop
    Nechama back to "Everything open" every thirty minutes — mid-meeting, while
    she was looking at it. The caller reads the live values and passes them back.
    """
    c = controls or {}
    return [
        ["", "FOCUS — everything still open", "", "",
         _counts_formula(1, "Overdue"), _counts_formula(2, "Today"),
         _counts_formula(3, "This week"), _counts_formula(6, "No date")],
        ["Group by:", c.get("group") or GROUP_CHOICES[0],
         "Owner:", c.get("owner") or "All",
         "Area:", c.get("area") or "All",
         "Show:", c.get("show") or SHOW_CHOICES[0]],
        [f'=" refreshed "&TEXT(NOW(),"ddd d mmm HH:MM")&"  ·  '
         f'tick ✓ to close · Due and Priority are editable · '
         f'everything else is generated"'],
        HEADERS + FOCUS_HIDDEN_HEADERS,
    ]
