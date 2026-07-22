"""
Rule 2 rail: reconcile must NEVER revert a manually-set field. [2026-07-22]

Background — the bug these tests pin. The `manual_*` columns were written by five
paths but read by exactly ONE (cross_reference.py, manual_status only). Rule 4
(the DB->Sheet refresh) ignored all six, so the sequence

    Eyal edits a cell -> Rule 1 pulls it + marks sticky -> a system/inference path
    later writes that field in the DB -> next reconcile takes the Rule 4 branch
    (cell == snapshot) and pushes the system value back over Eyal's

silently reverted his edit and counted it as a generic "pushed". With Nechama
editing the sheet daily this becomes a routine data-loss path.

The rail: when `manual_<field>` is set and the DB has diverged, HOLD the human's
cell and surface the divergence (summary["manual_held"]) instead of reverting.
The authoritative human paths (Telegram, MCP) write the cell as well as the DB,
so a DB-only divergence on a sticky field is by definition a system write.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

try:
    import processors.sheets_sync as ss
except Exception as e:  # pragma: no cover
    pytest.skip(f"cannot import sheets_sync ({e})", allow_module_level=True)


_AREAS = [{"name": "PRODUCT & TECHNOLOGY"}, {"name": "SALES & BUSINESS DEVELOPMENT"}]


def _sheet_row(**kw):
    base = {"id": "", "task": "", "label": "", "category": "", "source_meeting": "",
            "priority": "M", "assignee": "Eyal Zror", "deadline": "", "status": "pending",
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
    fake._update_cell = AsyncMock(return_value=None)
    monkeypatch.setattr(gs, "sheets_service", fake)

    sc = ss.supabase_client
    calls = {"update": [], "manual": [], "snapshot": []}
    monkeypatch.setattr(sc, "get_tasks", lambda **k: db)
    monkeypatch.setattr(sc, "get_sheet_snapshots", lambda *a, **k: snap)
    monkeypatch.setattr(sc, "get_areas", lambda *a, **k: _AREAS)
    monkeypatch.setattr(sc, "update_task", lambda tid, **u: calls["update"].append((tid, u)) or {"id": tid})
    monkeypatch.setattr(sc, "mark_task_field_manual",
                        lambda tid, f, src: calls["manual"].append((tid, f, src)) or True)
    monkeypatch.setattr(sc, "upsert_sheet_snapshot", lambda *a, **k: calls["snapshot"].append(a) or True)
    monkeypatch.setattr(sc, "create_task", lambda **k: {"id": "new-uuid"})
    monkeypatch.setattr(sc, "log_action", lambda *a, **k: None)
    return calls, fake


def _pushed_values(fake):
    call = fake.service.spreadsheets.return_value.values.return_value.batchUpdate.call_args
    if call is None:
        return []
    return [w["values"] for w in call.kwargs["body"]["data"]]


class TestManualRailTasks:
    async def test_sticky_status_is_not_reverted_by_db_change(self, monkeypatch):
        # Eyal set status by hand last cycle (sheet == snapshot, manual_status set).
        # A system path has since written a different status into the DB.
        # Rule 4 must NOT push it over his cell.
        sheet = [_sheet_row(id="t1", task="A", status="in_progress")]
        db = [{"id": "t1", "title": "A", "status": "done", "deadline": None,
               "priority": "M", "assignee": "Eyal Zror", "manual_status": True}]
        snap = {"t1": {"status": "in_progress", "deadline": None,
                       "priority": "M", "assignee": "Eyal Zror"}}
        calls, fake = _setup(monkeypatch, sheet, db, snap)

        res = await ss.reconcile_tasks(shadow=False)

        assert res["manual_held"] == 1
        assert res["pushed"] == 0
        assert [["done"]] not in _pushed_values(fake)     # the revert never happened
        assert calls["update"] == []                       # and nothing was pulled either
        # the snapshot records the HUMAN's value, so a future real edit is still detected
        assert calls["snapshot"][0][2] == "in_progress"
        assert res["manual_held_fields"][0]["field"] == "status"

    async def test_non_sticky_status_still_refreshes(self, monkeypatch):
        # Regression guard: without the manual flag, Rule 4 must behave exactly
        # as before. The rail must not freeze normal DB->Sheet refresh.
        sheet = [_sheet_row(id="t1", task="A", status="in_progress")]
        db = [{"id": "t1", "title": "A", "status": "done", "deadline": None,
               "priority": "M", "assignee": "Eyal Zror"}]
        snap = {"t1": {"status": "in_progress", "deadline": None,
                       "priority": "M", "assignee": "Eyal Zror"}}
        calls, fake = _setup(monkeypatch, sheet, db, snap)

        res = await ss.reconcile_tasks(shadow=False)

        assert res["manual_held"] == 0
        assert res["pushed"] == 1
        assert [["done"]] in _pushed_values(fake)
        assert calls["snapshot"][0][2] == "done"

    async def test_sticky_assignee_is_not_reverted(self, monkeypatch):
        # The assignee case matters most for Nechama: assignee is also the fuzzy
        # matching key, so a silent revert corrupts dedup as well as the roster.
        sheet = [_sheet_row(id="t1", task="A", assignee="Nechama Tik")]
        db = [{"id": "t1", "title": "A", "status": "pending", "deadline": None,
               "priority": "M", "assignee": "Eyal Zror", "manual_assignee": True}]
        snap = {"t1": {"status": "pending", "deadline": None,
                       "priority": "M", "assignee": "Nechama Tik"}}
        calls, fake = _setup(monkeypatch, sheet, db, snap)

        res = await ss.reconcile_tasks(shadow=False)

        assert res["manual_held"] == 1 and res["pushed"] == 0
        assert [["Eyal Zror"]] not in _pushed_values(fake)
        assert calls["snapshot"][0][5] == "Nechama Tik"

    async def test_sticky_label_content_field_is_not_reverted(self, monkeypatch):
        # Content fields (title/label) go through the second Rule 4 branch.
        sheet = [_sheet_row(id="t1", task="A", label="Moldova Pilot")]
        db = [{"id": "t1", "title": "A", "label": "Product V1", "status": "pending",
               "deadline": None, "priority": "M", "assignee": "Eyal Zror",
               "manual_label": True}]
        snap = {"t1": {"status": "pending", "deadline": None, "priority": "M",
                       "assignee": "Eyal Zror", "title": "A", "label": "Moldova Pilot"}}
        calls, fake = _setup(monkeypatch, sheet, db, snap)

        res = await ss.reconcile_tasks(shadow=False)

        assert res["manual_held"] == 1
        assert [["Product V1"]] not in _pushed_values(fake)
        assert calls["snapshot"][0][7] == "Moldova Pilot"   # label slot

    async def test_genuine_new_edit_still_pulls_even_when_sticky(self, monkeypatch):
        # The rail guards Rule 4 only. If Eyal edits the cell AGAIN (sheet !=
        # snapshot), Rule 1 must still pull it — stickiness must not freeze a
        # field against its own owner.
        sheet = [_sheet_row(id="t1", task="A", status="done")]
        db = [{"id": "t1", "title": "A", "status": "pending", "deadline": None,
               "priority": "M", "assignee": "Eyal Zror", "manual_status": True}]
        snap = {"t1": {"status": "in_progress", "deadline": None,
                       "priority": "M", "assignee": "Eyal Zror"}}
        calls, _ = _setup(monkeypatch, sheet, db, snap)

        res = await ss.reconcile_tasks(shadow=False)

        assert res["pulled"] == 1 and res["manual_held"] == 0
        assert calls["update"] == [("t1", {"status": "done"})]


class TestManualRailDecisions:
    def _setup_dec(self, monkeypatch, sheet, db, snap):
        import services.google_sheets as gs
        from config.settings import settings
        monkeypatch.setattr(settings, "DECISION_RECONCILE_ENABLED", True, raising=False)
        monkeypatch.setattr(settings, "TASK_TRACKER_SHEET_ID", "sheet-x", raising=False)

        fake = MagicMock()
        fake.get_all_decisions = AsyncMock(return_value=sheet)
        fake.add_decisions_batch_to_sheet = AsyncMock(return_value=True)
        monkeypatch.setattr(gs, "sheets_service", fake)

        sc = ss.supabase_client
        calls = {"update": [], "snapshot": []}
        monkeypatch.setattr(sc, "list_decisions", lambda *a, **k: db)
        monkeypatch.setattr(sc, "get_decision_snapshots", lambda *a, **k: snap)
        monkeypatch.setattr(sc, "update_decision",
                            lambda did, **u: calls["update"].append((did, u)) or {"id": did})
        monkeypatch.setattr(sc, "mark_decision_field_manual", lambda *a, **k: True)
        monkeypatch.setattr(sc, "upsert_decision_snapshot",
                            lambda *a, **k: calls["snapshot"].append(a) or True)
        monkeypatch.setattr(sc, "log_action", lambda *a, **k: None)
        return calls, fake

    async def test_sticky_decision_description_is_not_reverted(self, monkeypatch):
        sheet = [{"id": "d1", "label": "Moldova Pilot", "decision": "Eyal's wording",
                  "rationale": "r", "confidence": "3", "source_meeting": "",
                  "date": "", "status": "active", "row_number": 2}]
        db = [{"id": "d1", "description": "system wording", "label": "Moldova Pilot",
               "rationale": "r", "confidence": 3, "decision_status": "active",
               "approval_status": "approved", "manual_description": True}]
        snap = {"d1": {"description": "Eyal's wording", "label": "Moldova Pilot",
                       "rationale": "r", "confidence": 3, "decision_status": "active"}}
        calls, fake = self._setup_dec(monkeypatch, sheet, db, snap)

        res = await ss.reconcile_decisions(shadow=False)

        assert res["manual_held"] == 1 and res["pushed"] == 0
        assert calls["update"] == []
        assert calls["snapshot"][0][2] == "Eyal's wording"
