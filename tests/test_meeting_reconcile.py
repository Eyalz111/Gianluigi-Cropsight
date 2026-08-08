"""
Meetings tab reconcile — follow_up_meetings finally get a home. [2026-07-22]

Background: follow_up_meetings had NO Sheet identity. Their only surface was
add_follow_ups_as_tasks(), which appended a "Schedule: X" row to the TASKS tab
with 9 columns and NO col-J UUID — so reconcile classified each as hand-added
and created a DUPLICATE `tasks` row on every run, forever. Confirmed on live
data: Tasks row 200 was "Schedule: Virtual Friday sync meeting" with an empty
id. This is the fourth use of the entity_type reconcile recipe.

Invariants pinned here:
  - hand-added rows create in the DB AND get their UUID written back
    synchronously; a writeback failure ROLLS BACK the create (that failure mode
    is exactly what made the old rows multiply)
  - status regression is blocked only FROM A TERMINAL state — a stale cell can
    never un-hold a meeting, but parking one that was merely queued is allowed
  - the Rule 2 manual rail applies here too
  - an empty sheet read with snapshots present ABORTS
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

try:
    import processors.sheets_sync as ss
except Exception as e:  # pragma: no cover
    pytest.skip(f"cannot import sheets_sync ({e})", allow_module_level=True)


def _srow(**kw):
    base = {"id": "", "title": "", "label": "", "led_by": "", "proposed_date": "",
            "proposed_date_raw": "", "participants": "", "status": "not_scheduled",
            "agenda": "", "prep_needed": "", "source_meeting": "", "row_number": 2}
    base.update(kw)
    return base


def _dbrow(**kw):
    base = {"id": "m1", "title": "", "label": "", "led_by": "", "proposed_date": None,
            "participants": [], "status": "not_scheduled", "approval_status": "approved"}
    base.update(kw)
    return base


def _setup(monkeypatch, sheet, db, snap, enabled=True, shadow=False):
    import services.google_sheets as gs
    from config.settings import settings

    monkeypatch.setattr(settings, "MEETING_RECONCILE_ENABLED", enabled, raising=False)
    monkeypatch.setattr(settings, "MEETING_RECONCILE_SHADOW_MODE", shadow, raising=False)
    monkeypatch.setattr(settings, "TASK_TRACKER_SHEET_ID", "sheet-x", raising=False)

    fake = MagicMock()
    fake.get_all_meetings = AsyncMock(return_value=sheet)
    fake.add_meetings_batch_to_sheet = AsyncMock(return_value=True)
    fake._update_cell = AsyncMock(return_value=None)
    monkeypatch.setattr(gs, "sheets_service", fake)

    sc = ss.supabase_client
    calls = {"update": [], "manual": [], "snapshot": [], "create": [], "delete": [], "archive": []}
    fake.archive_meeting_rows = AsyncMock(
        side_effect=lambda rows: calls["archive"].extend(rows) or len(rows))
    monkeypatch.setattr(sc, "list_follow_up_meetings", lambda *a, **k: db)
    monkeypatch.setattr(sc, "get_meeting_snapshots", lambda *a, **k: snap)
    monkeypatch.setattr(sc, "update_follow_up_meeting",
                        lambda mid, **u: calls["update"].append((mid, u)) or {"id": mid})
    monkeypatch.setattr(sc, "mark_meeting_field_manual",
                        lambda mid, f, src: calls["manual"].append((mid, f, src)) or True)
    monkeypatch.setattr(sc, "upsert_meeting_snapshot",
                        lambda *a, **k: calls["snapshot"].append(a) or True)
    monkeypatch.setattr(sc, "create_follow_up_meeting_manual",
                        lambda **k: calls["create"].append(k) or {"id": "new-m", **k})
    monkeypatch.setattr(sc, "log_action", lambda *a, **k: None)
    return calls, fake


class TestGating:
    async def test_disabled_is_a_noop(self, monkeypatch):
        calls, _ = _setup(monkeypatch, [], [], {}, enabled=False)
        res = await ss.reconcile_meetings()
        assert "skipped" in res

    async def test_empty_read_with_snapshots_aborts(self, monkeypatch):
        """A transient Sheets read returning [] must never mass re-add."""
        calls, fake = _setup(monkeypatch, [], [_dbrow()], {"m1": {}})
        res = await ss.reconcile_meetings()
        assert res.get("error") == "sheet_read_empty"
        fake.add_meetings_batch_to_sheet.assert_not_called()


class TestPullAndPush:
    async def test_human_edit_pulls_and_marks_sticky(self, monkeypatch):
        sheet = [_srow(id="m1", title="Kickoff with Ido", led_by="Nechama Tik")]
        db = [_dbrow(id="m1", title="Kickoff with Ido", led_by="Eyal Zror")]
        snap = {"m1": {"title": "Kickoff with Ido", "led_by": "Eyal Zror"}}
        calls, _ = _setup(monkeypatch, sheet, db, snap)

        res = await ss.reconcile_meetings()

        assert res["pulled"] == 1
        assert calls["update"] == [("m1", {"led_by": "Nechama Tik"})]
        assert ("m1", "led_by", "sheet_edit") in calls["manual"]

    async def test_db_change_refreshes_untouched_cell(self, monkeypatch):
        sheet = [_srow(id="m1", title="Old title")]
        db = [_dbrow(id="m1", title="New title")]
        snap = {"m1": {"title": "Old title"}}
        calls, _ = _setup(monkeypatch, sheet, db, snap)

        res = await ss.reconcile_meetings()

        assert res["pushed"] >= 1
        assert calls["update"] == []

    async def test_sticky_field_is_not_reverted(self, monkeypatch):
        """Same Rule 2 rail as tasks/decisions."""
        sheet = [_srow(id="m1", title="Eyal's wording")]
        db = [_dbrow(id="m1", title="system wording", manual_title=True)]
        snap = {"m1": {"title": "Eyal's wording"}}
        calls, _ = _setup(monkeypatch, sheet, db, snap)

        res = await ss.reconcile_meetings()

        assert res["manual_held"] == 1
        assert calls["update"] == []


class TestStatusRegressionGuard:
    async def test_forward_move_pulls(self, monkeypatch):
        sheet = [_srow(id="m1", title="T", status="scheduled")]
        db = [_dbrow(id="m1", title="T", status="not_scheduled")]
        snap = {"m1": {"title": "T", "status": "not_scheduled"}}
        calls, _ = _setup(monkeypatch, sheet, db, snap)

        res = await ss.reconcile_meetings()

        assert res["pulled"] == 1
        assert calls["update"] == [("m1", {"status": "scheduled"})]

    async def test_backward_move_is_guarded(self, monkeypatch):
        """A meeting that was HELD cannot become merely scheduled again because
        a stale cell says so — it already happened."""
        sheet = [_srow(id="m1", title="T", status="scheduled")]
        db = [_dbrow(id="m1", title="T", status="held")]
        snap = {"m1": {"title": "T", "status": "not_scheduled"}}
        calls, _ = _setup(monkeypatch, sheet, db, snap)

        res = await ss.reconcile_meetings()

        assert res["status_guarded"] == 1
        assert calls["update"] == []

    async def test_unknown_status_is_ignored(self, monkeypatch):
        sheet = [_srow(id="m1", title="T", status="banana")]
        db = [_dbrow(id="m1", title="T", status="not_scheduled")]
        snap = {"m1": {"title": "T", "status": "not_scheduled"}}
        calls, _ = _setup(monkeypatch, sheet, db, snap)

        await ss.reconcile_meetings()
        assert calls["update"] == []


class TestHandAddedRows:
    async def test_blank_id_row_creates_and_writes_uuid_back(self, monkeypatch):
        """Unlike decisions (which need a source meeting), a meeting typed
        straight into the Sheet is legitimate — source_meeting_id is nullable."""
        sheet = [_srow(id="", title="Coffee with Marco", led_by="Nechama Tik",
                       participants="Marco Sutter, Eyal Zror", row_number=5)]
        calls, fake = _setup(monkeypatch, sheet, [], {})

        res = await ss.reconcile_meetings()

        assert res["created"] == 1
        assert calls["create"][0]["title"] == "Coffee with Marco"
        assert calls["create"][0]["participants"] == ["Marco Sutter", "Eyal Zror"]
        fake._update_cell.assert_awaited_once()
        assert "new-m" in str(fake._update_cell.await_args.kwargs.get("value"))

    async def test_uuid_writeback_failure_rolls_back_the_create(self, monkeypatch):
        """THE guard. A row left without its UUID is re-created on every run —
        precisely how the old "Schedule: X" rows multiplied forever."""
        sheet = [_srow(id="", title="Coffee with Marco", row_number=5)]
        calls, fake = _setup(monkeypatch, sheet, [], {})
        fake._update_cell = AsyncMock(side_effect=RuntimeError("sheets down"))

        deleted = []

        class _Tbl:
            def delete(self): return self
            def eq(self, col, val): deleted.append(val); return self
            def execute(self): return MagicMock(data=[])

        monkeypatch.setattr(ss.supabase_client, "_client",
                            MagicMock(table=lambda *a, **k: _Tbl()))

        res = await ss.reconcile_meetings()

        assert res["created"] == 0
        assert deleted == ["new-m"], "the orphaned DB row must be rolled back"

    async def test_blank_row_with_no_title_is_ignored(self, monkeypatch):
        calls, _ = _setup(monkeypatch, [_srow(id="", title="   ")], [], {})
        res = await ss.reconcile_meetings()
        assert res["created"] == 0
        assert calls["create"] == []


class TestReadd:
    async def test_db_only_meeting_is_readded(self, monkeypatch):
        calls, fake = _setup(monkeypatch, [], [_dbrow(id="m9", title="Missing")], {})
        res = await ss.reconcile_meetings()
        assert res["readded"] == 1
        fake.add_meetings_batch_to_sheet.assert_awaited_once()

    async def test_aged_dropped_meeting_not_readded(self, monkeypatch):
        # An AGED-OUT dropped meeting lives on Past Meetings — not re-added. [review #3]
        calls, fake = _setup(
            monkeypatch, [],
            [_dbrow(id="m9", title="Gone", status="dropped", updated_at="2020-01-01T00:00:00+00:00")], {})
        res = await ss.reconcile_meetings()
        assert res["readded"] == 0

    async def test_recent_dropped_meeting_is_readded(self, monkeypatch):
        # A RECENT dropped meeting stays on the working tab (greyed) until it ages
        # out, so an absent one is re-added. [review #3]
        calls, fake = _setup(
            monkeypatch, [],
            [_dbrow(id="m9", title="Recent", status="dropped", updated_at="2099-01-01T00:00:00+00:00")], {})
        res = await ss.reconcile_meetings()
        assert res["readded"] == 1

    async def test_shadow_mode_writes_nothing(self, monkeypatch):
        sheet = [_srow(id="m1", title="Edited")]
        db = [_dbrow(id="m1", title="Original")]
        snap = {"m1": {"title": "Original"}}
        calls, fake = _setup(monkeypatch, sheet, db, snap, shadow=True)

        res = await ss.reconcile_meetings()

        assert res["shadow"] is True
        assert calls["update"] == [] and calls["snapshot"] == []


class TestReviewFindings:
    """Regressions for the 2026-07-23 cloud-review findings."""

    async def test_readd_seeds_a_snapshot(self, monkeypatch):
        """bug_001 — reconcile_tasks seeds snapshots on re-add ([audit P1-04]);
        the meetings copy dropped it, so the next cycle read snap={} and pulled
        every field as a phantom human edit, freezing them. Worse here than on
        tasks: proposed_date's Rule 1 has no '!= db' guard, so it froze with no
        DB change at all."""
        db = [_dbrow(id="m9", title="Missing", led_by="Eyal Zror",
                     participants=["Eyal Zror"], status="not_scheduled")]
        calls, fake = _setup(monkeypatch, [], db, {})

        res = await ss.reconcile_meetings()

        assert res["readded"] == 1
        assert len(calls["snapshot"]) == 1, "a re-added row MUST get a snapshot"
        assert calls["snapshot"][0][0] == "m9"

    async def test_hand_added_row_syncs_canonical_values_back_to_the_cells(self, monkeypatch):
        """bug_003 — create canonicalizes ('roye' -> 'Roye Tadmor') but the cell
        kept the raw text, so snapshot != cell and the next reconcile marked the
        field manually-sticky: a fake human edit produced by our own write."""
        sheet = [_srow(id="", title="Sync with Roye", led_by="roye",
                       label="moldova", row_number=5)]
        calls, fake = _setup(monkeypatch, sheet, [], {})
        monkeypatch.setattr(
            ss.supabase_client, "create_follow_up_meeting_manual",
            lambda **k: {"id": "new-m", **k,
                         "led_by": "Roye Tadmor", "label": "Moldova Pilot"},
        )

        res = await ss.reconcile_meetings()

        assert res["created"] == 1
        body = fake.service.spreadsheets.return_value.values.return_value.batchUpdate.call_args
        written = [w["values"][0][0] for w in body.kwargs["body"]["data"]]
        assert "Roye Tadmor" in written, "canonical led_by must reach the cell"
        assert "Moldova Pilot" in written, "canonical label must reach the cell"
        # and the snapshot matches what is now in the sheet
        assert calls["snapshot"][0][4] == "Roye Tadmor"


class TestTerminalArchive:
    """Held meetings STAY on the tab as visible history; dropped meetings stay
    too until they've been untouched for the archival window (TASK_ARCHIVAL_DAYS),
    then age out to Past Meetings — the "60-day dropped timer". [2026-07-24]"""

    async def test_held_meeting_stays_on_the_tab(self, monkeypatch):
        # Eyal wants to SEE completed meetings — held no longer leaves the tab.
        sheet = [_srow(id="m1", title="Kickoff", status="held")]
        db = [_dbrow(id="m1", title="Kickoff", status="scheduled")]
        snap = {"m1": {"title": "Kickoff", "status": "scheduled"}}
        calls, _ = _setup(monkeypatch, sheet, db, snap)

        res = await ss.reconcile_meetings()

        assert res.get("archived", 0) == 0
        assert calls["archive"] == []
        assert len(calls["snapshot"]) == 1  # stays -> snapshotted

    async def test_recent_dropped_meeting_stays(self, monkeypatch):
        # Dropped but touched recently: within the window, so it stays (greyed,
        # sorted last) — it does NOT leave immediately anymore.
        sheet = [_srow(id="m1", title="X", status="dropped")]
        db = [_dbrow(id="m1", title="X", status="not_scheduled",
                     updated_at="2099-01-01T00:00:00+00:00")]
        snap = {"m1": {"title": "X", "status": "not_scheduled"}}
        calls, _ = _setup(monkeypatch, sheet, db, snap)

        res = await ss.reconcile_meetings()
        assert res.get("archived", 0) == 0
        assert calls["archive"] == []

    async def test_aged_dropped_meeting_archives(self, monkeypatch):
        # ALREADY dropped in the DB (not just this cycle) and untouched well beyond
        # the window -> ages out to Past Meetings. A meeting dropped *this* sync gets
        # the full window first (the drop edit is itself a touch). [review #14]
        sheet = [_srow(id="m1", title="X", status="dropped")]
        db = [_dbrow(id="m1", title="X", status="dropped",
                     updated_at="2020-01-01T00:00:00+00:00")]
        snap = {"m1": {"title": "X", "status": "dropped"}}
        calls, _ = _setup(monkeypatch, sheet, db, snap)

        res = await ss.reconcile_meetings()
        assert res.get("archived") == 1
        assert calls["archive"][0]["id"] == "m1"

    async def test_freshly_dropped_meeting_not_archived_same_sync(self, monkeypatch):
        # DB still holds the pre-drop status (not_scheduled) with an OLD timestamp;
        # the sheet cell was just set to 'dropped'. The drop is a fresh edit, so it
        # gets the full window — NOT archived on the same reconcile. [review #14]
        sheet = [_srow(id="m1", title="X", status="dropped")]
        db = [_dbrow(id="m1", title="X", status="not_scheduled",
                     updated_at="2020-01-01T00:00:00+00:00")]
        snap = {"m1": {"title": "X", "status": "not_scheduled"}}
        calls, _ = _setup(monkeypatch, sheet, db, snap)

        res = await ss.reconcile_meetings()
        assert res.get("archived", 0) == 0
        assert calls["archive"] == []

    async def test_held_db_meeting_is_readded(self, monkeypatch):
        """A HELD meeting must stay on the working tab as history — if its row is
        absent it is RE-ADDED. The old `not in _TERMINAL` filter silently vanished
        held meetings from the whole workspace. [review #3]"""
        calls, fake = _setup(
            monkeypatch, [], [_dbrow(id="m9", title="Kept", status="held")], {})
        res = await ss.reconcile_meetings()
        assert res["readded"] == 1
        fake.add_meetings_batch_to_sheet.assert_called()

    async def test_active_meeting_stays_and_gets_a_snapshot(self, monkeypatch):
        sheet = [_srow(id="m1", title="Soon", status="scheduled")]
        db = [_dbrow(id="m1", title="Soon", status="not_scheduled")]
        snap = {"m1": {"title": "Soon", "status": "not_scheduled"}}
        calls, _ = _setup(monkeypatch, sheet, db, snap)

        res = await ss.reconcile_meetings()
        assert res.get("archived", 0) == 0
        assert len(calls["snapshot"]) == 1
        assert calls["archive"] == []


class TestAddMeetingsIdempotency:
    """add_meetings_batch_to_sheet must be idempotent by col-J UUID — a second
    write for the same follow-up (re-distribution, or the reconcile re-add racing
    the distribution write) must NOT append a duplicate row. This is the
    2026-07-24 weekly-meeting bug: 10 rows for 7 follow-ups."""

    async def test_skips_follow_ups_already_on_the_tab(self, monkeypatch):
        import services.google_sheets as gs
        svc = MagicMock()
        vals = svc.spreadsheets.return_value.values.return_value
        vals.get.return_value.execute.return_value = {"values": [["m1"]]}  # m1 present
        captured = {}

        def _append(**kw):
            captured["rows"] = kw["body"]["values"]
            r = MagicMock(); r.execute.return_value = {}
            return r
        vals.append.side_effect = _append

        monkeypatch.setattr(gs.sheets_service, "_service", svc)
        monkeypatch.setattr(gs.sheets_service, "ensure_meetings_tab", AsyncMock(return_value=0))
        monkeypatch.setattr(gs.settings, "TASK_TRACKER_SHEET_ID", "sheet123")

        ok = await gs.sheets_service.add_meetings_batch_to_sheet(
            meetings=[{"id": "m1", "title": "Already there"},
                      {"id": "m2", "title": "New one"}])
        assert ok is True
        assert len(captured["rows"]) == 1, "only the not-yet-present follow-up is appended"
        assert captured["rows"][0][gs.MEETING_COL_INDEX["id"]] == "m2"

    async def test_all_present_is_a_noop(self, monkeypatch):
        import services.google_sheets as gs
        svc = MagicMock()
        vals = svc.spreadsheets.return_value.values.return_value
        vals.get.return_value.execute.return_value = {"values": [["m1"], ["m2"]]}
        vals.append.side_effect = AssertionError("append must not be called when all present")
        monkeypatch.setattr(gs.sheets_service, "_service", svc)
        monkeypatch.setattr(gs.sheets_service, "ensure_meetings_tab", AsyncMock(return_value=0))
        monkeypatch.setattr(gs.settings, "TASK_TRACKER_SHEET_ID", "sheet123")
        ok = await gs.sheets_service.add_meetings_batch_to_sheet(
            meetings=[{"id": "m1", "title": "A"}, {"id": "m2", "title": "B"}])
        assert ok is True  # nothing to add, but not a failure


class TestFollowUpRenameThroughEdit:
    """When Eyal renames a follow-up in the summary edit, it must update the DB
    row IN PLACE (keep the UUID) — not create a new row + orphan the old one."""

    def test_rename_with_index_updates_in_place(self):
        from guardrails.edit_reconcile import reconcile_children
        old = [{"id": "X", "title": "To Seed VC", "led_by": "Eyal"}]
        edited = [{"index": 1, "title": "2SID VC", "led_by": "Eyal"}]
        plan = reconcile_children(
            old, edited, text_of=lambda f: f.get("title", ""),
            secondary_of=lambda f: f.get("led_by", ""))
        assert plan["updates"] == [("X", edited[0])]
        assert plan["creates"] == [] and plan["deletes"] == []

    def test_rename_without_index_loses_identity_known_limitation(self):
        # KNOWN LIMITATION pinned as a test: if the edit LLM DROPS the index on a
        # renamed follow-up whose content words changed ("To Seed"->"2SID"), the
        # record-linkage can't tie it back — the old row is deleted and a new one
        # created (new UUID). The apply_edits prompt instructs the LLM to keep the
        # index precisely to avoid this; this documents the fallback.
        from guardrails.edit_reconcile import reconcile_children
        old = [{"id": "X", "title": "To Seed VC", "led_by": "Eyal"}]
        edited = [{"title": "2SID VC", "led_by": "Eyal"}]  # no index
        plan = reconcile_children(
            old, edited, text_of=lambda f: f.get("title", ""),
            secondary_of=lambda f: f.get("led_by", ""))
        assert plan["deletes"] == ["X"]
        assert [i["title"] for i in plan["creates"]] == ["2SID VC"]


class TestParkedAndBackwardMoves:
    """The guard was a FULL monotonic ordering, so every backward transition
    was illegal. That is wrong for a working document: parking something
    already marked "to schedule" is a normal decision, and it was silently
    refused with the cell snapping back inside 30 minutes — the sheet arguing
    with the person using it. Only history is worth protecting. [2026-08-09]"""

    async def test_parked_is_a_valid_status(self, monkeypatch):
        sheet = [_srow(id="m1", title="T", status="parked")]
        db = [_dbrow(id="m1", title="T", status="not_scheduled")]
        snap = {"m1": {"title": "T", "status": "not_scheduled"}}
        calls, _ = _setup(monkeypatch, sheet, db, snap)

        res = await ss.reconcile_meetings()

        assert res["pulled"] == 1
        assert calls["update"] == [("m1", {"status": "parked"})]

    async def test_parking_a_queued_meeting_is_allowed(self, monkeypatch):
        """'scheduled' -> 'parked' is backwards in the old ordering and was
        refused. It is a decision, not a stale cell."""
        sheet = [_srow(id="m1", title="T", status="parked")]
        db = [_dbrow(id="m1", title="T", status="scheduled")]
        snap = {"m1": {"title": "T", "status": "scheduled"}}
        calls, res = _setup(monkeypatch, sheet, db, snap)

        out = await ss.reconcile_meetings()

        assert out["status_guarded"] == 0
        assert calls["update"] == [("m1", {"status": "parked"})]

    async def test_a_held_meeting_still_cannot_be_un_held(self, monkeypatch):
        sheet = [_srow(id="m1", title="T", status="parked")]
        db = [_dbrow(id="m1", title="T", status="held")]
        snap = {"m1": {"title": "T", "status": "not_scheduled"}}
        calls, _ = _setup(monkeypatch, sheet, db, snap)

        out = await ss.reconcile_meetings()

        assert out["status_guarded"] == 1
        assert calls["update"] == []

    async def test_a_dropped_meeting_still_cannot_be_revived_from_a_cell(self, monkeypatch):
        sheet = [_srow(id="m1", title="T", status="scheduled")]
        db = [_dbrow(id="m1", title="T", status="dropped")]
        snap = {"m1": {"title": "T", "status": "not_scheduled"}}
        calls, _ = _setup(monkeypatch, sheet, db, snap)

        out = await ss.reconcile_meetings()

        assert out["status_guarded"] == 1
        assert calls["update"] == []


class TestTheMeetingsWorkbookIsOneAccessor:
    """The meetings tabs touch a spreadsheet id in a dozen places. Every one of
    them used to name the Task Tracker directly, so moving the tabs meant
    finding all twelve — and a reader left pointed at a workbook where the tab
    no longer exists reads nothing and reports every meeting as an orphan
    rather than failing."""

    def test_it_defaults_to_the_task_tracker(self, monkeypatch):
        from config.settings import settings
        from services.google_sheets import sheets_service
        monkeypatch.setattr(settings, "TASK_TRACKER_SHEET_ID", "tracker", raising=False)
        monkeypatch.setattr(settings, "MEETINGS_SHEET_ID", "", raising=False)
        assert sheets_service.meetings_workbook() == "tracker"

    def test_the_override_wins(self, monkeypatch):
        from config.settings import settings
        from services.google_sheets import sheets_service
        monkeypatch.setattr(settings, "TASK_TRACKER_SHEET_ID", "tracker", raising=False)
        monkeypatch.setattr(settings, "MEETINGS_SHEET_ID", "elsewhere", raising=False)
        assert sheets_service.meetings_workbook() == "elsewhere"

    def test_no_meetings_method_names_the_task_tracker_directly(self):
        """The mechanical form of "one accessor". A new call site that reads
        the setting itself is exactly how the move leaves one reader behind."""
        import inspect
        import services.google_sheets as gs
        for name in ("ensure_meetings_tab", "dedupe_meetings_tab",
                     "get_all_meetings", "add_meetings_batch_to_sheet",
                     "archive_meeting_rows", "rebuild_meetings_sheet",
                     "format_meetings_tab"):
            src = inspect.getsource(getattr(gs.GoogleSheetsService, name))
            assert "TASK_TRACKER_SHEET_ID" not in src, name

    def test_the_orphan_detector_reads_the_meetings_workbook(self):
        import inspect
        import processors.meeting_qa as mq
        src = inspect.getsource(mq.read_sheet_index)
        assert "meetings_workbook()" in src
