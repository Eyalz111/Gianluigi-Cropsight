"""The five defects found by the 2026-08-12 max review, pinned.

Four are the same family the 2026-08-09 review kept surfacing: something is
ADDED to a live sheet on every refresh and never asserted, so state accumulates
at whatever position it last occupied. The fifth is a proposer with no caller —
shipped reachable only by hand, which no test would ever notice because the
function itself works perfectly.
"""
from datetime import date
from unittest.mock import MagicMock, patch

from processors.timeline_view import N_LABEL_COLS, week_starts

_WHITE = {"red": 1.0, "green": 1.0, "blue": 1.0}


async def _render():
    """Render one project with an archive block; return the captured calls."""
    import services.timeline_sheet as ts

    cap = {"clear_ranges": []}
    svc = MagicMock()
    svc._execute_with_retry.side_effect = lambda f: f()

    async def _ensure(*a, **kw):
        return 7
    svc._ensure_tab = _ensure
    sheets = svc.service.spreadsheets.return_value
    sheets.get.side_effect = lambda **kw: {"sheets": []}
    cap["batches"] = []

    def _batch(**kw):
        cap["batches"].append(kw["body"]["requests"])
        # The widen call comes first and is a single request; the formatting
        # batch is the big one. Keyed on content, not call order — a positional
        # side_effect list is how three tests broke this month.
        if len(kw["body"]["requests"]) > 1:
            cap["reqs"] = kw["body"]["requests"]
        return MagicMock()
    sheets.batchUpdate.side_effect = _batch
    sheets.values.return_value.update.side_effect = \
        lambda **kw: cap.setdefault("values", kw["body"]["values"])
    sheets.values.return_value.clear.side_effect = \
        lambda **kw: cap["clear_ranges"].append(kw["range"])

    data = {
        "weeks": week_starts(),
        "areas": {"AREA": [{
            "project_id": "p1", "name": "P", "owner": "E",
            "start": date(2026, 3, 2), "target": date(2026, 4, 6),
            "first_col": 0, "last_col": 5, "open_ended": False,
            "status": "active", "retired": False, "tasks": []}]},
        "archive": [{"section": "PRODUCT & TECHNOLOGY", "lanes": [
            {"lane": "Execution #1", "bars": [
                {"first_col": 2, "last_col": 8, "label": "old plan",
                 "start": date(2026, 3, 16), "end": date(2026, 4, 27)}]}]}],
        "stats": {},
    }
    with patch.object(ts, "build_timeline", return_value=data), \
         patch.object(ts, "sheets_service", svc), \
         patch.object(ts.settings, "PROJECT_STATUS_SHEET_ID", "ssid"):
        await ts.refresh_timeline()
    return cap


class TestTodayMarkerDoesNotAccumulate:
    """A new red line every week, and none of them meaning today."""

    async def test_the_border_is_cleared_before_the_marker_is_drawn(self):
        """updateBorders lives outside userEnteredFormat.backgroundColor, so the
        fill wipe never touched it. Last Monday's line stayed and a new one
        appeared beside it, every week."""
        from services.timeline_sheet import MONTH_ROW
        cap = await _render()
        borders = [(i, r["updateBorders"]) for i, r in enumerate(cap["reqs"])
                   if "updateBorders" in r]
        assert len(borders) >= 2, "expected a clear and then the marker"

        clears = [i for i, b in borders if b.get("left", {}).get("style") == "NONE"]
        marks = [i for i, b in borders
                 if b.get("left", {}).get("style") == "SOLID_MEDIUM"]
        assert clears, "the previous today-marker is never cleared"
        assert marks, "the today marker was not drawn"
        assert min(clears) < min(marks), "the clear must precede the marker"

    async def test_the_clear_spans_every_week_column(self):
        """Clearing only today's column would leave every previous week's line."""
        cap = await _render()
        clear = next(r["updateBorders"] for r in cap["reqs"]
                     if "updateBorders" in r
                     and r["updateBorders"].get("left", {}).get("style") == "NONE")
        assert clear["range"]["startColumnIndex"] == N_LABEL_COLS
        assert clear["range"]["endColumnIndex"] == N_LABEL_COLS + 96

    async def test_the_clear_range_stays_inside_the_grid(self):
        """A bounded updateBorders range past the grid is rejected outright, and
        batchUpdate is atomic — one such request discards the whole formatting
        batch and the tab keeps exactly the state the wipe exists to clear.
        That is the trap project_status_sheet.py:202 documents."""
        cap = await _render()
        n_rows = len(cap["values"])
        clear = next(r["updateBorders"] for r in cap["reqs"]
                     if "updateBorders" in r
                     and r["updateBorders"].get("left", {}).get("style") == "NONE")
        assert clear["range"]["endRowIndex"] <= n_rows


