"""
PR-E — reconcile / sheet data-integrity (June 2026 audit).

  P1-02 — reconcile writes the new col-J UUID to the sheet SYNCHRONOUSLY per
          create (own _update_cell) before any await; on writeback failure it
          rolls the DB create back so a UUID-less duplicate can't appear.
  P1-03 — compute_sheets_diff matches by col-J UUID first; two tasks sharing
          title+assignee no longer collapse to one key (edit → wrong task).
  P1-04 — re-added DB-only rows are seeded a snapshot so the next cycle doesn't
          read snap={} and pull every field as a phantom "Eyal edit".
  P1-10 — archive_task_rows is idempotent (skips UUIDs already in Archive) and a
          failed DELETE leg raises + logs CRITICAL instead of silently leaving a
          row duplicated on both tabs.
  P1-11 — compute_sheets_diff reads the DB with limit=2000 (was 500).
"""

import logging
from unittest.mock import AsyncMock, MagicMock

import pytest

from config.settings import settings
import processors.sheets_sync as ss


# =============================================================================
# compute_sheets_diff — P1-03 (UUID-first matching) + P1-11 (limit)
# =============================================================================

def _diff_patches(monkeypatch, sheet, db):
    from services import google_sheets as gs
    monkeypatch.setattr(gs.sheets_service, "get_all_tasks", AsyncMock(return_value=sheet))
    monkeypatch.setattr(ss, "_read_decisions_from_sheets", AsyncMock(return_value=[]))
    monkeypatch.setattr(ss.supabase_client, "list_decisions", lambda **k: [])


class TestComputeDiffUuidFirst:
    async def test_shared_title_assignee_do_not_collapse(self, monkeypatch):
        # Two distinct tasks share title+assignee. Each sheet row carries its own
        # col-J UUID. The status edit on u1's row must route to u1 (not u2), and
        # neither must surface as sheets_only / db_only.
        db = [
            {"id": "u1", "title": "Follow up with investor", "assignee": "Eyal",
             "status": "pending", "deadline": None, "priority": "M", "label": "", "category": ""},
            {"id": "u2", "title": "Follow up with investor", "assignee": "Eyal",
             "status": "done", "deadline": None, "priority": "M", "label": "", "category": ""},
        ]
        sheet = [
            {"id": "u1", "task": "Follow up with investor", "assignee": "Eyal",
             "status": "in_progress", "deadline": "", "priority": "M", "label": "",
             "category": "", "row_number": 2},
            {"id": "u2", "task": "Follow up with investor", "assignee": "Eyal",
             "status": "done", "deadline": "", "priority": "M", "label": "",
             "category": "", "row_number": 3},
        ]
        _diff_patches(monkeypatch, sheet, db)
        monkeypatch.setattr(ss.supabase_client, "get_tasks", lambda **k: db)

        diff = await ss.compute_sheets_diff()
        mods = diff["tasks"]["modified"]
        assert len(mods) == 1
        assert mods[0]["db_id"] == "u1"                      # routed to the right task
        assert mods[0]["changes"]["status"]["to"] == "in_progress"
        assert diff["tasks"]["in_sync"] == 1                 # u2 unchanged
        assert diff["tasks"]["sheets_only"] == []
        assert diff["tasks"]["db_only"] == []

    async def test_row_without_uuid_falls_back_to_title_assignee(self, monkeypatch):
        db = [{"id": "u9", "title": "Unique task", "assignee": "Roye",
               "status": "pending", "deadline": None, "priority": "M", "label": "", "category": ""}]
        sheet = [{"id": "", "task": "Unique task", "assignee": "Roye",
                  "status": "done", "deadline": "", "priority": "M", "label": "",
                  "category": "", "row_number": 2}]
        _diff_patches(monkeypatch, sheet, db)
        monkeypatch.setattr(ss.supabase_client, "get_tasks", lambda **k: db)

        diff = await ss.compute_sheets_diff()
        mods = diff["tasks"]["modified"]
        assert len(mods) == 1
        assert mods[0]["db_id"] == "u9"                      # matched by fallback key
        assert mods[0]["changes"]["status"]["to"] == "done"

    async def test_uuid_match_excludes_db_task_from_db_only(self, monkeypatch):
        # A renamed sheet row (UUID matches, title differs) must NOT also appear
        # as db_only under its old title.
        db = [{"id": "u1", "title": "Old title", "assignee": "Eyal",
               "status": "pending", "deadline": None, "priority": "M", "label": "", "category": ""}]
        sheet = [{"id": "u1", "task": "New title", "assignee": "Eyal",
                  "status": "pending", "deadline": "", "priority": "M", "label": "",
                  "category": "", "row_number": 2}]
        _diff_patches(monkeypatch, sheet, db)
        monkeypatch.setattr(ss.supabase_client, "get_tasks", lambda **k: db)

        diff = await ss.compute_sheets_diff()
        assert diff["tasks"]["db_only"] == []
        assert diff["tasks"]["sheets_only"] == []

    async def test_p1_11_limit_is_2000(self, monkeypatch):
        captured = {}

        def _get_tasks(**k):
            captured.update(k)
            return []

        _diff_patches(monkeypatch, [], [])
        monkeypatch.setattr(ss.supabase_client, "get_tasks", _get_tasks)

        await ss.compute_sheets_diff()
        assert captured.get("limit") == 2000


