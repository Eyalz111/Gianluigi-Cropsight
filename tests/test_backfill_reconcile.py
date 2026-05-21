"""
Tests for the v3 reconcile backfill (scripts.backfill_reconcile_v3).

No live DB/Sheets. Covers: ambiguous (title,assignee) aborts with no writes;
clean match assigns + seeds snapshot; sheet-only row left unmatched;
already-has-id is skipped (idempotent).
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

try:
    import scripts.backfill_reconcile_v3 as bf
except Exception as e:  # pragma: no cover
    pytest.skip(f"cannot import backfill ({e})", allow_module_level=True)


def _row(**kw):
    base = {"id": "", "task": "", "assignee": "Eyal", "status": "pending",
            "deadline": "", "priority": "M", "row_number": 2}
    base.update(kw)
    return base


def _setup(monkeypatch, sheet, db):
    import services.google_sheets as gs
    fake = MagicMock()
    fake.get_all_tasks = AsyncMock(return_value=sheet)
    monkeypatch.setattr(gs, "sheets_service", fake)
    sc = bf.supabase_client
    monkeypatch.setattr(sc, "get_tasks", lambda **k: db)
    snaps = []
    monkeypatch.setattr(sc, "upsert_sheet_snapshot", lambda *a, **k: snaps.append(a) or True)
    monkeypatch.setattr(sc, "log_action", lambda *a, **k: None)
    return fake, snaps


class TestBackfill:
    async def test_aborts_on_ambiguous_no_writes(self, monkeypatch):
        sheet = [_row(task="A", row_number=2)]
        db = [{"id": "x", "title": "A", "assignee": "Eyal"},
              {"id": "y", "title": "A", "assignee": "Eyal"}]
        fake, snaps = _setup(monkeypatch, sheet, db)
        res = await bf.run_backfill(apply=True)
        assert res["status"] == "aborted_ambiguous"
        assert snaps == []  # no snapshots written on abort

    async def test_two_sheet_rows_same_key_ambiguous(self, monkeypatch):
        sheet = [_row(task="A", row_number=2), _row(task="A", row_number=3)]
        db = [{"id": "x", "title": "A", "assignee": "Eyal"}]
        fake, snaps = _setup(monkeypatch, sheet, db)
        res = await bf.run_backfill(apply=True)
        assert res["status"] == "aborted_ambiguous"

    async def test_clean_match_assigns_and_seeds(self, monkeypatch):
        sheet = [_row(task="A", row_number=2)]
        db = [{"id": "x", "title": "A", "assignee": "Eyal"}]
        fake, snaps = _setup(monkeypatch, sheet, db)
        res = await bf.run_backfill(apply=True)
        assert res["status"] == "ok" and res["to_assign"] == 1
        assert any(s[0] == "x" for s in snaps)  # snapshot seeded for matched id

    async def test_unmatched_sheet_only(self, monkeypatch):
        sheet = [_row(task="Orphan", row_number=2)]
        fake, snaps = _setup(monkeypatch, sheet, [])
        res = await bf.run_backfill(apply=False)
        assert res["unmatched_sheet_only"] == 1 and res["to_assign"] == 0

    async def test_already_has_id_skipped(self, monkeypatch):
        sheet = [_row(id="existing", task="A", row_number=2)]
        db = [{"id": "existing", "title": "A", "assignee": "Eyal"}]
        fake, snaps = _setup(monkeypatch, sheet, db)
        res = await bf.run_backfill(apply=False)
        assert res["to_assign"] == 0 and res["already_have_id"] == 1
