"""
Bidirectional reconcile for the Project Status sheet.  [2026-08-07]

Nechama runs the weekly project review out of this workbook, so her edits have
to reach the database and the system's updates have to reach her file — without
either side overwriting the other. That is the same three-way merge the
Tasks/Meetings/Decisions tabs already use (`processors/sheets_sync.py`), with
`sheet_snapshots` as the merge base:

    Rule 1  sheet != snapshot  and  sheet != db   ->  a human edited it.
                                                      Pull into the DB, mark
                                                      manual_<field> sticky.
    Rule 2  db != sheet  and  manual_<field>      ->  HOLD. Never revert a
                                                      value a human set.
    Rule 4  db != sheet  and  not sticky          ->  the DB advanced. Refresh
                                                      the cell from it.
    Rule 3  snapshots are rewritten LAST, and only for rows whose DB write
            actually succeeded — a stale snapshot re-detects the edit next
            cycle, an advanced one silently reverts it.

Kept OUT of sheets_sync.py, which is already ~2200 lines, mirroring the way
project_status_sheet.py was kept out of google_sheets.py.

WHAT IS DIFFERENT HERE, AND WHY IT NEEDED ITS OWN ENGINE

1. TWO ENTITY TYPES ON ONE SURFACE. A tab holds project rows and action rows,
   which merge against different tables (`canonical_projects` / `tasks`) and
   different snapshot rows (`ps_project` / `ps_action`).

2. IDENTITY IS PER-ROW, NOT POSITIONAL. Every system row carries its own uid
   and its parent's in hidden columns, so a row can be dragged, inserted or
   re-ordered without the engine losing it. sheets_sync leans on a stable row
   number; here a human physically re-arranges blocks during a meeting.

3. THE HUMAN OWNS THE FILE. sheets_sync reconciles a sheet Eyal occasionally
   corrects. This one reconciles a document somebody WORKS IN. So the load-
   bearing invariant is stronger than "don't lose data": the system must never
   write into a line it did not author. A row with no marker is hers, and
   nothing here may touch it except the one deliberate exception — rewriting a
   date she typed into the canonical form, which is reported every time.

SHADOW MODE IS THE DEFAULT. The engine computes the entire diff and writes
nothing, so a real review cycle can be watched first.

STRUCTURAL WORK IS CONFINED TO THE QUIET SLOTS. Inserting and deleting ROWS
only happens in PROJECT_STATUS_STRUCTURAL_SLOTS (02:00 and the weekly
pre-digest), never on the 30-minute interval: shifting rows under somebody who
is typing in the middle of a review is the one thing a working document must
not do. And because every row number in the plan comes from a read that already
happened, the structural pass re-reads the uid column first and skips any tab
whose sequence moved.
"""

import logging
from datetime import datetime, timezone

from config.settings import settings
from core.dates import parse_human_date
from services.project_status_rows import (
    COL_ACTION, COL_COMMENTS, COL_DATE, COL_RESP, COL_SUBJECT, COL_TODO,
    HUMAN_ACTION, HUMAN_PROJECT, INCOMPLETE, SYSTEM_ACTION, SYSTEM_PROJECT,
    find_duplicate_uids, parse_tab, strip_provenance, tab_fingerprint,
)
from services.supabase_client import supabase_client

logger = logging.getLogger(__name__)

READ_RANGE = "A1:L2000"
HOWTO_TAB = "How to use"

# Sheet column -> the DB field it maps to, per entity.
_ACTION_FIELDS = {COL_DATE: "deadline", COL_RESP: "assignee",
                  COL_COMMENTS: "notes", COL_ACTION: "title",
                  COL_SUBJECT: "label"}
_PROJECT_FIELDS = {COL_TODO: "objective", COL_DATE: "target_date",
                   COL_RESP: "owner", COL_COMMENTS: "notes"}

# Fields whose value is a DATE column: a cell that doesn't parse is NEVER
# pulled, or a typo would null a real deadline.
_DATE_FIELDS = {"deadline", "target_date"}
# Fields that must never be pulled BLANK. Clearing a cell is how a human tidies
# a view; it is not an instruction to erase the task's text.
_NEVER_BLANK = {"title", "label"}
# Statuses that mean the work is finished, for the closed-row pass.
_CLOSED = {"done", "cancelled", "archived"}


def _normalize(value) -> str:
    return str(value if value is not None else "").strip().lower()


class _Assignees:
    """Lazy roster canonicaliser — 'Nechama' and 'Nechama Tik' are one person.

    Loaded only when two raw values actually differ, so the common case (sheet
    == db) never queries. Same trick as sheets_sync._canon_assignee, which was
    added after shorthand names reported a phantom divergence every 30 minutes,
    forever.
    """

    def __init__(self):
        self._roster = None
        self._loaded = False
        self._cache: dict = {}

    def canon(self, value) -> str:
        key = str(value or "").strip()
        if key not in self._cache:
            if not self._loaded:
                try:
                    self._roster = supabase_client.list_team_members()
                except Exception as e:                      # noqa: BLE001
                    logger.warning(f"[ps-reconcile] roster load failed: {e}")
                    self._roster = None
                self._loaded = True
            try:
                self._cache[key] = supabase_client.resolve_assignee(
                    key, roster=self._roster)
            except Exception:                               # noqa: BLE001
                self._cache[key] = key
        return self._cache[key]

    def known(self, value) -> bool:
        """False when nobody on the roster matches — resolve_assignee returns
        an unrecognised name unchanged, which is exactly the signal."""
        raw = str(value or "").strip()
        return bool(raw) and _normalize(self.canon(raw)) != _normalize(raw) or (
            bool(raw) and any(
                _normalize(m.get("name")) == _normalize(raw)
                for m in (self._roster or [])))


