"""The legacy ghost layer — the old board redrawn on the new grid.

Decision 7 of docs/GANTT_V2_PLAN.md, approved by Eyal 2026-08-12.

The first build of this drew each project's matched legacy bar directly beneath
it. Measured on live data it produced 2 ghosts across 27 projects, and the
matches it suppressed included obviously-correct ones ("Legal" -> "Legal entity
Establishment") scoring identically to obvious junk ("Corporate" -> "Monthly
close — send docs to Shimony"). No threshold admits one without the other, so
the archive now asserts nothing about which project a bar belongs to: it
reproduces the old board's own sections and lanes and lets Eyal read the
correspondence himself.

What these tests defend is that honesty, plus the two mechanical traps: a bar
with no end must not run to the edge of the grid, and the block must actually
arrive collapsed.
"""
from datetime import date

from processors.timeline_view import GHOST_BG, STATUS_BG, legacy_archive, week_starts


def _rgb_of(hex_colour: str) -> dict:
    from services.timeline_sheet import _rgb
    return _rgb(hex_colour)


def _bar(label, start, end, section="PRODUCT & TECHNOLOGY", lane="Execution #1"):
    return {"label": label, "start_date": start, "end_date": end,
            "section": section, "lane": lane}


class TestArchiveGrouping:
    def test_bars_collapse_into_section_then_lane_rows(self):
        """38 lane rows stand in for 244 bars on live data — the old board's own
        shape, which is what makes the block readable at all."""
        bars = [
            _bar("Product V1 SOW", "2026-03-02", "2026-03-23"),
            _bar("Accuracy work", "2026-04-06", "2026-04-27"),
            _bar("Hiring", "2026-03-16", "2026-04-20", lane="Human Resources"),
            _bar("Outreach", "2026-04-13", "2026-05-04",
                 section="FUNDRAISING & INVESTOR RELATIONS"),
        ]
        out = legacy_archive(bars, week_starts())
        assert [s["section"] for s in out] == [
            "FUNDRAISING & INVESTOR RELATIONS", "PRODUCT & TECHNOLOGY"]
        pt = next(s for s in out if s["section"] == "PRODUCT & TECHNOLOGY")
        assert [l["lane"] for l in pt["lanes"]] == ["Execution #1", "Human Resources"]
        assert len(next(l for l in pt["lanes"]
                        if l["lane"] == "Execution #1")["bars"]) == 2

    def test_bars_within_a_lane_are_ordered_left_to_right(self):
        bars = [_bar("late", "2026-06-01", "2026-06-08"),
                _bar("early", "2026-03-09", "2026-03-16")]
        lane = legacy_archive(bars, week_starts())[0]["lanes"][0]
        assert [b["label"] for b in lane["bars"]] == ["early", "late"]

    def test_the_span_lands_on_the_right_columns(self):
        lane = legacy_archive(
            [_bar("x", "2026-03-09", "2026-04-06")], week_starts())[0]["lanes"][0]
        assert lane["bars"][0]["first_col"] == 1     # 2026-03-09, second Monday
        assert lane["bars"][0]["last_col"] == 5      # 2026-04-06, sixth


class TestWhatIsNotPlan:
    def test_the_meeting_lanes_are_dropped(self):
        """Attendance cadence, not something anyone planned. With OPERATIONAL
        RULES these are 99 of the 244 bars."""
        bars = [_bar("standup", "2026-03-09", "2026-03-09", lane="Meetings"),
                _bar("mgmt", "2026-03-09", "2026-03-09", lane="Management Meetings"),
                _bar("count", "2026-03-09", "2026-03-09", lane="Meeting Count"),
                _bar("free", "2026-03-09", "2026-03-09", lane="Availability")]
        assert legacy_archive(bars, week_starts()) == []

    def test_the_operational_rules_section_is_dropped(self):
        """That section is the old board's own legend, not work."""
        bars = [_bar("Tech: Conditional formatting", "2026-03-09", "2026-03-09",
                     section="OPERATIONAL RULES", lane="Tech: Cadence-driven.")]
        assert legacy_archive(bars, week_starts()) == []

    def test_real_work_in_a_kept_lane_survives(self):
        bars = [_bar("Planning", "2026-03-09", "2026-03-16", lane="Planning #1")]
        assert len(legacy_archive(bars, week_starts())) == 1


