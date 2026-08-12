"""Milestones — reading the old board's commitments, and keeping their moves.

Phase 3 of docs/GANTT_V2_PLAN.md.

The exit criterion the plan names is one specific row: `Signing #1 MVP client`
must render with BOTH its original and its current date. On the real board that
commitment is spread across four bars — it appears at 2026-06-01 and again as
"- postponed here" at 2026-07-06, and both of those are written into two
different lanes. Collapsing four bars into one milestone with one move, without
losing the June date, is the whole job.
"""
from unittest.mock import MagicMock, patch

import pytest

from processors.milestones import collapse_bars, is_move_marker, normalise_title


def _bar(label, start, end=None, lane="Company OKRs", section=None):
    return {"label": label, "start_date": start, "end_date": end or start,
            "lane": lane, "section": section}


class TestNormalising:
    def test_the_move_marker_is_stripped_for_identity(self):
        assert (normalise_title("Signing #1 MVP client - postponed here")
                == normalise_title("Signing #1 MVP client"))

    def test_owner_tags_and_glyphs_do_not_change_identity(self):
        assert (normalise_title("[E] ★ MVP Product Delivery")
                == normalise_title("MVP Product Delivery"))

    @pytest.mark.parametrize("label", [
        "X - postponed here", "X — moved to Q3", "X (postponed)",
        "X - delayed", "X - pushed to next quarter",
    ])
    def test_the_marker_is_recognised_in_the_forms_the_board_uses(self, label):
        """Hand-typed, so match the intent rather than one exact phrasing."""
        assert is_move_marker(label)

    def test_an_ordinary_title_is_not_a_move(self):
        assert not is_move_marker("Signing #1 MVP client")
        assert not is_move_marker("★ MVP Product Delivery (Q3 2026)")


class TestTheSlippageCase:
    """The plan's exit criterion, on the real shape of the data."""

    def _real(self):
        return [
            _bar("Signing #1 MVP client", "2026-06-01"),
            _bar("Signing #1 MVP client - postponed here", "2026-07-06"),
            _bar("Signing #1 MVP client", "2026-06-01", lane="Commercial"),
            _bar("Signing #1 MVP client - postponed here", "2026-07-06",
                 lane="Commercial"),
        ]

    def test_four_bars_collapse_to_one_milestone(self):
        ms = collapse_bars(self._real())
        assert len(ms) == 1
        assert ms[0]["bar_count"] == 4

    def test_both_dates_survive(self):
        m = collapse_bars(self._real())[0]
        assert m["original_date"] == "2026-06-01"
        assert m["target_date"] == "2026-07-06"

    def test_exactly_one_move_is_recorded_not_three(self):
        """Four bars, two distinct dates: the duplicate lanes are duplicates,
        not extra moves. Grouping on the row rather than the DATE would record
        three moves for one postponement."""
        m = collapse_bars(self._real())[0]
        assert m["moves"] == [{"from_date": "2026-06-01", "to_date": "2026-07-06"}]

    def test_the_title_does_not_keep_the_move_marker(self):
        m = collapse_bars(self._real())[0]
        assert m["title"] == "Signing #1 MVP client"

    def test_an_unmoved_milestone_records_no_moves(self):
        m = collapse_bars([_bar("★ MVP Product Delivery (Q3 2026)", "2026-08-31",
                                lane="Technology")])[0]
        assert m["moves"] == []
        assert m["original_date"] == m["target_date"]


class TestKind:
    @pytest.mark.parametrize("label,lane,kind", [
        ("★ MVP Product Delivery (Q3 2026)", "Technology", "product"),
        ("● First MVP Delivery (Sep 2026)", "Commercial", "commercial"),
        ("◆ Raising funds: Pre-Seed Round #1", "Funding", "funding"),
        ("[E] Q1 OKR: Finalize BP · Legal entity Establishment",
         "Company OKRs", "corporate"),
        ('"Investor\'s Package" readiness (all papers)', "Company OKRs", "funding"),
        ("Signing #1 MVP client", "Company OKRs", "commercial"),
    ])
    def test_the_board_glyphs_and_titles_map_to_a_kind(self, label, lane, kind):
        assert collapse_bars([_bar(label, "2026-06-01", lane=lane)])[0]["kind"] == kind