def _eq(field: str, a, b, assignees: _Assignees) -> bool:
    """Field-aware equality."""
    if _normalize(a) == _normalize(b):
        return True
    if field in ("assignee", "owner"):
        return _normalize(assignees.canon(a)) == _normalize(assignees.canon(b))
    if field in _DATE_FIELDS:
        # "12/08/2026" and "2026-08-12" are the same date. Compare parsed.
        pa, pb = parse_human_date(a), parse_human_date(b)
        if pa and pb:
            return pa == pb
    return False


class Plan:
    """Everything the cycle WOULD do. Pure data, so shadow mode is honest.

    Shadow reports exactly this structure; the write path consumes exactly this
    structure. There is no second code path that only runs for real — which is
    what makes a clean shadow week actual evidence.
    """

    def __init__(self):
        self.task_updates: dict = {}          # task_id -> {field: value}
        self.project_updates: dict = {}       # project_id -> {field: value}
        self.manual_marks: list = []          # (kind, id, field)
        self.cell_writes: list = []           # (tab, row, col_index, value)
        self.creates: list = []               # dicts describing new rows
        self.suppress: list = []              # task_ids removed from the view
        self.person_proposals: list = []      # names seen in Resp.
        self.snapshots: list = []             # (kind, id, tab, row, values)
        self.counters: dict = {
            "pulled": 0, "pushed": 0, "manual_held": 0, "bad_dates": 0,
            "reparented": 0, "ticked": 0, "unticked": 0, "incomplete": 0,
            "ghosts": 0, "dup_uids": 0, "normalized_dates": 0, "orphans": 0,
        }
        self.overrides: list = []             # human-readable "what changed"
        self.skipped_tabs: list = []
        # P5 structural work. Row numbers here are computed from THIS read, so
        # they go stale the moment a human re-orders a tab — which is why the
        # structural pass re-checks the fingerprint before applying anything.
        self.injects: list = []               # (tab, anchor_row, [row values])
        self.row_deletes: list = []           # (tab, row_number, task_id)
        self.strikes: list = []               # (tab, row_number, task_id)
        self.fingerprints: dict = {}          # tab -> uid-sequence hash

    def bump(self, key: str, n: int = 1) -> None:
        self.counters[key] = self.counters.get(key, 0) + n

    def summary(self) -> dict:
        return {
            **self.counters,
            "task_updates": len(self.task_updates),
            "project_updates": len(self.project_updates),
            "cell_writes": len(self.cell_writes),
            "creates": len(self.creates),
            "suppress": len(self.suppress),
            "person_proposals": len(self.person_proposals),
            "injects": sum(len(rows) for _, _, rows in self.injects),
            "row_deletes": len(self.row_deletes),
            "strikes": len(self.strikes),
            "skipped_tabs": self.skipped_tabs,
        }


def _read_tabs(spreadsheet_id: str) -> dict:
    """{tab -> raw grid} for every area tab, in one batchGet."""
    from services.google_sheets import sheets_service

    meta = sheets_service._execute_with_retry(
        lambda: sheets_service.service.spreadsheets().get(
            spreadsheetId=spreadsheet_id, fields="sheets.properties.title"))
    tabs = [s["properties"]["title"] for s in meta.get("sheets", [])
            if s["properties"]["title"] != HOWTO_TAB]
    if not tabs:
        return {}
    resp = sheets_service._execute_with_retry(
        lambda: sheets_service.service.spreadsheets().values().batchGet(
            spreadsheetId=spreadsheet_id,
            ranges=[f"'{t}'!{READ_RANGE}" for t in tabs]))
    return {tab: (vr.get("values") or [])
            for tab, vr in zip(tabs, resp.get("valueRanges", []))}


