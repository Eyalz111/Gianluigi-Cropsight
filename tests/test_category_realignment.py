"""
Tests for the 2026-06 category realignment + date robustness + archive flow.

Covers:
- core.dates.parse_human_date (the 2026-06-11 NULL-deadline incident class)
- supabase_client.resolve_category (canonical / legacy / unknown / blank)
- supabase_client._serialize_datetime accepting human dates
- TaskStatus.ARCHIVED + get_tasks include_archived filtering
- reconcile: unparseable deadline cells are never pulled (bad_dates)
- reconcile: archived rows move to the Archive tab, no snapshot
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core.dates import parse_human_date
from models.schemas import GENERAL_CATEGORY, TaskStatus


# =============================================================================
# parse_human_date
# =============================================================================

class TestParseHumanDate:
    def test_iso_passthrough(self):
        assert parse_human_date("2026-06-20") == "2026-06-20"

    def test_iso_datetime_truncated(self):
        assert parse_human_date("2026-06-20T10:30:00Z") == "2026-06-20"

    def test_dotted_dmy_two_digit_year(self):
        # The exact format from the 2026-06-11 incident
        assert parse_human_date("20.6.26") == "2026-06-20"

    def test_slash_dmy_full_year(self):
        assert parse_human_date("20/6/2026") == "2026-06-20"

    def test_dash_dmy(self):
        assert parse_human_date("20-6-26") == "2026-06-20"

    def test_day_first_wins_when_ambiguous(self):
        # 5.6.26 = 5 June, not 6 May (Israeli convention)
        assert parse_human_date("5.6.26") == "2026-06-05"

    def test_month_first_fallback_when_day_impossible(self):
        # 6.20.26 can't be DMY (month 20) -> falls back to MDY
        assert parse_human_date("6.20.26") == "2026-06-20"

    def test_date_object(self):
        from datetime import date
        assert parse_human_date(date(2026, 6, 20)) == "2026-06-20"

    def test_unparseable_returns_none(self):
        assert parse_human_date("when the meeting happens") is None

    def test_blank_and_none(self):
        assert parse_human_date("") is None
        assert parse_human_date(None) is None
        assert parse_human_date("   ") is None

    def test_garbage_numeric_returns_none(self):
        assert parse_human_date("99.99.99") is None

    def test_underspecified_inputs_rejected(self):
        # dateutil would "complete" these from today's date — inventing a
        # deadline. The two-default trick must reject them.
        assert parse_human_date("2026") is None
        assert parse_human_date("30") is None
        assert parse_human_date("June") is None
        assert parse_human_date("Jun 20") is None  # year missing

    def test_written_out_full_dates_accepted(self):
        assert parse_human_date("Jun 20 2026") == "2026-06-20"
        assert parse_human_date("20 June 2026") == "2026-06-20"


# =============================================================================
# resolve_category
# =============================================================================

_AREAS = [
    {"id": "a1", "name": "PRODUCT & TECHNOLOGY"},
    {"id": "a2", "name": "SALES & BUSINESS DEVELOPMENT"},
    {"id": "a3", "name": "FUNDRAISING & INVESTOR RELATIONS"},
    {"id": "a4", "name": "LEGAL, CORPORATE & FINANCE"},
    {"id": "a5", "name": "CLIENT DELIVERY & OPERATIONS"},
    {"id": "a6", "name": "TEAM & HUMAN RESOURCES"},
]


class TestResolveCategory:
    @pytest.fixture
    def client(self):
        from services.supabase_client import supabase_client
        return supabase_client

    def test_exact_match_case_insensitive(self, client):
        assert (
            client.resolve_category("product & technology", areas=_AREAS)
            == "PRODUCT & TECHNOLOGY"
        )

    def test_canonical_passthrough(self, client):
        assert (
            client.resolve_category("LEGAL, CORPORATE & FINANCE", areas=_AREAS)
            == "LEGAL, CORPORATE & FINANCE"
        )

    def test_legacy_taxonomy_mapped(self, client):
        assert (
            client.resolve_category("BD & Sales", areas=_AREAS)
            == "SALES & BUSINESS DEVELOPMENT"
        )
        assert (
            client.resolve_category("Finance & Fundraising", areas=_AREAS)
            == "FUNDRAISING & INVESTOR RELATIONS"
        )
        assert (
            client.resolve_category("R&D", areas=_AREAS) == "PRODUCT & TECHNOLOGY"
        )

    def test_blank_none_nonarea_to_general(self, client):
        assert client.resolve_category(None, areas=_AREAS) == GENERAL_CATEGORY
        assert client.resolve_category("", areas=_AREAS) == GENERAL_CATEGORY
        assert client.resolve_category("non-area", areas=_AREAS) == GENERAL_CATEGORY
        assert client.resolve_category("General", areas=_AREAS) == GENERAL_CATEGORY

    def test_unknown_value_kept_as_is(self, client):
        # sheets-wins: never destroy what Eyal typed
        assert client.resolve_category("Spaceships", areas=_AREAS) == "Spaceships"


# =============================================================================
# _serialize_datetime accepts human dates (the NULL-erasure fix)
# =============================================================================

class TestSerializeDatetimeHumanDates:
    def test_dotted_date_no_longer_nulls(self):
        from services.supabase_client import supabase_client
        assert supabase_client._serialize_datetime("20.6.26") == "2026-06-20"

    def test_truly_unparseable_still_none(self):
        from services.supabase_client import supabase_client
        assert supabase_client._serialize_datetime("Friday-ish maybe") is None

    def test_update_task_never_nulls_deadline_from_garbage(self, monkeypatch):
        """A provided-but-unparseable deadline must be DROPPED, not written as
        NULL over a real deadline (the deep-path version of the 2026-06-11 bug)."""
        from services.supabase_client import supabase_client
        captured = {}

        table = MagicMock()
        table.update.side_effect = lambda data: captured.update(data) or table
        table.eq.return_value = table
        table.execute.return_value = MagicMock(data=[{"id": "t-1"}])
        client = MagicMock()
        client.table.return_value = table
        monkeypatch.setattr(supabase_client, "_client", client)

        supabase_client.update_task("t-1", status="pending", deadline="end of Q3 vibes")
        assert "deadline" not in captured
        assert captured["status"] == "pending"

    def test_create_task_canonicalizes_category(self, monkeypatch):
        """create_task is the choke point: any caller's legacy category lands
        canonical in the insert payload."""
        from services.supabase_client import supabase_client
        captured = {}

        table = MagicMock()
        table.insert.side_effect = lambda data: captured.update(data) or table
        table.execute.return_value = MagicMock(data=[{"id": "t-new"}])
        client = MagicMock()
        client.table.return_value = table
        monkeypatch.setattr(supabase_client, "_client", client)
        monkeypatch.setattr(supabase_client, "get_areas", lambda status="active": _AREAS)
        monkeypatch.setattr(supabase_client, "log_action", lambda **kw: None)

        supabase_client.create_task(title="x", assignee="Eyal", category="BD & Sales")
        assert captured["category"] == "SALES & BUSINESS DEVELOPMENT"


# =============================================================================
# TaskStatus.ARCHIVED + schema
# =============================================================================

class TestArchivedStatus:
    def test_enum_has_archived(self):
        assert TaskStatus.ARCHIVED == "archived"

    def test_task_model_accepts_archived_and_str_category(self):
        from models.schemas import Task
        t = Task(
            title="x", assignee="Eyal",
            status=TaskStatus.ARCHIVED,
            category="PRODUCT & TECHNOLOGY",
        )
        assert t.status == TaskStatus.ARCHIVED
        assert t.category == "PRODUCT & TECHNOLOGY"


# =============================================================================
# Reconcile: date safety + archive moves
# =============================================================================

def _mk_sheet_task(**kw):
    base = {
        "row_number": 2, "priority": "M", "label": "", "task": "T1",
        "assignee": "Eyal", "source_meeting": "", "deadline": "",
        "deadline_raw": "", "status": "pending", "category": "",
        "created_date": "2026-06-01", "id": "t-1", "urgency": "M",
    }
    base.update(kw)
    return base


def _mk_db_task(**kw):
    base = {
        "id": "t-1", "title": "T1", "assignee": "Eyal", "priority": "M",
        "deadline": None, "status": "pending", "category": "PRODUCT & TECHNOLOGY",
        "urgency": "M", "label": "",
    }
    base.update(kw)
    return base


@pytest.fixture
def reconcile_env(monkeypatch):
    """Patch sheets_service + supabase_client for reconcile_tasks runs."""
    import processors.sheets_sync as ss

    sheets = MagicMock()
    sheets.get_all_tasks = AsyncMock(return_value=[])
    sheets.add_tasks_batch = AsyncMock(return_value=True)
    sheets.archive_task_rows = AsyncMock(return_value=1)
    sheets.service.spreadsheets.return_value.values.return_value.batchUpdate.return_value.execute.return_value = {}

    from services.supabase_client import supabase_client as _real_sc

    sc = MagicMock()
    sc.get_tasks.return_value = []
    sc.get_sheet_snapshots.return_value = {}
    sc.get_areas.return_value = list(_AREAS)
    # real canonicalization logic, pinned to the test areas list
    sc.resolve_category.side_effect = (
        lambda name, areas=None: _real_sc.resolve_category(name, areas=_AREAS)
    )
    sc.upsert_sheet_snapshot.return_value = True

    monkeypatch.setattr(ss, "supabase_client", sc)
    import services.google_sheets as gs_mod
    monkeypatch.setattr(gs_mod, "sheets_service", sheets)
    return ss, sheets, sc


class TestReconcileDateSafety:
    async def test_unparseable_deadline_cell_not_pulled(self, reconcile_env):
        ss, sheets, sc = reconcile_env
        sheets.get_all_tasks.return_value = [
            _mk_sheet_task(deadline="whenever works", deadline_raw="whenever works"),
        ]
        sc.get_tasks.return_value = [_mk_db_task(deadline="2026-06-28")]
        sc.get_sheet_snapshots.return_value = {
            "t-1": {"status": "pending", "deadline": "2026-06-28",
                    "priority": "M", "assignee": "Eyal"}
        }
        summary = await ss.reconcile_tasks(shadow=False)
        assert summary["bad_dates"] == 1
        # deadline must NOT appear in any DB update
        for call in sc.update_task.call_args_list:
            assert "deadline" not in call.kwargs

    async def test_parseable_nonISO_cell_pulled_as_iso(self, reconcile_env):
        ss, sheets, sc = reconcile_env
        sheets.get_all_tasks.return_value = [
            _mk_sheet_task(deadline="2026-06-20", deadline_raw="20.6.26"),
        ]
        sc.get_tasks.return_value = [_mk_db_task(deadline=None)]
        sc.get_sheet_snapshots.return_value = {
            "t-1": {"status": "pending", "deadline": None,
                    "priority": "M", "assignee": "Eyal"}
        }
        summary = await ss.reconcile_tasks(shadow=False)
        assert summary["bad_dates"] == 0
        assert summary["pulled"] >= 1
        pulled = [c.kwargs for c in sc.update_task.call_args_list if "deadline" in c.kwargs]
        assert pulled and pulled[0]["deadline"] == "2026-06-20"
        assert pulled[0]["deadline_confidence"] == "EXPLICIT"


class TestReconcileDateSafetyHardening:
    """Regressions from the 2026-06-11 code review."""

    async def test_unparseable_cell_text_never_overwritten(self, reconcile_env):
        """The ISO-normalize pass must NOT replace Eyal's unparseable text
        with the DB date — that destroys the edit the bad_dates guard kept."""
        ss, sheets, sc = reconcile_env
        sheets.get_all_tasks.return_value = [
            _mk_sheet_task(deadline="after Moldova trip",
                           deadline_raw="after Moldova trip"),
        ]
        sc.get_tasks.return_value = [_mk_db_task(deadline="2026-07-01")]
        sc.get_sheet_snapshots.return_value = {
            "t-1": {"status": "pending", "deadline": "2026-07-01",
                    "priority": "M", "assignee": "Eyal"}
        }
        await ss.reconcile_tasks(shadow=False)
        # no batched cell write may target the deadline cell
        batch = sheets.service.spreadsheets.return_value.values.return_value.batchUpdate
        for call in batch.call_args_list:
            for w in call.kwargs.get("body", {}).get("data", []):
                assert "!E" not in w["range"], f"deadline cell overwritten: {w}"

    async def test_cleared_cell_writes_explicit_null(self, reconcile_env):
        """Emptying a deadline cell must NULL the DB deadline (update_task
        treats deadline=None as 'not provided', so reconcile writes directly)."""
        ss, sheets, sc = reconcile_env
        sheets.get_all_tasks.return_value = [
            _mk_sheet_task(deadline="", deadline_raw=""),
        ]
        sc.get_tasks.return_value = [_mk_db_task(deadline="2026-06-20")]
        sc.get_sheet_snapshots.return_value = {
            "t-1": {"status": "pending", "deadline": "2026-06-20",
                    "priority": "M", "assignee": "Eyal"}
        }
        await ss.reconcile_tasks(shadow=False)
        upd_calls = sc.client.table.return_value.update.call_args_list
        assert any(
            c.args and c.args[0].get("deadline", "x") is None for c in upd_calls
        ), f"expected explicit NULL deadline write, got {upd_calls}"


class TestReconcileArchiveFlow:
    async def test_sheet_archived_status_moves_row(self, reconcile_env):
        ss, sheets, sc = reconcile_env
        sheets.get_all_tasks.return_value = [_mk_sheet_task(status="archived")]
        sc.get_tasks.return_value = [_mk_db_task(status="pending")]
        sc.get_sheet_snapshots.return_value = {
            "t-1": {"status": "pending", "deadline": None,
                    "priority": "M", "assignee": "Eyal"}
        }
        summary = await ss.reconcile_tasks(shadow=False)
        assert summary["archived"] == 1
        # status pulled to DB
        status_pulls = [c.kwargs for c in sc.update_task.call_args_list if c.kwargs.get("status") == "archived"]
        assert status_pulls
        # row moved
        sheets.archive_task_rows.assert_awaited()
        moved = sheets.archive_task_rows.await_args.args[0]
        assert moved[0]["id"] == "t-1" and moved[0]["status"] == "archived"
        # no snapshot for the archived row
        assert not sc.upsert_sheet_snapshot.called

    async def test_db_archived_task_not_readded(self, reconcile_env):
        ss, sheets, sc = reconcile_env
        sheets.get_all_tasks.return_value = []  # not on sheet
        sc.get_tasks.return_value = [_mk_db_task(status="archived")]
        summary = await ss.reconcile_tasks(shadow=False)
        assert summary["readded"] == 0
        sheets.add_tasks_batch.assert_not_awaited()

    async def test_archive_move_skipped_when_db_update_fails(self, reconcile_env):
        """If the status='archived' DB pull fails, the row must STAY on the
        sheet — deleting it while the task remains open causes the next
        cycle's re-add to resurrect it (archive oscillation)."""
        ss, sheets, sc = reconcile_env
        sheets.get_all_tasks.return_value = [_mk_sheet_task(status="archived")]
        sc.get_tasks.return_value = [_mk_db_task(status="pending")]
        sc.get_sheet_snapshots.return_value = {
            "t-1": {"status": "pending", "deadline": None,
                    "priority": "M", "assignee": "Eyal"}
        }
        sc.update_task.side_effect = Exception("transient PostgREST error")
        await ss.reconcile_tasks(shadow=False)
        sheets.archive_task_rows.assert_not_awaited()

    async def test_shadow_mode_never_writes(self, reconcile_env):
        ss, sheets, sc = reconcile_env
        sheets.get_all_tasks.return_value = [_mk_sheet_task(status="archived")]
        sc.get_tasks.return_value = [_mk_db_task(status="pending")]
        summary = await ss.reconcile_tasks(shadow=True)
        assert summary["archived"] == 1
        sheets.archive_task_rows.assert_not_awaited()
        sc.update_task.assert_not_called()


