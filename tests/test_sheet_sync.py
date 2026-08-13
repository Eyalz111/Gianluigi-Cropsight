"""On-demand sync — the workbook stops looking broken. [2026-08-13]

Eyal: *"the thing i feel that is a bit missing is the 'cross system and cross
tabs' liveliness … if not nechama might think that it is not working."*

A thirty-minute cycle is indistinguishable from a dead one: nothing tells you
"not yet" apart from "never". Most of what is pinned here is refusal — an
unknown tab runs nothing, a burst collapses to one pass, and a cycle can never
interleave with a webhook.
"""

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

import services.sheet_sync as ss


@pytest.fixture(autouse=True)
def _clear_floor():
    ss._last_run.clear()
    yield
    ss._last_run.clear()


@pytest.fixture
def _on(monkeypatch):
    from config.settings import settings
    monkeypatch.setattr(settings, "SHEET_SYNC_ENABLED", True, raising=False)
    monkeypatch.setattr(settings, "SHEET_SYNC_MIN_INTERVAL_SECONDS", 10,
                        raising=False)
    return settings


class TestOnlyKnownTabsRunAnything:
    """Running every reconcile on every edit would turn a typo on 'How to use'
    into a full workbook pass."""

    @pytest.mark.parametrize("tab,surface", [
        ("Timeline", "timeline"),
        ("Meetings", "meetings"),
        ("Past Meetings", "meetings"),
        ("Projects", "projects"),
        ("Focus", "focus"),
        ("Focus data", "focus"),
        ("FUNDRAISING & INVESTOR RELATIONS", "project_status"),
        ("TEAM & HUMAN RESOURCES", "project_status"),
    ])
    def test_each_tab_maps_to_its_own_reconcile(self, tab, surface):
        assert ss.surface_for_tab(tab) == surface

    @pytest.mark.parametrize("tab", ["How to use", "Sheet1", "", "   ", None])
    def test_an_unrecognised_tab_maps_to_nothing(self, tab):
        assert ss.surface_for_tab(tab) is None

    async def test_an_unknown_tab_runs_no_reconcile(self, _on):
        with patch.object(ss, "_run_surface", AsyncMock()) as run:
            out = await ss.sync_surface("How to use")
        run.assert_not_called()
        assert out["ok"] is True and out["surface"] is None


class TestTheFlagAndTheFloor:
    async def test_disabled_does_nothing(self, monkeypatch):
        from config.settings import settings
        monkeypatch.setattr(settings, "SHEET_SYNC_ENABLED", False, raising=False)
        with patch.object(ss, "_run_surface", AsyncMock()) as run:
            out = await ss.sync_surface("Timeline")
        run.assert_not_called()
        assert out["ok"] is False

    async def test_a_burst_collapses_to_one_pass(self, _on):
        """onEdit fires once per cell commit, so pasting a column emits a burst.
        Without the floor each cell queues its own full reconcile and the
        workbook is locked for minutes."""
        with patch.object(ss, "_run_surface", AsyncMock(return_value={})) as run:
            outs = [await ss.sync_surface("Timeline") for _ in range(5)]
        assert run.await_count == 1
        assert sum(1 for o in outs if o.get("coalesced")) == 4

    async def test_a_coalesced_call_still_reports_success(self, _on):
        """The reconcile reads the sheet WHOLE, so the run that just happened
        already covers this edit. Reporting a failure would be a lie about the
        person's data."""
        with patch.object(ss, "_run_surface", AsyncMock(return_value={})):
            await ss.sync_surface("Timeline")
            second = await ss.sync_surface("Timeline")
        assert second["ok"] is True

    async def test_different_surfaces_do_not_block_each_other(self, _on):
        with patch.object(ss, "_run_surface", AsyncMock(return_value={})) as run:
            await ss.sync_surface("Timeline")
            await ss.sync_surface("Meetings")
        assert run.await_count == 2

    async def test_concurrent_callers_do_not_double_run(self, _on):
        """The re-check under the lock. Several callers can queue on it, and the
        one that waited must not run a second pass over work already done."""
        with patch.object(ss, "_run_surface", AsyncMock(return_value={})) as run:
            await asyncio.gather(*[ss.sync_surface("Timeline") for _ in range(6)])
        assert run.await_count == 1


class TestFailureIsReportedNotSwallowed:
    async def test_an_exception_becomes_a_message(self, _on):
        with patch.object(ss, "_run_surface",
                          AsyncMock(side_effect=RuntimeError("sheets 500"))):
            out = await ss.sync_surface("Timeline")
        assert out["ok"] is False
        assert "sheets 500" in out["error"]

    async def test_the_floor_is_still_marked_after_a_failure(self, _on):
        """Otherwise a failing surface retries on every keystroke of a paste."""
        with patch.object(ss, "_run_surface",
                          AsyncMock(side_effect=RuntimeError("x"))) as run:
            await ss.sync_surface("Timeline")
            await ss.sync_surface("Timeline")
        assert run.await_count == 1

    async def test_the_lock_is_released_after_a_failure(self, _on):
        with patch.object(ss, "_run_surface",
                          AsyncMock(side_effect=RuntimeError("x"))):
            await ss.sync_surface("Timeline")
        assert not ss.SHEET_LOCK.locked()


