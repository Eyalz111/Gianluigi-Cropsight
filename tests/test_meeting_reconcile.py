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
            "proposed_date_raw": "", "participants": "", "status": "to_schedule",
            "agenda": "", "prep_needed": "", "source_meeting": "", "row_number": 2}
    base.update(kw)
    return base


def _dbrow(**kw):
    base = {"id": "m1", "title": "", "label": "", "led_by": "", "proposed_date": None,
            "participants": [], "status": "to_schedule", "approval_status": "approved"}
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
    fake.meetings_layout_ok = AsyncMock(return_value=True)
    # Nothing archived unless a test says so — so a terminal meeting on NEITHER
    # tab is still surfaced, which is the invariant review #3 established.
    fake.archived_meeting_ids = AsyncMock(return_value=set())
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


class TestStatusCellCanonicalisation:
    """The rename was invisible on the tab, and canonicalising is why. [2026-08-13]

    `not_scheduled` became `to_schedule` on 2026-08-12, and three live rows kept
    showing the old word for a day. Not a write failure: the cell, the DB row and
    the snapshot ALL held `not_scheduled`, `canonical_meeting_status()` maps all
    three to `to_schedule`, so every comparison in the merge was equal, no
    divergence was found, and no branch wrote the cell. The old spelling was also
    outside the dropdown (red triangle) and matched no colour rule.

    A tolerant reader needs a writer that settles on the canonical spelling, or
    the value it tolerates lives on screen forever.
    """

    @staticmethod
    def _status_writes(fake):
        from services.google_sheets import MEETING_COLUMNS
        call = fake.service.spreadsheets.return_value.values.return_value.batchUpdate.call_args
        if call is None:
            return []
        col = MEETING_COLUMNS["status"]
        return [w["values"][0][0] for w in call.kwargs["body"]["data"]
                if f"!{col}" in w["range"]]

    async def test_agreed_old_spelling_is_rewritten(self, monkeypatch):
        """The live bug. Three surfaces agree on the retired word — and agreeing
        is exactly why nothing used to fix it."""
        sheet = [_srow(id="m1", title="T", status="not_scheduled")]
        db = [_dbrow(id="m1", title="T", status="not_scheduled")]
        snap = {"m1": {"title": "T", "status": "not_scheduled"}}
        calls, fake = _setup(monkeypatch, sheet, db, snap)

        res = await ss.reconcile_meetings()

        assert res["canonicalized"] == 1
        assert self._status_writes(fake) == ["to_schedule"]
        # Cosmetic only: no pull, no DB write, no sticky mark.
        assert res["pulled"] == 0
        assert calls["update"] == []
        assert calls["manual"] == []

    async def test_snapshot_keeps_the_canonical_value(self, monkeypatch):
        """The snapshot must record what is NOW in the cell, or the next cycle
        reads a fresh divergence against the value we just wrote."""
        sheet = [_srow(id="m1", title="T", status="not_scheduled")]
        db = [_dbrow(id="m1", title="T", status="not_scheduled")]
        snap = {"m1": {"title": "T", "status": "not_scheduled"}}
        calls, _ = _setup(monkeypatch, sheet, db, snap)

        await ss.reconcile_meetings()

        assert calls["snapshot"][0][7] == "to_schedule"

    async def test_case_only_difference_is_normalised(self, monkeypatch):
        """Compare the LITERAL cell, not the lower-cased read. `To_Schedule`
        canonicalises to itself once lowered, so a lower-cased comparison would
        leave a hand-typed capital sitting outside the dropdown forever."""
        sheet = [_srow(id="m1", title="T", status="To_Schedule")]
        db = [_dbrow(id="m1", title="T", status="to_schedule")]
        snap = {"m1": {"title": "T", "status": "to_schedule"}}
        calls, fake = _setup(monkeypatch, sheet, db, snap)

        res = await ss.reconcile_meetings()

        assert res["canonicalized"] == 1
        assert self._status_writes(fake) == ["to_schedule"]
        assert calls["update"] == []

    async def test_already_canonical_cell_is_left_alone(self, monkeypatch):
        """No write means no write. A cosmetic pass that touches every row every
        cycle is a quota bill and a revision-history flood."""
        sheet = [_srow(id="m1", title="T", status="to_schedule")]
        db = [_dbrow(id="m1", title="T", status="to_schedule")]
        snap = {"m1": {"title": "T", "status": "to_schedule"}}
        calls, fake = _setup(monkeypatch, sheet, db, snap)

        res = await ss.reconcile_meetings()

        assert res["canonicalized"] == 0
        assert self._status_writes(fake) == []

    async def test_a_pulled_status_leaves_the_cell_canonical(self, monkeypatch):
        """Pulling `not_scheduled` writes `to_schedule` to the DB; the cell has
        to end up saying the same thing."""
        sheet = [_srow(id="m1", title="T", status="not_scheduled")]
        db = [_dbrow(id="m1", title="T", status="parked")]
        snap = {"m1": {"title": "T", "status": "parked"}}
        calls, fake = _setup(monkeypatch, sheet, db, snap)

        res = await ss.reconcile_meetings()

        assert calls["update"] == [("m1", {"status": "to_schedule"})]
        assert res["canonicalized"] == 1
        assert self._status_writes(fake) == ["to_schedule"]

    async def test_guarded_row_is_written_once(self, monkeypatch):
        """The terminal guard already pushes the DB value into the cell. A second
        cosmetic write of the same range would be the pass fighting the branch."""
        sheet = [_srow(id="m1", title="T", status="not_scheduled")]
        db = [_dbrow(id="m1", title="T", status="held")]
        snap = {"m1": {"title": "T", "status": "not_scheduled"}}
        calls, fake = _setup(monkeypatch, sheet, db, snap)

        res = await ss.reconcile_meetings()

        assert res["canonicalized"] == 0
        assert self._status_writes(fake) == ["held"]

    async def test_unknown_status_is_not_canonicalised_into_a_guess(self, monkeypatch):
        """`canonical_meeting_status` returns "" for anything it does not know,
        and "" must never reach the cell as an erase."""
        sheet = [_srow(id="m1", title="T", status="banana")]
        db = [_dbrow(id="m1", title="T", status="parked")]
        snap = {"m1": {"title": "T", "status": "parked"}}
        calls, fake = _setup(monkeypatch, sheet, db, snap)

        await ss.reconcile_meetings()

        assert self._status_writes(fake) == ["parked"]
        assert calls["update"] == []