class TestAMissingEndIsOneWeek:
    def test_a_bar_with_no_end_does_not_run_to_the_grid_edge(self):
        """A legacy bar is a run of filled cells, so a blank end means the run
        was one cell long. Treating it as open-ended would assert the old board
        planned something indefinitely — the opposite of what the blank says —
        and paint 90-odd weeks of lilac across the board."""
        lane = legacy_archive(
            [_bar("x", "2026-03-09", None)], week_starts())[0]["lanes"][0]
        assert lane["bars"][0]["first_col"] == lane["bars"][0]["last_col"] == 1

    def test_a_bar_with_no_start_is_dropped_entirely(self):
        assert legacy_archive([_bar("x", None, "2026-03-09")], week_starts()) == []

    def test_a_bar_beyond_the_grid_is_dropped(self):
        assert legacy_archive([_bar("x", "2029-01-01", "2029-02-01")],
                              week_starts()) == []

    def test_a_bar_starting_before_the_grid_is_clamped_not_dropped(self):
        lane = legacy_archive([_bar("x", "2026-01-05", "2026-03-16")],
                              week_starts())[0]["lanes"][0]
        assert lane["bars"][0]["first_col"] == 0


class TestGhostColour:
    def test_the_ghost_colour_is_none_of_the_live_colours(self):
        """The archive must not be readable as current truth at a glance."""
        from processors.timeline_view import OPEN_END_BG
        assert GHOST_BG not in set(STATUS_BG.values())
        assert GHOST_BG != OPEN_END_BG


