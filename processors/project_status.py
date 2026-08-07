"""
Project Status pack — the working document for the weekly project review
(Paolo + Nechama going area by area).

Mirrors the "PROJECTS STATUS TEMPLATE" workbook Eyal supplied: one tab per
project area, each laid out as

    # | Subject | Action | To do | Date | Resp. | Comments

**Deliberately a WORKING document, not a report.** `Subject`, `To do`, `Date`
and `Resp.` are pre-filled from the DB so nobody retypes them; `Action` and
`Comments` are left EMPTY for the meeting to fill in live. That is what the
template's blank columns are for.

Scope is OPEN ITEMS ONLY (pending / in_progress / overdue tasks, plus unresolved
open questions). Done and archived work is excluded so the review stays on what
still needs a decision.

TWO LAYOUTS LIVE HERE
---------------------

`build_status_pack` (v1) is TASK-CENTRIC: one row per task, grouped by AREA
(`tasks.category`), because when it shipped a per-project grouping could not be
assembled — `canonical_projects` had no link to tasks at all.

`build_status_blocks` (v2, behind PROJECT_STATUS_V2_LAYOUT) is PROJECT-CENTRIC:
a project row followed by its action rows. It became possible on 2026-08-07 when
migrate_project_status_v2.sql added `tasks.project_id` and the backfill attached
every open task to one of the 22 curated projects.

v2 also reads the template's columns the way Eyal actually means them, which v1
had backwards: **Action is the nearest concrete step, To do is the eventual
objective**. That is what makes the file project-centric — the objective belongs
to the project row, the steps are the rows beneath it.

v1 stays until the v2 rollout is verified, then the flag becomes the only path.
"""

import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from services.supabase_client import supabase_client

logger = logging.getLogger(__name__)

_ISRAEL_TZ = ZoneInfo("Asia/Jerusalem")

# Column layout — must match the supplied template exactly.
HEADERS = ["#", "Subject", "Action", "To do", "Date", "Resp.", "Comments"]

CONFIDENTIALITY = "Strictly Private & Confidential  - not to be distributed to third parties"

# Tab order for the review. "General" is last — it is the untagged bucket, not a
# real area, and Eyal should see it as the leftovers.
AREA_ORDER = [
    "PRODUCT & TECHNOLOGY",
    "SALES & BUSINESS DEVELOPMENT",
    "CLIENT DELIVERY & OPERATIONS",
    "FUNDRAISING & INVESTOR RELATIONS",
    "LEGAL, CORPORATE & FINANCE",
    "TEAM & HUMAN RESOURCES",
    "General",
]

# Google Sheets caps tab names at 100 chars and forbids : \ / ? * [ ]
_TAB_FORBIDDEN = set(':\\/?*[]')

OPEN_TASK_STATUSES = ["pending", "in_progress", "overdue"]

# Status band -> sort weight. Overdue first: the review should open on what has
# already slipped, not on what is merely queued.
_STATUS_WEIGHT = {"overdue": 0, "in_progress": 1, "pending": 2}
_URGENCY_WEIGHT = {"H": 0, "M": 1, "L": 2}


def tab_name_for(area: str) -> str:
    """Sheets-safe tab name for an area."""
    cleaned = "".join(c for c in (area or "General") if c not in _TAB_FORBIDDEN)
    return cleaned[:100].strip() or "General"


def _fmt_date(value) -> str:
    """Render a deadline as DD/MM/YYYY; blank when unset."""
    if not value:
        return ""
    text = str(value)
    if text.lower() in ("none", "null", ""):
        return ""
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).strftime("%d/%m/%Y")
    except (ValueError, TypeError):
        return text[:10]


def _effective(task: dict, field: str) -> str:
    """Read a task field as display text.

    NOTE `manual_<field>` is a BOOLEAN FLAG meaning "a human edited this", not an
    override value — the current value always lives in the plain column. Treating
    the flag as the value renders every cell as the literal "False".
    """
    value = task.get(field)
    return "" if value in (None, "None") else str(value)


