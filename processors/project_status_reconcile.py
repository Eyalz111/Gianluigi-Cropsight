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
from core.dates import edit_is_newer_than_sync, parse_human_date
from services.project_status_rows import (
    ALL_HEADERS, FIRST_BODY_ROW, COL_ACTION, COL_COMMENTS, COL_DATE, COL_PARENT,
    COL_KIND, COL_ORIGIN, COL_PRIORITY, COL_PROJECT, COL_RESP, COL_SRC,
    COL_TODO, COL_TOPIC, COL_UID,
    AMBIGUOUS, HEADER_ROW, HUMAN_ACTION, HUMAN_PROJECT, INCOMPLETE,
    SYSTEM_ACTION, SYSTEM_PROJECT, PRIORITY_TO_DB, PRIORITY_TO_SHEET,
    find_duplicate_uids, parse_tab, resolve_columns, strip_provenance,
    tab_fingerprint, unresolved_columns,
)
from services.supabase_client import supabase_client

logger = logging.getLogger(__name__)

# Must span every column in ALL_HEADERS. Derived rather than written out: it
# read "A1:L2000" for the 12-column layout, and a hand-maintained letter is
# exactly the thing that silently truncates the hidden identity columns the
# moment the sheet grows one.
READ_RANGE = f"A1:{chr(ord('A') + len(ALL_HEADERS) - 1)}2000"
HOWTO_TAB = "How to use"

# Tabs in this workbook that are NOT area tabs and must never be parsed as one.
# The layout guard would catch them anyway — they do not declare the columns, so
# they would be skipped — but it would log an ERROR about it every 30 minutes
# forever, and an alarm that always fires is an alarm nobody reads. Named
# explicitly instead: the meetings pool moved in here on 2026-08-09 and is a
# legitimate resident, not a malformed area tab.
# `Focus` and `Focus data` joined them on 2026-08-11 and are DERIVED — nothing
# on either is a source of truth, so the reconcile must neither read them as
# area tabs nor let the formatting pass wipe their rules. IMPORTED rather than
# spelled again: two copies of a tab name is how one of them ends up renamed
# alone, and the failure would be this engine quietly parsing the view.
from processors.focus_view import FOCUS_DATA_TAB, FOCUS_TAB  # noqa: E402
from processors.timeline_view import TIMELINE_TAB  # noqa: E402
from processors.milestones import CEO_TAB  # noqa: E402

NON_AREA_TABS = frozenset({HOWTO_TAB, "Meetings", "Past Meetings",
                           FOCUS_TAB, FOCUS_DATA_TAB, TIMELINE_TAB, CEO_TAB})

# Sheet column -> the DB field it maps to, per entity.
_ACTION_FIELDS = {COL_DATE: "deadline", COL_RESP: "assignee",
                  COL_COMMENTS: "notes", COL_ACTION: "title",
                  COL_TOPIC: "label", COL_PRIORITY: "priority"}
# Merged only once the column exists. `tasks.objective` arrives with
# migrate_task_objective_2026_08.sql; until then the row simply has no such
# key, and merging it would compare every cell against None and pull the lot in
# as human edits. Presence is checked PER ROW rather than assumed, so this ships
# before the migration runs — the same shape that worked for meeting priority.
_ACTION_OPTIONAL_FIELDS = {COL_TODO: "objective"}
_PROJECT_FIELDS = {COL_TODO: "objective", COL_DATE: "target_date",
                   COL_RESP: "owner", COL_COMMENTS: "notes"}

# Fields whose value is a DATE column: a cell that doesn't parse is NEVER
# pulled, or a typo would null a real deadline.
_DATE_FIELDS = {"deadline", "target_date"}
# The sheet spells priority 'Urgent/H/M/L'; the database stores 'U/H/M/L'. Like
# a date, the cell is translated on the way in and rendered on the way out, so
# the merge only ever compares canonical values — otherwise "Urgent" and "U"
# read as a divergence and the cell would be rewritten on every single cycle.
_VOCAB_FIELDS = {"priority"}
# NO FIELD IS EVER PULLED BLANK. An empty cell is refreshed from the database,
# never written into it.
#
# This used to cover only title/label, on the reasoning that clearing those
# would erase a task's text while clearing a date might genuinely mean "no
# deadline". Eyal's check #20 — select ten rows, delete — proved that wrong in
# the most direct way available: the hidden identity columns survived, so the
# rows were still recognised, and the reconcile proposed pulling every blank in.
# That single gesture would have erased 8 real deadlines and 10 real assignees.
#
# The asymmetry decides it. An accidental clear is common, silent, and
# destroys data; a deliberate clear is rare and has other routes (Telegram, the
# Tasks tab, saying so in the review). Refusing to pull a blank costs a
# sentence; honouring one costs the data. [2026-08-07]
_NEVER_BLANK = {"title", "label", "deadline", "assignee", "notes",
                "objective", "target_date", "owner", "priority"}
# Statuses that mean the work is finished, for the closed-row pass.
_CLOSED = {"done", "cancelled", "archived"}


def _normalize(value) -> str:
    return str(value if value is not None else "").strip().lower()


def _db_edit_is_newer(db_row: dict, snap: dict) -> bool:
    """Recency tie-break — see core.dates.edit_is_newer_than_sync.

    Shared with the Tasks-tab engine so the two surfaces can never disagree
    about which human edit wins.
    """
    return edit_is_newer_than_sync(db_row.get("manual_set_at"),
                                   snap.get("snapshot_at"))


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
    if field in _VOCAB_FIELDS:
        return _to_db_priority(a) == _to_db_priority(b)
    return False


def _to_db_priority(value) -> str:
    """'Urgent' -> 'U'. '' when the cell holds something off the vocabulary."""
    return PRIORITY_TO_DB.get(_normalize(value), "")


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
            "bad_priorities": 0, "ambiguous_rows": 0, "layout_mismatch": 0,
            "wrong_kind_cells": 0,
            "names_refreshed": 0, "paste_duplicates": 0, "blanks_refused": 0,
            "orphaned_by_deleted_project": 0, "adopted_rows": 0,
            "blocks_added": 0, "project_without_a_tab": 0,
            "matched_existing_project": 0, "db_read_aborted": 0,
            "identities_cleared": 0, "ghost_rows_removed": 0,
            "retired_blocks_removed": 0,
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
        # tab -> {header name: index} AS RESOLVED FROM THAT TAB. Writes must
        # use the same map the read used; a canonical index is only correct
        # while nobody has inserted a column. [2026-08-08 code review]
        self.columns: dict = {}
        # ps_action snapshots to delete alongside their removed rows.
        self.drop_snapshots: list = []
        # Human rows that resolve to an existing entity and must be
        # stamped with its identity so they stop being re-evaluated.
        self.adopt_rows: list = []
        # Active projects with no block on the sheet yet.
        self.new_blocks: list = []            # (tab, anchor_row, [row])
        # Rows whose task no longer exists in the database at all.
        self.ghosts: list = []                # (tab, row_number, uid)

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
            "new_blocks": sum(len(r) for _, _, r in self.new_blocks),
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
            if s["properties"]["title"] not in NON_AREA_TABS]
    if not tabs:
        return {}
    resp = sheets_service._execute_with_retry(
        lambda: sheets_service.service.spreadsheets().values().batchGet(
            spreadsheetId=spreadsheet_id,
            ranges=[f"'{t}'!{READ_RANGE}" for t in tabs]))
    return {tab: (vr.get("values") or [])
            for tab, vr in zip(tabs, resp.get("valueRanges", []))}


