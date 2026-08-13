"""Reading the Focus tab back — closing things where you actually work.

Phase C of the 2026-08-13 scope. Eyal: *"this tab i believe will be the one we
are working on in our meetings in the end … lets say i want to delete or sign as
done in this focus tab, can it be done?"*

Focus is the only view that shows everything needing action across all six
areas, so it is where a weekly review actually happens. Switching tabs
mid-meeting to close something is friction nobody accepts, and a review that
cannot record its own outcome is a meeting that has to be repeated.

WHAT IS EDITABLE HERE, AND WHAT DELIBERATELY IS NOT
---------------------------------------------------
Done, Due, Priority. **Not the assignee.**

`tasks.deadline` and `tasks.assignee` are edited daily by Nechama on the area
tabs, and are read-only on the Timeline for exactly that reason. Focus makes a
FOURTH writer on those rows, and every cross-surface defect of 2026-08 — the
rename-revert loop, the per-task manual_set_at recency bug, three permanently
divergent labels — came from two writers on one field.

Done is a status transition nothing else contends for. Due and Priority are what
actually move in a weekly review. Reassignment is rarer and the likeliest of the
three to be done in two places in one week, so it stays with its existing owner.
Eyal's call, 2026-08-13, after being shown the trade.

ABSENCE IS NEVER A DELETE
-------------------------
This is the rule that makes Focus different from every other readback in this
codebase. The Tasks tab, the area tabs and the Meetings pool all show a COMPLETE
set, so a row that vanishes means somebody removed it — and the meetings
reconcile reads that as a drop, deliberately.

Focus is a FILTERED view. Choose "Overdue only" and 70 of 84 rows disappear;
choose an owner and most of the rest do. Reading absence as intent here would
close most of the backlog the first time anyone touched a dropdown. So this
module only ever considers rows that are ON the tab, and has no delete path at
all.

THE MERGE RULE IS THE PROJECT STATUS RULE
-----------------------------------------
Not a new one — reimplementing it would mean two subtly different answers to
"did a human change this?".

    Rule 1  sheet != snapshot AND sheet != db  -> a human typed it: pull, mark manual
    Rule 2  db.manual_<field> is set           -> hold; a person decided that value
    Rule 4  otherwise sheet != db              -> push the db value to the sheet
"""

import logging
from datetime import datetime, timedelta, timezone

from processors.focus_view import (
    FCOL_DONE, FOCUS_EDITABLE, FOCUS_HIDDEN_HEADERS, HEADERS, N_FOCUS_HIDDEN,
    ROW_MEETING, ROW_TASK,
)

logger = logging.getLogger(__name__)

ISRAEL_TZ = timezone(timedelta(hours=3))

# The sheet spells priority Urgent/H/M/L; the database stores U/H/M/L. Like a
# date, it is translated on the way in and rendered on the way out, so the merge
# only ever compares canonical values — otherwise "Urgent" and "U" would look
# like a difference on every single cycle and churn the cell forever.
_PRI_TO_DB = {"URGENT": "U", "U": "U", "H": "H", "M": "M", "L": "L"}
_PRI_TO_SHEET = {"U": "Urgent", "H": "H", "M": "M", "L": "L"}

# A blank is never an instruction to erase. Clearing a due date on a filtered
# view is far more likely a stray keystroke than a decision, and Rule 1 would
# then freeze the blank as a human choice.
NEVER_BLANK = {"due"}

_TRUE = {"TRUE", "true", True, "1", "YES", "yes", "✓"}


def _is_ticked(value) -> bool:
    """A checkbox cell, whatever shape the API hands it back in."""
    if isinstance(value, bool):
        return value
    return str(value).strip() in {"TRUE", "true", "1", "YES", "yes", "✓"}


