"""Sync a single sheet surface on demand, in seconds rather than on the cycle.

Eyal, 2026-08-13: *"the thing i feel that is a bit missing is the 'cross system
and cross tabs' liveliness of the system and that it is not responsive … if we
change dates on the timeline or in the project tabs i will want to see it
happening live … if not nechama might think that it is not working."*

That is not a cosmetic complaint. A thirty-minute cycle is indistinguishable
from a broken one: there is no way to tell "not yet" from "never", so the honest
reading of a sheet that has not changed is that the system is down. Nechama edits
that workbook daily and has no other signal.

WHAT THIS IS. One authenticated endpoint that runs the reconcile for ONE surface
and returns its summary. An Apps Script `onEdit` trigger in the workbook calls
it (see `scripts/apps_script_sync.gs`) and writes the result into a status cell,
so an edit produces visible confirmation a few seconds later.

WHY IT CANNOT LOOP. Google's `onEdit` fires on USER edits only — changes written
through the Sheets API do not fire it. So the reconcile's own writes cannot
re-trigger the script. The script additionally ignores edits to the status cell
it writes itself, because a guard that costs one comparison is cheaper than
reasoning about trigger semantics every time this file is read.

WHAT IT IS NOT. It does not replace the scheduled cycle. The interval stays as
the backstop: a webhook that silently stops firing must not mean a workbook that
silently stops syncing, and that is exactly the failure a person would not
notice.
"""

import asyncio
import logging
import time

from config.settings import settings

logger = logging.getLogger(__name__)

# One lock for every path that writes a sheet. The scheduled reconcile and a
# webhook firing at the same moment would interleave a read-modify-write on the
# same rows — the merge would compare against a half-applied state and could pull
# the system's own in-flight write back in as a human edit. Everything that
# reconciles takes this.
SHEET_LOCK = asyncio.Lock()

# Per-surface floor between runs. `onEdit` fires once per cell commit, so pasting
# a column emits a burst; without this each cell would queue its own full
# reconcile and the workbook would be locked for minutes. A run inside the floor
# is COALESCED, not rejected — the reconcile that follows reads the sheet whole,
# so it necessarily picks up every edit in the burst.
_last_run: dict[str, float] = {}

# Which tab maps to which reconcile. Keyed on the tab name the script sends, so
# an unknown tab is a no-op rather than a guess at what to run.
SURFACE_TIMELINE = "timeline"
SURFACE_PROJECT_STATUS = "project_status"
SURFACE_MEETINGS = "meetings"
SURFACE_PROJECTS = "projects"
SURFACE_FOCUS = "focus"

_AREA_TABS = (
    "PRODUCT & TECHNOLOGY", "SALES & BUSINESS DEVELOPMENT",
    "CLIENT DELIVERY & OPERATIONS", "FUNDRAISING & INVESTOR RELATIONS",
    "LEGAL, CORPORATE & FINANCE", "TEAM & HUMAN RESOURCES",
)


def surface_for_tab(tab: str) -> "str | None":
    """Which reconcile owns this tab, or None if nothing does.

    Returning None for an unrecognised tab is deliberate: the alternative is
    running every reconcile on every edit, which turns a typo on the 'How to
    use' tab into a full workbook pass.
    """
    name = (tab or "").strip()
    if not name:
        return None
    if name == "Timeline":
        return SURFACE_TIMELINE
    if name in ("Meetings", "Past Meetings"):
        return SURFACE_MEETINGS
    if name == "Projects":
        return SURFACE_PROJECTS
    if name in ("Focus", "Focus data"):
        return SURFACE_FOCUS
    if name in _AREA_TABS:
        return SURFACE_PROJECT_STATUS
    return None