class TestWhatIsNotAMilestone:
    def test_strategy_and_decisions_is_not_a_source(self):
        """It holds a monthly investor update repeating to 2027-11, an annual
        offsite, and a couple of decisions. That is recurrence and decision
        history — the Meetings pool already models recurrence, and inventing a
        second concept here is the mistake the plan warns about."""
        bars = [
            _bar("[E] Monthly Investor & Strategic Stakeholder Update",
                 "2026-05-04", lane="Strategy & Decisions"),
            _bar("[ALL] Annual Strategic Planning 2027", "2026-12-07",
                 lane="Strategy & Decisions"),
            _bar("[E/P] DECISION: MVP client prioritization", "2026-04-06",
                 lane="Strategy & Decisions"),
        ]
        assert collapse_bars(bars) == []

    def test_execution_lanes_are_not_a_source(self):
        assert collapse_bars([_bar("Product V1 SOW", "2026-03-02",
                                   lane="Execution #1")]) == []

    def test_a_bar_with_no_start_is_dropped(self):
        assert collapse_bars([_bar("X", None)]) == []


class TestHistoryCell:
    """"moved 1 Jun -> 6 Jul", never "SLIPPED"."""

    def test_a_move_is_reported_as_a_fact(self):
        from services.ceo_sheet import history_cell
        out = history_cell({"original_date": "2026-06-01",
                            "target_date": "2026-07-06",
                            "moves": [{"from_date": "2026-06-01",
                                       "to_date": "2026-07-06"}]})
        assert "moved" in out and "Jun" in out and "Jul" in out

    def test_no_verdict_language_anywhere(self):
        """A move can be a slip or a deliberate re-plan and nothing here can
        tell the difference, so the board must not put a word in Eyal's mouth."""
        from services.ceo_sheet import history_cell
        out = history_cell({"original_date": "2026-06-01",
                            "target_date": "2026-07-06",
                            "moves": [{"from_date": "2026-06-01",
                                       "to_date": "2026-07-06"}]}).lower()
        for word in ("slip", "late", "overdue", "missed", "delay", "behind"):
            assert word not in out

    def test_an_unmoved_milestone_has_an_empty_history(self):
        from services.ceo_sheet import history_cell
        assert history_cell({"original_date": "2026-08-31",
                             "target_date": "2026-08-31", "moves": []}) == ""


class TestMoveKeepsTheOriginal:
    def _client(self, row):
        client = MagicMock()
        t = MagicMock()
        t.select.return_value = t
        t.eq.return_value = t
        t.limit.return_value = t
        t.execute.return_value = MagicMock(data=[row])
        t.update.return_value = t
        t.insert.return_value = t
        client.table.return_value = t
        return client, t

    def test_moving_a_target_never_touches_original_date(self):
        import processors.milestones as ms
        client, t = self._client({"id": "m1", "target_date": "2026-07-06",
                                  "original_date": "2026-06-01"})
        with patch.object(type(ms.supabase_client), "client",
                          property(lambda self: client)):
            out = ms.move_milestone("m1", "2026-09-14")
        assert out["ok"] and out["from_date"] == "2026-07-06"
        updates = t.update.call_args[0][0]
        assert "original_date" not in updates, "original_date must never move"
        assert updates["target_date"] == "2026-09-14"

    def test_a_missing_original_is_backfilled_from_the_date_being_left(self):
        """A milestone that cannot say where it started is worse than one that
        remembers — but this only ever fills a blank, never overwrites."""
        import processors.milestones as ms
        client, t = self._client({"id": "m1", "target_date": "2026-07-06",
                                  "original_date": None})
        with patch.object(type(ms.supabase_client), "client",
                          property(lambda self: client)):
            ms.move_milestone("m1", "2026-09-14")
        assert t.update.call_args[0][0]["original_date"] == "2026-07-06"

    def test_moving_to_the_same_date_is_a_no_op(self):
        """Otherwise every refresh cycle would record a move that never was."""
        import processors.milestones as ms
        client, t = self._client({"id": "m1", "target_date": "2026-07-06",
                                  "original_date": "2026-06-01"})
        with patch.object(type(ms.supabase_client), "client",
                          property(lambda self: client)):
            out = ms.move_milestone("m1", "2026-07-06")
        assert out["unchanged"] is True
        t.update.assert_not_called()


