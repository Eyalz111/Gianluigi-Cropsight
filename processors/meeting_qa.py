"""Repeatable post-distribution QA for the meeting loop. [2026-07-24]

Verifies that an APPROVED meeting's children reached the Sheet consistently —
the DB <-> Sheet <-> distribution chain that used to be spot-checked by hand.
Reusable, so it runs in two places:

  1. Immediately after every distribution (`verify_meeting_chain`) — an on-the-spot
     self-check on the exact meeting that was just distributed, so an edit that
     didn't propagate is caught within seconds, not days.
  2. A daily sweep (`verify_recent_meetings`) over meetings approved in the last N
     hours, folded into the QA scheduler's report (which only DMs Eyal on a NEW
     issue set).

Checks, per meeting:
  - PRESENCE : every approved task / decision / follow-up in the DB has a row on
    its tab (matched by the col-J / col-H UUID). A follow-up may live on the
    Meetings tab OR Past Meetings (archived) — either counts.
  - DUPLICATE: no follow-up UUID appears on more than one Meetings-tab row (the
    blind-append class fixed 2026-07-24 — this is the regression tripwire).
  - DRIFT    : the DB title == the Sheet title for each child (an edit that
    reached one side but not the other — e.g. a rename that hit the sheet but
    not the DB row).

Pure-ish: all I/O is confined to the two DB/Sheet reads; the comparison logic is
deterministic and unit-tested via a fabricated sheet_index.
"""

from __future__ import annotations

import logging

from config.settings import settings

logger = logging.getLogger(__name__)


def _norm(t: object) -> str:
    return " ".join(str(t or "").split()).strip().lower()


async def read_sheet_index() -> dict:
    """Read the workspace tabs ONCE into {tab: {uuid: [titles]}} maps so a sweep
    over many meetings costs a single set of reads. Titles are lists to expose
    duplicate UUIDs (len > 1)."""
    from services.google_sheets import (
        sheets_service, TASK_COL_INDEX, DECISION_ID_COLUMN, DECISION_COL_INDEX,
        MEETING_COL_INDEX, MEETING_TAB_NAME, MEETINGS_ARCHIVE_TAB_NAME,
    )
    sid = settings.TASK_TRACKER_SHEET_ID
    tab = settings.TASK_TRACKER_TAB_NAME or "Tasks"

    def _index(rows, id_i, title_i):
        out: dict[str, list] = {}
        for r in rows or []:
            uid = (r[id_i].strip() if len(r) > id_i and r[id_i] else "")
            if uid:
                out.setdefault(uid, []).append(r[title_i] if len(r) > title_i else "")
        return out

    tr = await sheets_service._read_sheet_range(sheet_id=sid, range_name=f"'{tab}'!A2:L")
    dr = await sheets_service._read_sheet_range(sheet_id=sid, range_name="'Decisions'!A2:H")
    mr = await sheets_service._read_sheet_range(sheet_id=sid, range_name=f"'{MEETING_TAB_NAME}'!A2:J")
    pr = await sheets_service._read_sheet_range(sheet_id=sid, range_name=f"'{MEETINGS_ARCHIVE_TAB_NAME}'!A2:J")

    d_id_i = ord(DECISION_ID_COLUMN) - ord("A")
    return {
        "tasks": _index(tr, TASK_COL_INDEX["id"], TASK_COL_INDEX["task"]),
        "decisions": _index(dr, d_id_i, DECISION_COL_INDEX["decision"]),
        "meetings": _index(mr, MEETING_COL_INDEX["id"], MEETING_COL_INDEX["title"]),
        "past_meetings": _index(pr, MEETING_COL_INDEX["id"], MEETING_COL_INDEX["title"]),
    }