# =============================================================================
# reconcile_tasks — P1-02 (atomic create→UUID) + P1-04 (snapshot seeding)
# =============================================================================

def _sheet_row(**kw):
    base = {"id": "", "task": "", "label": "", "category": "", "source_meeting": "",
            "priority": "M", "assignee": "Eyal", "deadline": "", "status": "pending",
            "created_date": "", "row_number": 2}
    base.update(kw)
    return base


def _recon_setup(monkeypatch, sheet, db, snap, update_cell=None):
    from services import google_sheets as gs
    fake = MagicMock()
    fake.get_all_tasks = AsyncMock(return_value=sheet)
    fake.add_tasks_batch = AsyncMock(return_value=True)
    fake.archive_task_rows = AsyncMock(return_value=0)
    fake._update_cell = update_cell or AsyncMock(return_value=None)
    monkeypatch.setattr(gs, "sheets_service", fake)

    sc = ss.supabase_client
    calls = {"snapshot": [], "create": [], "readd": [], "delete_ids": []}

    monkeypatch.setattr(sc, "get_tasks", lambda **k: db)
    monkeypatch.setattr(sc, "get_sheet_snapshots", lambda *a, **k: snap)
    monkeypatch.setattr(sc, "get_areas", lambda *a, **k: [])
    monkeypatch.setattr(sc, "resolve_category", lambda c, areas=None: c or "General")
    monkeypatch.setattr(sc, "update_task", lambda tid, **u: {"id": tid})
    monkeypatch.setattr(sc, "mark_task_field_manual", lambda *a, **k: True)
    monkeypatch.setattr(sc, "upsert_sheet_snapshot", lambda *a, **k: calls["snapshot"].append(a) or True)
    monkeypatch.setattr(sc, "create_task", lambda **k: calls["create"].append(k) or {"id": "new-uuid"})
    monkeypatch.setattr(sc, "log_action", lambda *a, **k: None)
    fake.add_tasks_batch.side_effect = lambda rows: calls["readd"].extend(rows) or True

    # a client mock that records the rollback delete chain
    client = MagicMock()
    client.table.return_value.delete.return_value.eq.return_value.execute.return_value = None

    def _record_delete(col, val):
        calls["delete_ids"].append(val)
        return client.table.return_value.delete.return_value
    client.table.return_value.delete.return_value.eq.side_effect = _record_delete
    monkeypatch.setattr(sc, "_client", client)
    return calls, fake


class TestReconcileCreateAtomic:
    async def test_create_writes_uuid_synchronously_then_snapshot(self, monkeypatch):
        sheet = [_sheet_row(id="", task="Brand new", assignee="Roye", row_number=4)]
        calls, fake = _recon_setup(monkeypatch, sheet, [], {})
        res = await ss.reconcile_tasks(shadow=False)

        assert res["created"] == 1
        # col-J UUID written immediately via _update_cell (NOT deferred cell_writes)
        fake._update_cell.assert_awaited_once()
        args = fake._update_cell.await_args.args
        assert args[2] == "new-uuid"           # the value
        assert args[1].endswith("J4")          # col J, row 4
        # snapshot still seeded for the new task
        assert any(s[0] == "new-uuid" for s in calls["snapshot"])
        # no rollback on the happy path
        assert calls["delete_ids"] == []

    async def test_writeback_failure_rolls_back_db_create(self, monkeypatch):
        sheet = [_sheet_row(id="", task="Brand new", assignee="Roye", row_number=4)]
        boom = AsyncMock(side_effect=RuntimeError("sheets down"))
        calls, fake = _recon_setup(monkeypatch, sheet, [], {}, update_cell=boom)
        res = await ss.reconcile_tasks(shadow=False)

        # the DB create is rolled back so no UUID-less duplicate survives
        assert calls["delete_ids"] == ["new-uuid"]
        assert res["created"] == 0                       # decremented back
        # and no snapshot is left pointing at the rolled-back task
        assert all(s[0] != "new-uuid" for s in calls["snapshot"])