class TestReconcileCategoryPull:
    async def test_legacy_cell_canonicalized_and_pulled(self, reconcile_env):
        ss, sheets, sc = reconcile_env
        sheets.get_all_tasks.return_value = [
            _mk_sheet_task(category="BD & Sales"),
        ]
        sc.get_tasks.return_value = [_mk_db_task(category="PRODUCT & TECHNOLOGY")]
        sc.get_sheet_snapshots.return_value = {
            "t-1": {"status": "pending", "deadline": None,
                    "priority": "M", "assignee": "Eyal"}
        }
        await ss.reconcile_tasks(shadow=False)
        cat_pulls = [c.kwargs for c in sc.update_task.call_args_list if "category" in c.kwargs]
        assert cat_pulls and cat_pulls[0]["category"] == "SALES & BUSINESS DEVELOPMENT"

    async def test_blank_cell_refreshed_from_db(self, reconcile_env):
        ss, sheets, sc = reconcile_env
        sheets.get_all_tasks.return_value = [_mk_sheet_task(category="")]
        sc.get_tasks.return_value = [_mk_db_task(category="PRODUCT & TECHNOLOGY")]
        sc.get_sheet_snapshots.return_value = {
            "t-1": {"status": "pending", "deadline": None,
                    "priority": "M", "assignee": "Eyal"}
        }
        summary = await ss.reconcile_tasks(shadow=False)
        assert summary["pushed"] >= 1
        # no category DB update
        assert not any("category" in c.kwargs for c in sc.update_task.call_args_list)
