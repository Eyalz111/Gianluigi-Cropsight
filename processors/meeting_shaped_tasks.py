"""Tasks that are really meetings. [2026-08-09]

"Coordinate in-person meeting with Calabria region president — determine
attendees and language" was sitting in the open-task pool the morning this was
written, and it is not a task: it is a meeting that needs booking. Nothing was
wrong with the extraction — until now the sheet had no meetings pool, so the
only place a meeting-shaped commitment could land was the action list.

`follow_up_meetings` is that pool, and since 2026-08-09 it lives in the same
workbook as the project blocks. This module spots the ones already filed as
tasks and PROPOSES the move.

Two decisions worth keeping:

**Deterministic, no LLM.** The signal is a verb about arranging plus a noun
meaning a meeting, both in a short title. An LLM would cost money per task and
be less predictable, and this is a proposal Eyal reads anyway — a false positive
he declines is cheap, a subtly-wrong LLM judgement he approves is not.

**It never moves anything.** Same pattern as `topic_project_link` and
`person_new`: it raises a proposal and stops. Moving a task out of the action
list on a keyword match would remove work from the review with no human in the
loop, and "Chase Roye about scheduling the architecture review" reads exactly
like a meeting while being a genuine chase.
"""

import logging
import re
import uuid
from datetime import datetime, timedelta, timezone

from services.supabase_client import supabase_client

logger = logging.getLogger(__name__)

PROPOSAL_TYPE = "task_is_a_meeting"

_EXPIRY_DAYS = 30
_DEFAULT_MAX = 5

# The verb has to be about ARRANGING the thing, not doing something at it.
# "present at the board meeting" is a real task; "set up the board meeting" is
# not. `book`/`arrange`/`schedule` are unambiguous; `coordinate` and `set up`
# earn their place from real titles.
_ARRANGE = re.compile(
    r"\b(schedule|scheduling|re-?schedule|book|arrange|arranging|organi[sz]e|"
    r"organi[sz]ing|set\s?up|setting\s?up|coordinate|coordinating|convene|"
    r"line\s?up)\b", re.IGNORECASE)

# ...and the noun has to be the meeting itself.
_MEETING = re.compile(
    r"\b(meeting|meet|call|session|sync|catch[- ]?up|standup|stand[- ]?up|"
    r"workshop|demo|interview|discussion|conversation|chat|coffee|visit|"
    r"kick[- ]?off)\b", re.IGNORECASE)

# Work that merely happens around a meeting. "Prepare a summary and schedule the
# follow-up call" is a writing task, not a booking.
#
# POSITION DECIDES IT, not presence. These words only disqualify a title when
# they come BEFORE the arranging verb — i.e. when they are the leading action.
# Matching them anywhere killed "Set up the architecture review session with
# Eyal Zamir", where "review" is part of the meeting's NAME rather than the
# work. A real example, and the exact kind of false negative nobody notices
# because it looks like the detector simply found nothing.
_ABOUT_SOMETHING_ELSE = re.compile(
    r"\b(prepare|prep|agenda|notes?|minutes|summary|summarise|summarize|"
    r"write|draft|send|share|recap|deck|slides?|present|report)\b",
    re.IGNORECASE)

# Extraction appends context after an em-dash or in parentheses — "Schedule
# introductory call with Avi Perl (retired grape breeder) — discuss grape
# variety data". That trailer is elaboration, not additional work, so the whole
# title is not what to measure: at 28 words it blew straight past any sensible
# cap while its actual commitment is six words long. Everything below is
# evaluated on the HEAD CLAUSE.
_TRAILER = re.compile(r"\s+[—–-]\s+|\(")

# A long HEAD is describing a body of work that a meeting is only part of. The
# real examples run 6-15 words.
_MAX_WORDS = 16


def _head(title: str) -> str:
    """The commitment itself, without the appended context."""
    return _TRAILER.split((title or "").strip(), 1)[0].strip()


def looks_like_a_meeting(title: str) -> bool:
    """Is this task title really a meeting that needs booking?

    Both halves must be present — a verb about arranging AND a noun meaning a
    meeting — and nothing that says the work is something else. Requiring both
    is what keeps "Send Paolo the deck" and "Book the flights" out.
    """
    text = _head(title)
    if not text or len(text.split()) > _MAX_WORDS:
        return False
    arrange = _ARRANGE.search(text)
    if not arrange or not _MEETING.search(text):
        return False
    other = _ABOUT_SOMETHING_ELSE.search(text)
    return not (other and other.start() < arrange.start())