class TestTheTimingReadingIsComparedWhole:
    """A guard keyed on the one field that never changes. [2026-08-13]

    The timing block only wrote when `timing_text` differed — and `timing_text`
    IS the cell, verbatim, so it differs exactly when the cell was edited. When
    the PARSER learned the `23-29/8/2026` form, every meeting already carrying
    that text was skipped and its new window was never written: three live rows
    kept a null window across the very deploy that taught the parser to read
    them, and the reconcile reported no work to do.

    Same shape as the `not_scheduled` cell earlier the same day — a guard keyed
    on a field that already agrees suppresses the update of the fields that do
    not.
    """

    async def test_the_window_lands_when_only_the_window_changed(self, monkeypatch):
        sheet = [_srow(id="m1", title="T", proposed_date_raw="23-29/8/2026")]
        db = [_dbrow(id="m1", title="T", timing_text="23-29/8/2026",
                     recurrence=None, window_start=None, window_end=None)]
        snap = {"m1": {"title": "T", "status": "to_schedule"}}
        calls, _ = _setup(monkeypatch, sheet, db, snap)

        await ss.reconcile_meetings()

        assert calls["update"], "the reconcile wrote nothing at all"
        upd = calls["update"][0][1]
        assert upd["window_start"] == "2026-08-23"
        assert upd["window_end"] == "2026-08-29"

    async def test_an_already_correct_reading_is_not_rewritten(self, monkeypatch):
        """No churn: a row whose stored reading already matches must not be
        updated on every 30-minute cycle."""
        sheet = [_srow(id="m1", title="T", proposed_date_raw="23-29/8/2026")]
        db = [_dbrow(id="m1", title="T", timing_text="23-29/8/2026",
                     recurrence=None, window_start="2026-08-23",
                     window_end="2026-08-29")]
        snap = {"m1": {"title": "T", "status": "to_schedule"}}
        calls, _ = _setup(monkeypatch, sheet, db, snap)

        await ss.reconcile_meetings()

        assert calls["update"] == []

    async def test_a_timestamptz_shaped_window_still_compares_equal(self, monkeypatch):
        """The column is a DATE, but a driver that hands back a full timestamp
        must not read as a difference and rewrite the row forever."""
        sheet = [_srow(id="m1", title="T", proposed_date_raw="23-29/8/2026")]
        db = [_dbrow(id="m1", title="T", timing_text="23-29/8/2026",
                     recurrence=None,
                     window_start="2026-08-23T00:00:00+00:00",
                     window_end="2026-08-29T00:00:00+00:00")]
        snap = {"m1": {"title": "T", "status": "to_schedule"}}
        calls, _ = _setup(monkeypatch, sheet, db, snap)

        await ss.reconcile_meetings()

        assert calls["update"] == []

    async def test_a_recurrence_that_changed_also_lands(self, monkeypatch):
        sheet = [_srow(id="m1", title="T", proposed_date_raw="Once a week")]
        db = [_dbrow(id="m1", title="T", timing_text="Once a week",
                     recurrence=None, window_start=None, window_end=None)]
        snap = {"m1": {"title": "T", "status": "to_schedule"}}
        calls, _ = _setup(monkeypatch, sheet, db, snap)

        await ss.reconcile_meetings()

        assert calls["update"][0][1]["recurrence"] == "weekly"


class TestHeldMeetingsAgeOut:
    """Held meetings sink, then leave after two weeks. Eyal's number. [2026-08-13]

    The sinking was already true (MEETING_DISPLAY_ORDER puts held second-from-
    last); the leaving needed a notion the schema did not have. `held_at` is
    stamped by a database trigger on the transition INTO held and is not touched
    afterwards, which is the whole point — `updated_at` moves on every edit, and
    the reconcile edits rows, so both held meetings on the live tab carried the
    timestamp of last night's sync rather than of the day they happened.
    """

    @staticmethod
    def _ago(days):
        from datetime import datetime, timezone, timedelta
        return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()

    async def test_held_longer_than_two_weeks_leaves(self, monkeypatch):
        sheet = [_srow(id="m1", title="Kickoff", status="held")]
        db = [_dbrow(id="m1", title="Kickoff", status="held",
                     held_at=self._ago(15))]
        snap = {"m1": {"title": "Kickoff", "status": "held"}}
        calls, _ = _setup(monkeypatch, sheet, db, snap)

        res = await ss.reconcile_meetings()

        assert res.get("archived_held") == 1
        assert calls["archive"][0]["id"] == "m1"

    async def test_held_inside_the_window_stays(self, monkeypatch):
        sheet = [_srow(id="m1", title="Kickoff", status="held")]
        db = [_dbrow(id="m1", title="Kickoff", status="held",
                     held_at=self._ago(3))]
        snap = {"m1": {"title": "Kickoff", "status": "held"}}
        calls, _ = _setup(monkeypatch, sheet, db, snap)

        res = await ss.reconcile_meetings()

        assert res.get("archived", 0) == 0
        assert calls["archive"] == []
        assert len(calls["snapshot"]) == 1, "it stays, so it is snapshotted"

    async def test_a_missing_held_at_NEVER_archives(self, monkeypatch):
        """THE GUARD THAT MATTERS. Before migrate_meeting_held_at.sql runs, every
        held row reads None. Treating an absent timestamp as infinitely old would
        sweep every held meeting off the tab the first time this ran against an
        unmigrated database — 48 rows, in one pass, on a deploy."""
        sheet = [_srow(id="m1", title="Kickoff", status="held")]
        db = [_dbrow(id="m1", title="Kickoff", status="held")]   # no held_at key
        snap = {"m1": {"title": "Kickoff", "status": "held"}}
        calls, _ = _setup(monkeypatch, sheet, db, snap)

        res = await ss.reconcile_meetings()

        assert res.get("archived", 0) == 0
        assert calls["archive"] == []

    async def test_an_explicit_null_held_at_never_archives(self, monkeypatch):
        """A present-but-null column, not just a missing key — the distinction
        that broke every meeting edit for two days on 2026-08-11."""
        sheet = [_srow(id="m1", title="Kickoff", status="held")]
        db = [_dbrow(id="m1", title="Kickoff", status="held", held_at=None)]
        snap = {"m1": {"title": "Kickoff", "status": "held"}}
        calls, _ = _setup(monkeypatch, sheet, db, snap)

        res = await ss.reconcile_meetings()

        assert res.get("archived", 0) == 0
        assert calls["archive"] == []

    async def test_the_timer_is_held_at_not_updated_at(self, monkeypatch):
        """Held long ago, edited last night. `updated_at` says "fresh" and
        `held_at` says "a month" — the row must leave. Keyed on updated_at (the
        way the dropped timer is) a rename would silently restart the fortnight."""
        sheet = [_srow(id="m1", title="Kickoff", status="held")]
        db = [_dbrow(id="m1", title="Kickoff", status="held",
                     held_at=self._ago(30), updated_at=self._ago(0))]
        snap = {"m1": {"title": "Kickoff", "status": "held"}}
        calls, _ = _setup(monkeypatch, sheet, db, snap)

        res = await ss.reconcile_meetings()

        assert res.get("archived_held") == 1

    async def test_an_ancient_updated_at_does_not_drag_a_fresh_hold_out(self, monkeypatch):
        """The converse. Held yesterday, untouched otherwise for a year."""
        sheet = [_srow(id="m1", title="Kickoff", status="held")]
        db = [_dbrow(id="m1", title="Kickoff", status="held",
                     held_at=self._ago(1), updated_at="2020-01-01T00:00:00+00:00")]
        snap = {"m1": {"title": "Kickoff", "status": "held"}}
        calls, _ = _setup(monkeypatch, sheet, db, snap)

        res = await ss.reconcile_meetings()

        assert res.get("archived", 0) == 0

    async def test_held_has_its_own_window_not_the_60_day_one(self, monkeypatch):
        """20 days is past the fortnight and nowhere near TASK_ARCHIVAL_DAYS.
        Sharing the dropped timer would keep held meetings for two months."""
        sheet = [_srow(id="m1", title="Kickoff", status="held")]
        db = [_dbrow(id="m1", title="Kickoff", status="held",
                     held_at=self._ago(20))]
        snap = {"m1": {"title": "Kickoff", "status": "held"}}
        calls, _ = _setup(monkeypatch, sheet, db, snap)

        res = await ss.reconcile_meetings()

        assert res.get("archived_held") == 1

    async def test_the_window_is_configurable(self, monkeypatch):
        from config.settings import settings
        monkeypatch.setattr(settings, "MEETING_HELD_ARCHIVE_DAYS", 60, raising=False)
        sheet = [_srow(id="m1", title="Kickoff", status="held")]
        db = [_dbrow(id="m1", title="Kickoff", status="held",
                     held_at=self._ago(20))]
        snap = {"m1": {"title": "Kickoff", "status": "held"}}
        calls, _ = _setup(monkeypatch, sheet, db, snap)

        res = await ss.reconcile_meetings()

        assert res.get("archived", 0) == 0

    async def test_a_meeting_held_in_THIS_cycle_gets_its_full_window(self, monkeypatch):
        """The edit that marks a meeting held is itself a touch. The DB still
        holds the pre-hold status, so nothing may archive it out from under the
        person who has just marked it — even with a stale held_at present."""
        sheet = [_srow(id="m1", title="Kickoff", status="held")]
        db = [_dbrow(id="m1", title="Kickoff", status="scheduled",
                     held_at=self._ago(90))]
        snap = {"m1": {"title": "Kickoff", "status": "scheduled"}}
        calls, _ = _setup(monkeypatch, sheet, db, snap)

        res = await ss.reconcile_meetings()

        assert res.get("archived", 0) == 0
        assert calls["update"] == [("m1", {"status": "held"})]

    async def test_shadow_mode_moves_nothing(self, monkeypatch):
        sheet = [_srow(id="m1", title="Kickoff", status="held")]
        db = [_dbrow(id="m1", title="Kickoff", status="held",
                     held_at=self._ago(90))]
        snap = {"m1": {"title": "Kickoff", "status": "held"}}
        calls, fake = _setup(monkeypatch, sheet, db, snap, shadow=True)

        await ss.reconcile_meetings()

        fake.archive_meeting_rows.assert_not_called()

    async def test_an_archived_held_meeting_is_not_dragged_back(self, monkeypatch):
        """It leaves the tab, so the next cycle sees a DB-only held meeting. The
        re-add path must read Past Meetings and leave it there, or the fortnight
        timer becomes a row that vanishes and reappears every 30 minutes."""
        db = [_dbrow(id="m1", title="Kickoff", status="held",
                     held_at=self._ago(90))]
        calls, fake = _setup(monkeypatch, [], db, {"m1": {"status": "held"}})
        fake.archived_meeting_ids = AsyncMock(return_value={"m1"})

        res = await ss.reconcile_meetings()

        assert res.get("readded", 0) == 0
        fake.add_meetings_batch_to_sheet.assert_not_called()


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
        # The append now refuses to run against a tab it cannot recognise; these
        # tests are about idempotency, so the layout is a given.
        monkeypatch.setattr(gs.sheets_service, "meetings_layout_ok",
                            AsyncMock(return_value=True))
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
        # The append now refuses to run against a tab it cannot recognise; these
        # tests are about idempotency, so the layout is a given.
        monkeypatch.setattr(gs.sheets_service, "meetings_layout_ok",
                            AsyncMock(return_value=True))
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


