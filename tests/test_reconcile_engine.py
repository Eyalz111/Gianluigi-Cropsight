"""
Tests for the v3 reconcile engine (processors.sheets_sync.reconcile_tasks).

No live DB/Sheets: patch sheets_service + supabase_client with recorders.
Covers Rule 1 (manual pull + sticky), Rule 4 (refresh), UUID match on rename,
create on blank UUID, DB-only re-add (never delete), deadline=>EXPLICIT, shadow.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

try:
    import processors.sheets_sync as ss
except Exception as e:  # pragma: no cover
    pytest.skip(f"cannot import sheets_sync ({e})", allow_module_level=True)


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
    monkeypatch.setattr(gs, "sheets_service", fake)

    sc = ss.supabase_client
    calls = {"update": [], "manual": [], "snapshot": [], "create": [], "readd": []}
    monkeypatch.setattr(sc, "get_tasks", lambda **k: db)
    monkeypatch.setattr(sc, "get_sheet_snapshots", lambda *a, **k: snap)
    monkeypatch.setattr(sc, "update_task", lambda tid, **u: calls["update"].append((tid, u)) or {"id": tid})
    monkeypatch.setattr(sc, "mark_task_field_manual", lambda tid, f, src: calls["manual"].append((tid, f, src)) or True)
    monkeypatch.setattr(sc, "upsert_sheet_snapshot", lambda *a, **k: calls["snapshot"].append(a) or True)
    monkeypatch.setattr(sc, "create_task", lambda **k: calls["create"].append(k) or {"id": "new-uuid"})
    monkeypatch.setattr(sc, "log_action", lambda *a, **k: None)
    fake.add_tasks_batch.side_effect = lambda rows: calls["readd"].extend(rows) or True
    return calls


class TestReconcile:
    async def test_shadow_detects_edit_without_writing(self, monkeypatch):
        sheet = [_sheet_row(id="t1", task="A", status="done")]
        db = [{"id": "t1", "title": "A", "status": "pending", "deadline": None, "priority": "M", "assignee": "Eyal"}]
        snap = {"t1": {"status": "pending", "deadline": None, "priority": "M", "assignee": "Eyal"}}
        calls = _setup(monkeypatch, sheet, db, snap)
        res = await ss.reconcile_tasks(shadow=True)
        assert res["pulled"] == 1
        assert calls["update"] == [] and calls["snapshot"] == []  # shadow writes nothing

    async def test_rule1_pull_and_stick(self, monkeypatch):
        sheet = [_sheet_row(id="t1", task="A", status="done")]
        db = [{"id": "t1", "title": "A", "status": "pending", "deadline": None, "priority": "M", "assignee": "Eyal"}]
        snap = {"t1": {"status": "pending", "deadline": None, "priority": "M", "assignee": "Eyal"}}
        calls = _setup(monkeypatch, sheet, db, snap)
        res = await ss.reconcile_tasks(shadow=False)
        assert res["pulled"] == 1
        assert calls["update"][0][0] == "t1" and calls["update"][0][1]["status"] == "done"
        assert ("t1", "status", "sheet_edit") in calls["manual"]
        assert calls["snapshot"]  # snapshot rewritten on success

    async def test_sheet_deadline_becomes_explicit(self, monkeypatch):
        sheet = [_sheet_row(id="t1", task="A", deadline="2026-06-01")]
        db = [{"id": "t1", "title": "A", "status": "pending", "deadline": None, "priority": "M", "assignee": "Eyal"}]
        snap = {"t1": {"status": "pending", "deadline": None, "priority": "M", "assignee": "Eyal"}}
        calls = _setup(monkeypatch, sheet, db, snap)
        await ss.reconcile_tasks(shadow=False)
        upd = calls["update"][0][1]
        assert upd["deadline"] == "2026-06-01" and upd["deadline_confidence"] == "EXPLICIT"

    async def test_rename_matches_by_uuid_no_duplicate(self, monkeypatch):
        # title differs (Eyal/Gianluigi reworded) but UUID same -> match, content push, no create
        sheet = [_sheet_row(id="t1", task="A renamed", status="pending")]
        db = [{"id": "t1", "title": "A", "status": "pending", "deadline": None, "priority": "M", "assignee": "Eyal"}]
        snap = {"t1": {"status": "pending", "deadline": None, "priority": "M", "assignee": "Eyal"}}
        calls = _setup(monkeypatch, sheet, db, snap)
        res = await ss.reconcile_tasks(shadow=False)
        assert res["matched"] == 1 and res["created"] == 0
        assert res["pushed"] >= 1  # content (title) refreshed DB->Sheet

    async def test_blank_uuid_row_creates(self, monkeypatch):
        sheet = [_sheet_row(id="", task="Brand new task", assignee="Roye")]
        calls = _setup(monkeypatch, sheet, [], {})
        res = await ss.reconcile_tasks(shadow=False)
        assert res["created"] == 1
        assert calls["create"][0]["title"] == "Brand new task"

    async def test_db_only_open_readded_done_not(self, monkeypatch):
        db = [
            {"id": "t2", "title": "Open one", "status": "in_progress", "priority": "M", "assignee": "Eyal"},
            {"id": "t3", "title": "Done one", "status": "done", "priority": "M", "assignee": "Eyal"},
        ]
        calls = _setup(monkeypatch, [], db, {})
        res = await ss.reconcile_tasks(shadow=False)
        assert res["readded"] == 1  # only the open one
        readded_ids = [r["id"] for r in calls["readd"]]
        assert "t2" in readded_ids and "t3" not in readded_ids