def parse_sheet_date(value: str) -> "str | None":
    """A typed date -> ISO, or None if it cannot be read.

    Refusing is the point: a cell that cannot be parsed must not be pulled in as
    a literal string, and must not be treated as a blank either.
    """
    s = str(value or "").strip()
    if not s:
        return None
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d/%m/%y", "%d.%m.%Y", "%d-%m-%Y",
                "%d %b %Y", "%d %B %Y"):
        try:
            return datetime.strptime(s, fmt).date().isoformat()
        except ValueError:
            continue
    return None


def layout_ok(header_row: list) -> bool:
    """Does this tab still have the shape the readback was written against?

    The engine must refuse an unfamiliar layout rather than parse it anyway — a
    tab on an older shape resolves the wrong column onto the wrong field and
    writes confident nonsense on every row of every cycle. Focus is the most
    exposed surface for this, because its column order is the one thing a person
    might reasonably rearrange to suit a meeting.
    """
    row = [str(c).strip() for c in (header_row or [])]
    if len(row) < len(HEADERS) + N_FOCUS_HIDDEN:
        return False
    if row[:len(HEADERS)] != HEADERS:
        return False
    return row[-N_FOCUS_HIDDEN:] == FOCUS_HIDDEN_HEADERS


class FocusPlan:
    """What the cycle would do. Built whole, then applied — or, in shadow mode,
    logged and thrown away."""

    def __init__(self):
        self.task_updates: dict[str, dict] = {}
        self.meeting_updates: dict[str, dict] = {}
        self.manual_marks: list[tuple[str, str, str]] = []   # (kind, uid, field)
        self.closed: list[tuple[str, str]] = []              # (kind, uid)
        self.conflicts: list[str] = []
        self.snapshots: list[tuple] = []                     # (kind, uid, row, settled)
        self.counters: dict[str, int] = {}

    def bump(self, key: str, n: int = 1) -> None:
        self.counters[key] = self.counters.get(key, 0) + n

    def summary(self) -> dict:
        return {"pulled": len(self.task_updates) + len(self.meeting_updates),
                "closed": len(self.closed),
                "conflicts": len(self.conflicts), **self.counters}


# Which database field each editable column maps to, per row kind. A task and a
# meeting keep their dates in different tables and different columns, which is
# exactly why `_kind` is STORED on the row rather than read off the visible Kind
# cell — that one is a value a person can retype.
_FIELD = {
    ROW_TASK: {"due": "deadline", "priority": "priority"},
    ROW_MEETING: {"due": "proposed_date", "priority": "priority"},
}


def _norm(field: str, value) -> str:
    """Comparable form. Dates compare as YYYY-MM-DD, priority as its DB letter."""
    if value in (None, ""):
        return ""
    s = str(value).strip()
    if field == "due":
        return s[:10]
    if field == "priority":
        return _PRI_TO_DB.get(s.upper(), s.upper())
    return s


