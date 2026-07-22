"""Generated workspace tabs: Open Questions + Areas. [2026-07-22]

Read-only views over the DB, refreshed on the reconcile cycle. They exist so
the workspace answers "what's outstanding, and where does it sit?" without
anyone having to query anything — Nechama has no DB access by design.

Both are rendered from the SAME hierarchy the rest of the workspace uses:

    Area (7)  <-  Project (canonical_projects.area_id)  <-  tasks/questions/meetings

Area is never stored per-entity; it is derived through the project. That is why
reclassifying a project moves everything under it in one edit.
"""

import logging
from collections import defaultdict
from datetime import date, datetime, timezone

from services.supabase_client import supabase_client

logger = logging.getLogger(__name__)

# Questions older than this are aged to `stale` by question_lifecycle and drop
# out of the tab. Mirrored here so the view and the aging rule can't disagree.
QUESTION_MAX_AGE_DAYS = 60
GENERAL = "General"


def _age_days(iso: str | None) -> int | None:
    if not iso:
        return None
    try:
        return (date.today() - date.fromisoformat(str(iso)[:10])).days
    except Exception:
        return None


def _project_to_area() -> dict[str, str]:
    """label(lower) -> Area name. The single Area lookup for the whole module."""
    try:
        areas = {a["id"]: a.get("name", "") for a in supabase_client.get_areas()}
        out = {}
        for p in supabase_client.get_canonical_projects(status="active"):
            name = (p.get("name") or "").strip()
            if not name:
                continue
            area = areas.get(p.get("area_id"))
            if area:
                out[name.lower()] = area
                for alias in (p.get("aliases") or []):
                    out[str(alias).lower()] = area
        return out
    except Exception as e:
        logger.warning(f"[workspace_views] project->area lookup failed: {e}")
        return {}


def _area_of(label: str | None, proj_area: dict[str, str], fallback: str = "") -> str:
    """Area for a project label, or `fallback` (a task's own category) if unknown."""
    key = (label or "").strip().lower()
    if key and key in proj_area:
        return proj_area[key]
    return fallback or GENERAL


async def build_questions_view() -> dict:
    """Refresh the Open Questions tab. Returns a summary; never raises."""
    from services.google_sheets import sheets_service

    result = {"rows": 0, "skipped_stale": 0}
    try:
        rows = (
            supabase_client.client.table("open_questions")
            # The FK must be named: open_questions has TWO relationships to
            # meetings (meeting_id and resolved_in_meeting_id), so a bare
            # `meetings(title)` is ambiguous and PostgREST refuses it (PGRST201).
            .select(
                "id, question, raised_by, status, label, created_at, "
                "meetings!open_questions_meeting_id_fkey(title)"
            )
            .eq("status", "open")
            .eq("approval_status", "approved")
            .order("created_at", desc=True)
            .limit(500)
            .execute()
            .data
            or []
        )
    except Exception as e:
        logger.error(f"[workspace_views] could not read questions: {e}")
        return {**result, "error": str(e)}

    proj_area = _project_to_area()
    out = []
    for q in rows:
        age = _age_days(q.get("created_at"))
        # Belt to the aging job's braces: if the nightly pass hasn't run yet,
        # don't show questions the tab is supposed to have retired.
        if age is not None and age > QUESTION_MAX_AGE_DAYS:
            result["skipped_stale"] += 1
            continue
        mi = q.get("meetings") if isinstance(q.get("meetings"), dict) else {}
        out.append({
            "id": q.get("id"),
            "question": q.get("question"),
            "raised_by": q.get("raised_by"),
            "label": q.get("label"),
            "age_days": age if age is not None else "",
            "source_meeting": (mi or {}).get("title", ""),
            "status": q.get("status"),
            "_area": _area_of(q.get("label"), proj_area),
        })

    # Group by Area, then oldest first inside each — the oldest unanswered
    # question in an area is the one most worth chasing.
    out.sort(key=lambda r: (r["_area"], -(r["age_days"] if isinstance(r["age_days"], int) else 0)))
    ok = await sheets_service.rebuild_questions_tab(out)
    result["rows"] = len(out)
    result["written"] = ok
    return result