class TestTheSimplifiedLayout:
    """Eyal, 2026-08-09: "we dont need the columns - agenda, prep needed,
    source meeting, id - too much non necessery information"."""

    def test_the_visible_columns_are_the_ones_agreed(self):
        import services.google_sheets as gs
        assert gs.MEETING_TRACKER_HEADERS == [
            "Meeting", "Project", "Led By", "Proposed Date", "Participants",
            "Status", "Priority", "_id"]

    def test_the_visible_count_matches_the_headers(self):
        """Derived, not asserted as a number — the count and the header list
        drifting apart is what hides a column."""
        import services.google_sheets as gs
        assert (gs.MEETING_VISIBLE_COLUMNS
                == gs.MEETING_TRACKER_HEADERS.index("_id"))

    def test_the_dropped_columns_are_gone(self):
        import services.google_sheets as gs
        for gone in ("agenda", "prep_needed", "source_meeting"):
            assert gone not in gs.MEETING_COLUMNS

    def test_the_identity_column_survives_hidden(self):
        """It cannot be deleted: without it the reconcile cannot tell which
        meeting a row IS - the defect that made "Schedule: X" rows multiply
        forever. Hidden is what Eyal actually wanted."""
        import services.google_sheets as gs
        row = gs.GoogleSheetsService._meeting_row({"id": "m1", "title": "T"})
        assert len(row) == len(gs.MEETING_TRACKER_HEADERS)
        assert row[gs.MEETING_COL_INDEX["id"]] == "m1"
        reqs = gs.sheets_service.meetings_format_requests(
            1, gs.MEETING_TRACKER_HEADERS)
        hide = [r["updateDimensionProperties"] for r in reqs
                if "updateDimensionProperties" in r
                and r["updateDimensionProperties"]["properties"].get(
                    "hiddenByUser") is True]
        assert hide[0]["range"]["startIndex"] == gs.MEETING_VISIBLE_COLUMNS

    def test_the_visible_columns_are_explicitly_shown(self):
        """Same assert-the-whole-state rule the area tabs needed after Priority
        and Comments spent a session invisible."""
        import services.google_sheets as gs
        reqs = gs.sheets_service.meetings_format_requests(
            1, gs.MEETING_TRACKER_HEADERS)
        show = [r["updateDimensionProperties"] for r in reqs
                if "updateDimensionProperties" in r
                and r["updateDimensionProperties"]["properties"].get(
                    "hiddenByUser") is False]
        assert show and show[0]["range"]["endIndex"] == gs.MEETING_VISIBLE_COLUMNS

    def test_the_archive_keeps_its_own_identity_column(self):
        """Past Meetings has an extra visible column, so `_id` sits a letter
        further right. Sharing the Meetings letter read the "Moved" date as a
        UUID and reported every archived meeting as missing."""
        import services.google_sheets as gs
        assert gs.MEETINGS_ARCHIVE_HEADERS[-1] == "_id"
        assert gs.MEETING_TRACKER_HEADERS[-1] == "_id"
        # One letter further right, because the archive has the extra "Moved".
        assert (ord(gs.MEETINGS_ARCHIVE_ID_COLUMN)
                == ord(gs.MEETING_COLUMNS["id"]) + 1)

    def test_outcome_is_gone_from_the_archive(self):
        """It was written from `status`, so it said "held" beside a Status
        column already saying "held"."""
        import services.google_sheets as gs
        assert "Outcome" not in gs.MEETINGS_ARCHIVE_HEADERS


class TestTheProvenanceChip:
    """Source Meeting survives as a chip in the title rather than a column, so
    "every extracted item cites its source" outlives the simplification."""

    def test_the_source_is_folded_into_the_title(self):
        import services.google_sheets as gs
        row = gs.GoogleSheetsService._meeting_row(
            {"id": "m1", "title": "Call with Brian"}, source_meeting="Weekly Sync")
        assert "Call with Brian" in row[0] and "Weekly Sync" in row[0]

    def test_it_is_stripped_on_read_so_the_merge_is_honest(self):
        """The DB title has no chip. Comparing raw text would make every row
        look edited, every cycle."""
        from services.project_status_rows import strip_provenance
        import services.google_sheets as gs
        row = gs.GoogleSheetsService._meeting_row(
            {"id": "m1", "title": "Call with Brian"}, source_meeting="Weekly Sync")
        assert strip_provenance(row[0]) == "Call with Brian"

    def test_a_chip_is_never_stacked_twice(self):
        """archive_meeting_rows passes a row read back OFF the sheet, whose
        title already carries one."""
        import services.google_sheets as gs
        once = gs.GoogleSheetsService._meeting_row(
            {"id": "m1", "title": "Call with Brian"}, source_meeting="Weekly")
        twice = gs.GoogleSheetsService._meeting_row(
            {"id": "m1", "title": once[0]}, source_meeting="Weekly")
        assert twice[0].count("[") == 1


