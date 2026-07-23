"""
Tasks sheet 'Last Update' column (L) — the staleness signal. [2026-07-22]

Why it exists: deadlines are legitimately optional here (75% of open tasks have
none, by design), so a due-date view renders nearly empty and reads as a defect
list when it isn't one. Staleness always applies — but `updated_at` was not in
the sheet at all (col I is *Created*), so it could not be sorted on or
conditionally formatted. Sorting by this column IS the weekly review agenda.

Invariants pinned here:
  - appended at L, AFTER the col-J UUID and col-K Urgency (never relocate J —
    reconcile keys the Sheet<->DB match on it)
  - one-way DB -> Sheet: a human editing it is editing a system field
  - the column is protected, and NOT contiguous with H:J, so it needs its own
    protected range (Urgency sits between them and stays editable)
"""

from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture
def gs_on(monkeypatch):
    """google_sheets with both append-column flags ON (prod-like).

    Deliberately NOT importlib.reload(): reload rebinds the module's globals to
    NEW objects, while every other module that did `from services.google_sheets
    import TASK_COLUMNS` keeps a reference to the OLD dict — so a reload here
    silently desynchronises sheets_sync and friends for the rest of the session.
    monkeypatch.setitem mutates the SAME dict and is reverted per-test.
    """
    import services.google_sheets as gs

    monkeypatch.setitem(gs.TASK_COLUMNS, "urgency", "K")
    monkeypatch.setitem(gs.TASK_COLUMNS, "last_update", "L")
    monkeypatch.setitem(gs.TASK_COL_INDEX, "urgency", 10)
    monkeypatch.setitem(gs.TASK_COL_INDEX, "last_update", 11)
    monkeypatch.setattr(
        gs, "TASK_TRACKER_HEADERS",
        [*gs.TASK_TRACKER_HEADERS_BASE, "Urgency", "Last Update"],
    )
    return gs


class TestLayout:
    def test_last_update_is_column_L_after_id_and_urgency(self, gs_on):
        assert gs_on.TASK_COLUMNS["id"] == "J", "col-J UUID must never move"
        assert gs_on.TASK_COLUMNS["urgency"] == "K"
        assert gs_on.TASK_COLUMNS["last_update"] == "L"

    def test_header_appended_last(self, gs_on):
        assert gs_on.TASK_TRACKER_HEADERS[-1] == "Last Update"
        assert gs_on.TASK_TRACKER_HEADERS.index("ID") == 9

    def test_absent_when_flag_off(self):
        """Flag off => the base A:J layout, unchanged.

        Asserted against the module as actually imported (the flag defaults to
        False), so this needs no reload and cannot leak state into other tests.
        """
        import services.google_sheets as gs

        assert gs.TASK_TRACKER_HEADERS_BASE[-1] == "ID"
        assert "Last Update" not in gs.TASK_TRACKER_HEADERS_BASE
        assert len(gs.TASK_TRACKER_HEADERS_BASE) == 10


class TestFormatDay:
    @pytest.mark.parametrize("raw,expected", [
        ("2026-07-22T15:04:05.123456+00:00", "2026-07-22"),
        ("2026-07-22", "2026-07-22"),
        (None, ""),
        ("", ""),
    ])
    def test_renders_day_only(self, gs_on, raw, expected):
        assert gs_on._fmt_day(raw) == expected

    def test_missing_value_never_renders_none(self, gs_on):
        """A blank cell must stay blank — 'None' would sort and read as data."""
        assert gs_on._fmt_day(None) == ""


class TestProtectionAndFormatting:
    @pytest.mark.asyncio
    async def test_last_update_gets_its_own_protected_range(self, gs_on):
        svc = gs_on.GoogleSheetsService()
        svc._service = MagicMock()
        svc._ensure_fresh_credentials = MagicMock(return_value=None)
        svc._execute_with_retry = MagicMock(side_effect=lambda fn: fn())
        svc._get_first_sheet_id = MagicMock(return_value=0)

        with patch("services.google_sheets.settings") as s:
            s.TASK_TRACKER_SHEET_ID = "sheet-1"
            s.TASK_TRACKER_TAB_NAME = "Tasks"
            await svc.format_task_tracker()

        calls = svc._service.spreadsheets.return_value.batchUpdate.call_args_list
        reqs = [r for c in calls for r in c.kwargs["body"]["requests"]]
        protects = [r["addProtectedRange"]["protectedRange"] for r in reqs
                    if "addProtectedRange" in r]
        descs = [p["description"] for p in protects]

        assert any("last update" in d.lower() for d in descs), (
            "Last Update is system-owned and must be protected"
        )
        # It cannot ride along with H:J — Urgency (K) sits between and is editable.
        lu = next(p for p in protects if "last update" in p["description"].lower())
        assert lu["range"]["startColumnIndex"] == gs_on.TASK_COL_INDEX["last_update"]
        assert lu["range"]["endColumnIndex"] == gs_on.TASK_COL_INDEX["last_update"] + 1
        assert all(p["warningOnly"] for p in protects), "never hard-block the bot's own writes"

    @pytest.mark.asyncio
    async def test_staleness_rules_register_60d_before_30d(self, gs_on):
        """First matching rule wins, so 60d must be registered first — otherwise
        everything over 30 days paints amber and the 60d rule never fires."""
        svc = gs_on.GoogleSheetsService()
        svc._service = MagicMock()
        svc._ensure_fresh_credentials = MagicMock(return_value=None)
        svc._execute_with_retry = MagicMock(side_effect=lambda fn: fn())
        svc._get_first_sheet_id = MagicMock(return_value=0)

        with patch("services.google_sheets.settings") as s:
            s.TASK_TRACKER_SHEET_ID = "sheet-1"
            s.TASK_TRACKER_TAB_NAME = "Tasks"
            await svc.format_task_tracker()

        calls = svc._service.spreadsheets.return_value.batchUpdate.call_args_list
        reqs = [r for c in calls for r in c.kwargs["body"]["requests"]]
        formulas = [
            r["addConditionalFormatRule"]["rule"]["booleanRule"]["condition"]["values"][0]["userEnteredValue"]
            for r in reqs
            if "addConditionalFormatRule" in r
            and r["addConditionalFormatRule"]["rule"]["booleanRule"]["condition"]["type"] == "CUSTOM_FORMULA"
        ]
        assert len(formulas) == 2
        assert ">=60" in formulas[0] and ">=30" in formulas[1]
        # blank cells must not paint as stale
        assert all('<>""' in f for f in formulas)