class TestTheWipeCoversTheLabelBlock:
    async def test_the_wipe_starts_at_column_zero(self):
        """Area headers and the archive title are painted across ALL columns, so
        a wipe starting at the week grid left their fills in columns A-E at
        whatever row they last occupied — coloured bands labelling nothing."""
        from services.timeline_sheet import FIRST_BODY_ROW
        cap = await _render()
        wipes = [r["repeatCell"] for r in cap["reqs"]
                 if (r.get("repeatCell") or {}).get("cell", {})
                 .get("userEnteredFormat", {}).get("backgroundColor") == _WHITE
                 and r["repeatCell"]["range"]["startRowIndex"] == FIRST_BODY_ROW]
        assert wipes, "no body wipe at all"
        assert wipes[0]["range"]["startColumnIndex"] == 0

    async def test_the_wipe_resets_bold_too(self):
        """Area header rows are bold and archive rows carry a smaller font.
        Resetting only the background leaves stale bold behind on a moved row."""
        from services.timeline_sheet import FIRST_BODY_ROW
        cap = await _render()
        wipe = next(r["repeatCell"] for r in cap["reqs"]
                    if (r.get("repeatCell") or {}).get("cell", {})
                    .get("userEnteredFormat", {}).get("backgroundColor") == _WHITE
                    and r["repeatCell"]["range"]["startRowIndex"] == FIRST_BODY_ROW)
        assert "textFormat" in wipe["fields"]
        assert wipe["cell"]["userEnteredFormat"]["textFormat"]["bold"] is False

    async def test_the_wipe_precedes_every_paint(self):
        """Whatever else changes, this ordering is the point."""
        from services.timeline_sheet import FIRST_BODY_ROW
        cap = await _render()
        reqs = cap["reqs"]
        wipe = next(i for i, r in enumerate(reqs)
                    if (r.get("repeatCell") or {}).get("cell", {})
                    .get("userEnteredFormat", {}).get("backgroundColor") == _WHITE
                    and r["repeatCell"]["range"]["startRowIndex"] == FIRST_BODY_ROW)
        paints = [i for i, r in enumerate(reqs)
                  if (r.get("repeatCell") or {}).get("range", {})
                  .get("startRowIndex", -1) >= FIRST_BODY_ROW
                  and (r["repeatCell"]["cell"].get("userEnteredFormat", {})
                       .get("backgroundColor") not in (None, _WHITE))]
        assert paints, "nothing was painted"
        assert wipe < min(paints)


class TestTheClearIsNotSizedFromTheNewGrid:
    async def test_the_clear_range_is_unbounded_in_rows(self):
        """`len(grid) + 40` was measured from the NEW render, so a refresh that
        shrank the tab by more than 40 rows left the previous tail behind as
        text — and the fill wipe reaches 200 rows, so those leftovers showed up
        as rows with no bar under them, reading as real work."""
        cap = await _render()
        assert len(cap["clear_ranges"]) == 1
        rng = cap["clear_ranges"][0]
        assert rng == "'Timeline'!A:CW", rng
        assert not any(ch.isdigit() for ch in rng.split("!")[1]), \
            "a row bound reintroduces the shrink bug"

    async def test_the_clear_covers_every_column_that_gets_written(self):
        cap = await _render()
        written = max(len(r) for r in cap["values"])
        from services.timeline_sheet import _a1_col
        assert cap["clear_ranges"][0].endswith(_a1_col(written - 1))


class TestTheGridWidthIsAsserted:
    """Not a live defect — the tab renders 101 columns today. But it does so
    because values().update expands the grid on its way past the edge, which is
    a side effect nobody wrote down; a bare addSheet allocates 26."""

    async def test_the_column_count_is_set_before_any_ranged_write(self):
        cap = await _render()
        widen = [b for b in cap["batches"]
                 if len(b) == 1 and "updateSheetProperties" in b[0]
                 and "columnCount" in b[0]["updateSheetProperties"]["fields"]]
        assert widen, "the grid width is never asserted"
        props = widen[0][0]["updateSheetProperties"]["properties"]
        assert props["gridProperties"]["columnCount"] == N_LABEL_COLS + 96

    async def test_it_only_touches_the_column_count(self):
        """A wider fields mask would reset rowCount, frozen panes, or the tab's
        colour as a side effect of widening it."""
        cap = await _render()
        widen = next(b[0] for b in cap["batches"]
                     if len(b) == 1 and "updateSheetProperties" in b[0]
                     and "columnCount" in b[0]["updateSheetProperties"]["fields"])
        assert widen["updateSheetProperties"]["fields"] == "gridProperties.columnCount"