def _merge_row(row, kind: str, db_row: dict, snap: dict, field_map: dict,
               tab: str, plan: Plan, assignees: _Assignees,
               cols: dict) -> dict:
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
            parsed = (parse_human_date(sheet_val, allow_relative=True)
                      if sheet_val else "")
            if sheet_val and not parsed:
                # Unparseable. Never pulled, cell left exactly as typed — a
                # typo must not be able to null a real deadline.
                plan.bump("bad_dates")
                final[field] = db_val
                continue
            sheet_cmp = parsed or ""
        elif field in _VOCAB_FIELDS:
            mapped = _to_db_priority(sheet_val) if sheet_val else ""
            if sheet_val and not mapped:
                # Off-vocabulary ("urgent!!", "P1"). Treated exactly like an
                # unparseable date: never pulled, cell left as typed, counted.
                plan.bump("bad_priorities")
                final[field] = db_val
                continue
            sheet_cmp = mapped
        else:
            sheet_cmp = sheet_val

        # NO MERGE BASE MEANS NO EDIT. With snap == {} every populated cell
        # differs from a None snapshot, so a row whose snapshot failed to write
        # (the helper logs and returns False rather than raising) or that was
        # injected before its snapshot landed would have its ENTIRE contents
        # pulled into the database and marked sticky — freezing machine-written
        # values as human decisions. The sibling rename rail already refuses to
        # act without a base; this one did not. [2026-08-08 code review]
        edited = (bool(snap)
                  and not _eq(field, sheet_cmp, snap_val, assignees)
                  and not _eq(field, sheet_cmp, db_val, assignees))
        if field in _NEVER_BLANK and not str(sheet_cmp).strip():
            if edited:
                # It looked like an edit and is being refused. Count it: ten of
                # these at once is somebody clearing rows, and the summary
                # should say so rather than reporting a quiet no-op.
                plan.bump("blanks_refused")
            edited = False

        if edited:
            updates[field] = sheet_cmp or None              # Rule 1
            plan.manual_marks.append((id_key, row_id, field))
            plan.bump("pulled")
            final[field] = sheet_cmp
            plan.overrides.append(f"{tab} r{row.row_number}: {field} <- {sheet_cmp!r}")
            # Canonicalise the cell in the SAME cycle. Normalisation used to
            # live only on the no-divergence path, so a freshly typed "12.9"
            # was understood but left looking unrecognised until the following
            # pass — the visible answer to "does it know what I meant?" arriving
            # 30 minutes after the question. [2026-08-07]
            if (field in (_DATE_FIELDS | _VOCAB_FIELDS) and sheet_val
                    and _display(field, sheet_cmp) != sheet_val):
                plan.cell_writes.append(
                    (tab, row.row_number, cols[col],
                     _display(field, sheet_cmp)))
                plan.bump("normalized_dates")
        elif not _eq(field, db_val, sheet_cmp, assignees):
            if db_row.get(f"manual_{field}") and not _db_edit_is_newer(db_row, snap):
                plan.bump("manual_held")                    # Rule 2
                # NOTE: `final` (and therefore the snapshot) keeps the sheet
                # value here, and that is not a mistake to "fix" — the hold
                # branch only fires when sheet == snapshot already, so the two
                # are the same value. Setting it to snap_val instead changes
                # nothing, and when there is NO base it would record None,
                # which is worse.
                #
                # Three of Eyal's labels were nevertheless stuck divergent
                # (sheet "Salesman In Italy", DB "Italy"). The cause is not
                # here: the task carries TWO snapshots, one per surface, and
                # the Tasks tab pulled its own stale cell over the value the
                # Project Status sheet had just written. A second writer, not a
                # bad merge rule. Retiring the Tasks tab as a writer is the
                # fix. [2026-08-08]
                final[field] = sheet_cmp
            else:
                plan.cell_writes.append(
                    (tab, row.row_number, cols[col], _display(field, db_val)))
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
                    (tab, row.row_number, cols[col],
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
            "objective": "objective", "target_date": "target_date",
            "priority": "priority"}.get(field, field)


def _col_index(col: str) -> int:
    """Canonical position — the FALLBACK only. Prefer the tab's resolved map."""
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
    if field in _VOCAB_FIELDS:
        return PRIORITY_TO_SHEET.get(str(value).strip().upper(), str(value))
    return str(value)