class TestTheHeaderBlock:
    def test_it_is_three_rows_like_the_area_tabs(self):
        import services.google_sheets as gs
        block = gs.meetings_header_block()
        assert len(block) == gs.MEETING_HEADER_ROW
        assert block[-1] == gs.MEETING_TRACKER_HEADERS
        assert gs.MEETING_FIRST_BODY_ROW == gs.MEETING_HEADER_ROW + 1

    def test_every_banner_row_spans_the_full_width(self):
        """A short row leaves the right-hand cells unstyled by the band."""
        import services.google_sheets as gs
        assert all(len(r) == len(gs.MEETING_TRACKER_HEADERS)
                   for r in gs.meetings_header_block())

    def test_the_body_starts_below_the_block(self):
        """Being one row out here writes every edit into the wrong meeting."""
        import services.google_sheets as gs
        reqs = gs.sheets_service.meetings_format_requests(
            1, gs.MEETING_TRACKER_HEADERS)
        frozen = next(r for r in reqs if "updateSheetProperties" in r)
        assert (frozen["updateSheetProperties"]["properties"]["gridProperties"]
                ["frozenRowCount"] == gs.MEETING_HEADER_ROW)


class TestTheProjectColumn:
    """Eyal: "column project - must be connected with a project on one of the
    tabs! in not, keep it clean or say not connected"."""

    def _reqs(self, names=("Italy", "Product V1")):
        import services.google_sheets as gs
        return gs.sheets_service.meetings_format_requests(
            1, gs.MEETING_TRACKER_HEADERS, list(names))

    def test_it_is_a_dropdown_of_the_canonical_projects(self):
        import services.google_sheets as gs
        dv = [r["setDataValidation"] for r in self._reqs()
              if "setDataValidation" in r
              and r["setDataValidation"]["range"]["startColumnIndex"]
              == gs.MEETING_COL_INDEX["label"]]
        assert len(dv) == 1
        vals = [v["userEnteredValue"]
                for v in dv[0]["rule"]["condition"]["values"]]
        assert vals == ["Italy", "Product V1"]

    def test_it_warns_rather_than_refusing(self):
        """Silently erasing what somebody typed is the one thing this system
        never does - the reconcile declines to store an off-vocabulary value,
        so a typo cannot reach the database either way."""
        import services.google_sheets as gs
        dv = next(r["setDataValidation"] for r in self._reqs()
                  if "setDataValidation" in r
                  and r["setDataValidation"]["range"]["startColumnIndex"]
                  == gs.MEETING_COL_INDEX["label"])
        assert dv["rule"]["strict"] is False

    def test_an_unassigned_project_is_greyed_not_labelled(self):
        """Grey registers instantly; twenty rows of "Not connected" is text you
        have to read past."""
        import services.google_sheets as gs
        rules = [r["addConditionalFormatRule"]["rule"] for r in self._reqs()
                 if "addConditionalFormatRule" in r
                 and r["addConditionalFormatRule"]["rule"]["ranges"][0]
                 ["startColumnIndex"] == gs.MEETING_COL_INDEX["label"]]
        assert len(rules) == 1
        f = rules[0]["booleanRule"]["condition"]["values"][0]["userEnteredValue"]
        assert f.startswith("=AND(") and '$B4=""' in f

    def test_no_dropdown_when_the_project_list_is_unavailable(self):
        """Better no dropdown than one listing nothing, which would flag every
        real project as invalid."""
        import services.google_sheets as gs
        dv = [r for r in self._reqs(names=())
              if "setDataValidation" in r
              and r["setDataValidation"]["range"]["startColumnIndex"]
              == gs.MEETING_COL_INDEX["label"]]
        assert dv == []


class TestOperationalOrdering:
    """Eyal: "i want it to be organized all the time (every refresh) in the
    order of scheduled, not scheduled, parked, held - operational importance"."""

    def test_the_display_order_is_operational(self):
        import services.google_sheets as gs
        order = sorted(gs.MEETING_DISPLAY_ORDER,
                       key=gs.MEETING_DISPLAY_ORDER.get)
        assert order == ["recurring", "scheduled", "to_schedule", "parked",
                         "held", "dropped"]

    def test_it_is_separate_from_the_state_machine(self):
        """Display order and progression order answer different questions;
        fusing them is how "show the booked ones first" would start meaning
        "a booked meeting cannot be parked"."""
        import services.google_sheets as gs
        assert gs.MEETING_DISPLAY_ORDER != gs.MEETING_STATUS_ORDER

    async def test_a_row_awaiting_its_identity_blocks_the_sort(self, monkeypatch):
        """That row is stamped by ROW NUMBER on the next cycle. Move it first
        and the stamp lands on somebody else's meeting."""
        from unittest.mock import AsyncMock, MagicMock
        import schedulers.reconcile_scheduler as rs
        import services.google_sheets as gs
        from config.settings import settings

        monkeypatch.setattr(settings, "MEETING_RECONCILE_ENABLED", True,
                            raising=False)
        fake = MagicMock()
        fake.meetings_layout_ok = AsyncMock(return_value=True)
        fake.get_all_meetings = AsyncMock(return_value=[
            {"id": "", "title": "Typed just now"}])
        fake.sort_meetings_tab = AsyncMock(return_value=9)
        monkeypatch.setattr(gs, "sheets_service", fake)

        assert await rs.reconcile_scheduler._sort_meetings_now() == 0
        fake.sort_meetings_tab.assert_not_called()

    async def test_a_settled_tab_is_sorted(self, monkeypatch):
        from unittest.mock import AsyncMock, MagicMock
        import schedulers.reconcile_scheduler as rs
        import services.google_sheets as gs
        from config.settings import settings

        monkeypatch.setattr(settings, "MEETING_RECONCILE_ENABLED", True,
                            raising=False)
        fake = MagicMock()
        fake.meetings_layout_ok = AsyncMock(return_value=True)
        fake.get_all_meetings = AsyncMock(return_value=[
            {"id": "m1", "title": "Booked"}])
        fake.sort_meetings_tab = AsyncMock(return_value=3)
        monkeypatch.setattr(gs, "sheets_service", fake)

        assert await rs.reconcile_scheduler._sort_meetings_now() == 3

    async def test_a_sort_failure_never_fails_the_cycle(self, monkeypatch):
        """It is cosmetic, and the cycle has already written real data."""
        from unittest.mock import AsyncMock, MagicMock
        import schedulers.reconcile_scheduler as rs
        import services.google_sheets as gs
        from config.settings import settings

        monkeypatch.setattr(settings, "MEETING_RECONCILE_ENABLED", True,
                            raising=False)
        fake = MagicMock()
        fake.meetings_layout_ok = AsyncMock(return_value=True)
        fake.get_all_meetings = AsyncMock(side_effect=RuntimeError("boom"))
        monkeypatch.setattr(gs, "sheets_service", fake)

        assert await rs.reconcile_scheduler._sort_meetings_now() == 0