class TestTheCycleAndTheWebhookShareOneLock:
    """A scheduled cycle interleaving with an on-demand run would
    read-modify-write the same rows from two places, and the merge would compare
    the sheet against a half-applied state — the cross-surface defect family
    this codebase spent August eliminating."""

    def test_the_scheduler_takes_the_same_lock_object(self):
        import inspect
        import schedulers.reconcile_scheduler as rs
        src = inspect.getsource(rs.ReconcileScheduler._run)
        assert "SHEET_LOCK" in src
        assert "_run_locked" in src

    async def test_a_sync_waits_while_the_lock_is_held(self, _on):
        # The patch must outlive the task: it resumes only after the lock is
        # released, which is after the `async with` block exits.
        with patch.object(ss, "_run_surface", AsyncMock(return_value={})) as run:
            await ss.SHEET_LOCK.acquire()
            try:
                task = asyncio.create_task(ss.sync_surface("Timeline"))
                await asyncio.sleep(0)
                assert run.await_count == 0, "it ran while the cycle held the lock"
            finally:
                ss.SHEET_LOCK.release()
            await task
            assert run.await_count == 1, "it never ran after the lock was freed"


class TestTheViewsFollowARealChange:
    """Eyal: *"cross system and cross tabs liveliness."* Reconciling one surface
    updates the DATABASE in seconds, but Focus and the Timeline are projections
    of it — without this they showed the old value until the next 30-minute
    cycle, so a change was live in the system and stale on screen."""

    @pytest.fixture(autouse=True)
    def _views_on(self, monkeypatch):
        from config.settings import settings
        monkeypatch.setattr(settings, "FOCUS_VIEW_ENABLED", True, raising=False)
        monkeypatch.setattr(settings, "TIMELINE_VIEW_ENABLED", True, raising=False)

    async def test_a_real_change_rebuilds_the_other_views(self, _on):
        with patch.object(ss, "_run_surface",
                          AsyncMock(return_value={"a": {"pulled": 2}})), \
             patch.object(ss, "_refresh_views",
                          AsyncMock(return_value={"focus": {}})) as views:
            out = await ss.sync_surface("FUNDRAISING & INVESTOR RELATIONS")
        views.assert_awaited_once()
        assert out["views"] == ["focus"]

    async def test_a_no_op_sync_rebuilds_nothing(self, _on):
        """Re-rendering on every keystroke would churn the revision history and
        flicker the tab under whoever is reading it."""
        with patch.object(ss, "_run_surface",
                          AsyncMock(return_value={"a": {"pulled": 0, "pushed": 3}})), \
             patch.object(ss, "_refresh_views", AsyncMock()) as views:
            await ss.sync_surface("FUNDRAISING & INVESTOR RELATIONS")
        views.assert_not_awaited()

    @pytest.mark.parametrize("key", [
        "pulled", "created", "task_updates", "project_updates", "ticked",
        "unticked", "archived", "deleted_to_dropped"])
    def test_every_kind_of_change_counts(self, key):
        assert ss._changed({"a": {key: 1}}) is True

    def test_a_push_alone_is_not_a_change(self):
        """A push writes the DB value back INTO the sheet — the database did not
        move, so nothing downstream of it can have."""
        assert ss._changed({"a": {"pushed": 5}}) is False

    def test_a_non_dict_part_does_not_crash_it(self):
        assert ss._changed({"a": "skipped"}) is False

    async def test_a_surface_does_not_rebuild_itself(self, _on):
        """Editing the Timeline already re-rendered it; doing it twice in one
        pass would overwrite the merge that just ran."""
        with patch("services.focus_sheet.refresh_focus",
                   AsyncMock(return_value={})) as focus, \
             patch("services.timeline_sheet.refresh_timeline",
                   AsyncMock(return_value={})) as timeline:
            out = await ss._refresh_views(ss.SURFACE_TIMELINE)
        timeline.assert_not_awaited()
        focus.assert_awaited_once()
        assert "timeline" not in out

    async def test_a_failing_view_never_takes_down_the_sync(self, _on):
        """The data is already written. A view failing costs a stale tab."""
        with patch("services.focus_sheet.refresh_focus",
                   AsyncMock(side_effect=RuntimeError("sheets 500"))), \
             patch("services.timeline_sheet.refresh_timeline",
                   AsyncMock(return_value={})):
            out = await ss._refresh_views(ss.SURFACE_MEETINGS)
        assert "focus" not in out
        assert "timeline" in out

    async def test_views_run_inside_the_lock(self, _on):
        """They read the state the reconcile just settled, and a cycle starting
        mid-render would have them rendering a half-applied database."""
        held = {}

        async def _check(_exclude):
            held["locked"] = ss.SHEET_LOCK.locked()
            return {}
        with patch.object(ss, "_run_surface",
                          AsyncMock(return_value={"a": {"pulled": 1}})), \
             patch.object(ss, "_refresh_views", AsyncMock(side_effect=_check)):
            await ss.sync_surface("Meetings")
        assert held.get("locked") is True

    async def test_a_disabled_view_is_skipped(self, monkeypatch):
        from config.settings import settings
        monkeypatch.setattr(settings, "FOCUS_VIEW_ENABLED", False, raising=False)
        monkeypatch.setattr(settings, "TIMELINE_VIEW_ENABLED", False, raising=False)
        out = await ss._refresh_views(ss.SURFACE_MEETINGS)
        assert out == {}