def build_plan(grids: dict) -> Plan:
    """Phases 0-2: classify every row and decide what would change. No I/O."""
    plan = Plan()
    assignees = _Assignees()

    db_projects = {p["id"]: p for p in supabase_client.get_canonical_projects(status=None)}
    db_tasks = {t["id"]: t for t in supabase_client.get_ps_tasks()}
    proj_snaps = supabase_client.get_ps_project_snapshots()
    act_snaps = supabase_client.get_sheet_snapshots(entity_type="ps_action")

    # THE DATABASE SIDE NEEDS THE SAME EMPTY-READ GUARD THE SHEET SIDE HAS.
    # get_ps_tasks logs and returns [] on error rather than raising, so a
    # transient Supabase failure would make EVERY row on the sheet a task the
    # database has never heard of. That was harmless while a ghost was merely
    # ignored; it stops being harmless now that a ghost row can be removed.
    if act_snaps and not db_tasks:
        logger.error(
            "[ps-reconcile] ABORTED — read 0 tasks while holding "
            f"{len(act_snaps)} snapshots. Refusing to treat a failed read as "
            "an empty database.")
        plan.bump("db_read_aborted")
        return plan

    seen_tasks: set = set()
    seen_projects: set = set()
    parsed: dict = {}

    # Every project row present ANYWHERE in the workbook. The re-parent check
    # needs this rather than a per-tab set, or a cross-tab move is refused.
    all_project_uids: set = set()
    for _grid in grids.values():
        _blocks, _o, _c = parse_tab(_grid)
        all_project_uids |= {b.project.uid for b in _blocks
                             if b.project and b.project.uid}

    for tab, grid in grids.items():
        # LAYOUT GUARD, BEFORE ANYTHING ELSE. resolve_columns falls back to the
        # canonical position for a header it cannot find — right for one renamed
        # column, catastrophic for a whole different layout. A tab still on the
        # 12-column shape resolves `Topic` onto the old `Action` column and
        # `Priority` onto `_kind`, so the engine would compare an action's text
        # against a task's label and try to pull "A" in as a priority, on every
        # row of every cycle. Skip it and say so. [2026-08-09]
        # A tab that reads back completely empty is not a layout problem — it
        # is either genuinely new or a bad read, and the guard below owns both.
        # Anything with content, though, has to declare its layout.
        header = grid[HEADER_ROW - 1] if len(grid or []) >= HEADER_ROW else []
        missing = unresolved_columns(header) if grid else []
        if missing:
            logger.error(
                f"[ps-reconcile] SKIPPED tab {tab!r} — its header row does not "
                f"declare {missing}. The tab is on an older layout; rebuild it "
                "before the engine will touch it again.")
            plan.skipped_tabs.append(tab)
            plan.bump("layout_mismatch")
            continue

        blocks, orphans, cols = parse_tab(grid)
        plan.fingerprints[tab] = tab_fingerprint(blocks, orphans)
        plan.columns[tab] = cols

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
        # NOTE: the workbook-wide set, not this tab's. See _handle_action.

        # ...UNLESS ITS TEXT HAS CHANGED, IN WHICH CASE IT IS NEW WORK.
        # Eyal copied a row and retyped it, so the copy carried the original's
        # identity while reading something else entirely. Left as a "paste" it
        # was ignored forever — and when the ORIGINAL task was marked done,
        # BOTH rows were queued for deletion, including the one whose text
        # existed nowhere but that cell. Copy-then-retype is a normal way to
        # write a similar line; it must not be a way to lose one. Clearing the
        # stolen identity makes it a plain typed row, and the next cycle
        # creates it. [2026-08-09]
        # It STAYS in `pasted` for this cycle. The identity blanking is a cell
        # write that has not landed yet, so the row still carries the original's
        # uid right now — processing it would merge the retyped text INTO the
        # original task, which is the very damage this is here to prevent. Next
        # cycle it reads as a plain typed row and is created.
        _clear_stolen_identities(dups, db_tasks, db_projects, tab, cols, plan)

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
                                       _PROJECT_FIELDS, tab, plan, assignees,
                                       cols)
                    plan.snapshots.append(("project", proj.uid, tab,
                                           proj.row_number, final))

                    # The project NAME is system-owned here: refreshed from the
                    # DB, never pulled. Renaming belongs on the Projects tab,
                    # which has its own snapshot rail and backfills `label`
                    # across five tables — doing it from two surfaces would give
                    # a consequential operation two masters. Without this the
                    # cell was neither pulled nor pushed, so a stray edit to a
                    # project name sat there diverging from the database
                    # silently and forever. [2026-08-07]
                    # A cell that belongs to the OTHER kind of row. The row's
                    # kind is already declared, so these are not ambiguous —
                    # just unreadable, and they look exactly like saved data.
                    for _c in (COL_TOPIC, COL_ACTION, COL_PRIORITY):
                        if (proj.values.get(_c) or "").strip():
                            plan.bump("wrong_kind_cells")
                            plan.overrides.append(
                                f"{tab} r{proj.row_number}: {_c!r} belongs to an "
                                "ACTION row — nothing reads it on a project row")

                    db_name = db_proj.get("name") or ""
                    if _normalize(proj.values.get(COL_PROJECT)) != _normalize(db_name):
                        plan.cell_writes.append(
                            (tab, proj.row_number, cols[COL_PROJECT], db_name))
                        plan.bump("names_refreshed")
            elif proj is not None and proj.kind == HUMAN_PROJECT:
                # A typed project name that ALREADY EXISTS is that project, not
                # a second one — add_canonical_project is idempotent by name, so
                # creating it would return the same row anyway. Resolving it
                # here instead matters for what comes next: the action rows
                # beneath inherit a real parent, so they can be checked against
                # the work already filed under it. Without this a pasted block
                # whose project row was also pasted gave its actions no parent
                # at all, and every one was recreated as a duplicate with no
                # project — which is exactly what Eyal's check #19 produced.
                # [2026-08-07]
                typed = proj.values.get(COL_PROJECT, "")
                existing = next(
                    (p for p in db_projects.values()
                     if _normalize(p.get("name")) == _normalize(typed)), None)
                if existing:
                    parent_uid = existing["id"]
                    plan.bump("matched_existing_project")
                    # ADOPT IT. Setting parent_uid alone left the row a HUMAN
                    # row forever: its objective/owner/date were never merged,
                    # never snapshotted and never persisted, so everything she
                    # typed on that line was silently discarded and the row was
                    # re-evaluated from scratch every cycle. Stamping identity
                    # makes it a system row, and the normal project merge runs
                    # on it from the next pass. [2026-08-08 code review]
                    plan.adopt_rows.append(
                        {"kind": "project", "tab": tab, "row": proj.row_number,
                         "uid": existing["id"], "parent": existing["id"]})
                    fields = {}
                    for col, field in _PROJECT_FIELDS.items():
                        typed_val = proj.values.get(col, "")
                        if not str(typed_val).strip():
                            continue
                        if field in _DATE_FIELDS:
                            typed_val = parse_human_date(typed_val,
                                                         allow_relative=True)
                            if not typed_val:
                                continue
                        if not _eq(field, typed_val, existing.get(field),
                                   assignees):
                            fields[field] = typed_val
                            plan.manual_marks.append(
                                ("project", existing["id"], field))
                    if fields:
                        plan.project_updates.setdefault(
                            existing["id"], {}).update(fields)
                        plan.bump("pulled", len(fields))
                else:
                    plan.creates.append({"kind": "project", "tab": tab,
                                         "row": proj.row_number,
                                         "name": typed,
                                         "objective": proj.values.get(COL_TODO, ""),
                                         "owner": proj.values.get(COL_RESP, "")})

            for row in block.actions:
                if id(row) in pasted:
                    continue
                _handle_action(row, tab, parent_uid, db_tasks, act_snaps,
                               plan, assignees, seen_tasks, cols,
                               all_project_uids)

        for row in orphans:
            plan.bump("orphans")
            if row.kind in (HUMAN_ACTION, INCOMPLETE, AMBIGUOUS):
                # An action typed above the first project row. Not dropped —
                # losing a line she typed because it sat in the wrong place
                # would be the worst possible behaviour.
                _handle_action(row, tab, "", db_tasks, act_snaps, plan,
                               assignees, seen_tasks, cols,
                               all_project_uids)

    _plan_missing_blocks(parsed, db_projects, seen_projects, plan)
    _plan_retired_blocks(parsed, db_projects, plan)
    _plan_ghost_rows(plan)
    _detect_suppressions(act_snaps, seen_tasks, db_tasks, plan)
    _propose_people(plan, assignees)
    _plan_closed_rows(parsed, db_tasks, plan)
    if getattr(settings, "PROJECT_STATUS_AUTO_INJECT_ENABLED", False):
        _plan_injections(parsed, db_tasks, act_snaps, seen_tasks, plan)
    return plan