class TestReconcileReaddSnapshot:
    async def test_readded_rows_seed_snapshot(self, monkeypatch):
        # DB-only open task → re-added to the sheet → a snapshot must be seeded
        # from the values we just wrote (P1-04).
        db = [{"id": "t2", "title": "Open one", "status": "in_progress",
               "priority": "M", "assignee": "Eyal", "deadline": None,
               "approval_status": "approved"}]
        calls, fake = _recon_setup(monkeypatch, [], db, {})
        res = await ss.reconcile_tasks(shadow=False)

        assert res["readded"] == 1
        seeded = [s for s in calls["snapshot"] if s[0] == "t2"]
        assert len(seeded) == 1
        # (task_id, sheet_row, status, deadline, priority, assignee, title, label)
        tid, row, status, deadline, priority, assignee, title, label = seeded[0]
        assert status == "in_progress" and priority == "M" and assignee == "Eyal"
        assert title == "Open one"  # content now carried in the snapshot (Phase 1)


# =============================================================================
# archive_task_rows — P1-10 (idempotent + loud delete failure)
# =============================================================================

def _archive_setup(monkeypatch, existing_uuids, delete_raises=None):
    from services.google_sheets import sheets_service
    svc = MagicMock()
    values = svc.spreadsheets.return_value.values.return_value
    values.get.return_value.execute.return_value = {"values": [[u] for u in existing_uuids]}
    values.append.return_value.execute.return_value = {}
    if delete_raises is not None:
        svc.spreadsheets.return_value.batchUpdate.return_value.execute.side_effect = delete_raises
    else:
        svc.spreadsheets.return_value.batchUpdate.return_value.execute.return_value = {}

    monkeypatch.setattr(sheets_service, "_execute_with_retry", lambda factory, **k: factory().execute())
    monkeypatch.setattr(sheets_service, "_service", svc)
    monkeypatch.setattr(sheets_service, "_ensure_fresh_credentials", lambda: None)
    monkeypatch.setattr(sheets_service, "_ensure_archive_tab", lambda: None)
    monkeypatch.setattr(sheets_service, "_get_sheet_id_by_name", lambda *a, **k: 0)
    monkeypatch.setattr(settings, "TASK_TRACKER_SHEET_ID", "sheet123")
    monkeypatch.setattr(settings, "TASK_TRACKER_TAB_NAME", "Tasks")
    return sheets_service, svc


class TestArchiveIdempotent:
    async def test_skips_existing_archive_uuid_but_deletes_active(self, monkeypatch):
        svc_obj, svc = _archive_setup(monkeypatch, existing_uuids=["dup-1"])
        rows = [
            {"id": "dup-1", "task": "Already archived", "row_number": 5},
            {"id": "fresh-2", "task": "New archive", "row_number": 6},
        ]
        moved = await svc_obj.archive_task_rows(rows)

        # only the un-archived row is appended (no Archive-tab duplication)
        append_body = svc.spreadsheets.return_value.values.return_value.append.call_args.kwargs["body"]
        appended = append_body["values"]
        assert len(appended) == 1
        assert appended[0][9] == "fresh-2"          # col J holds the UUID
        assert moved == 1
        # but BOTH active rows are deleted (the ghost self-heals)
        requests = svc.spreadsheets.return_value.batchUpdate.call_args.kwargs["body"]["requests"]
        deleted = sorted(r["deleteDimension"]["range"]["startIndex"] for r in requests)
        assert deleted == [4, 5]                     # startIndex = row-1 → rows 5 & 6

    async def test_delete_failure_raises_and_logs_critical(self, monkeypatch, caplog):
        svc_obj, svc = _archive_setup(
            monkeypatch, existing_uuids=[], delete_raises=ValueError("delete boom")
        )
        rows = [{"id": "x1", "task": "T", "row_number": 3}]
        with caplog.at_level(logging.CRITICAL, logger="services.google_sheets"):
            with pytest.raises(ValueError):
                await svc_obj.archive_task_rows(rows)

        # the append DID happen (row is now safely in Archive) — that's why the
        # delete failure must be loud rather than "rows stay put".
        assert svc.spreadsheets.return_value.values.return_value.append.called
        assert any("DELETE leg failed" in r.message for r in caplog.records
                   if r.levelno >= logging.CRITICAL)