class TestTheStatusLine:
    """Written for someone who will not read a JSON blob."""

    def test_it_says_what_changed(self):
        assert ss.summarise({"ok": True, "surface": "t",
                             "result": {"a": {"pulled": 2}}}) == \
            "synced — 2 changes saved"

    def test_one_change_is_singular(self):
        assert "1 change saved" in ss.summarise(
            {"ok": True, "surface": "t", "result": {"a": {"pulled": 1}}})

    def test_nothing_to_do_says_so_rather_than_going_quiet(self):
        """Silence is the thing that makes it look broken."""
        assert ss.summarise({"ok": True, "surface": "t",
                             "result": {"a": {"pulled": 0, "pushed": 0}}}) == \
            "synced — nothing to change"

    def test_a_failure_says_why(self):
        assert "boom" in ss.summarise({"ok": False, "error": "boom"})

    def test_a_non_dict_part_does_not_crash_it(self):
        assert ss.summarise({"ok": True, "surface": "t",
                             "result": {"a": "skipped"}}).startswith("synced")


class TestTheTimelineKeepsItsReadBeforeRenderOrder:
    async def test_the_readback_runs_before_the_render(self, monkeypatch):
        """The render rewrites every cell from the database, so refreshing first
        would overwrite what was just typed before anything looked at it."""
        from config.settings import settings
        monkeypatch.setattr(settings, "TIMELINE_READBACK_ENABLED", True, raising=False)
        monkeypatch.setattr(settings, "TIMELINE_VIEW_ENABLED", True, raising=False)
        order = []
        with patch("processors.timeline_readback.reconcile_timeline",
                   AsyncMock(side_effect=lambda *a, **k: order.append("read") or {})), \
             patch("services.timeline_sheet.refresh_timeline",
                   AsyncMock(side_effect=lambda *a, **k: order.append("render") or {})):
            await ss._run_surface(ss.SURFACE_TIMELINE)
        assert order == ["read", "render"]


class TestStructuralWorkNeverRunsFromAWebhook:
    async def test_project_status_is_called_with_no_slot(self):
        """Structural work — inserting and deleting rows — is confined to the
        quiet slots because shifting rows under someone who is typing is the one
        thing a working document must not do. A webhook fires precisely while
        they are typing."""
        seen = {}

        async def _fake(**kw):
            seen.update(kw)
            return {}
        with patch("processors.project_status_reconcile.reconcile_project_status",
                   AsyncMock(side_effect=_fake)):
            await ss._run_surface(ss.SURFACE_PROJECT_STATUS)
        assert seen.get("slot") is None


class TestEveryPushHasAKillSwitch:
    """Twice now a scheduler has sent to Eyal with no way to silence it short of
    a code change: `meeting_prep` (found 2026-08-11) and `weekly_digest` (found
    2026-08-13, whose own comment read "Digest always starts").

    A push nobody can turn off is not a feature, and "the plan named a flag" is
    not evidence the flag exists — PROJECT_LEARNING_ENABLED was referenced for
    weeks and was never in settings at all.
    """

    def test_the_weekly_digest_flag_actually_exists(self):
        from config.settings import Settings
        assert "WEEKLY_DIGEST_ENABLED" in Settings.model_fields

    def test_it_defaults_true_so_adding_it_changed_nothing(self):
        """A kill switch that silently turns something off on deploy is its own
        kind of surprise."""
        from config.settings import Settings
        assert Settings.model_fields["WEEKLY_DIGEST_ENABLED"].default is True

    def test_main_gates_the_digest_on_it(self):
        import pathlib
        src = pathlib.Path("main.py").read_text(encoding="utf-8")
        assert "settings.WEEKLY_DIGEST_ENABLED" in src

    def test_the_qa_flag_map_names_only_flags_that_exist(self):
        """The map decides whether a silent scheduler is a fault or a choice. An
        entry naming a missing flag falls through `getattr(..., True)` and
        reports a deliberately-disabled scheduler as stale, forever."""
        from config.settings import Settings
        from schedulers.qa_scheduler import _SCHEDULER_ENABLE_FLAG
        missing = sorted({f for f in _SCHEDULER_ENABLE_FLAG.values()
                          if f and f not in Settings.model_fields})
        assert not missing, f"flag map names settings that do not exist: {missing}"
