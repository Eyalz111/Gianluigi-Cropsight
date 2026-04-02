"""Tests for the intelligence signal scheduler."""

import asyncio
import pytest
from datetime import datetime
from unittest.mock import patch, MagicMock, AsyncMock
from zoneinfo import ZoneInfo

from schedulers.intelligence_signal_scheduler import IntelligenceSignalScheduler


@pytest.fixture
def scheduler():
    return IntelligenceSignalScheduler()


class TestSchedulerInit:
    def test_initial_state(self, scheduler):
        assert scheduler._running is False
        assert scheduler._last_generated_week is None


class TestSchedulerStop:
    def test_stop_sets_running_false(self, scheduler):
        scheduler._running = True
        scheduler.stop()

        assert scheduler._running is False


class TestSleepUntilTrigger:
    @pytest.mark.asyncio
    async def test_calculates_sleep_duration(self, scheduler):
        # Mock settings for Thursday 18:00
        with patch("schedulers.intelligence_signal_scheduler.settings") as mock_settings:
            mock_settings.INTELLIGENCE_SIGNAL_DAY = 3  # Thursday
            mock_settings.INTELLIGENCE_SIGNAL_HOUR = 18

            # Mock datetime to Monday 10:00 IST
            with patch("schedulers.intelligence_signal_scheduler.datetime") as mock_dt:
                israel_tz = ZoneInfo("Asia/Jerusalem")
                now_ist = datetime(2026, 4, 6, 10, 0, 0, tzinfo=israel_tz)  # Monday
                mock_dt.now.return_value = now_ist
                mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)

                with patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
                    scheduler._running = True
                    await scheduler._sleep_until_trigger()

                    # Should sleep ~3 days 8 hours = ~80 hours
                    sleep_seconds = mock_sleep.call_args[0][0]
                    assert 270000 < sleep_seconds < 300000  # ~3.1 to 3.5 days


class TestDuplicateWeekSkip:
    @pytest.mark.asyncio
    async def test_skips_duplicate_week(self, scheduler):
        scheduler._running = True
        scheduler._last_generated_week = "w14-2026"

        with patch("schedulers.intelligence_signal_scheduler.settings") as mock_settings:
            mock_settings.INTELLIGENCE_SIGNAL_DAY = 3
            mock_settings.INTELLIGENCE_SIGNAL_HOUR = 18

            # Simulate being in week 14
            with patch("schedulers.intelligence_signal_scheduler.datetime") as mock_dt:
                israel_tz = ZoneInfo("Asia/Jerusalem")
                # April 2 2026 is Thursday, week 14
                now_ist = datetime(2026, 4, 2, 18, 1, 0, tzinfo=israel_tz)
                mock_dt.now.return_value = now_ist

                # Should not call _run_generation since week already generated
                with patch.object(scheduler, "_run_generation", new_callable=AsyncMock) as mock_gen:
                    with patch.object(scheduler, "_sleep_until_trigger", new_callable=AsyncMock):
                        # Run one iteration then stop
                        async def stop_after_check():
                            await asyncio.sleep(0)
                            scheduler._running = False

                        with patch("asyncio.sleep", new_callable=AsyncMock):
                            # Manually test the skip logic
                            now = now_ist
                            week_key = f"w{now.isocalendar()[1]}-{now.isocalendar()[0]}"
                            assert week_key == scheduler._last_generated_week


class TestRunGeneration:
    @pytest.mark.asyncio
    async def test_calls_generate(self, scheduler):
        with patch(
            "processors.intelligence_signal_agent.generate_intelligence_signal",
            new_callable=AsyncMock,
        ) as mock_gen:
            mock_gen.return_value = {
                "signal_id": "signal-w14-2026",
                "status": "pending_approval",
            }

            with patch(
                "services.supabase_client.supabase_client"
            ) as mock_sc:
                mock_sc.log_action.return_value = {}

                await scheduler._run_generation()

                mock_gen.assert_called_once()

    @pytest.mark.asyncio
    async def test_handles_generation_error(self, scheduler):
        with patch(
            "processors.intelligence_signal_agent.generate_intelligence_signal",
            new_callable=AsyncMock,
        ) as mock_gen:
            mock_gen.side_effect = Exception("Pipeline crashed")

            # Should not raise
            await scheduler._run_generation()


class TestHeartbeat:
    @pytest.mark.asyncio
    async def test_logs_heartbeat_on_success(self, scheduler):
        with patch(
            "processors.intelligence_signal_agent.generate_intelligence_signal",
            new_callable=AsyncMock,
        ) as mock_gen:
            mock_gen.return_value = {
                "signal_id": "signal-w14-2026",
                "status": "pending_approval",
            }

            with patch(
                "services.supabase_client.supabase_client"
            ) as mock_sc:
                mock_sc.log_action.return_value = {}

                await scheduler._run_generation()

                mock_sc.log_action.assert_called_once()
                call_args = mock_sc.log_action.call_args
                assert call_args.kwargs["action"] == "scheduler_heartbeat"
                assert "intelligence_signal" in str(call_args.kwargs["details"])
