"""
Tests for restart-safe fire-once reconstruction (audit P4-03).

The sleep-until schedulers rebuild their in-memory "already ran this period"
guard from their last SUCCESSFUL heartbeat on boot, so a Cloud Run cycle can't
re-fire (a duplicate weekly-signal ping, a re-run of the live-sheet reconcile).
"""

import datetime as _dt
from unittest.mock import patch

import pytest


def _hb(status="ok", last_run_at="2026-06-12T05:00:00+00:00", details=None):
    return {
        "scheduler_name": "x",
        "status": status,
        "last_run_at": last_run_at,
        "details": details or {},
    }


# =============================================================================
# fire_once helpers
# =============================================================================

class TestFireOnceHelpers:
    def test_day_key_from_ok_heartbeat(self):
        from schedulers import fire_once
        with patch("services.supabase_client.supabase_client.get_scheduler_heartbeat",
                   return_value=_hb(last_run_at="2026-06-12T05:00:00+00:00")):
            # 05:00 UTC = 08:00 IST (DST) — same calendar day.
            assert fire_once.last_ok_day_key("knowledge_nightly") == "2026-06-12"

    def test_week_key_from_ok_heartbeat(self):
        from schedulers import fire_once
        with patch("services.supabase_client.supabase_client.get_scheduler_heartbeat",
                   return_value=_hb(last_run_at="2026-06-11T15:00:00+00:00")):
            iso = _dt.datetime(2026, 6, 11).isocalendar()
            assert fire_once.last_ok_week_key("intelligence_signal") == f"w{iso[1]}-{iso[0]}"

    def test_error_status_returns_none(self):
        from schedulers import fire_once
        with patch("services.supabase_client.supabase_client.get_scheduler_heartbeat",
                   return_value=_hb(status="error")):
            assert fire_once.last_ok_day_key("x") is None
            assert fire_once.last_ok_week_key("x") is None
            assert fire_once.last_ok_heartbeat("x") is None

    def test_no_heartbeat_returns_none(self):
        from schedulers import fire_once
        with patch("services.supabase_client.supabase_client.get_scheduler_heartbeat",
                   return_value=None):
            assert fire_once.last_ok_day_key("x") is None

    def test_naive_timestamp_treated_as_utc(self):
        from schedulers import fire_once
        with patch("services.supabase_client.supabase_client.get_scheduler_heartbeat",
                   return_value=_hb(last_run_at="2026-06-12T05:00:00")):  # no tz
            assert fire_once.last_ok_day_key("x") == "2026-06-12"

    def test_query_failure_returns_none(self):
        from schedulers import fire_once
        with patch("services.supabase_client.supabase_client.get_scheduler_heartbeat",
                   side_effect=RuntimeError("db down")):
            assert fire_once.last_ok_heartbeat("x") is None
            assert fire_once.last_ok_day_key("x") is None


# =============================================================================
# get_scheduler_heartbeat helper
# =============================================================================

class TestGetSchedulerHeartbeat:
    def test_returns_row_or_none(self):
        from services.supabase_client import supabase_client
        from unittest.mock import MagicMock

        chain = MagicMock()
        chain.table.return_value = chain
        chain.select.return_value = chain
        chain.eq.return_value = chain
        chain.limit.return_value = chain
        chain.execute.return_value = MagicMock(data=[{"scheduler_name": "reconcile", "status": "ok"}])
        with patch.object(supabase_client, "_client", chain):
            row = supabase_client.get_scheduler_heartbeat("reconcile")
        assert row["scheduler_name"] == "reconcile"

    def test_returns_none_on_error(self):
        from services.supabase_client import supabase_client
        from unittest.mock import MagicMock
        mock = MagicMock()
        mock.table.side_effect = RuntimeError("down")
        with patch.object(supabase_client, "_client", mock):
            assert supabase_client.get_scheduler_heartbeat("reconcile") is None


# =============================================================================
# Scheduler boot reconstruction (loop short-circuited)
# =============================================================================

class TestSchedulerBootReconstruction:
    @pytest.mark.asyncio
    async def test_knowledge_nightly_reconstructs_day_guard(self):
        from schedulers.knowledge_nightly_scheduler import KnowledgeNightlyScheduler
        sched = KnowledgeNightlyScheduler()

        async def _stop():
            sched._running = False
        sched._sleep_until_trigger = _stop

        with patch("schedulers.fire_once.last_ok_day_key", return_value="2026-06-12"):
            await sched.start()
        assert sched._last_run_date == "2026-06-12"

    @pytest.mark.asyncio
    async def test_intelligence_signal_reconstructs_week_guard(self):
        from schedulers.intelligence_signal_scheduler import IntelligenceSignalScheduler
        sched = IntelligenceSignalScheduler()

        async def _stop():
            sched._running = False
        sched._sleep_until_trigger = _stop

        with patch("schedulers.fire_once.last_ok_week_key", return_value="w24-2026"):
            await sched.start()
        assert sched._last_generated_week == "w24-2026"

    @pytest.mark.asyncio
    async def test_reconcile_reconstructs_slot_guard(self):
        from schedulers.reconcile_scheduler import ReconcileScheduler
        sched = ReconcileScheduler()

        async def _next():
            sched._running = False
            return "2026-06-12:midday"  # non-None so the loop skips the idle sleep
        sched._sleep_until_next = _next

        with patch("schedulers.fire_once.last_ok_heartbeat",
                   return_value={"status": "ok", "details": {"slot": "2026-06-12:midday"}}):
            await sched.start()
        assert sched._last_slot == "2026-06-12:midday"
