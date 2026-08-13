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