_MAX_GHOST_ROWS_PER_CYCLE = 5


def _plan_ghost_rows(plan: Plan) -> None:
    """Queue removal of rows whose task no longer exists. Capped.

    The cap is the same instinct as the suppression cap: five rows vanishing is
    the system tidying up, fifty is a symptom, and the difference has to be
    visible BEFORE the rows are gone rather than after.
    """
    if not plan.ghosts:
        return
    take = plan.ghosts[:_MAX_GHOST_ROWS_PER_CYCLE]
    if len(plan.ghosts) > len(take):
        logger.warning(
            f"[ps-reconcile] {len(plan.ghosts)} row(s) point at tasks that no "
            f"longer exist; removing {len(take)} this cycle. If this number is "
            "not falling, something is deleting tasks out from under the sheet.")
        plan.overrides.append(
            f"{len(plan.ghosts)} row(s) whose task no longer exists — removing "
            f"{len(take)} this cycle")
    for tab, row_number, uid in take:
        plan.row_deletes.append((tab, row_number, uid))
        plan.drop_snapshots.append(uid)
        plan.bump("ghost_rows_removed")


def _plan_closed_rows(parsed: dict, db_tasks: dict, plan: Plan) -> None:
    """A finished row leaves the sheet. All of them, no exceptions.

    There used to be an exception: a row carrying anything of hers — a comment,
    a date she chose — was kept and struck through instead, on the reasoning
    that removing it would throw away the only copy. That reasoning was simply
    wrong. EVERY column on an action row persists to the database: Date ->
    deadline, Resp. -> assignee, Comments -> notes, Action -> title, Topic ->
    label, Priority -> priority. Nothing on the row exists only on the row, so removing it loses
    nothing and the exception was protecting data that was never at risk.

    Its actual effect was the opposite of the intent. Setting a date or an owner
    is the NORMAL thing to do before finishing a task, so in practice almost
    every completed row qualified, stayed, and accumulated. Eyal, after his
    first session: "I do want to have an ability to tick something and wait for
    the last schedule that it will disappear automatically." A tick is an
    instruction, not a suggestion. [2026-08-08]

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
                # The snapshot goes WITH the row. Left behind, a task that
                    # is later re-opened has a snapshot but no sheet row:
                # _detect_suppressions reads that as "she deleted it" and
                # sets ps_suppressed, while _plan_injections refuses to
                # re-add anything that already has a snapshot. The task
                # becomes permanently invisible with no way back short of
                # manual DB surgery. [2026-08-08 code review]
                plan.row_deletes.append((tab, row.row_number, row.uid))
                plan.drop_snapshots.append(row.uid)


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
    from services.project_status_rows import (
        ORIGIN_AUTO, action_row_values, format_provenance)

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
            rows = [
                action_row_values(
                    task, parent_uid=pid,
                    marker=format_provenance(ORIGIN_AUTO, "", ""),
                    fmt_date=lambda v: _display("deadline", v))
                for task in queue[:take]
            ]
            plan.injects.append((tab, block.end_row, rows))


def _handle_action(row, tab: str, parent_uid: str, db_tasks: dict,
                   act_snaps: dict, plan: Plan, assignees: _Assignees,
                   seen_tasks: set, cols: dict,
                   tab_project_uids: set | None = None) -> None:
    tab_project_uids = tab_project_uids if tab_project_uids is not None else set()
    if row.kind == AMBIGUOUS:
        # Project AND Action both filled. The declared rule cannot settle this
        # and guessing is precisely what the declared rule exists to remove, so
        # the row is reported and left exactly as typed.
        plan.bump("ambiguous_rows")
        plan.overrides.append(
            f"{tab} r{row.row_number}: Project and Action are both filled, so "
            "it is unclear which this row is — left untouched. Clear one: "
            "Project names a project, Action is a step underneath one.")
        return

    if row.kind == INCOMPLETE:
        # Date or owner filled but no Action text. Counted and surfaced, never
        # written: a task with no title is worse than no task.
        plan.bump("incomplete")
        return

    if row.kind == HUMAN_ACTION:
        title = row.values.get(COL_ACTION, "")
        # A PASTED BLOCK ARRIVES WITHOUT IDENTITY. Copying rows in Sheets does
        # not reliably bring the hidden columns — if the selection was the
        # visible range, or the columns were hidden at copy time, the pasted
        # rows have no _uid at all. They then look exactly like lines somebody
        # typed, and every one becomes a NEW task duplicating the original.
        # Eyal's check #19 did precisely this. The duplicate-uid path cannot
        # help, because there is no uid to duplicate — so match on the text
        # instead: an identical open title under the same project is a copy,
        # not a new commitment. [2026-08-07]
        if _looks_like_a_copy(title, parent_uid, db_tasks):
            plan.bump("paste_duplicates")
            plan.overrides.append(
                f"{tab} r{row.row_number}: ignored — same text as an existing "
                f"action under this project (looks pasted)")
            return
        plan.creates.append({
            "kind": "task", "tab": tab, "row": row.row_number,
            "title": title,
            "project_id": parent_uid,
            "deadline": parse_human_date(row.values.get(COL_DATE),
                                         allow_relative=True),
            "assignee": row.values.get(COL_RESP, ""),
            "notes": row.values.get(COL_COMMENTS, ""),
            "label": row.values.get(COL_TOPIC, ""),
            "objective": row.values.get(COL_TODO, ""),
            # Off-vocabulary text is dropped rather than stored; the DB default
            # ('M') then applies and the next push writes the cell back.
            "priority": _to_db_priority(row.values.get(COL_PRIORITY)),
        })
        return

    if row.kind != SYSTEM_ACTION or not row.uid:
        return

    db_task = db_tasks.get(row.uid)
    if not db_task:
        # The task is gone from the database — hard-deleted, superseded
        # (valid_to set) or un-approved. The row is the last trace of it and
        # nothing else will ever remove it, so it accumulates.
        #
        # Removal is STRUCTURAL and therefore only happens in a quiet slot,
        # after the fingerprint re-check. It is also capped and it is the ONLY
        # deletion path with no snapshot to fall back on, which is why the
        # empty-read abort above had to come first: a failed read would
        # otherwise make every row on the sheet look like this.
        plan.bump("ghosts")
        plan.ghosts.append((tab, row.row_number, row.uid))
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

    # A row dragged into another project's block is a deliberate re-parent —
    # but ONLY if its own project row is still in this tab. Deleting a project
    # row makes every action beneath it parse into the block ABOVE, so without
    # this check one delete silently re-files all of that project's tasks under
    # a neighbouring project AND marks each sticky, which stops the system ever
    # correcting itself. "The row moved" and "the heading above it vanished"
    # produce identical parses; the surviving project row is what tells them
    # apart. And the _parent CELL is rewritten too — leaving it stale meant the
    # same re-parent was re-applied, with a fresh manual timestamp, on every
    # cycle forever. [2026-08-08 code review]
    if parent_uid and row.parent and parent_uid != row.parent:
        # WORKBOOK-WIDE, not per-tab. Checking only the current tab made a
        # CROSS-TAB move — dragging a task from Product & Technology into the
        # Italy block on Sales — look identical to "the project row above me was
        # deleted", so the move was refused and reported as an orphan. Eyal did
        # exactly that in his first real session. The question is only ever "does
        # the project this row claims still exist anywhere?": if it does, the ROW
        # moved and the re-parent is deliberate; if it does not, the heading was
        # deleted and the rows beneath it merely fell into the block above.
        # [2026-08-08]
        if row.parent in tab_project_uids:
            plan.task_updates.setdefault(row.uid, {})["project_id"] = parent_uid
            plan.manual_marks.append(("task", row.uid, "project_id"))
            plan.cell_writes.append(
                (tab, row.row_number, cols[COL_PARENT], parent_uid))
            plan.bump("reparented")
        else:
            plan.bump("orphaned_by_deleted_project")
            plan.overrides.append(
                f"{tab} r{row.row_number}: its project row is gone — left where "
                "it is rather than re-filed under the block above")

    # `To do` USED TO BE A DEAD END HERE and was reported as one. It is now the
    # action's own objective (tasks.objective), so the warning is gone and the
    # text is merged like any other field. What remains reported is the cell
    # that belongs to the OTHER kind of row — see _report_wrong_kind_cells.
    # [2026-08-09]
    if (row.values.get(COL_PROJECT) or "").strip():
        plan.bump("wrong_kind_cells")
        plan.overrides.append(
            f"{tab} r{row.row_number}: 'Project' names a project, so text there "
            "on an ACTION row is not saved anywhere — it is the column that "
            "makes a row a project")

    fields = dict(_ACTION_FIELDS)
    fields.update({c: f for c, f in _ACTION_OPTIONAL_FIELDS.items()
                   if f in db_task})
    final = _merge_row(row, "task", db_task, snap, fields, tab, plan,
                       assignees, cols)
    final["status"] = "done" if row.checked else "pending"
    plan.snapshots.append(("task", row.uid, tab, row.row_number, final))


def _looks_like_a_copy(title: str, project_id: str, db_tasks: dict) -> bool:
    """Does an open task with this exact text already sit under this project?

    Exact text match on purpose. Two people writing the same sentence
    independently is vanishingly rare; a paste reproduces it character for
    character. Anything fuzzier would start swallowing real follow-up work
    ("Call the bank" twice, a fortnight apart, is two genuine calls).
    """
    key = _normalize(strip_provenance(title))
    if not key or not project_id:
        return False
    for task in db_tasks.values():
        if (task.get("project_id") == project_id
                and _normalize(task.get("status")) not in _CLOSED
                and _normalize(task.get("title")) == key):
            return True
    return False


def _clear_stolen_identities(dups: dict, db_tasks: dict, db_projects: dict,
                             tab: str, cols: dict, plan: Plan) -> None:
    """Blank the identity cells of a duplicate row whose TEXT has changed.

    Only the copy is ever touched, never the topmost row, and only its five
    hidden cells — her text is not modified. Matching is exact: a paste
    reproduces text character for character, so anything else is a retype.
    """
    for uid, rows in dups.items():
        for row in rows[1:]:
            if row.kind == SYSTEM_ACTION:
                db_row = db_tasks.get(uid)
                current = _normalize(strip_provenance(row.values.get(COL_ACTION)))
                known = _normalize(db_row.get("title")) if db_row else ""
            elif row.kind == SYSTEM_PROJECT:
                db_row = db_projects.get(uid)
                current = _normalize(row.values.get(COL_PROJECT))
                known = _normalize(db_row.get("name")) if db_row else ""
            else:
                continue
            if not db_row or not current or current == known:
                continue        # a true paste, or nothing to compare against
            for col in (COL_KIND, COL_UID, COL_PARENT, COL_ORIGIN, COL_SRC):
                plan.cell_writes.append((tab, row.row_number, cols[col], ""))
            plan.bump("identities_cleared")
            plan.overrides.append(
                f"{tab} r{row.row_number}: reads differently from the row it "
                f"was copied from — treated as new work, not a duplicate")



def _plan_retired_blocks(parsed: dict, db_projects: dict, plan: Plan) -> None:
    """A retired project's block leaves the sheet, but only once it is empty.

    Retiring is how a project is removed — Eyal's rule is retire, don't delete,
    so the row has to actually go or the file grows a tail of dead headings that
    nothing will ever clear.

    A block with actions still under it is LEFT ALONE and reported. Removing the
    heading would re-parent every one of those rows into the block above on the
    next parse, which is the same silent mis-filing the re-parent guard exists
    to prevent.
    """
    for tab, blocks in parsed.items():
        if tab in plan.skipped_tabs:
            continue
        for block in blocks:
            proj = block.project
            if proj is None or proj.kind != SYSTEM_PROJECT or not proj.uid:
                continue
            db_proj = db_projects.get(proj.uid)
            if not db_proj or (db_proj.get("status") or "active") != "retired":
                continue
            if block.actions:
                plan.overrides.append(
                    f"{tab} r{proj.row_number}: {db_proj.get('name')!r} is "
                    f"retired but still has {len(block.actions)} action(s) — "
                    "left in place; move or close them first")
                continue
            plan.row_deletes.append((tab, proj.row_number, proj.uid))
            plan.bump("retired_blocks_removed")


def _plan_missing_blocks(parsed: dict, db_projects: dict, seen: set,
                         plan: Plan) -> None:
    """Give an active project a block if it has none.

    NOTHING ELSE CAN. write_project_status_blocks is only reached from the
    one-shot rollout script, the weekly slot became review-prep and writes
    nothing, and _plan_injections can only append actions INTO blocks that
    already exist. So a project created anywhere else — MCP, the Projects tab, a
    seed script — never appeared here, and every task attached to it was
    invisible too: not an empty block, no warning, simply absent from the review
    it belonged to. The only remedy was re-running the rollout by hand.
    [2026-08-08 code review]
    """
    from processors.project_status import _project_area_names, tab_name_for
    from services.project_status_rows import project_row_values

    missing = [p for p in db_projects.values()
               if p["id"] not in seen
               and (p.get("status") or "active") != "retired"]
    if not missing:
        return

    area_of = _project_area_names()
    ends = {}
    for tab, blocks in parsed.items():
        if tab in plan.skipped_tabs:
            continue
        ends[tab] = max((b.end_row for b in blocks), default=FIRST_BODY_ROW)

    for project in sorted(missing, key=lambda r: (r.get("display_order") or 999,
                                                  r.get("name") or "")):
        tab = tab_name_for(area_of.get(project.get("area_id")) or "General")
        if tab not in ends:
            # No tab for that area on this sheet — report rather than guess.
            plan.bump("project_without_a_tab")
            continue
        # `#` is left blank: the structural pass renumbers every tab
        # contiguously right after this, so writing display_order here would
        # only be overwritten by a different number seconds later.
        row = project_row_values(
            project, fmt_date=lambda v: _display("target_date", v))
        plan.new_blocks.append((tab, ends[tab], [row]))
        ends[tab] += 1
        plan.bump("blocks_added")


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

    # A CLOSED task leaving the sheet is expected, not a deletion. The view
    # renders open work only, so a task ticked done vanishes on the next
    # rebuild — suppressing it would record a decision nobody made, and its
    # snapshot lingering is what made the cutover's own count disagree.
    gone = [tid for tid in act_snaps
            if tid not in seen and tid in db_tasks
            and _normalize(db_tasks[tid].get("status")) not in _CLOSED
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
    keeps her value immediately.

    Scans EDITS as well as new rows. It used to look only at `creates`, so
    typing an unknown name onto a row that already existed — by far the more
    common gesture in a review, and how Eyal hit it on 2026-08-07 with "Ayala" —
    pulled the name into the database and raised nothing. The person stayed
    unknown to the roster, silently, which is exactly the case the proposal
    exists to catch.
    """
    names = set()
    for create in plan.creates:
        raw = str(create.get("assignee") or "").strip()
        if raw and not assignees.known(raw):
            names.add(raw)
    for fields in list(plan.task_updates.values()) + list(plan.project_updates.values()):
        for key in ("assignee", "owner"):
            raw = str(fields.get(key) or "").strip()
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