class TestSeedingIsProposalOnly:
    def test_nothing_is_written_to_milestones_when_proposing(self):
        import processors.milestones as ms
        written = []

        def _table(name):
            t = MagicMock()
            t.select.return_value = t
            t.limit.return_value = t
            t.execute.return_value = MagicMock(data=(
                [_bar("★ MVP Product Delivery", "2026-08-31", lane="Technology")]
                if name == "gantt_legacy_bars" else []))
            t.insert.side_effect = lambda *a, **k: written.append(name)
            return t

        client = MagicMock()
        client.table.side_effect = _table
        with patch.object(type(ms.supabase_client), "client",
                          property(lambda self: client)), \
             patch.object(ms.supabase_client, "get_pending_approvals_by_status",
                          return_value=[]), \
             patch.object(ms.supabase_client, "upsert_pending_approval"):
            out = ms.propose_milestones()
        assert out["proposed"] == 1
        assert written == [], "seeding must propose, never create"

    def test_it_declines_before_the_migration_has_run(self):
        """Queueing cards that cannot be actioned if approved is worse than
        staying quiet — approving would error on a missing table."""
        import processors.milestones as ms

        def _table(name):
            t = MagicMock()
            t.select.return_value = t
            t.limit.return_value = t
            if name == "milestones":
                t.execute.side_effect = Exception(
                    "Could not find the table 'public.milestones'")
            else:
                t.execute.return_value = MagicMock(data=[
                    _bar("★ MVP Product Delivery", "2026-08-31", lane="Technology")])
            return t

        client = MagicMock()
        client.table.side_effect = _table
        proposed = []
        with patch.object(type(ms.supabase_client), "client",
                          property(lambda self: client)), \
             patch.object(ms.supabase_client, "get_pending_approvals_by_status",
                          return_value=[]), \
             patch.object(ms.supabase_client, "upsert_pending_approval",
                          side_effect=lambda **kw: proposed.append(kw)):
            out = ms.propose_milestones()
        assert out.get("skipped") == "migration not run"
        assert proposed == []


class TestApplyWritesOriginalOnce:
    def test_approving_records_the_move_history(self):
        import processors.milestones as ms
        inserts = {}

        def _table(name):
            t = MagicMock()

            def _insert(payload):
                inserts[name] = payload
                return t          # NOT `setdefault(...) or t` — setdefault
                                  # returns the payload, which is truthy.
            t.insert.side_effect = _insert
            t.execute.return_value = MagicMock(data=[{"id": "m1"}])
            return t

        client = MagicMock()
        client.table.side_effect = _table
        with patch.object(type(ms.supabase_client), "client",
                          property(lambda self: client)):
            out = ms.apply_milestone({
                "title": "Signing #1 MVP client", "kind": "commercial",
                "original_date": "2026-06-01", "target_date": "2026-07-06",
                "moves": [{"from_date": "2026-06-01", "to_date": "2026-07-06"}]})
        assert out["ok"] and out["moves"] == 1
        assert inserts["milestones"]["original_date"] == "2026-06-01"
        assert inserts["milestones"]["target_date"] == "2026-07-06"
        assert inserts["milestone_moves"][0]["from_date"] == "2026-06-01"

    def test_a_proposal_with_no_title_is_refused(self):
        import processors.milestones as ms
        assert ms.apply_milestone({"target_date": "2026-06-01"})["ok"] is False


class TestTabRegistration:
    def test_the_ceo_tab_is_not_parsed_as_an_area_tab(self):
        """An unregistered tab is parsed as an area tab and stripped by the
        formatting pass — what happened to the meetings pool on 2026-08-09."""
        from processors.project_status_reconcile import NON_AREA_TABS
        from processors.milestones import CEO_TAB
        assert CEO_TAB in NON_AREA_TABS


