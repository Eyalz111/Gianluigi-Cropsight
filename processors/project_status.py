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

Grouping is by AREA (`tasks.category`, 6 areas + General), not by
`canonical_projects`. That is a data reality, not a preference: the 10 rows in
`canonical_projects` have no link to tasks — no `project_id` column exists, and
`tasks.label` is ~66% NULL — so a per-project grouping cannot be assembled from
what is stored today. Tagging tasks to projects is the follow-up that would let
this switch over; see AREA_ORDER / `build_status_pack(group_by=...)`.

Scope is OPEN ITEMS ONLY (pending / in_progress / overdue tasks, plus unresolved
open questions). Done and archived work is excluded so the review stays on what
still needs a decision.
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


def title_block(area: str, generated_at: datetime | None = None) -> dict:
    """Header text for a tab: the three rows above the column headers."""
    stamp = (generated_at or datetime.now(_ISRAEL_TZ)).strftime("%d/%m/%Y")
    return {
        "title": area,
        "confidentiality": CONFIDENTIALITY,
        "distribution": "Distribution: Eyal Zror, Paolo Vailetti, Nechama Tik",
        "generated": f"Generated {stamp}",
    }