def _row_still_matches(sheets_service, spreadsheet_id: str, spec: dict) -> bool:
    """Is the row we planned to adopt still the row at that number?

    Compares the text we based the decision on against the cell as it is right
    now. Cheap (one row read per create, and creates are capped) and it makes
    inserting rows mid-cycle safe: a shifted row is simply deferred, and the
    next cycle re-plans it against fresh numbers with nothing lost.
    """
    from services.project_status_rows import ALL_HEADERS, resolve_columns

    try:
        values = sheets_service._execute_with_retry(
            lambda: sheets_service.service.spreadsheets().values().get(
                spreadsheetId=spreadsheet_id,
                range=f"'{spec['tab']}'!A{spec['row']}:L{spec['row']}")
        ).get("values", [[]])
    except Exception as e:                                  # noqa: BLE001
        logger.warning(f"[ps-reconcile] re-read failed for {spec['tab']} "
                       f"r{spec['row']}: {e}")
        return False        # never adopt on an unverified row

    row = (values[0] if values else []) + [""] * len(ALL_HEADERS)
    cols = resolve_columns(ALL_HEADERS)
    if str(row[cols[COL_KIND]] or "").strip():
        return False        # something already claimed it

    expected = (spec.get("title") if spec["kind"] == "task" else spec.get("name"))
    column = COL_ACTION if spec["kind"] == "task" else COL_PROJECT
    return _normalize(row[cols[column]]) == _normalize(expected)


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
        # THE TAB IS THE AREA. A project typed into "LEGAL, CORPORATE & FINANCE"
        # belongs to that area — creating it without one leaves it homeless, and
        # the next rebuild invents a "General" tab to put it in, which is not a
        # real area and immediately disagrees with the curated list. Eyal's new
        # project landed there on 2026-08-07. [2026-08-07]
        area_id = None
        try:
            for row in (supabase_client.client.table("areas")
                        .select("id,name").execute().data or []):
                if _normalize(row.get("name")) == _normalize(spec.get("tab")):
                    area_id = row["id"]
                    break
        except Exception as e:                              # noqa: BLE001
            logger.warning(f"[ps-reconcile] area lookup failed for "
                           f"{spec.get('tab')!r}: {e}")
        # add_canonical_project is idempotent by name, so a name matching a
        # RETIRED project reactivates it instead of creating a duplicate.
        row = supabase_client.add_canonical_project(name=name, area_id=area_id)
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
        # THE PRIORITY SHE TYPED. `_handle_action` collects it into the spec and
        # a test asserts the plan carries it, but it was never passed on — so
        # create_task applied its own 'M' default, and on the very next cycle
        # the Rule-4 push wrote "M" back into the cell she had typed "Urgent"
        # into. Her decision was overwritten within 30 minutes, by the system,
        # in the cell she made it in. [2026-08-09 code review]
        priority=spec.get("priority") or "M",
        deadline=spec.get("deadline") or None,
        project_id=spec.get("project_id") or None,
        notes=spec.get("notes") or None,
        label=spec.get("label") or None,
        approval_status="approved",
    )
    return (created or {}).get("id", ""), "A"