class TestTheManualBlockSurvives:
    """The CEO tab regenerates every reconcile cycle. The management block is
    typed by Eyal and has no database source, so a naive rewrite would delete
    his words every thirty minutes. This is the part that must not break."""

    async def _render(self, prior_values, milestones=None, fail_read=False):
        import services.ceo_sheet as cs

        cap = {"clear": [], "batches": []}
        svc = MagicMock()
        svc._execute_with_retry.side_effect = lambda f: f()

        async def _ensure(*a, **kw):
            return 11
        svc._ensure_tab = _ensure
        sheets = svc.service.spreadsheets.return_value

        def _get(**kw):
            if fail_read:
                raise RuntimeError("transient read failure")
            return {"values": prior_values}
        sheets.values.return_value.get.side_effect = _get
        sheets.values.return_value.update.side_effect = \
            lambda **kw: cap.setdefault("values", kw["body"]["values"])
        sheets.values.return_value.clear.side_effect = \
            lambda **kw: cap["clear"].append(kw["range"])
        sheets.get.side_effect = lambda **kw: {"sheets": []}
        sheets.batchUpdate.side_effect = \
            lambda **kw: cap["batches"].append(kw["body"]["requests"])

        with patch.object(cs, "list_milestones", return_value=milestones or []), \
             patch.object(cs, "sheets_service", svc), \
             patch.object(cs.settings, "PROJECT_STATUS_SHEET_ID", "ssid"):
            cap["out"] = await cs.refresh_ceo_tab()
        return cap

    def _prior(self, block_rows):
        from services.ceo_sheet import MANAGEMENT_MARKER
        return ([["CEO — milestones and management"], ["Milestone"], ["old row"]]
                + [[MANAGEMENT_MARKER]] + block_rows)

    async def test_the_block_is_carried_across_verbatim(self):
        cap = await self._render(self._prior([
            ["Company OKRs", "raise pre-seed by Q3"],
            ["Escalations", "Moldova contract stuck with legal"],
        ]))
        flat = ["|".join(str(c) for c in r) for r in cap["values"]]
        assert any("raise pre-seed by Q3" in r for r in flat)
        assert any("Moldova contract stuck with legal" in r for r in flat)
        assert cap["out"]["manual_rows"] == 2

    async def test_a_failed_read_writes_nothing_at_all(self):
        """We do not know what he wrote, so writing anyway would delete it.
        A stale tab is recoverable; his words are not."""
        cap = await self._render(self._prior([["Escalations", "something"]]),
                                 fail_read=True)
        assert cap["out"] == {"skipped": "manual block unreadable"}
        assert cap["clear"] == [] and "values" not in cap

    async def test_a_tab_with_no_marker_gets_the_default_block_once(self):
        cap = await self._render([])
        assert cap["out"]["seeded_block"] is True
        flat = [str(r[0]) for r in cap["values"]]
        assert "Company OKRs" in flat and "Escalations" in flat

    async def test_an_existing_block_is_never_reseeded_over(self):
        cap = await self._render(self._prior([["Escalations", "mine"]]))
        assert cap["out"]["seeded_block"] is False
        assert not any("Company OKRs" in str(r[0]) for r in cap["values"])

    async def test_trailing_blank_rows_do_not_accumulate(self):
        """The block is read back and re-emitted, so padding would compound:
        each cycle reads its own blank rows and adds more."""
        cap = await self._render(self._prior([
            ["Escalations", "mine"], ["", ""], ["", ""], ["", ""]]))
        assert cap["out"]["manual_rows"] == 1

    async def test_the_block_moves_down_when_milestones_are_added(self):
        """A fixed row boundary would clip the block or strand it."""
        from services.ceo_sheet import MANAGEMENT_MARKER
        few = await self._render(self._prior([["Escalations", "mine"]]),
                                 milestones=[{"title": "A", "kind": "product",
                                              "target_date": "2026-08-31",
                                              "status": "open", "moves": []}])
        many = await self._render(self._prior([["Escalations", "mine"]]),
                                  milestones=[{"title": f"M{i}", "kind": "product",
                                               "target_date": "2026-08-31",
                                               "status": "open", "moves": []}
                                              for i in range(9)])

        def marker_row(cap):
            return next(i for i, r in enumerate(cap["values"])
                        if str(r[0]).startswith("MANAGEMENT"))
        assert marker_row(many) > marker_row(few)
        for cap in (few, many):
            rows = [str(r[0]) for r in cap["values"]]
            assert rows[marker_row(cap) + 1] == "Escalations"

    async def test_only_the_generated_half_is_protected(self):
        """Protecting the whole tab, as the Timeline does, would make the
        hand-maintained block read-only and the design pointless."""
        cap = await self._render(self._prior([["Escalations", "mine"]]))
        reqs = [r for b in cap["batches"] for r in b]
        prot = next(r["addProtectedRange"]["protectedRange"] for r in reqs
                    if "addProtectedRange" in r)
        marker = next(i for i, r in enumerate(cap["values"])
                      if str(r[0]).startswith("MANAGEMENT"))
        assert prot["range"]["endRowIndex"] == marker + 1
        assert prot["warningOnly"] is True

    async def test_the_clear_is_unbounded_in_rows(self):
        """A shrinking milestone list has the same shape as the Timeline bug:
        a computed row bound leaves the previous tail behind as live text."""
        cap = await self._render(self._prior([["Escalations", "mine"]]))
        assert cap["clear"] == ["'CEO'!A:E"]

    async def test_the_body_is_wiped_before_any_colour_is_applied(self):
        """A milestone that changed row position would otherwise leave its kind
        colour behind on a row that no longer holds it."""
        cap = await self._render(
            self._prior([["Escalations", "mine"]]),
            milestones=[{"title": "A", "kind": "product",
                         "target_date": "2026-08-31", "status": "open",
                         "moves": []}])
        reqs = [r for b in cap["batches"] for r in b]
        white = {"red": 1.0, "green": 1.0, "blue": 1.0}
        wipe = next(i for i, r in enumerate(reqs)
                    if (r.get("repeatCell") or {}).get("cell", {})
                    .get("userEnteredFormat", {}).get("backgroundColor") == white)
        kind = next(i for i, r in enumerate(reqs)
                    if (r.get("repeatCell") or {}).get("cell", {})
                    .get("userEnteredFormat", {}).get("backgroundColor")
                    == {"red": 0.80, "green": 0.88, "blue": 0.94})
        assert wipe < kind
