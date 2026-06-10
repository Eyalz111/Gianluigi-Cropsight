"""PR9 — close the urgency/area edit-back gaps.

Two surfaces: (a) the Sheet->DB reconcile now carries urgency/area, and
(b) `supabase_client.resolve_area` turns a free-text Area into the FK + label
the MCP task tools and the reconcile both write. Everything is gated on
TASK_SHEET_URGENCY_AREA_ENABLED (the columns only exist when it's on), so the
flag-off paths are unchanged.
"""
from unittest.mock import patch, AsyncMock

import pytest

from config.settings import settings
from services.supabase_client import supabase_client
from processors import sheets_sync


# ---------------------------------------------------------------------------
# resolve_area — the shared Area-name -> (id, label) resolver
# ---------------------------------------------------------------------------
class TestResolveArea:
    _AREAS = [{"id": "a1", "name": "Product & Tech"}, {"id": "a2", "name": "BD & Sales"}]

    def test_exact_match_case_insensitive(self):
        with patch.object(supabase_client, "get_areas", return_value=self._AREAS):
            assert supabase_client.resolve_area("product & tech") == ("a1", "Product & Tech")

    def test_blank_and_sentinels_are_non_area(self):
        assert supabase_client.resolve_area("") == (None, "non-area")
        assert supabase_client.resolve_area(None) == (None, "non-area")
        assert supabase_client.resolve_area("non-area") == (None, "non-area")

    def test_no_match_keeps_the_label(self):
        with patch.object(supabase_client, "get_areas", return_value=self._AREAS):
            assert supabase_client.resolve_area("Marketing") == (None, "Marketing")

    def test_lookup_error_keeps_label(self):
        with patch.object(supabase_client, "get_areas", side_effect=RuntimeError("db down")):
            assert supabase_client.resolve_area("BD & Sales") == (None, "BD & Sales")


# ---------------------------------------------------------------------------
# _compare_task — the /sync diff path detects urgency/area edits only when on
# ---------------------------------------------------------------------------
class TestCompareTask:
    def _sheet(self):
        return {"status": "pending", "assignee": "Roye", "urgency": "H", "area": "BD & Sales"}

    def _db(self):
        return {"status": "pending", "assignee": "Roye", "urgency": "M", "area_label": "Product & Tech"}

    def test_off_ignores_urgency_area(self):
        with patch.object(settings, "TASK_SHEET_URGENCY_AREA_ENABLED", False):
            ch = sheets_sync._compare_task(self._sheet(), self._db())
        assert "urgency" not in ch
        assert "area_label" not in ch

    def test_on_detects_both(self):
        with patch.object(settings, "TASK_SHEET_URGENCY_AREA_ENABLED", True):
            ch = sheets_sync._compare_task(self._sheet(), self._db())
        assert ch["urgency"]["to"] == "H"
        assert ch["area_label"]["to"] == "BD & Sales"   # keyed by the DB column

    def test_on_no_change_when_equal(self):
        eq_sheet = {"urgency": "M", "area": "Product & Tech"}
        with patch.object(settings, "TASK_SHEET_URGENCY_AREA_ENABLED", True):
            ch = sheets_sync._compare_task(eq_sheet, self._db())
        assert "urgency" not in ch
        assert "area_label" not in ch


# ---------------------------------------------------------------------------
# reconcile_tasks — the live path pulls an urgency edit Sheet->DB (gated)
# ---------------------------------------------------------------------------
class TestReconcilePull:
    def _setup(self):
        sheet = [{
            "row_number": 2, "id": "t1", "task": "X", "assignee": "Roye",
            "status": "pending", "deadline": "", "priority": "M",
            "category": "", "label": "", "urgency": "H", "area": "",
        }]
        db = [{
            "id": "t1", "title": "X", "assignee": "Roye", "status": "pending",
            "deadline": None, "priority": "M", "urgency": "M", "area_label": "non-area",
        }]
        # snapshot matches the sheet's action fields → no action-field pulls,
        # isolating the urgency pull.
        snap = {"t1": {"status": "pending", "deadline": None, "priority": "M", "assignee": "Roye"}}
        return sheet, db, snap

    async def _run(self, flag):
        sheet, db, snap = self._setup()
        from services import google_sheets as gs
        with patch.object(gs.sheets_service, "get_all_tasks", AsyncMock(return_value=sheet)), \
             patch.object(sheets_sync.supabase_client, "get_tasks", return_value=db), \
             patch.object(sheets_sync.supabase_client, "get_sheet_snapshots", return_value=snap), \
             patch.object(sheets_sync.supabase_client, "log_action", return_value=None), \
             patch.object(settings, "TASK_SHEET_URGENCY_AREA_ENABLED", flag):
            # dry_run → compute only, returns the summary
            return await sheets_sync.reconcile_tasks(dry_run=True)

    async def test_on_pulls_urgency(self):
        summary = await self._run(True)
        assert summary["matched"] == 1
        assert summary["pulled"] == 1   # just the urgency diff

    async def test_off_does_not_pull(self):
        summary = await self._run(False)
        assert summary["matched"] == 1
        assert summary["pulled"] == 0
