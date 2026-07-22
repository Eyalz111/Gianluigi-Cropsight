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
  - status is MONOTONIC — a stale cell can never un-hold a meeting
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
    calls = {"update": [], "manual": [], "snapshot": [], "create": [], "delete": []}
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


class TestMonotonicStatus:
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

    async def test_dropped_meetings_are_not_readded(self, monkeypatch):
        calls, fake = _setup(
            monkeypatch, [], [_dbrow(id="m9", title="Gone", status="dropped")], {})
        res = await ss.reconcile_meetings()
        assert res["readded"] == 0

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
