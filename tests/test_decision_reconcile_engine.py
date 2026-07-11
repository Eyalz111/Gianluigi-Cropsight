"""Tests for the Phase 2 decision reconcile engine (sheets_sync.reconcile_decisions).

No live DB/Sheets: patch sheets_service + supabase_client with recorders. Covers
the empty-read guard, content pull (Rule 1, sticky) + refresh (Rule 4), the
Status MONOTONIC-supersede rule (a stale 'active' cell can't un-retire a DB
superseded decision), DB-only re-add, the enable-flag gate, and snapshot-last.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

try:
    import processors.sheets_sync as ss
except Exception as e:  # pragma: no cover
    pytest.skip(f"cannot import sheets_sync ({e})", allow_module_level=True)


def _srow(**kw):
    base = {"id": "", "label": "", "decision": "", "rationale": "",
            "confidence": "3", "source_meeting": "", "date": "", "status": "active",
            "row_number": 2}
    base.update(kw)
    return base


def _ddec(**kw):
    base = {"id": "d1", "description": "", "label": "", "rationale": "",
            "confidence": 3, "decision_status": "active", "approval_status": "approved"}
    base.update(kw)
    return base


def _snap(dd):
    """A snapshot matching a DB decision (the 'last synced' state)."""
    return {"description": dd.get("description"), "label": dd.get("label"),
            "rationale": dd.get("rationale"), "confidence": dd.get("confidence"),
            "decision_status": dd.get("decision_status")}


def _setup(monkeypatch, sheet, db, snap, enabled=True):
    import services.google_sheets as gs
    from config.settings import settings
    monkeypatch.setattr(settings, "DECISION_RECONCILE_ENABLED", enabled, raising=False)
    monkeypatch.setattr(settings, "TASK_TRACKER_SHEET_ID", "sheet-x", raising=False)

    fake = MagicMock()
    fake.get_all_decisions = AsyncMock(return_value=sheet)
    fake.add_decisions_batch_to_sheet = AsyncMock(return_value=True)
    monkeypatch.setattr(gs, "sheets_service", fake)

    sc = ss.supabase_client
    calls = {"update": [], "manual": [], "snapshot": [], "readd": [], "log": []}
    monkeypatch.setattr(sc, "list_decisions", lambda *a, **k: db)
    monkeypatch.setattr(sc, "get_decision_snapshots", lambda *a, **k: snap)
    monkeypatch.setattr(sc, "update_decision", lambda did, **u: calls["update"].append((did, u)) or {"id": did})
    monkeypatch.setattr(sc, "mark_decision_field_manual", lambda did, f, src: calls["manual"].append((did, f, src)) or True)
    monkeypatch.setattr(sc, "upsert_decision_snapshot", lambda *a, **k: calls["snapshot"].append(a) or True)
    monkeypatch.setattr(sc, "log_action", lambda *a, **k: calls["log"].append(a) or None)
    fake.add_decisions_batch_to_sheet.side_effect = lambda decs, src, dt: calls["readd"].extend(decs) or True
    return calls, fake


def _cell_writes(fake):
    call = fake.service.spreadsheets().values().batchUpdate.call_args
    if call is None:
        return []
    body = call.kwargs.get("body") or call[1].get("body")
    return body["data"]


class TestGateAndGuard:
    async def test_gated_off_skips(self, monkeypatch):
        calls, fake = _setup(monkeypatch, [], [], {}, enabled=False)
        res = await ss.reconcile_decisions()
        assert "skipped" in res
        assert calls["update"] == [] and calls["readd"] == []

    async def test_aborts_on_empty_read_with_snapshots(self, monkeypatch):
        db = [_ddec(id="d1", description="X")]
        snap = {"d1": _snap(db[0])}
        calls, fake = _setup(monkeypatch, [], db, snap)  # sheet reads EMPTY
        res = await ss.reconcile_decisions()
        assert res.get("error") == "sheet_read_empty"
        assert calls["readd"] == [] and calls["update"] == []

    async def test_empty_read_ok_when_no_snapshots(self, monkeypatch):
        db = [_ddec(id="d1", description="X")]
        calls, fake = _setup(monkeypatch, [], db, {})  # no snapshots -> genuine first pop
        res = await ss.reconcile_decisions()
        assert res.get("error") is None
        assert res["readded"] == 1
        assert len(calls["readd"]) == 1


class TestContent:
    async def test_content_pull_marks_sticky(self, monkeypatch):
        db = [_ddec(id="d1", description="Old text")]
        snap = {"d1": _snap(db[0])}                       # snap == db == "Old text"
        sheet = [_srow(id="d1", decision="New text")]     # Eyal edited the cell
        calls, fake = _setup(monkeypatch, sheet, db, snap)
        res = await ss.reconcile_decisions()
        assert res["pulled"] == 1 and res["matched"] == 1
        assert calls["update"] == [("d1", {"description": "New text"})]
        assert ("d1", "description", "sheet_edit") in calls["manual"]
        assert _cell_writes(fake) == []                   # a pull writes no cell

    async def test_null_db_field_blank_cell_no_churn(self, monkeypatch):
        # A DB field that is None + a blank sheet cell must NOT push. The
        # 2026-07-11 cutover bug wrapped values in str() before _normalize, so
        # str(None)="None" never matched the blank cell "" -> permanent push churn.
        db = [_ddec(id="d1", description="X", rationale=None, label=None)]
        snap = {"d1": _snap(db[0])}                        # snapshot also None
        sheet = [_srow(id="d1", decision="X", rationale="", label="")]
        calls, fake = _setup(monkeypatch, sheet, db, snap)
        res = await ss.reconcile_decisions()
        assert res["pushed"] == 0 and res["pulled"] == 0
        assert _cell_writes(fake) == []

    async def test_junk_confidence_cell_self_heals(self, monkeypatch):
        # A stale "None" confidence cell (old rebuild artifact) is not pulled as
        # garbage — it's refreshed from the DB (blank here), then quiesces.
        db = [_ddec(id="d1", description="X", confidence=None)]
        snap = {"d1": _snap(db[0])}
        sheet = [_srow(id="d1", decision="X", confidence="None")]
        calls, fake = _setup(monkeypatch, sheet, db, snap)
        res = await ss.reconcile_decisions()
        assert res["pushed"] >= 1 and res["pulled"] == 0
        assert any(w["values"] == [[""]] for w in _cell_writes(fake))  # D -> blank

    async def test_content_refresh_from_db(self, monkeypatch):
        db = [_ddec(id="d1", description="New from DB")]
        snap = {"d1": _snap(_ddec(id="d1", description="Old"))}  # snap == sheet == "Old"
        sheet = [_srow(id="d1", decision="Old")]
        calls, fake = _setup(monkeypatch, sheet, db, snap)
        res = await ss.reconcile_decisions()
        assert res["pushed"] == 1 and res["pulled"] == 0
        assert calls["update"] == []
        writes = _cell_writes(fake)
        assert any(w["values"] == [["New from DB"]] for w in writes)


class TestStatusMonotonic:
    async def test_stale_active_cell_cannot_resurrect(self, monkeypatch):
        # DB retired the decision (superseded); the Sheet still shows 'active'.
        db = [_ddec(id="d1", description="X", decision_status="superseded")]
        snap = {"d1": _snap(_ddec(id="d1", description="X", decision_status="active"))}
        sheet = [_srow(id="d1", decision="X", status="active")]
        calls, fake = _setup(monkeypatch, sheet, db, snap)
        res = await ss.reconcile_decisions()
        assert res["status_guarded"] == 1
        # never pulled active back to the DB:
        assert all("decision_status" not in u for (_, u) in calls["update"])
        # the Sheet cell is refreshed to the DB's 'superseded':
        writes = _cell_writes(fake)
        assert any(w["values"] == [["superseded"]] for w in writes)

    async def test_forward_hand_retire_pulls(self, monkeypatch):
        # Eyal deliberately sets the Sheet status active -> superseded: that pulls.
        db = [_ddec(id="d1", description="X", decision_status="active")]
        snap = {"d1": _snap(db[0])}
        sheet = [_srow(id="d1", decision="X", status="superseded")]
        calls, fake = _setup(monkeypatch, sheet, db, snap)
        res = await ss.reconcile_decisions()
        assert res["pulled"] == 1
        assert ("d1", {"decision_status": "superseded"}) in calls["update"]
        assert ("d1", "status", "sheet_edit") in calls["manual"]


class TestReaddAndSnapshot:
    async def test_readd_db_only_decision(self, monkeypatch):
        db = [_ddec(id="d1", description="X"), _ddec(id="d2", description="Y")]
        snap = {"d1": _snap(db[0])}
        sheet = [_srow(id="d1", decision="X")]            # d2 missing from the sheet
        calls, fake = _setup(monkeypatch, sheet, db, snap)
        res = await ss.reconcile_decisions()
        assert res["readded"] == 1
        assert [d["id"] for d in calls["readd"]] == ["d2"]

    async def test_snapshot_written_for_matched(self, monkeypatch):
        db = [_ddec(id="d1", description="X")]
        snap = {"d1": _snap(db[0])}
        sheet = [_srow(id="d1", decision="X")]
        calls, fake = _setup(monkeypatch, sheet, db, snap)
        await ss.reconcile_decisions()
        assert any(a[0] == "d1" for a in calls["snapshot"])  # snapshot rewritten last

    async def test_dry_run_writes_nothing(self, monkeypatch):
        db = [_ddec(id="d1", description="Old")]
        snap = {"d1": _snap(db[0])}
        sheet = [_srow(id="d1", decision="New")]
        calls, fake = _setup(monkeypatch, sheet, db, snap)
        res = await ss.reconcile_decisions(dry_run=True)
        assert res["pulled"] == 1                         # computed
        assert calls["update"] == [] and calls["snapshot"] == []  # but not applied


class TestCutoverBootstrap:
    async def test_bootstraps_when_sheet_has_no_ids(self, monkeypatch):
        # Pre-cutover: sheet has A:G rows (decision text) but NO ids, no snapshots.
        db = [_ddec(id="d1", description="X"), _ddec(id="d2", description="Y")]
        sheet = [_srow(id="", decision="X"), _srow(id="", decision="Y")]
        calls, fake = _setup(monkeypatch, sheet, db, {})   # no snapshots
        fake.rebuild_decisions_sheet = AsyncMock(return_value=True)
        res = await ss.reconcile_decisions()
        assert res.get("bootstrapped") == 2
        fake.rebuild_decisions_sheet.assert_awaited_once()
        assert {a[0] for a in calls["snapshot"]} == {"d1", "d2"}  # snapshots seeded
        assert calls["readd"] == []                        # did NOT duplicate via re-add

    async def test_no_bootstrap_in_steady_state(self, monkeypatch):
        db = [_ddec(id="d1", description="X")]
        snap = {"d1": _snap(db[0])}
        sheet = [_srow(id="d1", decision="X")]
        calls, fake = _setup(monkeypatch, sheet, db, snap)
        fake.rebuild_decisions_sheet = AsyncMock(return_value=True)
        res = await ss.reconcile_decisions()
        assert "bootstrapped" not in res
        fake.rebuild_decisions_sheet.assert_not_awaited()

    async def test_dry_run_bootstrap_writes_nothing(self, monkeypatch):
        db = [_ddec(id="d1", description="X")]
        sheet = [_srow(id="", decision="X")]
        calls, fake = _setup(monkeypatch, sheet, db, {})
        fake.rebuild_decisions_sheet = AsyncMock(return_value=True)
        res = await ss.reconcile_decisions(dry_run=True)
        assert res.get("bootstrapped") == 1               # reported
        fake.rebuild_decisions_sheet.assert_not_awaited()  # but not executed
        assert calls["snapshot"] == []