def _merge_row(row, kind: str, db_row: dict, snap: dict, field_map: dict,
               tab: str, plan: Plan, assignees: _Assignees) -> dict:
    """Apply Rules 1/2/4 to one row. Returns the settled values for the snapshot."""
    updates, final = {}, {}
    id_key = "project" if kind == "project" else "task"
    row_id = row.uid

    for col, field in field_map.items():
        sheet_val = row.values.get(col, "")
        if field == "title":
            sheet_val = strip_provenance(sheet_val)
        snap_val = snap.get(_snap_key(field))
        db_val = db_row.get(field)

        if field in _DATE_FIELDS:
            parsed = parse_human_date(sheet_val) if sheet_val else ""
            if sheet_val and not parsed:
                # Unparseable. Never pulled, cell left exactly as typed — a
                # typo must not be able to null a real deadline.
                plan.bump("bad_dates")
                final[field] = db_val
                continue
            sheet_cmp = parsed or ""
        else:
            sheet_cmp = sheet_val

        edited = (not _eq(field, sheet_cmp, snap_val, assignees)
                  and not _eq(field, sheet_cmp, db_val, assignees))
        if field in _NEVER_BLANK and not str(sheet_cmp).strip():
            edited = False

        if edited:
            updates[field] = sheet_cmp or None              # Rule 1
            plan.manual_marks.append((id_key, row_id, field))
            plan.bump("pulled")
            final[field] = sheet_cmp
            plan.overrides.append(f"{tab} r{row.row_number}: {field} <- {sheet_cmp!r}")
        elif not _eq(field, db_val, sheet_cmp, assignees):
            if db_row.get(f"manual_{field}"):
                plan.bump("manual_held")                    # Rule 2
                final[field] = sheet_cmp
            else:
                plan.cell_writes.append(
                    (tab, row.row_number, _col_index(col), _display(field, db_val)))
                plan.bump("pushed")                         # Rule 4
                final[field] = db_val
        else:
            final[field] = sheet_cmp
            # Sloppy-but-valid date ("12/8") rewritten to the canonical form.
            # The single exception to "never touch a human cell": format only,
            # never meaning, and it is reported in overrides every time.
            if (field in _DATE_FIELDS and sheet_val
                    and _display(field, sheet_cmp) != sheet_val):
                plan.cell_writes.append(
                    (tab, row.row_number, _col_index(col),
                     _display(field, sheet_cmp)))
                plan.bump("normalized_dates")
                plan.overrides.append(
                    f"{tab} r{row.row_number}: date shown as "
                    f"{_display(field, sheet_cmp)}")

    if updates:
        target = plan.project_updates if kind == "project" else plan.task_updates
        target.setdefault(row_id, {}).update(updates)
    return final


def _snap_key(field: str) -> str:
    """DB field -> the snapshot column that holds it."""
    return {"assignee": "assignee", "owner": "owner", "notes": "notes",
            "title": "title", "label": "label", "deadline": "deadline",
            "objective": "objective", "target_date": "target_date"}.get(field, field)


def _col_index(col: str) -> int:
    from services.project_status_rows import ALL_HEADERS
    return ALL_HEADERS.index(col)


def _display(field: str, value) -> str:
    """How a settled value is rendered back into a cell."""
    if value in (None, ""):
        return ""
    if field in _DATE_FIELDS:
        try:
            return datetime.fromisoformat(
                str(value).replace("Z", "+00:00")).strftime("%d/%m/%Y")
        except (ValueError, TypeError):
            return str(value)
    return str(value)


def build_plan(grids: dict) -> Plan:
    """Phases 0-2: classify every row and decide what would change. No I/O."""
    plan = Plan()
    assignees = _Assignees()

    db_projects = {p["id"]: p for p in supabase_client.get_canonical_projects(status=None)}
    db_tasks = {t["id"]: t for t in supabase_client.get_ps_tasks()}
    proj_snaps = supabase_client.get_ps_project_snapshots()
    act_snaps = supabase_client.get_sheet_snapshots(entity_type="ps_action")

    seen_tasks: set = set()
    seen_projects: set = set()
    parsed: dict = {}

    for tab, grid in grids.items():
        blocks, orphans, _ = parse_tab(grid)
        plan.fingerprints[tab] = tab_fingerprint(blocks, orphans)

        # GUARD: a transient Sheets read can return an EMPTY tab without
        # raising. Treating that as "everything was deleted" would suppress the
        # entire review. If the tab reads empty but we hold snapshots for it,
        # the read is bad — skip the tab, change nothing. Mirrors the guard
        # added to sheets_sync after the 2026-07-10 duplication incident.
        if not blocks and not orphans:
            held = sum(1 for s in proj_snaps.values() if s.get("sheet_tab") == tab)
            if held:
                logger.error(
                    f"[ps-reconcile] SKIPPED tab {tab!r} — read 0 rows but "
                    f"{held} snapshots exist. Refusing to treat a bad read as "
                    "a mass deletion.")
                plan.skipped_tabs.append(tab)
            continue

        parsed[tab] = blocks
        dups = find_duplicate_uids(blocks, orphans)
        if dups:
            plan.bump("dup_uids", len(dups))
            logger.warning(
                f"[ps-reconcile] {tab}: {len(dups)} duplicated uid(s) — a block "
                "was pasted. Topmost keeps the identity.")

        # Only the TOPMOST occurrence of a uid is authoritative; the rest are a
        # paste and are left entirely alone this cycle.
        pasted = {id(r) for rows in dups.values() for r in rows[1:]}

        for block in blocks:
            proj = block.project
            parent_uid = ""

            if proj is not None and proj.kind == SYSTEM_PROJECT and id(proj) not in pasted:
                db_proj = db_projects.get(proj.uid)
                if not db_proj:
                    plan.bump("ghosts")             # unknown to the DB: leave it
                else:
                    parent_uid = proj.uid
                    seen_projects.add(proj.uid)
                    final = _merge_row(proj, "project", db_proj,
                                       proj_snaps.get(proj.uid, {}),
                                       _PROJECT_FIELDS, tab, plan, assignees)
                    plan.snapshots.append(("project", proj.uid, tab,
                                           proj.row_number, final))
            elif proj is not None and proj.kind == HUMAN_PROJECT:
                plan.creates.append({"kind": "project", "tab": tab,
                                     "row": proj.row_number,
                                     "name": proj.values.get(COL_SUBJECT, ""),
                                     "objective": proj.values.get(COL_TODO, ""),
                                     "owner": proj.values.get(COL_RESP, "")})

            for row in block.actions:
                if id(row) in pasted:
                    continue
                _handle_action(row, tab, parent_uid, db_tasks, act_snaps,
                               plan, assignees, seen_tasks)

        for row in orphans:
            plan.bump("orphans")
            if row.kind in (HUMAN_ACTION, INCOMPLETE):
                # An action typed above the first project row. Not dropped —
                # losing a line she typed because it sat in the wrong place
                # would be the worst possible behaviour.
                _handle_action(row, tab, "", db_tasks, act_snaps, plan,
                               assignees, seen_tasks)

    _detect_suppressions(act_snaps, seen_tasks, db_tasks, plan)
    _propose_people(plan, assignees)
    _plan_closed_rows(parsed, db_tasks, plan)
    if getattr(settings, "PROJECT_STATUS_AUTO_INJECT_ENABLED", False):
        _plan_injections(parsed, db_tasks, act_snaps, seen_tasks, plan)
    return plan


