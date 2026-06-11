"""PR9 (updated for the 2026-06 category realignment) — urgency/category edit-back.

Two surfaces: (a) the Sheet->DB reconcile carries urgency (flag-gated K column)
and Category (always — column G carries the Gantt-area taxonomy), and
(b) `supabase_client.resolve_category` canonicalizes a free-text Category cell /
MCP param against the live areas. The per-task "area" concept (area_id /
area_label FK+label pair) is gone from all task surfaces.
"""
from unittest.mock import patch, AsyncMock

import pytest

from config.settings import settings
from services.supabase_client import supabase_client
from processors import sheets_sync


_AREAS = [
    {"id": "a1", "name": "PRODUCT & TECHNOLOGY"},
    {"id": "a2", "name": "SALES & BUSINESS DEVELOPMENT"},
]


# ---------------------------------------------------------------------------
# resolve_category — the shared Category-name -> canonical-name resolver
# ---------------------------------------------------------------------------
class TestResolveCategory:
    def test_exact_match_case_insensitive(self):
        with patch.object(supabase_client, "get_areas", return_value=_AREAS):
            assert (
                supabase_client.resolve_category("product & technology")
                == "PRODUCT & TECHNOLOGY"
            )

    def test_blank_and_sentinels_are_general(self):
        for v in ("", None, "non-area", "none", "n/a", "-", "general", "General"):
            assert supabase_client.resolve_category(v) == "General"

    def test_legacy_taxonomy_is_mapped(self):
        with patch.object(supabase_client, "get_areas", return_value=_AREAS):
            assert (
                supabase_client.resolve_category("bd & sales")
                == "SALES & BUSINESS DEVELOPMENT"
            )
            assert (
                supabase_client.resolve_category("Product & Tech")
                == "PRODUCT & TECHNOLOGY"
            )

    def test_unknown_value_kept_as_is(self):
        # sheets-wins: never destroy what Eyal typed — QA flags it instead
        with patch.object(supabase_client, "get_areas", return_value=_AREAS):
            assert supabase_client.resolve_category("Marketing") == "Marketing"

    def test_lookup_error_falls_back_to_legacy_map(self):
        with patch.object(supabase_client, "get_areas", side_effect=RuntimeError("db down")):
            assert (
                supabase_client.resolve_category("bd & sales")
                == "SALES & BUSINESS DEVELOPMENT"
            )
            assert supabase_client.resolve_category("Mystery") == "Mystery"


# ---------------------------------------------------------------------------
# _compare_task — the /sync diff path: category always, urgency only when on
# ---------------------------------------------------------------------------
class TestCompareTask:
    def _sheet(self):
        return {
            "status": "pending", "assignee": "Roye",
            "urgency": "H", "category": "SALES & BUSINESS DEVELOPMENT",
        }

    def _db(self):
        return {
            "status": "pending", "assignee": "Roye",
            "urgency": "M", "category": "PRODUCT & TECHNOLOGY",
        }

    def test_off_ignores_urgency_but_detects_category(self):
        with patch.object(settings, "TASK_SHEET_URGENCY_AREA_ENABLED", False):
            ch = sheets_sync._compare_task(self._sheet(), self._db())
        assert "urgency" not in ch
        # category is a first-class task column now — compared unconditionally
        assert ch["category"]["to"] == "SALES & BUSINESS DEVELOPMENT"

    def test_on_detects_both(self):
        with patch.object(settings, "TASK_SHEET_URGENCY_AREA_ENABLED", True):
            ch = sheets_sync._compare_task(self._sheet(), self._db())
        assert ch["urgency"]["to"] == "H"
        assert ch["category"]["to"] == "SALES & BUSINESS DEVELOPMENT"

    def test_on_no_change_when_equal(self):
        eq_sheet = {"urgency": "M", "category": "PRODUCT & TECHNOLOGY"}
        with patch.object(settings, "TASK_SHEET_URGENCY_AREA_ENABLED", True):
            ch = sheets_sync._compare_task(eq_sheet, self._db())
        assert "urgency" not in ch
        assert "category" not in ch


