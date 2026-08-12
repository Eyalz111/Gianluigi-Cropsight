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
                "Status", "Bucket", "BucketSort"]

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
            t.get("title") or "",
            t.get("assignee") or "(unassigned)",
            _PRI_SORT.get(pri, 3),
            _PRI_LABEL.get(pri, pri),
            pname,
            parea,
            t.get("status") or "",
            bucket,
            bsort,
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
            "",
            "",
            m.get("status") or "",
            bucket,
            bsort,
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
_GROUP_SORT = {
    "Due date": "K, B, E",
    "Owner": "D, K, B",
    "Project": "G, K, B",
    "Area": "H, K, B",
    "Priority": "E, K, B",
}

CTL_GROUP = "B2"
CTL_OWNER = "D2"
CTL_AREA = "F2"
CTL_SHOW = "H2"

HEADERS = ["When", "Due", "What", "Who", "Priority", "Project", "Area", "Kind"]


def _query_formula() -> str:
    """One QUERY, assembled from the four dropdowns.

    Built as a formula rather than resolved here on purpose: the whole point of
    the tab is that Nechama changes a dropdown and the answer changes at once.
    """
    src = f"'{FOCUS_DATA_TAB}'!A2:K"

    where = "where C is not null"
    owner = f"&IF({CTL_OWNER}=\"All\",\"\",\" and D = '\"&{CTL_OWNER}&\"'\")"
    # Meetings survive the Area filter. They carry no area — only tasks hang off
    # a project — so a strict `H = area` would hide all 22 of them the moment an
    # area was chosen, and 21 of those already have no date. A meeting that
    # disappears because you narrowed to your own area is the failure this whole
    # tab exists to prevent. [2026-08-11, Eyal's call]
    area = (f"&IF({CTL_AREA}=\"All\",\"\","
            f"\" and (H = '\"&{CTL_AREA}&\"' or A = 'Meeting')\")")
    show = (
        f"&SWITCH({CTL_SHOW},"
        f"\"Overdue only\",\" and K = 1\","
        f"\"Due this week\",\" and K <= 3\","
        f"\"No date only\",\" and K = 6\","
        f"\"\")"
    )
    order = (
        "&\" order by \"&SWITCH(" + CTL_GROUP
        + "," + ",".join(f'"{k}","{v}"' for k, v in _GROUP_SORT.items())
        + f",\"{_GROUP_SORT['Due date']}\")"
    )
    return (
        f'=IFERROR(QUERY({src},"select J, B, C, D, F, G, H, A "&"{where}"'
        f'{owner}{area}{show}{order},0),'
        f'"Nothing matches — widen a filter above.")'
    )


def _counts_formula(bucket_sort: int, label: str) -> str:
    return (f'="{label}: "&COUNTIF(\'{FOCUS_DATA_TAB}\'!K2:K,{bucket_sort})')


def focus_layout() -> list[list]:
    """Rows 1-5 of the visible tab. Row 5 is the single QUERY."""
    return [
        ["FOCUS — everything still open", "", "", "",
         _counts_formula(1, "Overdue"), _counts_formula(2, "Today"),
         _counts_formula(3, "This week"), _counts_formula(6, "No date")],
        ["Group by:", GROUP_CHOICES[0], "Owner:", "All",
         "Area:", "All", "Show:", SHOW_CHOICES[0]],
        [f'=" refreshed "&TEXT(NOW(),"ddd d mmm HH:MM")&"  ·  '
         f'this tab is generated — edit tasks on the area tabs, not here"'],
        HEADERS,
        [_query_formula()],
    ]