async def build_areas_view() -> dict:
    """Refresh the Areas tab — the index into every other tab."""
    from services.google_sheets import sheets_service

    result = {"rows": 0}
    try:
        areas = supabase_client.get_areas()
        proj_area = _project_to_area()
        tasks = supabase_client.get_tasks(
            status=None, limit=5000, include_pending=False, include_archived=False)
        questions = (
            supabase_client.client.table("open_questions")
            .select("label, status").eq("status", "open").limit(2000).execute().data or []
        )
        meetings = supabase_client.list_follow_up_meetings(limit=2000)
    except Exception as e:
        logger.error(f"[workspace_views] could not read source data: {e}")
        return {**result, "error": str(e)}

    today = date.today()
    open_tasks: dict[str, int] = defaultdict(int)
    overdue: dict[str, int] = defaultdict(int)
    last_activity: dict[str, str] = {}

    for t in tasks:
        if (t.get("status") or "") in ("done", "archived"):
            continue
        # A task carries its OWN category, which is authoritative; the project
        # lookup only fills in when that is blank/General.
        cat = (t.get("category") or "").strip()
        area = _area_of(t.get("label"), proj_area, fallback=cat)
        open_tasks[area] += 1
        dl = t.get("deadline")
        if dl:
            try:
                if date.fromisoformat(str(dl)[:10]) < today:
                    overdue[area] += 1
            except Exception:
                pass
        upd = str(t.get("updated_at") or "")[:10]
        if upd and upd > last_activity.get(area, ""):
            last_activity[area] = upd

    open_q: dict[str, int] = defaultdict(int)
    for q in questions:
        open_q[_area_of(q.get("label"), proj_area)] += 1

    to_schedule: dict[str, int] = defaultdict(int)
    for m in meetings:
        if (m.get("status") or "not_scheduled") != "not_scheduled":
            continue
        to_schedule[_area_of(m.get("label"), proj_area)] += 1

    rendered = []
    for a in areas:
        name = a.get("name") or ""
        brief = a.get("brief_json") or {}
        focus = ""
        if isinstance(brief, dict):
            focus = (brief.get("current_focus") or brief.get("summary")
                     or brief.get("narrative") or "")
        rendered.append({
            "name": name,
            "open_tasks": open_tasks.get(name, 0),
            "overdue": overdue.get(name, 0),
            "open_questions": open_q.get(name, 0),
            "meetings_to_schedule": to_schedule.get(name, 0),
            "last_activity": last_activity.get(name, ""),
            "current_focus": focus,
        })

    # 'General' is not an `areas` row but is where uncategorised work lands —
    # showing it is the point (it is the triage bucket), so append it explicitly.
    if open_tasks.get(GENERAL) or open_q.get(GENERAL) or to_schedule.get(GENERAL):
        rendered.append({
            "name": GENERAL,
            "open_tasks": open_tasks.get(GENERAL, 0),
            "overdue": overdue.get(GENERAL, 0),
            "open_questions": open_q.get(GENERAL, 0),
            "meetings_to_schedule": to_schedule.get(GENERAL, 0),
            "last_activity": last_activity.get(GENERAL, ""),
            "current_focus": "Uncategorised — needs an Area",
        })

    rendered.sort(key=lambda r: -r["open_tasks"])
    ok = await sheets_service.rebuild_areas_tab(rendered)
    result["rows"] = len(rendered)
    result["written"] = ok
    return result


async def refresh_workspace_views() -> dict:
    """Refresh both generated tabs. Called from the reconcile cycle."""
    out: dict = {}
    try:
        out["questions"] = await build_questions_view()
    except Exception as e:
        logger.error(f"[workspace_views] questions view failed: {e}")
        out["questions"] = {"error": str(e)}
    try:
        out["areas"] = await build_areas_view()
    except Exception as e:
        logger.error(f"[workspace_views] areas view failed: {e}")
        out["areas"] = {"error": str(e)}
    return out
