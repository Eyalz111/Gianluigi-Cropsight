"""When did a project actually start?

Phase 1 of docs/GANTT_V2_PLAN.md. A Gantt bar needs a left edge, and nothing in
this database records one — every date here is an END.

THE RULE, in priority order:

    1. manual_start_date  -> use start_date          (a human decided; final)
    2. start_date is set  -> use it                  (already frozen)
    3. otherwise          -> earliest task's created_at, THEN FREEZE IT

Freezing is the whole point of step 3. "Earliest task, evaluated on every
render" moves backwards the moment an older task is attached to the project, so
the bar silently redraws and reads as a plan change nobody made.

WHY NOT canonical_projects.created_at, the obvious choice: measured on live
data it clusters 23 projects onto 3 days, 15 of them on 2026-08-07 — the day
the Projects table was built. It records when the ROW was created, not when the
work began. The earliest TASK spreads across 14 distinct days and reads
plausibly (Business Plan 2026-03-26, Cloud Infrastructure 2026-07-22).

WHY THE OLD GANTT IS SHOWN BUT NOT TRUSTED: where its bars match, they are
often BETTER — Legal starts 2026-03-02 there against a first task of
2026-04-09, because the board records when planning began rather than when a
transcript first mentioned the work. But matching bars to projects by name is
unreliable in a way that looks confident: "Corporate" matches a recurring admin
line dated 2027-05-31, and four different projects all match one "MVP Product
Delivery" bar. So the candidate is DISPLAYED on the card as evidence and never
recommended. Eyal reads it and decides.
"""

import logging
import re
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from services.supabase_client import supabase_client

logger = logging.getLogger(__name__)

PROPOSAL_TYPE = "project_start_proposal"
ISRAEL_TZ = timezone(timedelta(hours=3))

# Words that carry no identity when matching a project name to a board label.
_STOP = {"the", "and", "of", "for", "a", "to", "in", "with", "on", "amp",
         "sow", "others", "new", "work"}
_TAG = re.compile(r"^\s*\[[^\]]*\]\s*")
# Lanes whose cells hold counts and absences rather than work.
_NOISE_LANES = ("Meeting Count", "All Meetings", "Availability")


def _day(ts) -> "datetime.date | None":
    if not ts:
        return None
    try:
        return (datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
                .astimezone(ISRAEL_TZ).date())
    except (ValueError, TypeError):
        return None


def _tokens(text: str) -> set:
    return {w for w in re.findall(r"[a-z0-9]+", _TAG.sub("", text).lower())
            if w not in _STOP and len(w) > 2}


def earliest_task_start(project_id: str, tasks: list[dict]) -> "datetime.date | None":
    """The earliest created_at among a project's tasks — approved or not.

    Approval status is deliberately ignored. A task pending approval still
    marks when the work surfaced, and filtering to approved-only would move the
    start later for any project whose first extraction is still in the queue.
    """
    days = [_day(t.get("created_at")) for t in tasks
            if t.get("project_id") == project_id]
    days = [d for d in days if d]
    return min(days) if days else None


def gantt_candidate(project_name: str, area_name: str,
                    bars: list[dict]) -> dict | None:
    """The best-matching bar from the archived board, or None.

    Returned as EVIDENCE for the card, never as a recommendation — see the
    module docstring for why name matching cannot be trusted here.
    """
    want = _tokens(project_name)
    if not want:
        return None
    best, score = None, 0
    for b in bars:
        if any(n in (b.get("lane") or "") for n in _NOISE_LANES):
            continue
        if area_name and (b.get("section") or "")[:10].lower() not in area_name.lower():
            continue
        overlap = len(want & _tokens(b.get("label") or ""))
        if overlap > score or (overlap == score and best
                               and overlap and b["start_date"] < best["start_date"]):
            score, best = overlap, b
    if not best or not score:
        return None
    return {"date": best["start_date"], "label": best["label"][:120],
            "lane": best.get("lane"), "overlap": score}


def propose_project_starts() -> dict:
    """Emit one proposal per active project that has no start date yet.

    Nothing is written to canonical_projects here. Approving the proposal is
    what sets the date — the same gate every other suggestion in this system
    passes through.
    """
    c = supabase_client.client
    # RETIRED PROJECTS ARE INCLUDED. Eyal asked for them to stay on the
    # Timeline in the Completed colour, and a bar needs a left edge like any
    # other — skipping them meant they could never be drawn at all. They are
    # also the case where the archived board is the ONLY possible source: a
    # retired project typically has no open tasks to derive from. [2026-08-12]
    projects = (c.table("canonical_projects").select("*").execute()).data or []
    areas = {a["id"]: a["name"] for a in
             (c.table("areas").select("id,name").execute()).data or []}
    tasks = (c.table("tasks").select("id,project_id,created_at")
             .limit(5000).execute()).data or []
    try:
        bars = (c.table("gantt_legacy_bars").select("*").limit(1000)
                .execute()).data or []
    except Exception as e:                                   # noqa: BLE001
        logger.warning(f"[project-start] legacy bars unavailable: {e}")
        bars = []

    existing = {
        (a.get("content") or {}).get("project_id")
        for a in (supabase_client.get_pending_approvals_by_status("pending") or [])
        if a.get("content_type") == PROPOSAL_TYPE
    }

    out = {"proposed": 0, "already_set": 0, "already_pending": 0, "no_signal": 0}
    for p in projects:
        if p.get("start_date"):
            out["already_set"] += 1
            continue
        if p["id"] in existing:
            out["already_pending"] += 1
            continue

        derived = earliest_task_start(p["id"], tasks)
        cand = gantt_candidate(p["name"], areas.get(p.get("area_id"), ""), bars)
        if not derived and not cand:
            # Nothing to go on. Better to leave it blank than invent a date —
            # the bar renders with no left edge until a human sets one.
            out["no_signal"] += 1
            continue

        content = {
            "project_id": p["id"],
            "project_name": p["name"],
            "area": areas.get(p.get("area_id"), ""),
            "recommended": str(derived) if derived else None,
            "recommended_source": "earliest_task" if derived else None,
            "gantt_date": cand["date"] if cand else None,
            "gantt_label": cand["label"] if cand else None,
        }
        supabase_client.upsert_pending_approval(
            approval_id=f"pstart-{p['id']}",
            content_type=PROPOSAL_TYPE,
            content=content,
        )
        out["proposed"] += 1

    logger.info(f"[project-start] {out}")
    return out


def apply_project_start(content: dict) -> dict:
    """Approve: freeze the recommended start date onto the project.

    `manual_start_date` is set because a human approved this specific date —
    that is a decision, and Rule 2 must defend it from later inference. It is
    NOT set when nothing was approved.
    """
    project_id = content.get("project_id")
    start = content.get("recommended")
    if not project_id:
        return {"ok": False, "error": "no project_id"}
    if not start:
        return {"ok": False, "error": "proposal carries no recommended date"}
    try:
        supabase_client.client.table("canonical_projects").update({
            "start_date": start,
            "manual_start_date": True,
        }).eq("id", project_id).execute()
    except Exception as e:                                   # noqa: BLE001
        logger.error(f"[project-start] apply failed for {project_id}: {e}")
        return {"ok": False, "error": str(e)}
    return {"ok": True, "project_id": project_id, "start_date": start}