class TestTheSharedTabHelpersStillWork:
    """_resort_tab and _ensure_tab gained a workbook parameter in a bulk
    rewrite that turned `settings.TASK_TRACKER_SHEET_ID` into `ssid` on BOTH
    sides of the assignment, leaving `ssid = spreadsheet_id or ssid` - an
    UnboundLocalError on every call. Every test passed, because every test
    mocks sheets_service rather than calling through it."""

    async def test_resort_tab_reads_from_the_given_first_body_row(self, monkeypatch):
        import services.google_sheets as gs

        seen = {}

        async def _read(sheet_id, range_name):
            seen["ssid"], seen["range"] = sheet_id, range_name
            return []

        monkeypatch.setattr(gs.sheets_service, "_read_sheet_range", _read)
        await gs.sheets_service._resort_tab(
            "Meetings", gs.MEETING_TRACKER_HEADERS, lambda r: 0,
            spreadsheet_id="wb-1", first_body_row=gs.MEETING_FIRST_BODY_ROW)
        assert seen["ssid"] == "wb-1"
        end = chr(ord("A") + len(gs.MEETING_TRACKER_HEADERS) - 1)
        assert seen["range"] == f"'Meetings'!A{gs.MEETING_FIRST_BODY_ROW}:{end}"

    async def test_resort_tab_falls_back_to_the_task_tracker(self, monkeypatch):
        import services.google_sheets as gs
        from config.settings import settings

        monkeypatch.setattr(settings, "TASK_TRACKER_SHEET_ID", "tracker",
                            raising=False)
        seen = {}

        async def _read(sheet_id, range_name):
            seen["ssid"] = sheet_id
            return []

        monkeypatch.setattr(gs.sheets_service, "_read_sheet_range", _read)
        await gs.sheets_service._resort_tab("Tasks", ["A", "B"], lambda r: 0)
        assert seen["ssid"] == "tracker"


class TestTheSortWritesWhereItRead:
    """_resort_tab read from `first_body_row` and wrote to a hard-coded A2, so
    on the Meetings tab — whose body starts at row 4 — it read 33 data rows and
    pasted them one row higher, straight over the column headers.

    Parameterising a read without its matching write is a half-move, and the
    half left behind is the one that writes."""

    async def _ranges(self, monkeypatch, first_body_row, n_rows):
        from unittest.mock import MagicMock
        import services.google_sheets as gs

        width = len(gs.MEETING_TRACKER_HEADERS)
        rows = [[f"t{i}"] + [""] * (width - 2) + [f"id{i}"]
                for i in range(n_rows)]

        async def _read(sheet_id, range_name):
            return list(reversed(rows))       # unsorted, so a write happens

        monkeypatch.setattr(gs.sheets_service, "_read_sheet_range", _read)
        # Mock the singleton's client, exactly as the conftest guard instructs.
        fake = MagicMock()
        monkeypatch.setattr(gs.sheets_service, "_service", fake)
        monkeypatch.setattr(gs.sheets_service, "_execute_with_retry",
                            lambda fn: fn())

        await gs.sheets_service._resort_tab(
            "Meetings", gs.MEETING_TRACKER_HEADERS, lambda r: r[0],
            spreadsheet_id="wb", first_body_row=first_body_row)
        return fake.spreadsheets.return_value.values.return_value.update.call_args

    async def test_it_writes_back_to_the_row_it_read_from(self, monkeypatch):
        import services.google_sheets as gs
        b = gs.MEETING_FIRST_BODY_ROW
        end = chr(ord("A") + len(gs.MEETING_TRACKER_HEADERS) - 1)
        call = await self._ranges(monkeypatch, b, 3)
        assert call.kwargs["range"] == f"'Meetings'!A{b}:{end}{b + 2}"

    async def test_the_default_still_starts_at_row_two(self, monkeypatch):
        """Tasks and Decisions have a one-row header and must be unaffected."""
        import services.google_sheets as gs
        end = chr(ord("A") + len(gs.MEETING_TRACKER_HEADERS) - 1)
        call = await self._ranges(monkeypatch, 2, 3)
        assert call.kwargs["range"] == f"'Meetings'!A2:{end}4"


class TestRecurringIsAStatus:
    """Eyal floated putting "recurring" in the PRIORITY list. A priority answers
    "how much does this matter" and a recurrence answers "does this need booking
    at all" — fusing them makes "is it urgent or recurring?" a question somebody
    has to answer. As a status it also falls out naturally: permanently live,
    never terminal, sorts to the top with no special case."""

    def test_it_is_a_status_not_a_priority(self):
        import services.google_sheets as gs
        assert "recurring" in gs.MEETING_STATUSES
        assert "recurring" not in [p.lower() for p in gs.MEETING_PRIORITIES]

    def test_it_sorts_to_the_top(self):
        """Eyal: "they will be fixed in the upper parts"."""
        import services.google_sheets as gs
        assert gs.MEETING_DISPLAY_ORDER["recurring"] == 0

    def test_it_is_never_terminal(self):
        """The most live state there is — a stale cell must still be able to
        move it, and the archive must never sweep it away."""
        import services.google_sheets as gs
        assert "recurring" not in gs.MEETING_TERMINAL_STATUSES

    async def test_a_recurring_meeting_can_still_be_changed(self, monkeypatch):
        sheet = [_srow(id="m1", title="Weekly sync", status="scheduled")]
        db = [_dbrow(id="m1", title="Weekly sync", status="recurring")]
        snap = {"m1": {"title": "Weekly sync", "status": "recurring"}}
        calls, _ = _setup(monkeypatch, sheet, db, snap)
        out = await ss.reconcile_meetings()
        assert out["status_guarded"] == 0


class TestEveryStatusLooksDifferent:
    """Eyal: "dropped and parked have the same color! not good!" — they both
    rendered in status_inactive grey, so a meeting deliberately set aside looked
    exactly like one abandoned. That is the single distinction the pool exists
    to show."""

    def test_every_status_has_its_own_colour(self):
        import services.google_sheets as gs
        seen = [tuple(sorted(c.items()))
                for c in gs.MEETING_STATUS_COLORS.values()]
        assert len(set(seen)) == len(gs.MEETING_STATUSES)

    def test_every_status_is_covered(self):
        import services.google_sheets as gs
        assert set(gs.MEETING_STATUS_COLORS) == set(gs.MEETING_STATUSES)

    def test_parked_and_dropped_differ(self):
        import services.google_sheets as gs
        assert (gs.MEETING_STATUS_COLORS["parked"]
                != gs.MEETING_STATUS_COLORS["dropped"])

    def test_the_rules_match_exactly_not_by_substring(self):
        """TEXT_CONTAINS made "not_scheduled" match the "scheduled" rule too,
        so which colour won depended on rule order rather than on the value."""
        import services.google_sheets as gs
        reqs = gs.sheets_service.meetings_format_requests(
            1, gs.MEETING_TRACKER_HEADERS)
        status_rules = [r["addConditionalFormatRule"]["rule"] for r in reqs
                        if "addConditionalFormatRule" in r
                        and r["addConditionalFormatRule"]["rule"]["ranges"][0]
                        ["startColumnIndex"] == gs.MEETING_COL_INDEX["status"]]
        assert len(status_rules) == len(gs.MEETING_STATUSES)
        assert all(r["booleanRule"]["condition"]["type"] == "TEXT_EQ"
                   for r in status_rules)


class TestMeetingDatesMatchTheRestOfTheWorkbook:
    """Eyal: "i want the proposed dates format to align with how the dates are
    formatted in the rest"."""

    def test_a_date_renders_day_first(self):
        import services.google_sheets as gs
        row = gs.GoogleSheetsService._meeting_row(
            {"id": "m1", "title": "T", "proposed_date": "2026-08-12"})
        assert row[gs.MEETING_COL_INDEX["proposed_date"]] == "12/08/2026"

    def test_an_empty_date_is_blank_not_none(self):
        import services.google_sheets as gs
        row = gs.GoogleSheetsService._meeting_row({"id": "m1", "title": "T"})
        assert row[gs.MEETING_COL_INDEX["proposed_date"]] == ""

    def test_an_unparseable_value_is_left_exactly_as_typed(self):
        """It used to truncate to ten characters — right for an ISO timestamp,
        and it silently mangled a human's "next Tuesday" into "next Tuesd"."""
        from services.google_sheets import _fmt_ddmmyyyy
        assert _fmt_ddmmyyyy("next Tuesday") == "next Tuesday"

    async def test_the_push_writes_the_sheet_format(self, monkeypatch):
        """It pushed the raw ISO, putting a differently-formatted date in a
        column where every other cell reads DD/MM/YYYY."""
        sheet = [_srow(id="m1", title="T", proposed_date="2026-01-01",
                       proposed_date_raw="01/01/2026")]
        db = [_dbrow(id="m1", title="T", proposed_date="2026-08-12")]
        snap = {"m1": {"title": "T", "proposed_date": "2026-01-01"}}
        calls, fake = _setup(monkeypatch, sheet, db, snap)
        await ss.reconcile_meetings()
        writes = fake.service.spreadsheets.return_value.values.return_value \
            .batchUpdate.call_args
        body = writes.kwargs["body"]["data"] if writes else []
        dates = [d["values"][0][0] for d in body
                 if d["range"].startswith("'Meetings'!D")]
        assert dates == ["12/08/2026"], dates