def _check_group(db_rows, id_of, title_of, sheet_map, kind, *, extra_maps=None):
    """Compare one child type's DB rows against its sheet map(s). Returns issues."""
    issues = []
    for r in db_rows:
        rid = id_of(r)
        if not rid:
            continue
        present = rid in sheet_map or any(rid in m for m in (extra_maps or []))
        if not present:
            issues.append(f"{kind} missing from sheet: “{str(title_of(r))[:44]}” ({rid[:8]})")
            continue
        rows_here = sheet_map.get(rid, [])
        if len(rows_here) > 1:
            issues.append(f"{kind} DUPLICATED on sheet ({len(rows_here)} rows): “{str(title_of(r))[:44]}” ({rid[:8]})")
        # Drift: DB title vs the sheet title (only meaningful when it's on this tab)
        if rows_here and _norm(rows_here[0]) != _norm(title_of(r)):
            issues.append(
                f"{kind} title DRIFT ({rid[:8]}): DB “{str(title_of(r))[:32]}” ≠ sheet “{str(rows_here[0])[:32]}”"
            )
    return issues


async def verify_meeting_chain(meeting_id: str, sheet_index: dict | None = None) -> dict:
    """Verify one approved meeting's DB children all reached the Sheet consistently.

    Returns {meeting_id, title, ok, issues[], counts{}}. Never raises — a read
    failure is reported as an issue, not an exception (this runs in the
    distribution hot path and must not break a send).
    """
    from services.supabase_client import supabase_client as sc
    result = {"meeting_id": meeting_id, "title": "", "ok": True, "issues": [], "counts": {}}
    try:
        m = sc.client.table("meetings").select("title,approval_status").eq("id", meeting_id).execute().data
        if not m:
            result["ok"] = False
            result["issues"].append("meeting row not found")
            return result
        result["title"] = m[0].get("title") or ""

        tasks = [t for t in sc.get_tasks(meeting_id=meeting_id, include_pending=False, status=None, limit=500)
                 if (t.get("status") or "") != "archived"]
        decisions = sc.list_decisions(meeting_id=meeting_id, include_pending=False)
        follow_ups = sc.list_follow_up_meetings(source_meeting_id=meeting_id, include_pending=False, limit=200)

        idx = sheet_index or await read_sheet_index()
        result["counts"] = {"tasks": len(tasks), "decisions": len(decisions), "follow_ups": len(follow_ups)}

        result["issues"] += _check_group(
            tasks, lambda r: r.get("id"), lambda r: r.get("title"), idx["tasks"], "task")
        result["issues"] += _check_group(
            decisions, lambda r: r.get("id"), lambda r: r.get("description") or r.get("decision"),
            idx["decisions"], "decision")
        result["issues"] += _check_group(
            follow_ups, lambda r: r.get("id"), lambda r: r.get("title"), idx["meetings"],
            "follow-up", extra_maps=[idx["past_meetings"]])

        result["ok"] = not result["issues"]
    except Exception as e:
        result["ok"] = False
        result["issues"].append(f"verify error: {e}")
        logger.warning(f"[meeting-qa] verify failed for {meeting_id}: {e}")
    return result


async def verify_recent_meetings(hours: int = 48) -> dict:
    """Daily sweep: verify every meeting approved in the last `hours`. Reads the
    sheet ONCE and checks all of them against it. Returns
    {checked, failing: [ {meeting_id,title,issues} ], issue_lines: [str]}."""
    from services.supabase_client import supabase_client as sc
    from datetime import datetime, timezone, timedelta
    out = {"checked": 0, "failing": [], "issue_lines": []}
    try:
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
        rows = (sc.client.table("meetings").select("id,title,approved_at")
                .eq("approval_status", "approved").gte("approved_at", cutoff)
                .order("approved_at", desc=True).limit(50).execute().data) or []
        if not rows:
            return out
        idx = await read_sheet_index()
        for m in rows:
            r = await verify_meeting_chain(m["id"], sheet_index=idx)
            out["checked"] += 1
            if not r["ok"]:
                out["failing"].append({"meeting_id": m["id"], "title": r["title"], "issues": r["issues"]})
                for iss in r["issues"]:
                    out["issue_lines"].append(f"{(r['title'] or m['id'])[:30]}: {iss}")
    except Exception as e:
        logger.warning(f"[meeting-qa] recent sweep failed: {e}")
        out["issue_lines"].append(f"meeting-qa sweep error: {e}")
    return out