def _annotated(row, db_task: dict) -> bool:
    """Has a human left anything of their own on this auto row?

    Comments is hers by definition, `auto_edited` records a previous cycle's
    finding, and any manual_* flag means somebody set a field by hand on some
    surface. Any of those makes the row worth keeping when the work closes —
    deleting it would throw away a note nobody else has a copy of.
    """
    if str(row.values.get(COL_COMMENTS) or "").strip():
        return True
    if row.origin == "auto_edited":
        return True
    return any(k.startswith("manual_") and v for k, v in (db_task or {}).items())


def _plan_closed_rows(parsed: dict, db_tasks: dict, plan: Plan) -> None:
    """Rows whose task is finished: remove the plain ones, keep the annotated.

    Only for tasks ALREADY closed in the database. A row ticked during this very
    cycle is deliberately left alone — she would watch the line vanish under the
    cursor a second after clicking it. It goes on the next structural pass.
    """
    ticking_now = {tid for tid, fields in plan.task_updates.items()
                   if "status" in fields}
    for tab, blocks in parsed.items():
        if tab in plan.skipped_tabs:
            continue
        for block in blocks:
            for row in block.actions:
                if row.kind != SYSTEM_ACTION or not row.uid:
                    continue
                if row.uid in ticking_now:
                    continue
                task = db_tasks.get(row.uid)
                if not task or _normalize(task.get("status")) not in _CLOSED:
                    continue
                if _annotated(row, task):
                    plan.strikes.append((tab, row.row_number, row.uid))
                else:
                    plan.row_deletes.append((tab, row.row_number, row.uid))


def _plan_injections(parsed: dict, db_tasks: dict, act_snaps: dict,
                     seen: set, plan: Plan) -> None:
    """Append newly-extracted tasks under their project block.

    A task qualifies when it is open, approved, attached to a project, not
    suppressed, and has never had a row here (no ps_action snapshot). Source is
    therefore extraction — this never re-adds something she deleted, because
    deletion sets ps_suppressed and the snapshot survives.

    Two caps: per project per cycle, so a busy meeting can't dump twenty lines
    into one block overnight, and a total per block, so a project nobody prunes
    doesn't grow without limit. Both are reported.
    """
    from services.project_status_rows import KIND_ACTION, ORIGIN_AUTO, format_provenance

    per_cycle = getattr(settings, "PROJECT_STATUS_MAX_AUTO_PER_PROJECT", 5)
    per_block = getattr(settings, "PROJECT_STATUS_MAX_ACTIONS_PER_PROJECT", 25)

    candidates: dict = {}
    for task in db_tasks.values():
        pid = task.get("project_id")
        if (not pid or task.get("ps_suppressed")
                or _normalize(task.get("status")) in _CLOSED
                or task["id"] in act_snaps or task["id"] in seen):
            continue
        candidates.setdefault(pid, []).append(task)

    if not candidates:
        return

    for tab, blocks in parsed.items():
        if tab in plan.skipped_tabs:
            continue
        for block in blocks:
            pid = block.project_uid
            queue = candidates.get(pid)
            if not queue:
                continue
            room = max(0, per_block - len(block.actions))
            take = min(len(queue), per_cycle, room)
            if take < len(queue):
                logger.info(
                    f"[ps-reconcile] {tab}: {len(queue)} new task(s) for this "
                    f"project, injecting {take} (per-cycle {per_cycle}, "
                    f"block room {room}).")
            if not take:
                continue
            rows = []
            for task in queue[:take]:
                rows.append([
                    False, task.get("label") or "",
                    f"{task.get('title') or ''} "
                    f"{format_provenance(ORIGIN_AUTO, '', '')}".strip(),
                    "", _display("deadline", task.get("deadline")),
                    task.get("assignee") or "", task.get("notes") or "",
                    KIND_ACTION, task["id"], pid, ORIGIN_AUTO,
                    task.get("meeting_id") or "",
                ])
            plan.injects.append((tab, block.end_row, rows))