def _pending_keys() -> set:
    """Task ids already awaiting a decision, so nothing is proposed twice."""
    try:
        rows = supabase_client.get_pending_approvals_by_status("pending") or []
    except Exception:                                       # noqa: BLE001
        return set()
    return {(r.get("content") or {}).get("task_id")
            for r in rows
            if r.get("content_type") == PROPOSAL_TYPE
            and (r.get("content") or {}).get("task_id")}


def propose_meeting_shaped_tasks(max_proposals: int = _DEFAULT_MAX) -> dict:
    """Scan open tasks and propose the meeting-shaped ones. Never raises."""
    result = {"scanned": 0, "candidates": 0, "proposed": 0, "titles": []}
    try:
        tasks = supabase_client.get_tasks(status="pending") or []
        tasks += supabase_client.get_tasks(status="in_progress") or []
    except Exception as e:                                  # noqa: BLE001
        logger.warning(f"[meeting-shaped] could not read tasks: {e}")
        return result

    result["scanned"] = len(tasks)
    seen = _pending_keys()
    candidates = [t for t in tasks
                  if t.get("id") not in seen
                  and looks_like_a_meeting(t.get("title"))]
    result["candidates"] = len(candidates)
    if len(candidates) > max_proposals:
        # Say what was left, rather than quietly showing the first five. A
        # capped list that looks complete is how a backlog stays invisible.
        logger.info(f"[meeting-shaped] {len(candidates)} candidate(s), "
                    f"proposing {max_proposals} this run")

    expires = (datetime.now(timezone.utc)
               + timedelta(days=_EXPIRY_DAYS)).isoformat()
    for task in candidates[:max_proposals]:
        content = {
            "proposal_type": PROPOSAL_TYPE,
            "task_id": task["id"],
            "title": task.get("title") or "",
            "assignee": task.get("assignee") or "",
            "deadline": task.get("deadline") or "",
            "label": task.get("label") or "",
            "project_id": task.get("project_id") or "",
            "meeting_id": task.get("meeting_id") or "",
        }
        try:
            supabase_client.create_pending_approval(
                approval_id=f"taskmeet-{uuid.uuid4()}",
                content_type=PROPOSAL_TYPE, content=content,
                expires_at=expires)
            result["proposed"] += 1
            result["titles"].append(content["title"][:60])
        except Exception as e:                              # noqa: BLE001
            logger.warning(f"[meeting-shaped] could not store proposal for "
                           f"{task.get('id')}: {e}")

    if result["proposed"]:
        logger.info(f"[meeting-shaped] proposed {result['proposed']} task(s) "
                    f"as follow-up meetings: {result['titles']}")
    return result


def apply_meeting_shaped_proposal(content: dict) -> dict:
    """Approve one — create the follow-up meeting and close the task.

    The task is CANCELLED, not deleted: it carries a source meeting_id and the
    citation trail is the point of every extracted item. Cancelled takes it out
    of the action list and off the sheet by the normal closed-row path, and the
    row still explains where it came from.
    """
    task_id = (content or {}).get("task_id")
    title = (content or {}).get("title")
    if not task_id or not title:
        return {"ok": False, "error": "proposal carries no task"}

    try:
        created = supabase_client.create_follow_up_meeting_manual(
            title=title,
            label=content.get("label") or "",
            led_by=content.get("assignee") or "",
            proposed_date=content.get("deadline") or None,
            status="not_scheduled",
        )
    except Exception as e:                                  # noqa: BLE001
        logger.error(f"[meeting-shaped] could not create the meeting: {e}")
        return {"ok": False, "error": str(e)[:150]}

    if not created or not created.get("id"):
        return {"ok": False, "error": "meeting was not created"}

    # ORDER MATTERS. The meeting exists before the task is closed, so a failure
    # here leaves a duplicate rather than losing the commitment entirely.
    try:
        supabase_client.update_task(
            task_id, status="cancelled",
            status_reason=f"moved to the meetings pool ({created['id'][:8]})")
    except Exception as e:                                  # noqa: BLE001
        logger.error(f"[meeting-shaped] meeting {created['id']} was created but "
                     f"the task could not be closed — both now exist: {e}")
        return {"ok": True, "meeting_id": created["id"],
                "warning": "task still open"}

    return {"ok": True, "meeting_id": created["id"], "task_id": task_id}