class TestMeetingPriority:
    """Eyal: "i think we should have priorities also for the meetings"."""

    def test_it_is_the_same_scale_as_a_task(self):
        import services.google_sheets as gs
        from services.project_status_rows import PRIORITIES
        assert tuple(gs.MEETING_PRIORITIES) == tuple(PRIORITIES)

    def test_the_sheet_spelling_maps_to_the_stored_letter(self):
        import services.google_sheets as gs
        row = gs.GoogleSheetsService._meeting_row(
            {"id": "m1", "title": "T", "priority": "U"})
        assert row[gs.MEETING_COL_INDEX["priority"]] == "Urgent"

    def test_no_priority_renders_blank_not_a_default(self):
        import services.google_sheets as gs
        row = gs.GoogleSheetsService._meeting_row({"id": "m1", "title": "T"})
        assert row[gs.MEETING_COL_INDEX["priority"]] == ""

    def test_the_dropdown_offers_the_four_levels(self):
        import services.google_sheets as gs
        reqs = gs.sheets_service.meetings_format_requests(
            1, gs.MEETING_TRACKER_HEADERS)
        dv = next(r["setDataValidation"] for r in reqs
                  if "setDataValidation" in r
                  and r["setDataValidation"]["range"]["startColumnIndex"]
                  == gs.MEETING_COL_INDEX["priority"])
        vals = [v["userEnteredValue"] for v in dv["rule"]["condition"]["values"]]
        assert vals == ["Urgent", "H", "M", "L"]

    async def test_it_is_not_merged_until_the_column_exists(self, monkeypatch):
        """The code ships before the migration runs. Merging a column the DB row
        does not have would compare every sheet value against None and pull the
        lot in as human edits."""
        sheet = [_srow(id="m1", title="T", priority="U")]
        db = [_dbrow(id="m1", title="T")]              # no priority key
        snap = {"m1": {"title": "T"}}
        calls, _ = _setup(monkeypatch, sheet, db, snap)
        await ss.reconcile_meetings()
        assert all("priority" not in u for _mid, u in calls["update"])

    async def test_it_is_merged_once_the_column_exists(self, monkeypatch):
        sheet = [_srow(id="m1", title="T", priority="U")]
        db = [_dbrow(id="m1", title="T", priority="M")]
        snap = {"m1": {"title": "T", "priority": "M"}}
        calls, _ = _setup(monkeypatch, sheet, db, snap)
        await ss.reconcile_meetings()
        assert ("m1", {"priority": "U"}) in calls["update"]


class TestDeletingARowMeansDropIt:
    """Eyal: "all that i pressed park and than deleted are either not relevant
    or duplications - so stick and align with those actions of mine". They came
    straight back on the next re-add."""

    def _plan(self, monkeypatch, snap, status="not_scheduled"):
        # A SURVIVING ROW MATTERS. An entirely empty read is a bad read, and
        # the guard that aborts on one fires before any of this — correctly.
        # A deletion always leaves the rest of the tab behind.
        sheet = [_srow(id="keep", title="Still here", row_number=4)]
        db = [_dbrow(id="keep", title="Still here"),
              _dbrow(id="m1", title="Deleted by hand", status=status)]
        snap = {"keep": {"title": "Still here"}, **snap}
        return _setup(monkeypatch, sheet, db, snap)

    async def test_a_deleted_row_is_dropped_not_re_added(self, monkeypatch):
        calls, fake = self._plan(monkeypatch, {"m1": {"title": "Deleted by hand"}})
        out = await ss.reconcile_meetings()
        assert out.get("deleted_to_dropped") == 1
        assert ("m1", {"status": "dropped"}) in calls["update"]

    async def test_a_row_never_rendered_is_still_re_added(self, monkeypatch):
        """No snapshot means it was never on the tab, so its absence is not a
        deletion."""
        calls, fake = self._plan(monkeypatch, {})
        out = await ss.reconcile_meetings()
        assert out.get("deleted_to_dropped") == 0
        fake.add_meetings_batch_to_sheet.assert_called()

    async def test_a_mass_disappearance_drops_nothing(self, monkeypatch):
        """Five meetings vanishing is tidying up; fifty is a bad read, and the
        difference has to be visible BEFORE they go."""
        sheet = [_srow(id="keep", title="Still here", row_number=4)]
        db = ([_dbrow(id="keep", title="Still here")]
              + [_dbrow(id=f"m{i}", title=f"T{i}") for i in range(12)])
        snap = {"keep": {"title": "Still here"},
                **{f"m{i}": {"title": f"T{i}"} for i in range(12)}}
        calls, _ = _setup(monkeypatch, sheet, db, snap)
        out = await ss.reconcile_meetings()
        assert out.get("deleted_to_dropped") == 0
        assert not [u for _m, u in calls["update"] if u.get("status") == "dropped"]

    async def test_an_already_terminal_meeting_is_not_re_dropped(self, monkeypatch):
        calls, _ = self._plan(monkeypatch, {"m1": {"title": "x"}}, status="held")
        out = await ss.reconcile_meetings()
        assert out.get("deleted_to_dropped") == 0