async def _run_surface(surface: str) -> dict:
    """Run exactly the reconcile that owns this surface. No fan-out."""
    if surface == SURFACE_TIMELINE:
        from processors.timeline_readback import reconcile_timeline
        from services.timeline_sheet import refresh_timeline

        # READ BEFORE RENDER, the same order the scheduler uses. The render
        # rewrites every cell from the database, so refreshing first would
        # overwrite what was just typed before anything had looked at it.
        out = {}
        if getattr(settings, "TIMELINE_READBACK_ENABLED", False):
            out["readback"] = await reconcile_timeline()
        if getattr(settings, "TIMELINE_VIEW_ENABLED", False):
            out["render"] = await refresh_timeline()
        return out

    if surface == SURFACE_MEETINGS:
        from processors.sheets_sync import reconcile_meetings
        return {"meetings": await reconcile_meetings()}

    if surface == SURFACE_PROJECTS:
        from processors.sheets_sync import reconcile_projects
        return {"projects": await reconcile_projects()}

    if surface == SURFACE_PROJECT_STATUS:
        from processors.project_status_reconcile import reconcile_project_status
        # NO SLOT. Structural work — inserting and deleting rows — is confined
        # to the quiet slots because shifting rows under someone who is typing
        # is the one thing a working document must not do, and a webhook fires
        # precisely while they are typing. Values only.
        return {"project_status": await reconcile_project_status(slot=None)}

    if surface == SURFACE_FOCUS:
        from services.focus_sheet import refresh_focus
        return {"focus": await refresh_focus()}

    return {"skipped": f"unknown surface {surface!r}"}


async def sync_surface(tab: str) -> dict:
    """Entry point for the webhook. Returns what happened, for the status cell."""
    if not getattr(settings, "SHEET_SYNC_ENABLED", False):
        return {"ok": False, "reason": "sync disabled"}

    surface = surface_for_tab(tab)
    if surface is None:
        return {"ok": True, "surface": None, "reason": f"{tab!r} is not synced"}

    floor = int(getattr(settings, "SHEET_SYNC_MIN_INTERVAL_SECONDS", 10) or 10)
    now = time.monotonic()
    last = _last_run.get(surface, 0.0)
    if now - last < floor:
        # Coalesced, and reported as success: the run that just happened (or is
        # happening) reads the sheet whole, so this edit is already covered.
        # Telling the person "rate limited" would be a lie about their data.
        return {"ok": True, "surface": surface, "coalesced": True}

    async with SHEET_LOCK:
        # Re-check under the lock: a burst can queue several callers here, and
        # the one that waited should not run a second pass over work the first
        # already did.
        now = time.monotonic()
        if now - _last_run.get(surface, 0.0) < floor:
            return {"ok": True, "surface": surface, "coalesced": True}
        _last_run[surface] = now
        try:
            result = await _run_surface(surface)
        except Exception as e:                                # noqa: BLE001
            logger.error(f"[sheet-sync] {surface} failed: {e}")
            return {"ok": False, "surface": surface, "error": str(e)[:200]}

    logger.info(f"[sheet-sync] {surface} <- {tab!r}: {result}")
    return {"ok": True, "surface": surface, "result": result}


def summarise(result: dict) -> str:
    """A single line for the sheet's status cell.

    Written for someone who is not going to read a JSON blob: it says what
    changed, or that nothing did, in words.
    """
    if not result.get("ok"):
        return f"sync failed — {result.get('error') or result.get('reason') or 'unknown'}"
    if result.get("coalesced"):
        return "synced"
    if result.get("surface") is None:
        return ""
    pulled = pushed = 0
    for part in (result.get("result") or {}).values():
        if isinstance(part, dict):
            pulled += int(part.get("pulled") or 0)
            pushed += int(part.get("pushed") or 0)
    if pulled and pushed:
        return f"synced — {pulled} saved, {pushed} refreshed"
    if pulled:
        return f"synced — {pulled} change{'s' if pulled != 1 else ''} saved"
    if pushed:
        return f"synced — {pushed} cell{'s' if pushed != 1 else ''} refreshed"
    return "synced — nothing to change"