def _was_hand_edited(task: dict, field: str) -> bool:
    """True when `field` carries a human edit (Sheet edit / Telegram correction)."""
    return bool(task.get(f"manual_{field}"))


_project_index: list[tuple[str, str]] | None = None


def _load_project_index() -> list[tuple[str, str]]:
    """[(needle_lower, project_name)] from canonical_projects names + aliases.

    Longest needle first so 'Moldova wheat' wins over a bare 'Moldova'.
    Cached per process — this is a 10-row table read on every pack build.
    """
    global _project_index
    if _project_index is not None:
        return _project_index
    pairs: list[tuple[str, str]] = []
    try:
        rows = (
            supabase_client.client.table("canonical_projects")
            .select("name,aliases")
            .eq("status", "active")
            .execute()
            .data
        ) or []
        for r in rows:
            name = (r.get("name") or "").strip()
            if not name:
                continue
            pairs.append((name.lower(), name))
            for alias in (r.get("aliases") or []):
                alias = (alias or "").strip()
                if alias:
                    pairs.append((alias.lower(), name))
    except Exception as e:
        logger.warning(f"[project-status] project index unavailable: {e}")
    pairs.sort(key=lambda p: len(p[0]), reverse=True)
    _project_index = pairs
    return _project_index


def _match_project(*texts: str) -> str:
    """First canonical project whose name/alias appears in any given text."""
    haystack = " ".join(t for t in texts if t).lower()
    if not haystack:
        return ""
    for needle, name in _load_project_index():
        if needle in haystack:
            return name
    return ""


def _subject_for(task: dict) -> str:
    """The 'Subject' cell — the thing being discussed.

    Order: the task's own label -> a canonical project matched from its
    title/label -> a dash. The source meeting is deliberately NOT used as a
    fallback: it already appears in Comments, so it would just duplicate.
    A dash makes untagged work visibly untagged, which is itself the signal
    that this task needs a label.
    """
    label = _effective(task, "label")
    if label:
        return label
    project = _match_project(_effective(task, "title"))
    return project or "—"


def _comment_for(task: dict) -> str:
    """Provenance + anything the reviewer needs that isn't its own column."""
    bits = []
    status = _effective(task, "status")
    if status == "overdue":
        bits.append("OVERDUE")
    elif status:
        bits.append(status.replace("_", " "))
    urgency = (task.get("urgency") or task.get("priority") or "").strip()
    if urgency:
        bits.append(f"urgency {urgency}")
    # Surface human edits so the reviewer knows a value was set by hand rather
    # than extracted — otherwise a corrected owner/date looks like model output.
    edited = [f for f in ("status", "deadline", "assignee", "title")
              if _was_hand_edited(task, f)]
    if edited:
        bits.append("edited: " + "/".join(edited))
    meeting = task.get("meetings") or {}
    m_title, m_date = (meeting.get("title") or "").strip(), meeting.get("date")
    if m_title:
        stamp = f" {_fmt_date(m_date)}" if m_date else ""
        bits.append(f"from: {m_title[:45]}{stamp}")
    return " · ".join(bits)


def _sort_key(task: dict):
    """Overdue first, then in-progress, then by urgency, then by due date.

    Undated tasks sort last within their band rather than first — an empty date
    string would otherwise win an ascending sort and push dated work down.
    """
    status = _effective(task, "status")
    deadline = _effective(task, "deadline")
    return (
        _STATUS_WEIGHT.get(status, 3),
        _URGENCY_WEIGHT.get((task.get("urgency") or "").upper(), 3),
        deadline or "9999-12-31",
    )


def _fetch_open_tasks() -> list[dict]:
    """Approved, live, still-open tasks with their source meeting."""
    try:
        return (
            supabase_client.client.table("tasks")
            .select("*,meetings(title,date)")
            .eq("approval_status", "approved")
            .is_("valid_to", "null")
            .in_("status", OPEN_TASK_STATUSES)
            .limit(1000)
            .execute()
            .data
        ) or []
    except Exception as e:
        logger.error(f"[project-status] task fetch failed: {e}")
        return []