def build_focus_plan(grid: list[list], tasks: dict, meetings: dict,
                     snaps: dict) -> FocusPlan:
    """Compare the tab against the merge base and the database. No I/O."""
    plan = FocusPlan()
    if not grid or len(grid) < 4:
        return plan

    if not layout_ok(grid[3]):
        logger.error(
            "[focus-readback] SKIPPED — the header row does not declare the "
            "expected layout. Rebuild the tab before the engine touches it.")
        plan.bump("layout_mismatch")
        return plan

    n_cols = len(grid[3])
    uid_col, kind_col = n_cols - N_FOCUS_HIDDEN, n_cols - N_FOCUS_HIDDEN + 1

    for row_number, row in enumerate(grid, start=1):
        if row_number <= 4 or len(row) <= kind_col:
            continue
        kind = str(row[kind_col]).strip()
        uid = str(row[uid_col]).strip()
        if kind not in (ROW_TASK, ROW_MEETING) or not uid:
            continue

        source = tasks if kind == ROW_TASK else meetings
        db_row = source.get(uid)
        if not db_row:
            # A row whose item is gone. Not ours to clean up — and NEVER read as
            # a deletion: this is a filtered view.
            plan.bump("orphan_rows")
            continue

        snap = snaps.get((kind, uid)) or {}
        settled: dict = {}
        updates: dict = {}

        # --- Done: a TRANSITION, not a value -------------------------------
        #
        # Focus lists only OPEN work, so a row being here already means it is not
        # closed, and the render always writes the box unticked. That makes the
        # merge rules unnecessary in one direction and wrong in the other: there
        # is no "database value" to push back, because an item with a value worth
        # pushing would not be on the tab at all.
        #
        # So ticking CLOSES, and unticking does nothing. Unticking a box the
        # render just drew is not a request to reopen anything — the item was
        # never closed — and treating it as one would let a stray click resurrect
        # work on a tab where rows move under the cursor every time a dropdown
        # changes.
        if _is_ticked(row[FCOL_DONE] if FCOL_DONE < len(row) else ""):
            plan.closed.append((kind, uid))
            plan.bump("closed")
            # Nothing else on a row being closed is worth merging: the values
            # are about to stop mattering, and pulling a half-typed date on the
            # way out is how a tidy-up writes something nobody meant.
            plan.snapshots.append((kind, uid, row_number, settled))
            continue

        for col, field in FOCUS_EDITABLE.items():
            if field == "done":
                continue
            raw = str(row[col]).strip() if col < len(row) else ""
            db_field = _FIELD[kind][field]

            if field == "due" and raw:
                parsed = parse_sheet_date(raw)
                if not parsed:
                    # Unreadable: leave the database alone and say so. Pulling
                    # the literal string poisons a DATE column; treating it as
                    # blank silently discards what was typed.
                    plan.bump("unparsed_dates")
                    plan.conflicts.append(
                        f"row {row_number}: due — cannot read {raw!r}")
                    settled[field] = _norm(field, db_row.get(db_field))
                    continue
                sheet_cmp = parsed
            elif field == "priority" and raw:
                letter = _PRI_TO_DB.get(raw.upper())
                if not letter:
                    # Costs the CELL, never the row — a bad priority must not
                    # take a legitimate date edit on the same line down with it.
                    plan.bump("bad_priorities")
                    plan.conflicts.append(
                        f"row {row_number}: priority — {raw!r} is not "
                        "Urgent/H/M/L")
                    settled[field] = _norm(field, db_row.get(db_field))
                    continue
                sheet_cmp = letter
            else:
                sheet_cmp = raw

            snap_val = _norm(field, snap.get(field))
            db_val = _norm(field, db_row.get(db_field))

            # NO MERGE BASE MEANS NO EDIT. With no snapshot every populated cell
            # differs from "", so the whole row would be pulled in and frozen as
            # human decisions — the guard the sibling rail shipped without.
            edited = bool(snap) and sheet_cmp != snap_val and sheet_cmp != db_val

            if edited and field in NEVER_BLANK and not sheet_cmp:
                plan.bump("blanks_refused")
                edited = False

            if edited:
                updates[db_field] = sheet_cmp or None
                plan.manual_marks.append((kind, uid, db_field))
                plan.bump("pulled")
                settled[field] = sheet_cmp
            elif sheet_cmp != db_val:
                if db_row.get(f"manual_{db_field}"):
                    plan.bump("manual_held")            # Rule 2
                    settled[field] = sheet_cmp
                else:
                    plan.bump("stale_cell")             # Rule 4
                    settled[field] = db_val
            else:
                settled[field] = sheet_cmp

        if updates:
            target = (plan.task_updates if kind == ROW_TASK
                      else plan.meeting_updates)
            target.setdefault(uid, {}).update(updates)
        plan.snapshots.append((kind, uid, row_number, settled))

    return plan