def _a1(index: int) -> str:
    """0-based column index -> A1 letters, past Z."""
    letters = ""
    index += 1
    while index:
        index, rem = divmod(index - 1, 26)
        letters = chr(65 + rem) + letters
    return letters


def _write_identity(sheets_service, spreadsheet_id: str, tab: str, row: int,
                    kind: str, uid: str, parent: str, cols: dict) -> None:
    """Stamp _kind/_uid/_parent into a row we just created a record for.

    Synchronous and per-row on purpose: batching this with the other cell
    writes would make one failure indistinguishable from all of them, and the
    rollback below has to know exactly which create lost its identity.
    """
    # Addressed through the tab's OWN resolved columns, not a hard-coded H:J.
    # If someone inserts a column, H:J are three of her VISIBLE cells — stamping
    # there blanks her text and leaves the real identity columns empty, so the
    # row reads as fresh human input next cycle and the task is created a second
    # time. Written as one contiguous range only when the three are adjacent,
    # which they are in the standard layout. [2026-08-08 code review]
    from services.project_status_rows import COL_KIND, COL_PARENT, COL_UID

    idx = [cols[COL_KIND], cols[COL_UID], cols[COL_PARENT]]
    contiguous = idx == list(range(idx[0], idx[0] + 3))
    try:
        if contiguous:
            data = [{"range": f"'{tab}'!{_a1(idx[0])}{row}:{_a1(idx[2])}{row}",
                     "values": [[kind, uid, parent or uid]]}]
        else:
            data = [{"range": f"'{tab}'!{_a1(c)}{row}", "values": [[v]]}
                    for c, v in zip(idx, [kind, uid, parent or uid])]
        sheets_service._execute_with_retry(
            lambda: sheets_service.service.spreadsheets().values().batchUpdate(
                spreadsheetId=spreadsheet_id,
                body={"valueInputOption": "RAW", "data": data}))
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
    from services.project_status_rows import (
        ALL_HEADERS, COL_ORIGIN, VISIBLE_HEADERS, parse_tab)

    out = {"injected": 0, "rows_deleted": 0, "struck": 0, "stale_tabs": []}

    tabs = {t for t, _, _ in plan.injects} | {t for t, _, _ in plan.row_deletes} \
        | {t for t, _, _ in plan.strikes} | {t for t, _, _ in plan.new_blocks}
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
        # Column spans DERIVED, not written out. These read 0..7 and 10..11 for
        # the 12-column layout; adding two columns would have struck through
        # seven of nine visible cells and stamped 'auto_edited' into Priority.
        origin_col = ALL_HEADERS.index(COL_ORIGIN)
        ops.append((row, {"repeatCell": {
            "range": {"sheetId": gid[tab], "startRowIndex": row - 1,
                      "endRowIndex": row, "startColumnIndex": 0,
                      "endColumnIndex": len(VISIBLE_HEADERS)},
            "cell": {"userEnteredFormat": {"textFormat": {"strikethrough": True}}},
            "fields": "userEnteredFormat.textFormat.strikethrough"}}))
        ops.append((row, {"updateCells": {
            "range": {"sheetId": gid[tab], "startRowIndex": row - 1,
                      "endRowIndex": row, "startColumnIndex": origin_col,
                      "endColumnIndex": origin_col + 1},
            "rows": [{"values": [{"userEnteredValue":
                                  {"stringValue": "auto_edited"}}]}],
            "fields": "userEnteredValue"}}))
        out["struck"] += 1

    for tab, anchor, rows in list(plan.new_blocks) + list(plan.injects):
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
        # Every injected row gets the same treatment a hand-typed one does:
        # checkbox, date validation, the bold provenance chip, and a format
        # reset (inheritFromBefore copies the PROJECT row when a block had no
        # actions yet, so the first injected action would arrive bold like a
        # heading). One shared helper, so an auto row and a manual row can never
        # end up looking different. [2026-08-07]
        # ALL_HEADERS is already imported at the top of this function. Re-importing
        # it here would make no difference today but is the exact shape that broke
        # comms_spine on 2026-07-30: a redundant function-local import makes the
        # name local for the WHOLE body, so any earlier use raises
        # UnboundLocalError. Not worth leaving lying around.
        from services.project_status_sheet import new_row_format_requests
        from services.project_status_rows import KIND_ACTION

        act_col = ALL_HEADERS.index(COL_ACTION)
        kind_col = ALL_HEADERS.index(COL_KIND)
        for offset, new_row in enumerate(rows):
            # A project row must NOT get the action-row treatment (checkbox,
            # date validation, bold reset) — it needs its block fence instead.
            row_kind = str(new_row[kind_col] or KIND_ACTION)
            for req in new_row_format_requests(
                    sheet_id, anchor + offset, row_kind,
                    str(new_row[act_col] or "")):
                ops.append((anchor, req))
        out["injected"] += sum(1 for r in rows if str(r[kind_col] or "") != "P")
        out["blocks_added"] = out.get("blocks_added", 0) + sum(
            1 for r in rows if str(r[kind_col] or "") == "P")

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