def _handle_action(row, tab: str, parent_uid: str, db_tasks: dict,
                   act_snaps: dict, plan: Plan, assignees: _Assignees,
                   seen_tasks: set) -> None:
    if row.kind == INCOMPLETE:
        # Date or owner filled but no Action text. Counted and surfaced, never
        # written: a task with no title is worse than no task.
        plan.bump("incomplete")
        return

    if row.kind == HUMAN_ACTION:
        plan.creates.append({
            "kind": "task", "tab": tab, "row": row.row_number,
            "title": row.values.get(COL_ACTION, ""),
            "project_id": parent_uid,
            "deadline": parse_human_date(row.values.get(COL_DATE)),
            "assignee": row.values.get(COL_RESP, ""),
            "notes": row.values.get(COL_COMMENTS, ""),
            "label": row.values.get(COL_SUBJECT, ""),
        })
        return

    if row.kind != SYSTEM_ACTION or not row.uid:
        return

    db_task = db_tasks.get(row.uid)
    if not db_task:
        plan.bump("ghosts")
        return

    seen_tasks.add(row.uid)
    snap = act_snaps.get(row.uid, {})

    # The checkbox. Compared against the SNAPSHOT's cell value, not the DB
    # status: the snapshot records what the box said last cycle, so a tick is a
    # change she just made rather than a permanent disagreement.
    was_ticked = _normalize(snap.get("status")) == "done"
    if row.checked != was_ticked:
        plan.task_updates.setdefault(row.uid, {})["status"] = (
            "done" if row.checked else "pending")
        plan.manual_marks.append(("task", row.uid, "status"))
        plan.bump("ticked" if row.checked else "unticked")

    # A row dragged into another project's block is a deliberate re-parent.
    if parent_uid and row.parent and parent_uid != row.parent:
        plan.task_updates.setdefault(row.uid, {})["project_id"] = parent_uid
        plan.manual_marks.append(("task", row.uid, "project_id"))
        plan.bump("reparented")

    final = _merge_row(row, "task", db_task, snap, _ACTION_FIELDS, tab, plan,
                       assignees)
    final["status"] = "done" if row.checked else "pending"
    plan.snapshots.append(("task", row.uid, tab, row.row_number, final))


def _detect_suppressions(act_snaps: dict, seen: set, db_tasks: dict,
                         plan: Plan) -> None:
    """A system row that had a snapshot and is no longer in the sheet.

    Deleting a row is a VIEW operation: the task stays live, stays on the Tasks
    tab and is still chased. `ps_suppressed` only stops it being re-injected
    here, so she isn't in a resurrection loop.

    Capped. Above the cap NOTHING is suppressed — a bulk delete, a bad paste or
    a truncated read must be incapable of emptying the review.

    A SKIPPED TAB'S ROWS ARE NEVER SUPPRESSED. This runs across all snapshots,
    so without that exclusion a tab that was skipped for reading empty would
    have every one of its tasks suppressed anyway — the bad-read guard would
    announce it had protected the tab and then let the damage through the back
    door. A snapshot with no recorded tab is excluded too whenever anything was
    skipped: unknown provenance plus a known-bad read is not a safe combination.
    """
    skipped = set(plan.skipped_tabs)

    def readable(tid: str) -> bool:
        if not skipped:
            return True
        tab = (act_snaps.get(tid) or {}).get("sheet_tab")
        return bool(tab) and tab not in skipped

    gone = [tid for tid in act_snaps
            if tid not in seen and tid in db_tasks
            and not db_tasks[tid].get("ps_suppressed")
            and readable(tid)]
    cap = getattr(settings, "PROJECT_STATUS_MAX_SUPPRESS_PER_CYCLE", 5)
    if len(gone) > cap:
        logger.warning(
            f"[ps-reconcile] {len(gone)} rows vanished (cap {cap}) — suppressing "
            "NONE. A bulk delete or a bad read must not empty the review.")
        return
    plan.suppress.extend(gone)


def _propose_people(plan: Plan, assignees: _Assignees) -> None:
    """A name in Resp. that matches nobody: PROPOSE a person, never create one.

    Deliberate exception to "a human typing it is the approval". team_members
    .tier drives distribution tier-capping, so auto-creating someone at founders
    tier would put a stranger on the next founders-tier email. Nothing is
    blocked meanwhile: resolve_assignee returns the name as typed, so the task
    is created with it immediately.
    """
    names = set()
    for create in plan.creates:
        raw = str(create.get("assignee") or "").strip()
        if raw and not assignees.known(raw):
            names.add(raw)
    plan.person_proposals.extend(sorted(names))


def structural_allowed(slot: str | None) -> bool:
    """Is this reconcile slot permitted to insert or delete ROWS?

    The 30-minute interval never is. Shifting rows under someone who is typing
    in the middle of a review is the one thing a working document must not do,
    so structural work is confined to the quiet slots (02:00 and the weekly
    pre-digest). An empty setting disables it entirely.
    """
    allowed = [s.strip().lower() for s in
               str(getattr(settings, "PROJECT_STATUS_STRUCTURAL_SLOTS", "") or "").split(",")
               if s.strip()]
    if not allowed or not slot:
        return False
    # Slots arrive as "2026-08-07:prenightly" or "2026-08-07-1748:interval".
    return slot.rsplit(":", 1)[-1].strip().lower() in allowed