def _fetch_open_questions() -> list[dict]:
    """Approved, unresolved open questions with their source meeting."""
    try:
        return (
            supabase_client.client.table("open_questions")
            .select("*,meetings!open_questions_meeting_id_fkey(title,date)")
            .eq("approval_status", "approved")
            .eq("status", "open")
            .limit(500)
            .execute()
            .data
        ) or []
    except Exception as e:
        logger.error(f"[project-status] question fetch failed: {e}")
        return []


def _area_of(row: dict) -> str:
    """Normalise a row's area, mapping unknown/blank onto 'General'."""
    area = (row.get("category") or row.get("area_label") or "").strip()
    return area if area in AREA_ORDER else "General"


def build_status_pack(include_questions: bool = True) -> dict[str, list[list]]:
    """Assemble {area -> rows} in the template's column order.

    Every area in AREA_ORDER gets a key even when empty, so the workbook always
    has the same tabs and a quiet area is visibly quiet rather than missing.
    """
    tasks = _fetch_open_tasks()
    questions = _fetch_open_questions() if include_questions else []

    by_area: dict[str, list[dict]] = {a: [] for a in AREA_ORDER}
    for t in tasks:
        by_area.setdefault(_area_of(t), []).append(t)

    # open_questions carries NO category column. Routing them all to General
    # buries the areas under ~65 rows, so infer each question's area from the
    # area its source meeting's tasks were filed under (the meeting is the only
    # shared key). Ties and unknowns fall to General, which stays honest.
    meeting_area: dict[str, str] = {}
    area_votes: dict[str, dict[str, int]] = {}
    for t in tasks:
        mid = t.get("meeting_id")
        if mid:
            area_votes.setdefault(mid, {})
            a = _area_of(t)
            area_votes[mid][a] = area_votes[mid].get(a, 0) + 1
    for mid, votes in area_votes.items():
        real = {a: n for a, n in votes.items() if a != "General"}
        if real:
            meeting_area[mid] = max(real, key=real.get)

    q_by_area: dict[str, list[dict]] = {a: [] for a in AREA_ORDER}
    for q in questions:
        label = (q.get("label") or "").strip()
        area = label if label in AREA_ORDER else meeting_area.get(q.get("meeting_id"), "General")
        q_by_area.setdefault(area, []).append(q)

    pack: dict[str, list[list]] = {}
    for area in AREA_ORDER:
        rows: list[list] = []
        for t in sorted(by_area.get(area, []), key=_sort_key):
            rows.append([
                len(rows) + 1,
                _subject_for(t),
                "",                                   # Action — filled in the meeting
                _effective(t, "title"),
                _fmt_date(_effective(t, "deadline")),
                _effective(t, "assignee"),
                _comment_for(t),
            ])
        for q in q_by_area.get(area, []):
            meeting = q.get("meetings") or {}
            question = (q.get("question") or "").strip()
            # Subject gets the real topic (label / matched project), same as a
            # task. Tagging every one of ~69 rows "Open question" would make the
            # column useless — the fact that it IS a question goes in Comments.
            subject = (q.get("label") or "").strip() or _match_project(question) or "—"
            note = ["open question"]
            m_title = (meeting.get("title") or "").strip()
            if m_title:
                note.append(f"from: {m_title[:45]} {_fmt_date(meeting.get('date'))}".strip())
            rows.append([
                len(rows) + 1,
                subject,
                "",
                question,
                "",
                (q.get("raised_by") or "").strip(),
                " · ".join(note),
            ])
        pack[area] = rows

    total = sum(len(v) for v in pack.values())
    logger.info(
        f"[project-status] built {total} open item(s) across {len(pack)} area(s): "
        + ", ".join(f"{a}={len(pack[a])}" for a in AREA_ORDER)
    )
    return pack


def _project_area_names() -> dict:
    """{area_id -> area name}. One read, used to bucket projects into tabs."""
    try:
        rows = supabase_client.client.table("areas").select("id,name").execute().data or []
    except Exception as e:                                  # noqa: BLE001
        logger.error(f"[project-status] could not read areas: {e}")
        return {}
    return {r["id"]: r["name"] for r in rows}