class TestTheProposerHasACaller:
    """Phase 1 shipped with propose_project_starts reachable only by hand. The
    function was correct and fully tested; nothing ran it."""

    def test_the_daily_qa_runs_it(self):
        import schedulers.qa_scheduler as qa
        assert hasattr(qa, "_run_project_start_dates")

        called = {"n": 0}

        def _proposer():
            called["n"] += 1
            return {"proposed": 3}

        with patch("processors.project_start_dates.propose_project_starts",
                   side_effect=_proposer):
            issues: list[str] = []
            out = qa._run_project_start_dates(issues)
        assert called.get("n") == 1
        assert out["proposed"] == 3
        assert issues and "start date" in issues[0]

    def test_it_is_wired_into_the_check_list(self):
        """A helper nobody calls is exactly the defect being fixed."""
        import ast
        import pathlib
        src = pathlib.Path("schedulers/qa_scheduler.py").read_text(encoding="utf-8")
        tree = ast.parse(src)
        calls = {n.func.id for n in ast.walk(tree)
                 if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
        assert "_run_project_start_dates" in calls

    def test_a_failure_does_not_break_the_qa_run(self):
        import schedulers.qa_scheduler as qa
        with patch("processors.project_start_dates.propose_project_starts",
                   side_effect=RuntimeError("boom")):
            out = qa._run_project_start_dates([])
        assert out == {"proposed": 0}


class TestAProposalAlwaysCarriesADate:
    """`apply_project_start` refuses content without `recommended`, so a card
    that recommends nothing could be approved forever and never clear."""

    def _run(self, projects, tasks, bars):
        import processors.project_start_dates as psd

        saved = []

        def _table(name):
            t = MagicMock()
            t.select.return_value = t
            t.order.return_value = t
            t.limit.return_value = t
            t.eq.return_value = t
            data = {"canonical_projects": projects, "areas": [
                {"id": "a1", "name": "PRODUCT & TECHNOLOGY"}],
                "tasks": tasks, "gantt_legacy_bars": bars}.get(name, [])
            t.execute.return_value = MagicMock(data=data)
            return t

        client = MagicMock()
        client.table.side_effect = _table
        with patch.object(type(psd.supabase_client), "client",
                          property(lambda self: client)), \
             patch.object(psd.supabase_client, "get_pending_approvals_by_status",
                          return_value=[]), \
             patch.object(psd.supabase_client, "upsert_pending_approval",
                          side_effect=lambda **kw: saved.append(kw)):
            psd.propose_project_starts()
        return saved

    def test_a_gantt_only_project_still_gets_an_applicable_date(self):
        saved = self._run(
            projects=[{"id": "p1", "name": "Cloud Infrastructure",
                       "area_id": "a1", "status": "active"}],
            tasks=[],
            bars=[{"label": "[R/E] Cloud Infrastructure buildout",
                   "start_date": "2026-03-09", "end_date": "2026-04-06",
                   "section": "PRODUCT & TECHNOLOGY", "lane": "Execution #1"}])
        assert len(saved) == 1
        content = saved[0]["content"]
        assert content["recommended"] == "2026-03-09"
        assert content["recommended_source"] == "gantt_bar"

    def test_that_proposal_can_actually_be_approved(self):
        """The bug was end-to-end: emitted fine, refused on approve."""
        import processors.project_start_dates as psd
        saved = self._run(
            projects=[{"id": "p1", "name": "Cloud Infrastructure",
                       "area_id": "a1", "status": "active"}],
            tasks=[],
            bars=[{"label": "[R/E] Cloud Infrastructure buildout",
                   "start_date": "2026-03-09", "end_date": "2026-04-06",
                   "section": "PRODUCT & TECHNOLOGY", "lane": "Execution #1"}])

        client = MagicMock()
        with patch.object(type(psd.supabase_client), "client",
                          property(lambda self: client)):
            out = psd.apply_project_start(saved[0]["content"])
        assert out["ok"] is True
        assert out["start_date"] == "2026-03-09"

    def test_a_task_derived_date_still_wins_over_the_archive(self):
        """The archive is the fallback, never the preference — its name matching
        is the untrusted step this whole phase works around."""
        saved = self._run(
            projects=[{"id": "p1", "name": "Cloud Infrastructure",
                       "area_id": "a1", "status": "active"}],
            tasks=[{"id": "t1", "project_id": "p1",
                    "created_at": "2026-07-22T09:00:00+00:00"}],
            bars=[{"label": "[R/E] Cloud Infrastructure buildout",
                   "start_date": "2026-03-09", "end_date": "2026-04-06",
                   "section": "PRODUCT & TECHNOLOGY", "lane": "Execution #1"}])
        content = saved[0]["content"]
        assert content["recommended_source"] == "earliest_task"
        assert content["recommended"] == "2026-07-22"
        assert content["gantt_date"] == "2026-03-09", "evidence must survive"