async def renumber_blocks(spreadsheet_id: str, tabs: list) -> dict:
    """Give every project row a contiguous 1,2,3… within its own tab.

    `#` used to show `canonical_projects.display_order` — a sort key with
    deliberate gaps (10/20/30/90) written once by the seed and never maintained.
    On the sheet it read as an arbitrary number that skipped values and did not
    match the order of the blocks after anything moved, so it carried no
    meaning to the person using the file.

    Recomputed here rather than at build time because it is a property of where
    the blocks SIT, and they move: drag a block, and it renumbers overnight.

    Runs AFTER the structural batch, on a fresh read — inserts and deletes have
    already changed the row numbers by this point, so planning it earlier would
    address the wrong rows. Writes only column A of PROJECT rows, and only where
    the value differs, so a tab that is already correct costs nothing.
    """
    from services.google_sheets import sheets_service
    from services.project_status_rows import (
        COL_NUM, SYSTEM_PROJECT, HUMAN_PROJECT, parse_tab)

    tabs = sorted(set(tabs or []))
    if not tabs:
        return {"renumbered": 0}
    try:
        fresh = sheets_service._execute_with_retry(
            lambda: sheets_service.service.spreadsheets().values().batchGet(
                spreadsheetId=spreadsheet_id,
                ranges=[f"'{t}'!{READ_RANGE}" for t in tabs]))
    except Exception as e:                                  # noqa: BLE001
        logger.warning(f"[ps-reconcile] renumber read failed: {e}")
        return {"renumbered": 0}

    data = []
    for tab, vr in zip(tabs, fresh.get("valueRanges", [])):
        values = vr.get("values") or []
        if not values:
            # EMPTY READ ABORTS. A transient empty response would otherwise be
            # read as "this tab has no projects", which writes nothing here but
            # is the same reflex that has to hold everywhere.
            continue
        blocks, _orphans, cols = parse_tab(values)
        col = cols.get(COL_NUM, 0)
        n = 0
        for block in blocks:
            proj = block.project
            if proj is None or proj.kind not in (SYSTEM_PROJECT, HUMAN_PROJECT):
                continue
            n += 1
            if str(proj.values.get(COL_NUM, "")).strip() == str(n):
                continue
            data.append({
                "range": f"'{tab}'!{_a1_cell(col, proj.row_number)}",
                "values": [[n]],
            })

    if not data:
        return {"renumbered": 0}
    try:
        sheets_service._execute_with_retry(
            lambda: sheets_service.service.spreadsheets().values().batchUpdate(
                spreadsheetId=spreadsheet_id,
                body={"valueInputOption": "USER_ENTERED", "data": data}))
    except Exception as e:                                  # noqa: BLE001
        logger.warning(f"[ps-reconcile] renumber write failed: {e}")
        return {"renumbered": 0}
    return {"renumbered": len(data)}


def _a1_cell(col_index: int, row_number: int) -> str:
    """0-based column index + 1-based row -> 'A4'. Two letters is plenty here."""
    letters = ""
    n = col_index
    while True:
        letters = chr(ord("A") + n % 26) + letters
        n = n // 26 - 1
        if n < 0:
            break
    return f"{letters}{row_number}"