async def reconcile_project_status(dry_run: bool = False,
                                   shadow: bool | None = None,
                                   slot: str | None = None) -> dict:
    """Run one reconcile cycle. Shadow by default.

    Returns the summary dict; in shadow it is the full would-do diff and
    nothing has been written.
    """
    if shadow is None:
        shadow = getattr(settings, "PROJECT_STATUS_RECONCILE_SHADOW_MODE", True)
    write_allowed = not (dry_run or shadow)
    structural = write_allowed and structural_allowed(slot)

    sid = settings.PROJECT_STATUS_SHEET_ID
    if not sid:
        return {"error": "PROJECT_STATUS_SHEET_ID not configured"}

    try:
        grids = _read_tabs(sid)
    except Exception as e:                                  # noqa: BLE001
        logger.error(f"[ps-reconcile] read failed: {e}")
        return {"error": "sheet_read_failed", "detail": str(e)}

    if not grids:
        return {"error": "no_tabs"}

    plan = build_plan(grids)
    summary = {**plan.summary(), "shadow": shadow, "dry_run": dry_run,
               "tabs": len(grids), "structural": structural, "slot": slot}

    if not write_allowed:
        logger.info(f"[ps-reconcile][{'shadow' if shadow else 'dry-run'}] {summary}")
        if plan.overrides:
            for line in plan.overrides[:20]:
                logger.info(f"[ps-reconcile][would] {line}")
        try:
            supabase_client.log_action(
                "ps_shadow_reconcile" if shadow else "ps_reconcile_dryrun",
                details=summary, triggered_by="auto")
        except Exception:                                   # noqa: BLE001
            pass
        return summary

    summary.update(await _apply(plan, sid, structural=structural))
    logger.info(f"[ps-reconcile] {summary}")
    try:
        supabase_client.log_action("ps_reconcile", details=summary,
                                   triggered_by="auto")
    except Exception:                                       # noqa: BLE001
        pass
    return summary


def _create_entity(spec: dict) -> tuple[str, str]:
    """Create the task or project a human row describes. Returns (id, kind).

    A human typing a line IS the approval — the house convention already used
    for hand-added tasks/decisions/meetings — so these land approved rather
    than queueing behind a card she would never see.
    """
    if spec["kind"] == "project":
        name = (spec.get("name") or "").strip()
        if not name:
            return "", ""
        # add_canonical_project is idempotent by name, so a name matching a
        # RETIRED project reactivates it instead of creating a duplicate.
        row = supabase_client.add_canonical_project(name=name)
        pid = (row or {}).get("id", "")
        if pid and (spec.get("objective") or spec.get("owner")):
            supabase_client.update_canonical_project(
                pid, objective=spec.get("objective") or None,
                owner=spec.get("owner") or None)
        return pid, "P"

    title = (spec.get("title") or "").strip()
    if not title:
        return "", ""
    created = supabase_client.create_task(
        title=title,
        assignee=spec.get("assignee") or "",
        deadline=spec.get("deadline") or None,
        project_id=spec.get("project_id") or None,
        notes=spec.get("notes") or None,
        label=spec.get("label") or None,
        approval_status="approved",
    )
    return (created or {}).get("id", ""), "A"


def _write_identity(sheets_service, spreadsheet_id: str, tab: str, row: int,
                    kind: str, uid: str, parent: str) -> None:
    """Stamp _kind/_uid/_parent into a row we just created a record for.

    Synchronous and per-row on purpose: batching this with the other cell
    writes would make one failure indistinguishable from all of them, and the
    rollback below has to know exactly which create lost its identity.
    """
    try:
        sheets_service._execute_with_retry(
            lambda: sheets_service.service.spreadsheets().values().update(
                spreadsheetId=spreadsheet_id,
                range=f"'{tab}'!H{row}:J{row}",
                valueInputOption="RAW",
                body={"values": [[kind, uid, parent or uid]]}))
    except Exception as e:                                  # noqa: BLE001
        # Roll the create back rather than leave an identity-less row that
        # would be re-created on every future cycle.
        logger.error(f"[ps-reconcile] uid writeback failed for {uid} — rolling "
                     f"the create back: {e}")
        try:
            table = "canonical_projects" if kind == "P" else "tasks"
            supabase_client.client.table(table).delete().eq("id", uid).execute()
        except Exception as rb:                             # noqa: BLE001
            logger.critical(
                f"[ps-reconcile] ROLLBACK FAILED for {table} {uid}: {rb}. "
                "This row has no uid in the sheet and WILL be duplicated next "
                "cycle — delete it by hand.")
        raise


