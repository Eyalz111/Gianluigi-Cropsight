"""Reading the Timeline back — what Eyal typed, and what to do about it.

Phase 4b of docs/GANTT_V2_PLAN.md. Eyal: *"if I change in the new gantt
something like responsible or due dates, I will want to see it in the sheet of
the project status (and the DB of course)."*

THE MERGE RULE IS THE PROJECT STATUS RULE, NOT A NEW ONE. Reimplementing it
would mean two subtly different answers to "did a human change this?", and the
whole cross-surface defect family of 2026-08 came from surfaces disagreeing:

    Rule 1  sheet ≠ snapshot AND sheet ≠ db  -> a human typed it. Pull to the
                                               DB and set the manual rail.
    Rule 2  db.manual_<field> is set         -> hold. The DB value was decided
                                               by a person; do not overwrite.
    Rule 4  otherwise, sheet ≠ db            -> push the DB value to the sheet.

NO MERGE BASE MEANS NO EDIT. With no snapshot every populated cell differs from
None, so a row whose snapshot failed to write would have its entire contents
pulled in and frozen as human decisions. This is the guard the sibling rail
shipped without, found in the 2026-08-08 review.

PROJECT ROWS ONLY. `start_date`, `target_date`, `owner` — the three fields whose
only real home is this tab or a tab nobody visits. Task rows are read-only here:
`tasks.deadline` and `tasks.assignee` are edited daily by Nechama on the area
tabs, and putting a second writer on those rows is what produced the
rename-revert loop, the manual_set_at recency bug, and three permanently
divergent labels.

CONFLICTS ARE REPORTED, NEVER GUESSED. If the Timeline wants to set a value and
another surface is already showing a different one, both are human intentions
and the machine has no basis for choosing. Two people disagreeing is a question,
not a merge.
"""

import logging
from datetime import datetime, timedelta, timezone

from config.settings import settings
from processors.sheet_format import display_date
from processors.timeline_view import (
    HEADERS, HIDDEN_HEADERS, N_HIDDEN, N_LABEL_COLS, ROW_PROJECT, TIMELINE_TAB,
)
from services.supabase_client import supabase_client

logger = logging.getLogger(__name__)

ISRAEL_TZ = timezone(timedelta(hours=3))

# Sheet column index -> database field. Deliberately short: these are the only
# fields the Timeline is allowed to own. Column 0 is the project name (renaming
# a project from a Gantt row is a different decision), and column 4 shows
# "retired", which is a status derived elsewhere.
EDITABLE = {1: "owner", 2: "start_date", 3: "target_date"}
_DATE_FIELDS = {"start_date", "target_date"}

# A blank cell is never an instruction to erase. Clearing a start date would
# remove the bar's left edge entirely, which is far more likely to be an
# accidental deletion than a decision — and Rule 1 would then freeze the blank
# as a manual choice.
NEVER_BLANK = {"start_date", "target_date", "owner"}


class ReadbackPlan:
    """What the cycle would do. Built whole, then applied — or, in shadow mode,
    logged and thrown away."""

    def __init__(self):
        self.updates: list[tuple[str, dict]] = []       # (project_id, {field: value})
        self.manual_marks: list[tuple[str, str]] = []   # (project_id, field)
        self.cell_writes: list[tuple[int, int, str]] = []  # (row, col, value)
        self.conflicts: list[str] = []
        self.snapshots: list[tuple[str, int, dict]] = []
        self.counters: dict[str, int] = {}

    def bump(self, key: str, n: int = 1) -> None:
        self.counters[key] = self.counters.get(key, 0) + n

    def summary(self) -> dict:
        return {"pulled": len(self.updates), "pushed": len(self.cell_writes),
                "conflicts": len(self.conflicts), **self.counters}


def _norm(field: str, value) -> str:
    """Comparable form. Dates compare as YYYY-MM-DD, everything else stripped."""
    if value in (None, ""):
        return ""
    s = str(value).strip()
    if field in _DATE_FIELDS:
        try:
            return datetime.fromisoformat(s[:10]).date().isoformat()
        except (ValueError, TypeError):
            return s
    return s


