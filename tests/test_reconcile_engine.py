"""
Tests for the v3 reconcile engine (processors.sheets_sync.reconcile_tasks).

No live DB/Sheets: patch sheets_service + supabase_client with recorders.
Covers Rule 1 (manual pull + sticky), Rule 4 (refresh), UUID match on rename,
create on blank UUID, DB-only re-add (never delete), deadline=>EXPLICIT, shadow,
plus the 2026-06 category realignment semantics: category = Gantt-area taxonomy
(canonicalized via resolve_category), unparseable deadline cells never pulled
(bad_dates), and status 'archived' moving rows to the Archive tab.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

try:
    import processors.sheets_sync as ss
except Exception as e:  # pragma: no cover
    pytest.skip(f"cannot import sheets_sync ({e})", allow_module_level=True)


# canned live areas (the canonical task taxonomy post-realignment)
_AREAS = [
    {"name": "PRODUCT & TECHNOLOGY"},
    {"name": "SALES & BUSINESS DEVELOPMENT"},
    {"name": "FUNDRAISING & INVESTOR RELATIONS"},
    {"name": "LEGAL, CORPORATE & FINANCE"},
    {"name": "CLIENT DELIVERY & OPERATIONS"},
    {"name": "TEAM & HUMAN RESOURCES"},
]


def _sheet_row(**kw):
    base = {"id": "", "task": "", "label": "", "category": "", "source_meeting": "",
            "priority": "M", "assignee": "Eyal", "deadline": "", "status": "pending",
            "created_date": "", "row_number": 2}
    base.update(kw)
    return base


def _setup(monkeypatch, sheet, db, snap):
    import services.google_sheets as gs
    fake = MagicMock()
    fake.get_all_tasks = AsyncMock(return_value=sheet)
    fake.add_tasks_batch = AsyncMock(return_value=True)
    fake.add_task = AsyncMock(return_value=True)
    fake.archive_task_rows = AsyncMock(return_value=0)
    # P1-02: the create path now writes the col-J UUID synchronously per-create
    # via _update_cell (not the deferred cell_writes batch), so it must be awaitable.
    fake._update_cell = AsyncMock(return_value=None)
    monkeypatch.setattr(gs, "sheets_service", fake)

    sc = ss.supabase_client
    calls = {"update": [], "manual": [], "snapshot": [], "create": [], "readd": [],
             "get_tasks_kwargs": [], "archive": []}

    def _get_tasks(**k):
        calls["get_tasks_kwargs"].append(k)
        return db

    monkeypatch.setattr(sc, "get_tasks", _get_tasks)
    monkeypatch.setattr(sc, "get_sheet_snapshots", lambda *a, **k: snap)
    monkeypatch.setattr(sc, "get_areas", lambda *a, **k: _AREAS)
    monkeypatch.setattr(sc, "update_task", lambda tid, **u: calls["update"].append((tid, u)) or {"id": tid})
    monkeypatch.setattr(sc, "mark_task_field_manual", lambda tid, f, src: calls["manual"].append((tid, f, src)) or True)
    monkeypatch.setattr(sc, "upsert_sheet_snapshot", lambda *a, **k: calls["snapshot"].append(a) or True)
    monkeypatch.setattr(sc, "create_task", lambda **k: calls["create"].append(k) or {"id": "new-uuid"})
    monkeypatch.setattr(sc, "log_action", lambda *a, **k: None)
    fake.add_tasks_batch.side_effect = lambda rows: calls["readd"].extend(rows) or True
    fake.archive_task_rows.side_effect = (
        lambda rows, reason="manual": calls["archive"].extend(rows) or len(rows))
    return calls, fake


class TestReconcile:
    async def test_aborts_on_empty_read_with_snapshots(self, monkeypatch):
        # 2026-07-10 incident: a transient EMPTY sheet read must NOT drive a mass
        # re-add. With snapshots present (tasks synced before), an empty read aborts.
        db = [{"id": "t1", "title": "Open", "status": "in_progress", "deadline": None,
               "priority": "M", "assignee": "Eyal", "approval_status": "approved"}]
        snap = {"t1": {"status": "in_progress", "deadline": None, "priority": "M", "assignee": "Eyal"}}
        calls, fake = _setup(monkeypatch, [], db, snap)  # sheet reads EMPTY
        res = await ss.reconcile_tasks(shadow=False)
        assert res.get("error") == "sheet_read_empty"
        assert calls["readd"] == [] and calls["update"] == []  # nothing written/re-added

    async def test_empty_read_ok_when_no_snapshots(self, monkeypatch):
        # No snapshots = plausibly a fresh/empty sheet -> genuine first population is
        # allowed (re-add proceeds, guard doesn't fire).
        db = [{"id": "t1", "title": "Open", "status": "in_progress", "deadline": None,
               "priority": "M", "assignee": "Eyal", "approval_status": "approved"}]
        calls, fake = _setup(monkeypatch, [], db, {})  # empty sheet, NO snapshots
        res = await ss.reconcile_tasks(shadow=False)
        assert res.get("error") is None
        assert res["readded"] == 1  # legitimate first-population re-add

    async def test_readd_capped_on_truncated_read(self, monkeypatch):
        # A truncated (non-empty) read: 1 row matches but the DB has 40 approved-open
        # tasks -> re-add would be ~40, over the cap (max(30, matched=1)) -> skipped.
        sheet = [_sheet_row(id="t0", task="Real", status="pending")]
        db = [{"id": "t0", "title": "Real", "status": "pending", "deadline": None,
               "priority": "M", "assignee": "Eyal", "approval_status": "approved"}]
        for i in range(40):
            db.append({"id": f"x{i}", "title": f"T{i}", "status": "pending", "deadline": None,
                       "priority": "M", "assignee": "Eyal", "approval_status": "approved"})
        snap = {"t0": {"status": "pending", "deadline": None, "priority": "M",
                       "assignee": "Eyal", "title": "Real"}}
        calls, fake = _setup(monkeypatch, sheet, db, snap)
        res = await ss.reconcile_tasks(shadow=False)
        assert res["readded"] == 0        # capped — not appended
        assert calls["readd"] == []       # add_tasks_batch never called with the flood

    async def test_shadow_detects_edit_without_writing(self, monkeypatch):
        sheet = [_sheet_row(id="t1", task="A", status="done")]
        db = [{"id": "t1", "title": "A", "status": "pending", "deadline": None, "priority": "M", "assignee": "Eyal"}]
        snap = {"t1": {"status": "pending", "deadline": None, "priority": "M", "assignee": "Eyal"}}
        calls, _ = _setup(monkeypatch, sheet, db, snap)
        res = await ss.reconcile_tasks(shadow=True)
        assert res["pulled"] == 1
        assert calls["update"] == [] and calls["snapshot"] == []  # shadow writes nothing
        # archived tasks are part of the reconcile universe (so an Eyal-typed
        # 'archived' that already reached the DB isn't re-added as sheet-only)
        assert calls["get_tasks_kwargs"][0].get("include_archived") is True
        # the new summary keys exist even when nothing trips them
        assert res["archived"] == 0 and res["bad_dates"] == 0

    async def test_rule1_pull_and_stick(self, monkeypatch):
        sheet = [_sheet_row(id="t1", task="A", status="done")]
        db = [{"id": "t1", "title": "A", "status": "pending", "deadline": None, "priority": "M", "assignee": "Eyal"}]
        snap = {"t1": {"status": "pending", "deadline": None, "priority": "M", "assignee": "Eyal"}}
        calls, _ = _setup(monkeypatch, sheet, db, snap)
        res = await ss.reconcile_tasks(shadow=False)
        assert res["pulled"] == 1
        assert calls["update"][0][0] == "t1" and calls["update"][0][1]["status"] == "done"
        assert ("t1", "status", "sheet_edit") in calls["manual"]
        assert calls["snapshot"]  # snapshot rewritten on success

    async def test_sheet_deadline_becomes_explicit(self, monkeypatch):
        sheet = [_sheet_row(id="t1", task="A", deadline="2026-06-01")]
        db = [{"id": "t1", "title": "A", "status": "pending", "deadline": None, "priority": "M", "assignee": "Eyal"}]
        snap = {"t1": {"status": "pending", "deadline": None, "priority": "M", "assignee": "Eyal"}}
        calls, _ = _setup(monkeypatch, sheet, db, snap)
        await ss.reconcile_tasks(shadow=False)
        upd = calls["update"][0][1]
        assert upd["deadline"] == "2026-06-01" and upd["deadline_confidence"] == "EXPLICIT"

    async def test_rename_matches_by_uuid_no_duplicate(self, monkeypatch):
        # Sheet title differs from DB but UUID matches -> match, no duplicate row.
        # Phase 1: Task text is editable, so a sheet title that differs from BOTH
        # the snapshot and the DB is Eyal's edit -> PULLED to the DB + marked sticky
        # (this is the fix for the silent content-revert; it used to overwrite).
        sheet = [_sheet_row(id="t1", task="A renamed", status="pending")]
        db = [{"id": "t1", "title": "A", "status": "pending", "deadline": None, "priority": "M", "assignee": "Eyal"}]
        snap = {"t1": {"status": "pending", "deadline": None, "priority": "M", "assignee": "Eyal", "title": "A"}}
        calls, _ = _setup(monkeypatch, sheet, db, snap)
        res = await ss.reconcile_tasks(shadow=False)
        assert res["matched"] == 1 and res["created"] == 0  # UUID match -> no duplicate
        assert res["pulled"] >= 1
        assert calls["update"][0][1].get("title") == "A renamed"  # content pulled to DB
        assert ("t1", "title", "sheet_edit") in calls["manual"]

    async def test_content_db_advance_refreshes_sheet(self, monkeypatch):
        # DB title advanced (e.g. inference reworded) while the sheet cell is
        # untouched (== snapshot) -> refresh the Sheet from the DB, do NOT pull.
        sheet = [_sheet_row(id="t1", task="Old title", status="pending")]
        db = [{"id": "t1", "title": "New title", "status": "pending", "deadline": None, "priority": "M", "assignee": "Eyal"}]
        snap = {"t1": {"status": "pending", "deadline": None, "priority": "M", "assignee": "Eyal", "title": "Old title"}}
        calls, _ = _setup(monkeypatch, sheet, db, snap)
        res = await ss.reconcile_tasks(shadow=False)
        assert res["pushed"] >= 1
        assert all("title" not in u for (_, u) in calls["update"])  # no content pull

    async def test_blanked_content_cell_never_nulls_db(self, monkeypatch):
        # Eyal blanked the Task-text cell -> never null the DB title; refresh the
        # cell from the DB instead (a task must keep its text).
        sheet = [_sheet_row(id="t1", task="", status="pending")]
        db = [{"id": "t1", "title": "Keep me", "status": "pending", "deadline": None, "priority": "M", "assignee": "Eyal"}]
        snap = {"t1": {"status": "pending", "deadline": None, "priority": "M", "assignee": "Eyal", "title": "Keep me"}}
        calls, _ = _setup(monkeypatch, sheet, db, snap)
        res = await ss.reconcile_tasks(shadow=False)
        assert all("title" not in u for (_, u) in calls["update"])  # no null pull
        assert res["pushed"] >= 1  # cell refreshed from DB

    async def test_blank_uuid_row_creates(self, monkeypatch):
        # category typed legacy-style is canonicalized on create; the
        # unparseable deadline is dropped (created without one) + flagged
        sheet = [_sheet_row(id="", task="Brand new task", assignee="Roye",
                            category="BD & Sales", deadline="after the demo")]
        calls, _ = _setup(monkeypatch, sheet, [], {})
        res = await ss.reconcile_tasks(shadow=False)
        assert res["created"] == 1
        assert res["bad_dates"] == 1
        created = calls["create"][0]
        assert created["title"] == "Brand new task"
        assert created["category"] == "SALES & BUSINESS DEVELOPMENT"
        assert created["deadline"] is None
        assert created["deadline_confidence"] == "NONE"

    async def test_db_only_open_readded_done_archived_not(self, monkeypatch):
        db = [
            {"id": "t2", "title": "Open one", "status": "in_progress", "priority": "M", "assignee": "Eyal"},
            {"id": "t3", "title": "Done one", "status": "done", "priority": "M", "assignee": "Eyal"},
            {"id": "t4", "title": "Archived one", "status": "archived", "priority": "M", "assignee": "Eyal"},
        ]
        calls, _ = _setup(monkeypatch, [], db, {})
        res = await ss.reconcile_tasks(shadow=False)
        assert res["readded"] == 1  # only the open one
        readded_ids = [r["id"] for r in calls["readd"]]
        assert "t2" in readded_ids and "t3" not in readded_ids and "t4" not in readded_ids
        # post-realignment readd rows carry no area key (category is the taxonomy)
        assert all("area" not in r for r in calls["readd"])
        assert "category" in calls["readd"][0]

    # --- 2026-06 category realignment ------------------------------------

    async def test_category_edit_canonicalized_and_pulled(self, monkeypatch):
        # Eyal types a legacy category in the cell -> canonicalized + pulled to
        # the DB, and the cell itself is rewritten to the canonical area name
        sheet = [_sheet_row(id="t1", task="A", category="BD & Sales")]
        db = [{"id": "t1", "title": "A", "status": "pending", "deadline": None,
               "priority": "M", "assignee": "Eyal", "category": "PRODUCT & TECHNOLOGY"}]
        snap = {"t1": {"status": "pending", "deadline": None, "priority": "M", "assignee": "Eyal"}}
        calls, fake = _setup(monkeypatch, sheet, db, snap)
        res = await ss.reconcile_tasks(shadow=False)
        assert res["pulled"] == 1
        assert calls["update"] == [("t1", {"category": "SALES & BUSINESS DEVELOPMENT"})]
        body = fake.service.spreadsheets.return_value.values.return_value.batchUpdate.call_args.kwargs["body"]
        assert [["SALES & BUSINESS DEVELOPMENT"]] in [w["values"] for w in body["data"]]

    async def test_blank_category_cell_refreshed_from_db(self, monkeypatch):
        sheet = [_sheet_row(id="t1", task="A", category="")]
        db = [{"id": "t1", "title": "A", "status": "pending", "deadline": None,
               "priority": "M", "assignee": "Eyal", "category": "PRODUCT & TECHNOLOGY"}]
        snap = {"t1": {"status": "pending", "deadline": None, "priority": "M", "assignee": "Eyal"}}
        calls, fake = _setup(monkeypatch, sheet, db, snap)
        res = await ss.reconcile_tasks(shadow=False)
        assert res["pushed"] == 1 and res["pulled"] == 0
        assert calls["update"] == []  # nothing pulled into the DB
        body = fake.service.spreadsheets.return_value.values.return_value.batchUpdate.call_args.kwargs["body"]
        assert [["PRODUCT & TECHNOLOGY"]] in [w["values"] for w in body["data"]]

    async def test_unparseable_deadline_never_pulled(self, monkeypatch):
        # the 2026-06-11 data-loss guard: raw text in the deadline cell is
        # flagged (bad_dates), the DB value is kept, nothing is pulled
        sheet = [_sheet_row(id="t1", task="A", deadline="after the demo")]
        db = [{"id": "t1", "title": "A", "status": "pending", "deadline": "2026-06-01",
               "priority": "M", "assignee": "Eyal"}]
        snap = {"t1": {"status": "pending", "deadline": "2026-06-01", "priority": "M", "assignee": "Eyal"}}
        calls, _ = _setup(monkeypatch, sheet, db, snap)
        res = await ss.reconcile_tasks(shadow=False)
        assert res["bad_dates"] == 1 and res["pulled"] == 0
        assert calls["update"] == []
        # snapshot keeps the DB deadline (the cell stays for Eyal to fix)
        assert calls["snapshot"][0][3] == "2026-06-01"

    async def test_sloppy_date_cell_normalized_to_iso(self, monkeypatch):
        # get_all_tasks parses "20.6.26" to ISO in 'deadline' and keeps the raw
        # cell in 'deadline_raw'; reconcile rewrites the cell to ISO
        sheet = [_sheet_row(id="t1", task="A", deadline="2026-06-20",
                            deadline_raw="20.6.26")]
        db = [{"id": "t1", "title": "A", "status": "pending", "deadline": "2026-06-20",
               "priority": "M", "assignee": "Eyal"}]
        snap = {"t1": {"status": "pending", "deadline": "2026-06-20", "priority": "M", "assignee": "Eyal"}}
        calls, fake = _setup(monkeypatch, sheet, db, snap)
        res = await ss.reconcile_tasks(shadow=False)
        assert calls["update"] == []  # no pull — values agree
        body = fake.service.spreadsheets.return_value.values.return_value.batchUpdate.call_args.kwargs["body"]
        assert [["2026-06-20"]] in [w["values"] for w in body["data"]]

    async def test_archived_status_moves_row_no_snapshot(self, monkeypatch):
        # Eyal types 'archived' -> status pulled to DB, row moved to the
        # Archive tab, and NO snapshot is written (the row leaves the view)
        sheet = [_sheet_row(id="t1", task="A", status="archived")]
        db = [{"id": "t1", "title": "A", "status": "pending", "deadline": None,
               "priority": "M", "assignee": "Eyal"}]
        snap = {"t1": {"status": "pending", "deadline": None, "priority": "M", "assignee": "Eyal"}}
        calls, fake = _setup(monkeypatch, sheet, db, snap)
        res = await ss.reconcile_tasks(shadow=False)
        assert res["archived"] == 1
        assert ("t1", {"status": "archived"}) in calls["update"]
        fake.archive_task_rows.assert_awaited_once()
        assert calls["archive"][0]["id"] == "t1"
        # prior_status must come from the DB: the sheet cell already reads
        # 'archived' (that IS the removal signal), so the pre-archive value only
        # survives in the DB. Without it Archive cannot tell finished work from
        # abandoned work. [2026-07-22]
        assert calls["archive"][0]["prior_status"] == "pending"
        assert calls["snapshot"] == []  # archived rows get no snapshot

    async def test_shadow_archived_counts_but_never_moves(self, monkeypatch):
        sheet = [_sheet_row(id="t1", task="A", status="archived")]
        db = [{"id": "t1", "title": "A", "status": "pending", "deadline": None,
               "priority": "M", "assignee": "Eyal"}]
        snap = {"t1": {"status": "pending", "deadline": None, "priority": "M", "assignee": "Eyal"}}
        calls, fake = _setup(monkeypatch, sheet, db, snap)
        res = await ss.reconcile_tasks(shadow=True)
        assert res["archived"] == 1
        fake.archive_task_rows.assert_not_awaited()
        assert calls["update"] == [] and calls["snapshot"] == []