class TestArchiveRendering:
    async def _render(self, archive):
        from unittest.mock import MagicMock, patch
        import services.timeline_sheet as ts

        captured = {}
        svc = MagicMock()
        svc._execute_with_retry.side_effect = lambda f: f()

        async def _ensure(*a, **kw):
            return 7
        svc._ensure_tab = _ensure
        sheets = svc.service.spreadsheets.return_value
        sheets.get.side_effect = lambda **kw: {"sheets": []}
        sheets.batchUpdate.side_effect = \
            lambda **kw: captured.setdefault("reqs", kw["body"]["requests"])
        sheets.values.return_value.update.side_effect = \
            lambda **kw: captured.setdefault("values", kw["body"]["values"])

        data = {
            "weeks": week_starts(),
            "areas": {"AREA": [{
                "project_id": "p1", "name": "P", "owner": "E",
                "start": date(2026, 3, 2), "target": date(2026, 4, 6),
                "first_col": 0, "last_col": 5, "open_ended": False,
                "status": "active", "retired": False, "tasks": []}]},
            "archive": archive,
            "stats": {},
        }
        with patch.object(ts, "build_timeline", return_value=data), \
             patch.object(ts, "sheets_service", svc), \
             patch.object(ts.settings, "PROJECT_STATUS_SHEET_ID", "ssid"):
            await ts.refresh_timeline()
        return captured

    def _archive(self):
        return [{"section": "PRODUCT & TECHNOLOGY", "lanes": [
            {"lane": "Execution #1", "bars": [
                {"first_col": 2, "last_col": 8, "label": "[R/E] old plan",
                 "start": date(2026, 3, 16), "end": date(2026, 4, 27)}]}]}]

    async def test_the_block_renders_below_the_live_rows(self):
        cap = await self._render(self._archive())
        rows = [r[0] for r in cap["values"]]
        proj = next(i for i, r in enumerate(rows) if r.strip() == "P")
        title = next(i for i, r in enumerate(rows) if r.startswith("OLD BOARD"))
        section = next(i for i, r in enumerate(rows)
                       if r == "PRODUCT & TECHNOLOGY")
        lane = next(i for i, r in enumerate(rows) if r.strip() == "Execution #1")
        assert proj < title < section < lane

    async def test_the_bar_label_sits_in_the_first_cell_of_its_span(self):
        from processors.timeline_view import N_LABEL_COLS
        cap = await self._render(self._archive())
        lane_row = next(r for r in cap["values"] if r[0].strip() == "Execution #1")
        assert lane_row[N_LABEL_COLS + 2] == "[R/E] old plan"
        assert lane_row[N_LABEL_COLS + 3] == ""

    async def test_the_span_is_painted_in_the_ghost_colour(self):
        from processors.timeline_view import N_LABEL_COLS
        cap = await self._render(self._archive())
        painted = [r for r in cap["reqs"]
                   if (r.get("repeatCell") or {}).get("cell", {})
                   .get("userEnteredFormat", {}).get("backgroundColor")
                   == _rgb_of(GHOST_BG)
                   and r["repeatCell"]["range"]["startColumnIndex"] >= N_LABEL_COLS]
        assert len(painted) == 1
        rng = painted[0]["repeatCell"]["range"]
        assert rng["startColumnIndex"] == N_LABEL_COLS + 2
        assert rng["endColumnIndex"] == N_LABEL_COLS + 8 + 1

    async def test_the_block_arrives_collapsed(self):
        """addDimensionGroup CREATES the group, it does not close it. Without
        the follow-up request the honest answer to "it doubles what is on
        screen" would be yes."""
        cap = await self._render(self._archive())
        rows = [r[0] for r in cap["values"]]
        first = next(i for i, r in enumerate(rows) if r == "PRODUCT & TECHNOLOGY")

        added = [r["addDimensionGroup"]["range"] for r in cap["reqs"]
                 if "addDimensionGroup" in r
                 and r["addDimensionGroup"]["range"]["startIndex"] == first]
        assert added, "the archive block was never grouped"
        closed = [r for r in cap["reqs"]
                  if (r.get("updateDimensionGroup") or {})
                  .get("dimensionGroup", {}).get("collapsed") is True
                  and r["updateDimensionGroup"]["dimensionGroup"]["range"]
                  ["startIndex"] == first]
        assert closed, "the archive group was added but never collapsed"

    async def test_no_archive_means_no_block_and_no_lilac(self):
        cap = await self._render([])
        assert not any((r[0] or "").startswith("OLD BOARD") for r in cap["values"])
        assert not any((r.get("repeatCell") or {}).get("cell", {})
                       .get("userEnteredFormat", {}).get("backgroundColor")
                       == _rgb_of(GHOST_BG) for r in cap["reqs"])

    async def test_the_spare_legend_cell_is_repainted_white_when_absent(self):
        """ASSERT, DON'T ADD. Painting the swatch only when the archive exists
        leaves a lilac cell with no label behind forever the first time the
        overlay is switched off — values().clear() takes the text, never the
        fill. Five of the fifteen defects in the 2026-08-09 review were this."""
        from services.timeline_sheet import LEGEND_ROW, _LEGEND
        cap = await self._render([])
        cells = [r for r in cap["reqs"]
                 if (r.get("repeatCell") or {}).get("range", {}).get("startRowIndex")
                 == LEGEND_ROW
                 and r["repeatCell"]["range"]["startColumnIndex"] == len(_LEGEND)]
        assert len(cells) == 1, "the spare legend column is never asserted"
        assert (cells[0]["repeatCell"]["cell"]["userEnteredFormat"]
                ["backgroundColor"] == {"red": 1.0, "green": 1.0, "blue": 1.0})

    async def test_the_legend_gains_its_swatch_when_the_archive_is_drawn(self):
        from services.timeline_sheet import LEGEND_ROW, _GHOST_LEGEND, _LEGEND
        cap = await self._render(self._archive())
        assert cap["values"][LEGEND_ROW][len(_LEGEND)] == _GHOST_LEGEND
        cells = [r for r in cap["reqs"]
                 if (r.get("repeatCell") or {}).get("range", {}).get("startRowIndex")
                 == LEGEND_ROW
                 and r["repeatCell"]["range"]["startColumnIndex"] == len(_LEGEND)]
        assert (cells[0]["repeatCell"]["cell"]["userEnteredFormat"]
                ["backgroundColor"] == _rgb_of(GHOST_BG))


class TestOverlayFlag:
    def _client(self, seen):
        from unittest.mock import MagicMock

        def _table(name):
            seen.append(name)
            t = MagicMock()
            t.select.return_value = t
            t.order.return_value = t
            t.limit.return_value = t
            t.execute.return_value = MagicMock(data=[])
            return t

        client = MagicMock()
        client.table.side_effect = _table
        return client

    def test_the_archive_is_not_read_when_the_overlay_is_off(self):
        """The flag is Eyal's escape hatch if the block reads as clutter, and it
        should cost nothing to have off."""
        from unittest.mock import patch
        import processors.timeline_view as tv

        seen = []
        client = self._client(seen)
        with patch.object(type(tv.supabase_client), "client",
                          property(lambda self: client)), \
             patch.object(tv.settings, "TIMELINE_LEGACY_OVERLAY_ENABLED", False):
            out = tv.build_timeline()
        assert "gantt_legacy_bars" not in seen
        assert out["archive"] == []

        seen.clear()
        with patch.object(type(tv.supabase_client), "client",
                          property(lambda self: client)), \
             patch.object(tv.settings, "TIMELINE_LEGACY_OVERLAY_ENABLED", True):
            tv.build_timeline()
        assert "gantt_legacy_bars" in seen