async def _apply_structural(plan: Plan, spreadsheet_id: str) -> dict:
    """Insert new rows, remove closed ones, strike the annotated ones.

    THE FINGERPRINT RE-CHECK IS THE WHOLE POINT. Every row number in the plan
    was computed from a read that happened seconds-to-minutes ago. If a human
    inserted, deleted or dragged a row since, those numbers now address
    DIFFERENT rows — and a structural edit applied at the wrong offset deletes
    somebody's work. So the uid column is re-read and any tab whose sequence
    moved is skipped entirely. A skipped tab loses nothing: the same work is
    re-planned against fresh numbers on the next structural slot.

    Requests are emitted DESCENDING by row so that earlier edits don't shift
    the targets of later ones within the same batch.
    """
    from services.google_sheets import sheets_service
    from services.project_status_rows import ALL_HEADERS, parse_tab

    out = {"injected": 0, "rows_deleted": 0, "struck": 0, "stale_tabs": []}

    tabs = {t for t, _, _ in plan.injects} | {t for t, _, _ in plan.row_deletes} \
        | {t for t, _, _ in plan.strikes}
    if not tabs:
        return out

    fresh = sheets_service._execute_with_retry(
        lambda: sheets_service.service.spreadsheets().values().batchGet(
            spreadsheetId=spreadsheet_id,
            ranges=[f"'{t}'!{READ_RANGE}" for t in sorted(tabs)]))
    now_fp = {}
    for tab, vr in zip(sorted(tabs), fresh.get("valueRanges", [])):
        blocks, orphans, _ = parse_tab(vr.get("values") or [])
        now_fp[tab] = tab_fingerprint(blocks, orphans)

    stale = {t for t in tabs if now_fp.get(t) != plan.fingerprints.get(t)}
    if stale:
        out["stale_tabs"] = sorted(stale)
        logger.warning(
            f"[ps-reconcile] structural SKIPPED for {sorted(stale)} — the tab "
            "changed since it was read; row numbers are stale. Re-planned next "
            "structural slot.")

    meta = sheets_service._execute_with_retry(
        lambda: sheets_service.service.spreadsheets().get(
            spreadsheetId=spreadsheet_id, fields="sheets.properties"))
    gid = {s["properties"]["title"]: s["properties"]["sheetId"]
           for s in meta.get("sheets", [])}

    # (row, request) pairs, applied strictly bottom-to-top.
    ops: list = []
    for tab, row, _tid in plan.row_deletes:
        if tab in stale or tab not in gid:
            continue
        ops.append((row, {"deleteDimension": {"range": {
            "sheetId": gid[tab], "dimension": "ROWS",
            "startIndex": row - 1, "endIndex": row}}}))
        out["rows_deleted"] += 1

    for tab, row, _tid in plan.strikes:
        if tab in stale or tab not in gid:
            continue
        ops.append((row, {"repeatCell": {
            "range": {"sheetId": gid[tab], "startRowIndex": row - 1,
                      "endRowIndex": row, "startColumnIndex": 0,
                      "endColumnIndex": 7},
            "cell": {"userEnteredFormat": {"textFormat": {"strikethrough": True}}},
            "fields": "userEnteredFormat.textFormat.strikethrough"}}))
        ops.append((row, {"updateCells": {
            "range": {"sheetId": gid[tab], "startRowIndex": row - 1,
                      "endRowIndex": row, "startColumnIndex": 10,
                      "endColumnIndex": 11},
            "rows": [{"values": [{"userEnteredValue":
                                  {"stringValue": "auto_edited"}}]}],
            "fields": "userEnteredValue"}}))
        out["struck"] += 1

    for tab, anchor, rows in plan.injects:
        if tab in stale or tab not in gid:
            continue
        sheet_id = gid[tab]
        ops.append((anchor, {"insertDimension": {
            "range": {"sheetId": sheet_id, "dimension": "ROWS",
                      "startIndex": anchor - 1, "endIndex": anchor - 1 + len(rows)},
            # Inherit the row ABOVE so an inserted action row picks up the body
            # formatting of the block it joins.
            "inheritFromBefore": True}}))
        ops.append((anchor, {"updateCells": {
            "range": {"sheetId": sheet_id, "startRowIndex": anchor - 1,
                      "endRowIndex": anchor - 1 + len(rows),
                      "startColumnIndex": 0, "endColumnIndex": len(ALL_HEADERS)},
            "rows": [{"values": [_cell_value(v) for v in row]} for row in rows],
            "fields": "userEnteredValue"}}))
        # inheritFromBefore copies the PROJECT row when a block had no actions
        # yet — that row is bold, so the first injected action would arrive
        # looking like a heading. Reset the format explicitly in the same batch.
        ops.append((anchor, {"repeatCell": {
            "range": {"sheetId": sheet_id, "startRowIndex": anchor - 1,
                      "endRowIndex": anchor - 1 + len(rows),
                      "startColumnIndex": 0, "endColumnIndex": 7},
            "cell": {"userEnteredFormat": {
                "textFormat": {"bold": False},
                "wrapStrategy": "WRAP", "verticalAlignment": "TOP"}},
            "fields": ("userEnteredFormat(textFormat.bold,wrapStrategy,"
                       "verticalAlignment)")}}))
        ops.append((anchor, {"setDataValidation": {
            "range": {"sheetId": sheet_id, "startRowIndex": anchor - 1,
                      "endRowIndex": anchor - 1 + len(rows),
                      "startColumnIndex": 0, "endColumnIndex": 1},
            "rule": {"condition": {"type": "BOOLEAN"}, "strict": False}}}))
        out["injected"] += len(rows)

    if not ops:
        return out
    ops.sort(key=lambda pair: pair[0], reverse=True)
    try:
        sheets_service._execute_with_retry(
            lambda: sheets_service.service.spreadsheets().batchUpdate(
                spreadsheetId=spreadsheet_id,
                body={"requests": [req for _, req in ops]}))
    except Exception as e:                                  # noqa: BLE001
        logger.error(f"[ps-reconcile] structural batch failed: {e}")
        return {**out, "structural_error": str(e)[:120]}
    return out


def _cell_value(value) -> dict:
    """A python value as a Sheets userEnteredValue."""
    if isinstance(value, bool):
        return {"userEnteredValue": {"boolValue": value}}
    if isinstance(value, (int, float)):
        return {"userEnteredValue": {"numberValue": value}}
    text = "" if value is None else str(value)
    return {"userEnteredValue": {"stringValue": text}}