def build_status_blocks() -> dict[str, list[list]]:
    """{area -> rows} in the v2 BLOCK layout, including the hidden columns.

    A project row, then its open action rows, then the next project. Every row
    carries its own uid and its parent's, so the reconcile engine can identify
    it after a drag, an insert or a re-order — identity is never positional.

    RETIRED projects are omitted. Retire-not-delete keeps the row so historical
    labels still canonicalize (see resolve_label), but the point of retiring is
    that it stops appearing in the review.

    Empty projects are KEPT. A project with no open actions is a real and
    interesting state in a review — it means either finished or stalled, and
    hiding it would make the file silently disagree with the project list Eyal
    curated. It also has to exist as a block for Nechama to add the first
    action under it.
    """
    from services.project_status_rows import (
        KIND_ACTION, KIND_PROJECT, ORIGIN_AUTO, format_provenance,
    )

    area_of = _project_area_names()
    projects = [p for p in supabase_client.get_canonical_projects(status=None)
                if (p.get("status") or "active") != "retired"]
    by_project = supabase_client.get_open_tasks_by_project()

    pack: dict[str, list[list]] = {a: [] for a in AREA_ORDER if a != "General"}
    for p in sorted(projects, key=lambda r: (r.get("display_order") or 999,
                                             (r.get("name") or "").lower())):
        area = area_of.get(p.get("area_id")) or "General"
        if area not in pack:
            # An area with projects but no tab would drop them silently.
            pack[area] = []
        pid = p["id"]
        pack[area].append([
            p.get("display_order") or "",
            p.get("name") or "",
            "",                                   # Action is blank on a project row
            p.get("objective") or "",
            _fmt_date(p.get("target_date")),
            p.get("owner") or "",
            p.get("notes") or "",
            KIND_PROJECT, pid, pid, "", "",
        ])

        for t in sorted(by_project.get(pid, []), key=_sort_key):
            meeting = t.get("meetings") or {}
            marker = format_provenance(
                ORIGIN_AUTO,
                (meeting.get("title") or "").strip(),
                _fmt_date(meeting.get("date")),
            )
            title = _effective(t, "title")
            pack[area].append([
                # Column A is the done CHECKBOX on an action row. A real boolean,
                # not "FALSE": the cell carries BOOLEAN validation so Sheets
                # renders a tick box, and reconcile reads a clean bool back.
                False,
                # Subject on an action row is the TOPIC (tasks.label). Seeded
                # from the DB rather than left blank: leaving it empty while the
                # DB holds a label made the reconcile want to push it every
                # cycle — 37 phantom cell writes on a sheet nobody had touched.
                # Builder and merge have to agree on what a column means.
                _effective(t, "label"),
                f"{title} {marker}".strip(),
                "",                               # To do belongs to the project row
                _fmt_date(_effective(t, "deadline")),
                _effective(t, "assignee"),
                t.get("notes") or "",
                KIND_ACTION, t["id"], pid, ORIGIN_AUTO, t.get("meeting_id") or "",
            ])

    blocks = sum(1 for rows in pack.values() for r in rows if r[7] == KIND_PROJECT)
    actions = sum(1 for rows in pack.values() for r in rows if r[7] == KIND_ACTION)
    logger.info(
        f"[project-status] v2 built {blocks} project block(s) and {actions} "
        f"action(s) across {len(pack)} tab(s)"
    )
    return pack


def title_block(area: str, generated_at: datetime | None = None) -> dict:
    """Header text for a tab: the three rows above the column headers."""
    stamp = (generated_at or datetime.now(_ISRAEL_TZ)).strftime("%d/%m/%Y")
    return {
        "title": area,
        "confidentiality": CONFIDENTIALITY,
        "distribution": "Distribution: Eyal Zror, Paolo Vailetti, Nechama Tik",
        "generated": f"Generated {stamp}",
    }