# ---------------------------------------------------------------------------
# reconcile_tasks — the live path pulls urgency (gated) + category edits
# ---------------------------------------------------------------------------
class TestReconcilePull:
    def _setup(self, sheet_category="", db_category=""):
        sheet = [{
            "row_number": 2, "id": "t1", "task": "X", "assignee": "Roye",
            "status": "pending", "deadline": "", "priority": "M",
            "category": sheet_category, "label": "", "urgency": "H",
        }]
        db = [{
            "id": "t1", "title": "X", "assignee": "Roye", "status": "pending",
            "deadline": None, "priority": "M", "urgency": "M",
            "category": db_category,
        }]
        # snapshot matches the sheet's action fields → no action-field pulls,
        # isolating the urgency/category pulls.
        snap = {"t1": {"status": "pending", "deadline": None, "priority": "M", "assignee": "Roye"}}
        return sheet, db, snap

    async def _run(self, flag, **kw):
        sheet, db, snap = self._setup(**kw)
        from services import google_sheets as gs
        with patch.object(gs.sheets_service, "get_all_tasks", AsyncMock(return_value=sheet)), \
             patch.object(sheets_sync.supabase_client, "get_tasks", return_value=db), \
             patch.object(sheets_sync.supabase_client, "get_sheet_snapshots", return_value=snap), \
             patch.object(sheets_sync.supabase_client, "get_areas", return_value=_AREAS), \
             patch.object(sheets_sync.supabase_client, "log_action", return_value=None), \
             patch.object(settings, "TASK_SHEET_URGENCY_AREA_ENABLED", flag):
            # dry_run → compute only, returns the summary
            return await sheets_sync.reconcile_tasks(dry_run=True)

    async def test_on_pulls_urgency(self):
        summary = await self._run(True)
        assert summary["matched"] == 1
        assert summary["pulled"] == 1   # just the urgency diff

    async def test_off_does_not_pull_urgency(self):
        summary = await self._run(False)
        assert summary["matched"] == 1
        assert summary["pulled"] == 0

    async def test_category_cell_edit_is_canonicalized_and_pulled(self):
        # Eyal types a legacy name → canonicalized + pulled, regardless of the
        # urgency flag (category is a first-class column now).
        summary = await self._run(
            False, sheet_category="bd & sales", db_category="General"
        )
        assert summary["matched"] == 1
        assert summary["pulled"] == 1   # the category diff (urgency gated off)


# ---------------------------------------------------------------------------
# resolve_category areas-cache param — a batch caller avoids re-querying
# ---------------------------------------------------------------------------
class TestResolveCategoryCache:
    def test_uses_passed_cache_without_querying(self):
        cache = [{"id": "a9", "name": "CLIENT DELIVERY & OPERATIONS"}]
        with patch.object(supabase_client, "get_areas", side_effect=AssertionError("should not query")):
            assert (
                supabase_client.resolve_category("client delivery & operations", areas=cache)
                == "CLIENT DELIVERY & OPERATIONS"
            )

    def test_empty_cache_keeps_unknown_as_is(self):
        with patch.object(supabase_client, "get_areas", side_effect=AssertionError("should not query")):
            assert supabase_client.resolve_category("Anything", areas=[]) == "Anything"


# ---------------------------------------------------------------------------
# MCP task-tool helpers — the resolution/normalization logic, unit-tested
# (the tools themselves are closures; this is the extracted core)
# ---------------------------------------------------------------------------
from services.mcp_server import _coerce_urgency, _resolve_category_field


class TestMcpTaskHelpers:
    def test_coerce_urgency(self):
        assert _coerce_urgency("h") == "H"
        assert _coerce_urgency(" m ") == "M"
        assert _coerce_urgency(None) == "M"
        assert _coerce_urgency("bogus") == "M"

    def test_resolve_category_field_none_is_skip(self):
        # update_task semantics: category=None leaves the field untouched
        fields, label = _resolve_category_field(None, lambda c: (_ for _ in ()).throw(AssertionError()))
        assert fields == {}
        assert label is None

    def test_resolve_category_field_resolves(self):
        resolver = lambda c: "SALES & BUSINESS DEVELOPMENT"
        fields, label = _resolve_category_field("bd & sales", resolver)
        assert fields == {"category": "SALES & BUSINESS DEVELOPMENT"}
        assert label == "SALES & BUSINESS DEVELOPMENT"