def _sheet_ids(sheets_service, spreadsheet_id: str) -> dict:
    """{tab title -> sheetId}. Needed by every structural/format request."""
    meta = sheets_service._execute_with_retry(
        lambda: sheets_service.service.spreadsheets().get(
            spreadsheetId=spreadsheet_id, fields="sheets.properties"))
    return {s["properties"]["title"]: s["properties"]["sheetId"]
            for s in meta.get("sheets", [])}


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
    _format_rows: list = []       # (tab, row, kind, action_text) to style after
    cap = getattr(settings, "PROJECT_STATUS_MAX_CREATES_PER_CYCLE", 25)
    if len(plan.creates) > cap:
        logger.warning(
            f"[ps-reconcile] {len(plan.creates)} new rows (cap {cap}) — creating "
            "NONE. A bulk paste must not silently mint hundreds of tasks.")
        plan.creates = []
    for spec in plan.creates:
        try:
            # RE-READ THE ROW BEFORE ADOPTING IT. Row numbers came from a read
            # that has already happened; if a row was INSERTED above this one in
            # the meantime — which is exactly what happens when someone adds
            # rows while the cycle runs — the number now addresses a different
            # line, and the uid would be stamped onto the wrong row. That row
            # would then be treated as the task, and the line actually typed
            # would be created again next cycle, forever. [2026-08-07]
            if not _row_still_matches(sheets_service, spreadsheet_id, spec):
                logger.info(
                    f"[ps-reconcile] {spec['tab']} r{spec['row']} moved since the "
                    "read — deferring the create to the next cycle.")
                continue
            new_id, kind = _create_entity(spec)
            if not new_id:
                continue
            _write_identity(sheets_service, spreadsheet_id, spec["tab"],
                            spec["row"], kind, new_id, spec.get("project_id", ""),
                            plan.columns.get(spec["tab"])
                            or resolve_columns(ALL_HEADERS))
            applied["created"] += 1
            # Style it NOW, not at the next structural pass. A line she typed
            # this morning has to be tickable this morning; the colour rules
            # already cover themselves, but a checkbox does not.
            _format_rows.append((spec["tab"], spec["row"], kind,
                                 spec.get("title", "")))
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

    # 4.1c Stamp identity on rows that resolved to an EXISTING entity. Without
    #      this they stay human rows and are re-resolved from scratch forever.
    for spec in plan.adopt_rows:
        try:
            _write_identity(sheets_service, spreadsheet_id, spec["tab"],
                            spec["row"], "P" if spec["kind"] == "project" else "A",
                            spec["uid"], spec.get("parent", ""),
                            plan.columns.get(spec["tab"])
                            or resolve_columns(ALL_HEADERS))
            applied["adopted"] = applied.get("adopted", 0) + 1
            _format_rows.append((spec["tab"], spec["row"],
                                 "P" if spec["kind"] == "project" else "A", ""))
        except Exception as e:                              # noqa: BLE001
            logger.warning(f"[ps-reconcile] adopt {spec['uid']} failed: {e}")

    # 4.2b Style the rows we just adopted. Row numbers are unchanged here — a
    #      create writes into a row that already physically exists, so nothing
    #      has shifted and these are safe outside the structural slot.
    if _format_rows:
        from services.project_status_sheet import new_row_format_requests
        gid = _sheet_ids(sheets_service, spreadsheet_id)
        reqs = [r for tab, row, kind, text in _format_rows
                if tab in gid
                for r in new_row_format_requests(gid[tab], row, kind, text)]
        if reqs:
            try:
                sheets_service._execute_with_retry(
                    lambda: sheets_service.service.spreadsheets().batchUpdate(
                        spreadsheetId=spreadsheet_id, body={"requests": reqs}))
            except Exception as e:                          # noqa: BLE001
                # Cosmetic only — the row exists and is correct in the DB. The
                # next structural pass repaints it.
                logger.warning(f"[ps-reconcile] styling new rows failed: {e}")

    # 4.2c RE-BASELINE after our own identity writes. The structural guard
    #      compares the tab against the fingerprint taken at read time — but
    #      stamping a uid on a row we just adopted CHANGES that tab's sequence,
    #      so the engine's own writes read as human interference and every tab
    #      with a new row had its structural work silently skipped. Exactly the
    #      busiest tabs, every time. Re-read the affected tabs so the baseline
    #      reflects what WE did; anything a human does after this still trips it.
    #      [2026-08-08 code review]
    touched = {t for t, _r, _k, _x in _format_rows}
    if structural and touched:
        try:
            fresh = sheets_service._execute_with_retry(
                lambda: sheets_service.service.spreadsheets().values().batchGet(
                    spreadsheetId=spreadsheet_id,
                    ranges=[f"'{t}'!{READ_RANGE}" for t in sorted(touched)]))
            for tab, vr in zip(sorted(touched), fresh.get("valueRanges", [])):
                blocks, orphans, _ = parse_tab(vr.get("values") or [])
                plan.fingerprints[tab] = tab_fingerprint(blocks, orphans)
        except Exception as e:                              # noqa: BLE001
            logger.warning(f"[ps-reconcile] re-baseline failed, structural will "
                           f"skip those tabs: {e}")

    # 4.3/4.4 Structural pass. Only in the allowed slots, and only after
    #         re-reading the sheet to prove nobody moved anything since we
    #         computed these row numbers.
    if structural and (plan.injects or plan.row_deletes or plan.strikes
                       or plan.new_blocks):
        applied.update(await _apply_structural(plan, spreadsheet_id))

    # 4.4a Renumber. Every tab we read, not only the ones with structural work —
    #      dragging a block changes the numbering without producing a single
    #      insert or delete, and that is the common case.
    if structural:
        applied.update(await renumber_blocks(spreadsheet_id,
                                             list(plan.fingerprints)))

    # 4.4b Drop the snapshots of rows the structural pass removed. Only after
    #      the batch succeeded — dropping a snapshot for a row that is still
    #      there would make the next cycle treat that row as having no merge
    #      base.
    if structural and plan.drop_snapshots:
        for task_id in plan.drop_snapshots:
            try:
                supabase_client.client.table("sheet_snapshots").delete().eq(
                    "task_id", task_id).eq("entity_type", "ps_action").execute()
            except Exception as e:                          # noqa: BLE001
                logger.warning(f"[ps-reconcile] snapshot drop {task_id}: {e}")

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
                    priority=values.get("priority"),
                    assignee=values.get("assignee"),
                    title=values.get("title"), label=values.get("label"),
                    entity_type="ps_action", notes=values.get("notes"),
                    objective=values.get("objective"), sheet_tab=tab)
        except Exception as e:                              # noqa: BLE001
            logger.warning(f"[ps-reconcile] snapshot {row_id}: {e}")
    return applied