async def reconcile_focus(spreadsheet_id: str | None = None) -> dict:
    """Read the Focus tab, decide, and (unless shadowed) apply."""
    from config.settings import settings
    from processors.focus_view import FOCUS_TAB
    from services.google_sheets import sheets_service
    from services.supabase_client import supabase_client

    ssid = spreadsheet_id or settings.PROJECT_STATUS_SHEET_ID
    if not ssid:
        return {"skipped": "no PROJECT_STATUS_SHEET_ID"}

    try:
        resp = sheets_service._execute_with_retry(
            lambda: sheets_service.service.spreadsheets().values().get(
                spreadsheetId=ssid, range=f"'{FOCUS_TAB}'!A1:Z1000"))
        grid = resp.get("values") or []
    except Exception as e:                                   # noqa: BLE001
        logger.warning(f"[focus-readback] could not read the tab: {e}")
        return {"skipped": "unreadable"}

    if not grid:
        # A transient read returns empty without raising. Treating that as "every
        # row was deleted" would be catastrophic anywhere; here it would also be
        # meaningless, because absence on Focus is never a delete.
        logger.warning("[focus-readback] the tab read empty — changing nothing")
        return {"skipped": "empty read"}

    c = supabase_client.client
    tasks = {t["id"]: t for t in
             (c.table("tasks").select("*").limit(5000).execute()).data or []}
    meetings = {m["id"]: m for m in
                (c.table("follow_up_meetings").select("*")
                 .limit(2000).execute()).data or []}
    snaps = supabase_client.get_focus_snapshots()

    plan = build_focus_plan(grid, tasks, meetings, snaps)
    shadow = getattr(settings, "FOCUS_SHADOW_MODE", True)
    out = {**plan.summary(), "shadow": shadow}

    for line in plan.conflicts:
        logger.warning(f"[focus-readback] {line}")

    if shadow:
        if plan.task_updates or plan.meeting_updates or plan.closed:
            logger.info(
                f"[focus-readback] SHADOW would update "
                f"{len(plan.task_updates)} task(s), "
                f"{len(plan.meeting_updates)} meeting(s) and close "
                f"{len(plan.closed)}")
        logger.info(f"[focus-readback] {out}")
        return out

    failed = set()
    for uid, updates in plan.task_updates.items():
        try:
            supabase_client.update_task(uid, **updates)
        except Exception as e:                               # noqa: BLE001
            logger.error(f"[focus-readback] task {uid}: {e}")
            failed.add(("task", uid))
    for uid, updates in plan.meeting_updates.items():
        try:
            supabase_client.update_follow_up_meeting(uid, **updates)
        except Exception as e:                               # noqa: BLE001
            logger.error(f"[focus-readback] meeting {uid}: {e}")
            failed.add(("meeting", uid))

    # Closing LAST, so a row that also carried a failed field edit is not marked
    # done on the strength of a write that did not land.
    for kind, uid in plan.closed:
        if (kind, uid) in failed:
            continue
        try:
            if kind == ROW_TASK:
                supabase_client.update_task(uid, status="done")
            else:
                # A meeting is not "done", it is HELD — and held_at is stamped by
                # the trigger, so the fortnight timer starts here.
                supabase_client.update_follow_up_meeting(uid, status="held")
        except Exception as e:                               # noqa: BLE001
            logger.error(f"[focus-readback] closing {kind} {uid}: {e}")
            failed.add((kind, uid))

    for kind, uid, field in plan.manual_marks:
        if (kind, uid) in failed:
            continue
        try:
            if kind == ROW_TASK:
                supabase_client.mark_task_field_manual(uid, field, "focus")
            else:
                supabase_client.mark_meeting_field_manual(uid, field, "focus")
        except Exception as e:                               # noqa: BLE001
            logger.warning(f"[focus-readback] manual mark {field}: {e}")

    # Snapshots LAST, and only for rows whose write succeeded — a base written
    # for a failed row would make the next cycle believe the value had landed.
    for kind, uid, row_number, settled in plan.snapshots:
        if (kind, uid) in failed:
            continue
        supabase_client.upsert_focus_snapshot(
            kind, uid, sheet_row=row_number,
            due=settled.get("due") or None,
            priority=settled.get("priority") or None)

    logger.info(f"[focus-readback] {out}")
    return out