class TestTheCriticalReviewFindings:
    """2026-08-09 max-effort review. Each of these was CONFIRMED against the
    live data or reproduced with this harness before the fix."""

    async def test_a_deliberately_dropped_meeting_is_never_re_added(self, monkeypatch):
        """FINDING 2. Deleting a row set `dropped`, and `_readd_ok` then read
        that as "a recent drop, not yet aged out" and put the row straight back
        — the same resurrection loop the delete-detection was written to end,
        30 minutes later instead of immediately."""
        sheet = [_srow(id="keep", title="Still here", row_number=4)]
        db = [_dbrow(id="keep", title="Still here"),
              _dbrow(id="m1", title="Deleted on purpose", status="dropped",
                     manual_status=True)]
        snap = {"keep": {"title": "Still here"}, "m1": {"title": "x"}}
        calls, fake = _setup(monkeypatch, sheet, db, snap)

        out = await ss.reconcile_meetings()

        assert out.get("readded", 0) == 0
        fake.add_meetings_batch_to_sheet.assert_not_called()

    async def test_an_automatic_drop_still_ages_out_as_before(self, monkeypatch):
        """The permanence is for a HUMAN decision only."""
        sheet = [_srow(id="keep", title="Still here", row_number=4)]
        db = [_dbrow(id="keep", title="Still here"),
              _dbrow(id="m1", title="Auto dropped", status="dropped")]
        snap = {"keep": {"title": "Still here"}}
        calls, fake = _setup(monkeypatch, sheet, db, snap)

        await ss.reconcile_meetings()

        fake.add_meetings_batch_to_sheet.assert_called()

    async def test_the_priority_push_uses_the_sheet_spelling(self, monkeypatch):
        """FINDING 6. The cell stores 'Urgent' and the column stores 'U';
        pushing the raw letter put a value outside the dropdown into the cell,
        so the most important meeting was the only one with a red invalid-entry
        triangle and no colour."""
        sheet = [_srow(id="m1", title="T", priority="M")]
        db = [_dbrow(id="m1", title="T", priority="U")]
        snap = {"m1": {"title": "T", "priority": "M"}}
        calls, fake = _setup(monkeypatch, sheet, db, snap)

        await ss.reconcile_meetings()

        body = fake.service.spreadsheets.return_value.values.return_value \
            .batchUpdate.call_args
        written = [d["values"][0][0] for d in (body.kwargs["body"]["data"] if body else [])
                   if d["range"].startswith("'Meetings'!G")]
        assert written == ["Urgent"], written

    async def test_an_untouched_default_priority_is_not_written_to_the_sheet(
            self, monkeypatch):
        """FINDING 15. `ADD COLUMN priority DEFAULT 'M'` backfilled all 122
        meetings, so every blank cell differed from the DB and the merge stamped
        "M" across a column nobody had filled in — announcing a triage that had
        not happened."""
        sheet = [_srow(id="m1", title="T", priority="")]
        db = [_dbrow(id="m1", title="T", priority="M")]   # manual_priority False
        snap = {"m1": {"title": "T"}}
        calls, fake = _setup(monkeypatch, sheet, db, snap)

        out = await ss.reconcile_meetings()

        assert out.get("pushed", 0) == 0
        assert all("priority" not in u for _mid, u in calls["update"])

    async def test_a_real_priority_is_still_written(self, monkeypatch):
        """The suppression is for the untouched DEFAULT only — any other value
        is a decision somebody made and still reaches the cell."""
        sheet = [_srow(id="m1", title="T", priority="")]
        db = [_dbrow(id="m1", title="T", priority="H")]
        snap = {"m1": {"title": "T"}}
        calls, fake = _setup(monkeypatch, sheet, db, snap)

        out = await ss.reconcile_meetings()

        assert out.get("pushed", 0) >= 1


class TestTheSortPassesTheLayoutDoor:
    """FINDING 1, the most dangerous of the fifteen. `get_all_meetings()`
    returns [] when the header does not match, which made `pending` empty and
    the guard PASS — so the sort ran on an unrecognised layout, rewrote only the
    columns it knows about, and left the identity column where it was."""

    def _fake(self, monkeypatch, layout_ok, rows):
        from unittest.mock import AsyncMock, MagicMock
        import schedulers.reconcile_scheduler as rs
        import services.google_sheets as gs
        from config.settings import settings

        monkeypatch.setattr(settings, "MEETING_RECONCILE_ENABLED", True,
                            raising=False)
        fake = MagicMock()
        fake.meetings_layout_ok = AsyncMock(return_value=layout_ok)
        fake.get_all_meetings = AsyncMock(return_value=rows)
        fake.sort_meetings_tab = AsyncMock(return_value=7)
        monkeypatch.setattr(gs, "sheets_service", fake)
        return rs, fake

    async def test_a_failed_layout_check_stops_the_sort(self, monkeypatch):
        rs, fake = self._fake(monkeypatch, layout_ok=False, rows=[])
        assert await rs.reconcile_scheduler._sort_meetings_now() == 0
        fake.sort_meetings_tab.assert_not_called()
        fake.get_all_meetings.assert_not_called()

    async def test_an_empty_read_stops_the_sort(self, monkeypatch):
        """Belt and braces: even if the door says yes, no rows means no sort."""
        rs, fake = self._fake(monkeypatch, layout_ok=True, rows=[])
        assert await rs.reconcile_scheduler._sort_meetings_now() == 0
        fake.sort_meetings_tab.assert_not_called()

    async def test_a_healthy_tab_still_sorts(self, monkeypatch):
        rs, fake = self._fake(monkeypatch, layout_ok=True,
                              rows=[{"id": "m1", "title": "Booked"}])
        assert await rs.reconcile_scheduler._sort_meetings_now() == 7


class TestBatchThreeReviewFindings:
    """The last six of the fifteen, 2026-08-09."""

    def test_a_bracket_in_the_source_title_cannot_corrupt_it(self):
        """FINDING 14. `_PROV_RE`'s `[^\]]*` stops at the first `]`, so a chip
        built from "Weekly Sync [Italy]" could not be fully stripped: the read
        returned 'Book follow-up ]', the reconcile called that a human edit,
        wrote it to the DB and set manual_title — after which the stored title
        grew one ' ]' per cycle forever, uncorrectable."""
        import services.google_sheets as gs
        from services.project_status_rows import strip_provenance
        row = gs.GoogleSheetsService._meeting_row(
            {"id": "m1", "title": "Book follow-up"},
            source_meeting="Weekly Sync [Italy]")
        assert strip_provenance(row[0]) == "Book follow-up"
        assert row[0].count("[") == 1 and row[0].count("]") == 1

    def test_archiving_keeps_the_provenance_chip(self):
        """FINDING 8. `get_all_meetings` returns the STRIPPED title plus
        `title_displayed`, and archive_meeting_rows passes exactly those dicts —
        so every archived row lost the citation the Source Meeting column was
        removed in favour of."""
        import services.google_sheets as gs
        row = gs.GoogleSheetsService._meeting_row(
            {"id": "m1", "title": "Book follow-up",
             "title_displayed": "Book follow-up [auto · Weekly Sync]"})
        assert "[auto" in row[0] and "Weekly Sync" in row[0]

    def test_the_chip_is_still_not_stacked_twice(self):
        import services.google_sheets as gs
        once = gs.GoogleSheetsService._meeting_row(
            {"id": "m1", "title": "T"}, source_meeting="Weekly")
        twice = gs.GoogleSheetsService._meeting_row(
            {"id": "m1", "title": once[0]}, source_meeting="Weekly")
        assert twice[0].count("[auto") == 1

    async def test_a_terminal_meeting_is_never_dragged_back(self, monkeypatch):
        """FINDING 10. Held and dropped meetings live on Past Meetings now, but
        `_readd_ok` still expected them on the working tab, so it tried to pull
        ~46 archived rows back every cycle. The cap caught it every time, which
        jammed the re-add path completely — a genuinely missing LIVE meeting
        could never be restored either."""
        sheet = [_srow(id="keep", title="Still here", row_number=4)]
        db = ([_dbrow(id="keep", title="Still here")]
              + [_dbrow(id=f"h{i}", title=f"Held {i}", status="held")
                 for i in range(40)]
              + [_dbrow(id="d1", title="Dropped", status="dropped")])
        snap = {"keep": {"title": "Still here"}}
        calls, fake = _setup(monkeypatch, sheet, db, snap)
        from unittest.mock import AsyncMock
        fake.archived_meeting_ids = AsyncMock(
            return_value={f"h{i}" for i in range(40)} | {"d1"})

        out = await ss.reconcile_meetings()

        assert out.get("readded", 0) == 0
        fake.add_meetings_batch_to_sheet.assert_not_called()

    async def test_a_missing_LIVE_meeting_is_still_restored(self, monkeypatch):
        """The point of unjamming it: the re-add path has to still work."""
        sheet = [_srow(id="keep", title="Still here", row_number=4)]
        db = [_dbrow(id="keep", title="Still here"),
              _dbrow(id="live", title="Lost its row", status="not_scheduled")]
        snap = {"keep": {"title": "Still here"}}
        calls, fake = _setup(monkeypatch, sheet, db, snap)

        out = await ss.reconcile_meetings()

        assert out.get("readded") == 1

    async def test_a_terminal_meeting_on_NEITHER_tab_is_still_surfaced(
            self, monkeypatch):
        """The invariant review #3 established: a held meeting that lost its row
        must not vanish from the workspace. Skipping every terminal meeting
        outright would have reintroduced exactly that."""
        calls, fake = _setup(
            monkeypatch, [], [_dbrow(id="m9", title="Kept", status="held")], {})
        res = await ss.reconcile_meetings()
        assert res["readded"] == 1

    async def test_an_unreadable_archive_touches_nothing(self, monkeypatch):
        """If the archive cannot be read, everything would look un-archived —
        the failure direction has to be "leave it alone"."""
        from unittest.mock import AsyncMock
        sheet = [_srow(id="keep", title="Still here", row_number=4)]
        db = [_dbrow(id="keep", title="Still here"),
              _dbrow(id="h1", title="Held", status="held")]
        calls, fake = _setup(monkeypatch, sheet, db,
                             {"keep": {"title": "Still here"}})
        fake.archived_meeting_ids = AsyncMock(return_value=None)
        res = await ss.reconcile_meetings()
        assert res.get("readded", 0) == 0

    def test_a_meeting_priority_can_be_marked_sticky(self):
        """The Priority column was added to the tab and missed on the manual
        rail, so mark_meeting_field_manual REFUSED it — Rule 2 could not protect
        a priority somebody set."""
        from services.supabase_client import supabase_client as sc
        assert "priority" in sc._MEETING_MANUAL_FIELDS

    def test_the_rollout_wipe_spares_the_meetings_tabs(self):
        """FINDING 9. The wipe enumerated every sheet in the WORKBOOK, and the
        meetings pool moved in here — its ~12 colour rules were deleted and
        never re-added, because only area tabs get rules back."""
        import inspect
        import services.project_status_sheet as pss
        src = inspect.getsource(pss.write_project_status_blocks)
        assert "NON_AREA_TABS" in src
        wipe = src[src.index("Clear what a previous run"):]
        assert 'if sheet["properties"]["title"] in NON_AREA_TABS' in wipe

    def test_the_archive_moved_dates_are_read_by_shape(self):
        """FINDING 13. The read range came from the NEW headers (9 cells) while
        the loop indexed the OLD positions (9, 10), so its guard could never be
        true and the fallback restamped every historical date with today."""
        import pathlib
        src = pathlib.Path(
            "scripts/rollout_meetings_redesign_2026_08.py").read_text(
                encoding="utf-8")
        # By CODE, not by substring — the old expression is quoted in the
        # comment that explains why it was wrong, and a test that reads prose
        # is testing the prose.
        import ast
        body = ast.parse(src)
        srcs = {ast.unparse(nd) for nd in ast.walk(body)
                if isinstance(nd, ast.Compare)}
        assert not any("len(r)" in x and ">= 10" in x for x in srcs)
        assert "_UUID.match(c)" in src

    def test_the_batch_add_checks_the_layout_first(self):
        """FINDING 12. The idempotency read trusts one column to BE the
        identity; on an older layout it is blank, so every follow-up passes the
        already-present filter and blind-append duplication returns."""
        import inspect
        import services.google_sheets as gs
        src = inspect.getsource(gs.GoogleSheetsService.add_meetings_batch_to_sheet)
        assert "meetings_layout_ok()" in src
        # Before the READ that trusts the column, not merely before the word.
        assert (src.index("meetings_layout_ok()")
                < src.index("existing: set[str]"))