async def _apply(plan: Plan, spreadsheet_id: str, structural: bool = False) -> dict:
    """Phase 4: DB writes, then cell writes, then snapshots. Order is load-bearing.

    No structural work here — inserting and deleting rows is P5, and it is
    what `tab_fingerprint` exists for: re-read the uid column immediately
    before the structural batch and skip any tab whose fingerprint moved,
    because a human re-ordering mid-cycle makes every computed row number
    stale. Value writes address cells by uid-resolved row, so they are safe."""
    from services.google_sheets import sheets_service

    failed: set = set()
    applied = {"db_failed": 0, "created": 0, "suppressed": 0}

    # 4.1 DB writes first. A snapshot is only advanced for a row whose write
    #     succeeded (Rule 3), so failures are collected as we go.
    for task_id, fields in plan.task_updates.items():
        try:
            supabase_client.update_task(task_id, **fields)
        except Exception as e:                              # noqa: BLE001
            logger.error(f"[ps-reconcile] task {task_id} update failed: {e}")
            failed.add(task_id)
            applied["db_failed"] += 1

    for project_id, fields in plan.project_updates.items():
        try:
            supabase_client.update_canonical_project(project_id, **fields)
        except Exception as e:                              # noqa: BLE001
            logger.error(f"[ps-reconcile] project {project_id} update failed: {e}")
            failed.add(project_id)
            applied["db_failed"] += 1

    for kind, row_id, field in plan.manual_marks:
        if row_id in failed:
            continue
        try:
            if kind == "task":
                supabase_client.mark_task_field_manual(row_id, field, "sheet_edit")
            else:
                supabase_client.mark_project_field_manual(row_id, field, "sheet_edit")
        except Exception as e:                              # noqa: BLE001
            logger.warning(f"[ps-reconcile] manual mark {row_id}.{field}: {e}")

    for task_id in plan.suppress:
        try:
            supabase_client.client.table("tasks").update(
                {"ps_suppressed": True}).eq("id", task_id).execute()
            applied["suppressed"] += 1
        except Exception as e:                              # noqa: BLE001
            logger.error(f"[ps-reconcile] suppress {task_id} failed: {e}")

    # 4.1b Creates, with the anti-double-create protocol: create, then write the
    #      uid back into the row SYNCHRONOUSLY, and if the writeback fails roll
    #      the create back. A row left without its uid looks like a fresh human
    #      row next cycle and would be created again, every cycle, forever.
    cap = getattr(settings, "PROJECT_STATUS_MAX_CREATES_PER_CYCLE", 25)
    if len(plan.creates) > cap:
        logger.warning(
            f"[ps-reconcile] {len(plan.creates)} new rows (cap {cap}) — creating "
            "NONE. A bulk paste must not silently mint hundreds of tasks.")
        plan.creates = []
    for spec in plan.creates:
        try:
            new_id, kind = _create_entity(spec)
            if not new_id:
                continue
            _write_identity(sheets_service, spreadsheet_id, spec["tab"],
                            spec["row"], kind, new_id, spec.get("project_id", ""))
            applied["created"] += 1
        except Exception as e:                              # noqa: BLE001
            logger.error(f"[ps-reconcile] create at {spec['tab']} r{spec['row']} "
                         f"failed: {e}")
            applied["db_failed"] += 1

    # 4.2 ONE batched cell write. On failure RETURN — do not advance snapshots,
    #     or next cycle would see sheet == snapshot and revert her edit.
    if plan.cell_writes:
        from services.project_status_rows import ALL_HEADERS
        data = [{"range": f"'{tab}'!{chr(65 + col)}{row}", "values": [[value]]}
                for tab, row, col, value in plan.cell_writes
                if col < len(ALL_HEADERS)]
        try:
            sheets_service._execute_with_retry(
                lambda: sheets_service.service.spreadsheets().values().batchUpdate(
                    spreadsheetId=spreadsheet_id,
                    body={"valueInputOption": "RAW", "data": data}))
        except Exception as e:                              # noqa: BLE001
            logger.error(f"[ps-reconcile] cell write failed, snapshots NOT "
                         f"advanced: {e}")
            return {**applied, "error": "sheet_write_failed"}

    # 4.3/4.4 Structural pass. Only in the allowed slots, and only after
    #         re-reading the sheet to prove nobody moved anything since we
    #         computed these row numbers.
    if structural and (plan.injects or plan.row_deletes or plan.strikes):
        applied.update(await _apply_structural(plan, spreadsheet_id))

    # 4.5 Snapshots LAST, and only for rows that survived 4.1.
    for kind, row_id, tab, row_number, values in plan.snapshots:
        if row_id in failed:
            continue
        try:
            if kind == "project":
                supabase_client.upsert_ps_project_snapshot(
                    project_id=row_id, sheet_row=row_number, sheet_tab=tab,
                    objective=values.get("objective"),
                    target_date=parse_human_date(values.get("target_date")),
                    owner=values.get("owner"), notes=values.get("notes"))
            else:
                supabase_client.upsert_sheet_snapshot(
                    task_id=row_id, sheet_row=row_number,
                    status=values.get("status"),
                    deadline=parse_human_date(values.get("deadline")),
                    priority=None, assignee=values.get("assignee"),
                    title=values.get("title"), label=values.get("label"),
                    entity_type="ps_action", notes=values.get("notes"),
                    sheet_tab=tab)
        except Exception as e:                              # noqa: BLE001
            logger.warning(f"[ps-reconcile] snapshot {row_id}: {e}")
    return applied