def parse_sheet_date(value: str) -> "str | None":
    """A typed date -> ISO, or None if it cannot be read.

    Refusing is the point: a cell that cannot be parsed must not be pulled in as
    a literal string, and must not be treated as a blank either.
    """
    s = (value or "").strip()
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

    The engine must refuse an unfamiliar layout rather than parse it anyway. A
    tab on an older shape resolves the wrong column onto the wrong field and
    writes confident nonsense on every row of every cycle — precisely what the
    Project Status `unresolved_columns` gate exists to stop.
    """
    row = list(header_row or [])
    if len(row) < N_LABEL_COLS + N_HIDDEN:
        return False
    if [str(c).strip() for c in row[:N_LABEL_COLS]] != HEADERS:
        return False
    return [str(c).strip() for c in row[-N_HIDDEN:]] == HIDDEN_HEADERS


def _other_surface_value(project_id: str, field: str, others: dict):
    """What another editable surface currently shows for this field, if any."""
    for snaps in others:
        snap = snaps.get(project_id)
        if snap and snap.get(field) not in (None, ""):
            return snap.get(field)
    return None


def build_plan(grid: list[list], projects: dict, snaps: dict,
               others: tuple = ()) -> ReadbackPlan:
    """Compare the tab against the merge base and the database. No I/O."""
    plan = ReadbackPlan()
    if not grid:
        return plan

    header = grid[3] if len(grid) > 3 else []
    if not layout_ok(header):
        logger.error(
            "[timeline-readback] SKIPPED — the header row does not declare the "
            "expected layout. Rebuild the tab before the engine touches it.")
        plan.bump("layout_mismatch")
        return plan

    n_cols = len(header)
    uid_col, kind_col = n_cols - N_HIDDEN, n_cols - N_HIDDEN + 1

    for row_number, row in enumerate(grid, start=1):
        if len(row) <= kind_col:
            continue
        if str(row[kind_col]).strip() != ROW_PROJECT:
            continue
        uid = str(row[uid_col]).strip()
        if not uid:
            plan.bump("rows_without_uid")
            continue
        db_row = projects.get(uid)
        if not db_row:
            # A row whose project is gone. Not ours to clean up here.
            plan.bump("orphan_rows")
            continue

        snap = snaps.get(uid) or {}
        settled: dict = {}
        updates: dict = {}

        for col, field in EDITABLE.items():
            raw = str(row[col]).strip() if col < len(row) else ""
            if field in _DATE_FIELDS and raw:
                parsed = parse_sheet_date(raw)
                if not parsed:
                    # Unreadable: leave the database alone and say so. Pulling
                    # the literal string would poison a DATE column; treating it
                    # as blank would silently discard what was typed.
                    plan.bump("unparsed_dates")
                    plan.conflicts.append(
                        f"row {row_number}: {field} — cannot read {raw!r}")
                    settled[field] = _norm(field, db_row.get(field))
                    continue
                sheet_cmp = parsed
            else:
                sheet_cmp = raw

            snap_val = _norm(field, snap.get(field))
            db_val = _norm(field, db_row.get(field))

            # NO MERGE BASE MEANS NO EDIT — see the module docstring.
            edited = (bool(snap) and sheet_cmp != snap_val
                      and sheet_cmp != db_val)

            if edited and field in NEVER_BLANK and not sheet_cmp:
                plan.bump("blanks_refused")
                edited = False

            if edited:
                other = _other_surface_value(uid, field, others)
                if other is not None and _norm(field, other) not in (db_val, sheet_cmp):
                    # Three values in play: this tab, another tab, and the DB.
                    # Both sheets carry a human intention and nothing here can
                    # rank them. Report, write neither.
                    plan.conflicts.append(
                        f"row {row_number}: {field} — Timeline says "
                        f"{sheet_cmp!r}, another surface says "
                        f"{_norm(field, other)!r}. Not writing either.")
                    plan.bump("conflicts_held")
                    settled[field] = db_val
                    continue
                updates[field] = sheet_cmp or None
                plan.manual_marks.append((uid, field))
                plan.bump("pulled")
                settled[field] = sheet_cmp
                # Canonicalise in the SAME cycle, so "12/8" becomes 12/08/2026
                # now rather than looking unrecognised for 30 minutes.
                #
                # Compared against the DISPLAY form, not the ISO comparison
                # form: cells render as DD/MM/YYYY, so measuring against ISO
                # would mark every correctly-typed date as needing a rewrite
                # and churn a cell on every single cycle.
                if field in _DATE_FIELDS:
                    shown = display_date(sheet_cmp)
                    if raw != shown:
                        plan.cell_writes.append((row_number, col, shown))
                        plan.bump("normalized")
            elif sheet_cmp != db_val:
                if db_row.get(f"manual_{field}"):
                    plan.bump("manual_held")                # Rule 2
                    settled[field] = sheet_cmp
                else:
                    plan.cell_writes.append(
                        (row_number, col,
                         display_date(db_val) if field in _DATE_FIELDS else db_val))
                    plan.bump("pushed")                     # Rule 4
                    settled[field] = db_val
            else:
                settled[field] = sheet_cmp

        if updates:
            plan.updates.append((uid, updates))
        plan.snapshots.append((uid, row_number, settled))

    return plan


async def reconcile_timeline(spreadsheet_id: str | None = None) -> dict:
    """Read the Timeline, decide, and (unless shadowed) apply."""
    from services.google_sheets import sheets_service

    ssid = spreadsheet_id or settings.PROJECT_STATUS_SHEET_ID
    if not ssid:
        return {"skipped": "no PROJECT_STATUS_SHEET_ID"}

    try:
        resp = sheets_service._execute_with_retry(
            lambda: sheets_service.service.spreadsheets().values().get(
                spreadsheetId=ssid, range=f"'{TIMELINE_TAB}'!A1:DZ1000"))
        grid = resp.get("values") or []
    except Exception as e:                                   # noqa: BLE001
        logger.warning(f"[timeline-readback] could not read the tab: {e}")
        return {"skipped": "unreadable"}

    if not grid:
        # A transient read can return empty without raising. Treating that as
        # "every row was deleted" would be catastrophic; treat it as a bad read.
        logger.warning("[timeline-readback] the tab read empty — changing nothing")
        return {"skipped": "empty read"}

    c = supabase_client.client
    projects = {p["id"]: p for p in
                (c.table("canonical_projects").select("*").execute()).data or []}
    snaps = supabase_client.get_timeline_snapshots()
    others = (supabase_client.get_ps_project_snapshots(),)

    plan = build_plan(grid, projects, snaps, others)
    shadow = getattr(settings, "TIMELINE_SHADOW_MODE", True)
    out = {**plan.summary(), "shadow": shadow}

    for line in plan.conflicts:
        logger.warning(f"[timeline-readback] CONFLICT {line}")

    if shadow:
        if plan.updates or plan.cell_writes:
            logger.info(
                f"[timeline-readback] SHADOW would pull {len(plan.updates)} "
                f"row(s) and push {len(plan.cell_writes)} cell(s): "
                f"{plan.updates[:5]}")
        logger.info(f"[timeline-readback] {out}")
        return out

    failed = set()
    for project_id, updates in plan.updates:
        try:
            c.table("canonical_projects").update(updates).eq(
                "id", project_id).execute()
        except Exception as e:                               # noqa: BLE001
            logger.error(f"[timeline-readback] update {project_id}: {e}")
            failed.add(project_id)

    for project_id, field in plan.manual_marks:
        if project_id in failed:
            continue
        supabase_client.mark_project_field_manual(project_id, field, "timeline")

    if plan.cell_writes:
        try:
            data = [{"range": f"'{TIMELINE_TAB}'!{_a1(col)}{row}",
                     "values": [[value]]}
                    for row, col, value in plan.cell_writes]
            sheets_service._execute_with_retry(
                lambda: sheets_service.service.spreadsheets().values()
                .batchUpdate(spreadsheetId=ssid,
                             body={"valueInputOption": "RAW", "data": data}))
        except Exception as e:                               # noqa: BLE001
            logger.error(f"[timeline-readback] cell writes failed: {e}")

    # Snapshots LAST, and only for rows whose update succeeded — a snapshot
    # written for a failed row would make the next cycle believe the sheet value
    # had already landed.
    for project_id, row_number, settled in plan.snapshots:
        if project_id in failed:
            continue
        supabase_client.upsert_timeline_snapshot(
            project_id=project_id, sheet_row=row_number,
            start_date=settled.get("start_date") or None,
            target_date=settled.get("target_date") or None,
            owner=settled.get("owner") or None)

    logger.info(f"[timeline-readback] {out}")
    return out


def _a1(idx: int) -> str:
    out = ""
    idx += 1
    while idx:
        idx, rem = divmod(idx - 1, 26)
        out = chr(65 + rem) + out
    return out