class TestALabelIsComparedByTheProjectItNames:
    """Third instance of one shape in a single day. [2026-08-13]

    After `not_scheduled` (the cell) and `timing_text` (the guard), this is the
    same defect again: a value with a canonical form, compared raw.

    Renaming a project keeps the old name as an ALIAS and backfills every
    reference, so the sheet said `Business Plan` while the database said
    `Business Plan updates/refinements Q3 2026` — one project, two spellings.
    The raw comparison saw a difference, `manual_label` held the sheet value
    under Rule 2, and the cell would have stayed divergent forever.
    """

    @staticmethod
    def _alias(monkeypatch, old, new):
        """resolve_label maps the retired name onto the current one."""
        monkeypatch.setattr(
            ss.supabase_client, "resolve_label",
            lambda v: new if str(v).strip() in (old, new) else v)

    async def test_an_aliased_label_is_not_a_divergence(self, monkeypatch):
        self._alias(monkeypatch, "Business Plan", "BP Q3 2026")
        sheet = [_srow(id="m1", title="T", label="Business Plan")]
        db = [_dbrow(id="m1", title="T", label="BP Q3 2026", manual_label=True)]
        snap = {"m1": {"title": "T", "label": "Business Plan",
                       "status": "to_schedule"}}
        calls, _ = _setup(monkeypatch, sheet, db, snap)

        res = await ss.reconcile_meetings()

        assert calls["update"] == [], "an alias must not be pulled as an edit"
        assert res["manual_held"] == 0, "Rule 2 should not fire — they agree"

    async def test_the_cell_settles_on_the_current_name(self, monkeypatch):
        """Agreeing is exactly what stops anything rewriting the cell, so the
        retired name would sit there forever unless the spelling is settled."""
        self._alias(monkeypatch, "Business Plan", "BP Q3 2026")
        sheet = [_srow(id="m1", title="T", label="Business Plan")]
        db = [_dbrow(id="m1", title="T", label="BP Q3 2026", manual_label=True)]
        snap = {"m1": {"title": "T", "label": "Business Plan",
                       "status": "to_schedule"}}
        calls, fake = _setup(monkeypatch, sheet, db, snap)

        res = await ss.reconcile_meetings()

        assert res["canonicalized"] >= 1
        call = fake.service.spreadsheets.return_value.values.return_value \
            .batchUpdate.call_args
        written = [w["values"][0][0] for w in call.kwargs["body"]["data"]]
        assert "BP Q3 2026" in written

    async def test_an_already_current_label_is_not_rewritten(self, monkeypatch):
        """No churn: the common case must be silent."""
        self._alias(monkeypatch, "Business Plan", "BP Q3 2026")
        sheet = [_srow(id="m1", title="T", label="BP Q3 2026")]
        db = [_dbrow(id="m1", title="T", label="BP Q3 2026")]
        snap = {"m1": {"title": "T", "label": "BP Q3 2026",
                       "status": "to_schedule"}}
        calls, _ = _setup(monkeypatch, sheet, db, snap)

        res = await ss.reconcile_meetings()

        assert res["canonicalized"] == 0
        assert calls["update"] == []

    async def test_a_genuinely_different_label_still_pulls(self, monkeypatch):
        """The fix must not swallow a real relabel — someone moving a meeting to
        another project is an edit, not an alias."""
        self._alias(monkeypatch, "Business Plan", "BP Q3 2026")
        sheet = [_srow(id="m1", title="T", label="Legal")]
        db = [_dbrow(id="m1", title="T", label="BP Q3 2026")]
        snap = {"m1": {"title": "T", "label": "BP Q3 2026",
                       "status": "to_schedule"}}
        calls, _ = _setup(monkeypatch, sheet, db, snap)

        await ss.reconcile_meetings()

        assert calls["update"] == [("m1", {"label": "Legal"})]

    def test_an_unresolvable_label_falls_back_to_plain_text(self, monkeypatch):
        """The vocabulary cannot resolve everything, and an unknown label is
        still worth comparing as text rather than collapsing to blank."""
        monkeypatch.setattr(ss.supabase_client, "resolve_label",
                            lambda v: (_ for _ in ()).throw(RuntimeError("x")))
        assert ss._same_label("Whatever") == "whatever"

    def test_a_blank_label_stays_blank(self, monkeypatch):
        assert ss._same_label("") == ""
        assert ss._same_label(None) == ""
