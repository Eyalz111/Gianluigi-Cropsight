"""
Tests for Phase 5.4: Scheduler Rewrite + Compressed Timeline.

Tests cover:
- Timeline mode calculation (boundary values)
- Outline creation with meeting type classification
- Emergency mode: outline + background generation
- Auto-generate trigger after timeout
- Reminder scheduling
- Timer cancellation on Eyal response
- Startup reconstruction of prep timers
- Calendar re-verification: event deleted, rescheduled
- Skip mode for meetings too close
- Two meetings same day get separate outlines
"""

import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock, patch, PropertyMock
from datetime import datetime, timedelta, timezone


SUPABASE_PATCH = "schedulers.meeting_prep_scheduler.supabase_client"
CALENDAR_PATCH = "schedulers.meeting_prep_scheduler.calendar_service"
TELEGRAM_PATCH = "schedulers.meeting_prep_scheduler.telegram_bot"


def _make_event(event_id="evt1", title="Tech Review", hours_ahead=20):
    """Create a calendar event dict with start time hours_ahead from now."""
    start = datetime.now(timezone.utc) + timedelta(hours=hours_ahead)
    return {
        "id": event_id,
        "title": title,
        "start": start.isoformat(),
        "attendees": [
            {"displayName": "Eyal Zror", "email": "eyal@cropsight.com"},
            {"displayName": "Roye Tadmor", "email": "roye@cropsight.com"},
        ],
        "color_id": "3",
    }


# =============================================================================
# Test timeline mode (already tested in test_prep_outline.py, but verify boundaries)
# =============================================================================

class TestTimelineModeBoundaries:

    def test_exact_24(self):
        from processors.meeting_prep import calculate_timeline_mode
        assert calculate_timeline_mode(24) == "compressed"

    def test_exact_12(self):
        from processors.meeting_prep import calculate_timeline_mode
        assert calculate_timeline_mode(12) == "urgent"

    def test_exact_6(self):
        from processors.meeting_prep import calculate_timeline_mode
        assert calculate_timeline_mode(6) == "emergency"

    def test_exact_2(self):
        from processors.meeting_prep import calculate_timeline_mode
        assert calculate_timeline_mode(2) == "skip"

    def test_above_24(self):
        from processors.meeting_prep import calculate_timeline_mode
        assert calculate_timeline_mode(25) == "normal"

    def test_between_12_24(self):
        from processors.meeting_prep import calculate_timeline_mode
        assert calculate_timeline_mode(18) == "compressed"

    def test_between_6_12(self):
        from processors.meeting_prep import calculate_timeline_mode
        assert calculate_timeline_mode(9) == "urgent"

    def test_between_2_6(self):
        from processors.meeting_prep import calculate_timeline_mode
        assert calculate_timeline_mode(4) == "emergency"

    def test_below_2(self):
        from processors.meeting_prep import calculate_timeline_mode
        assert calculate_timeline_mode(1) == "skip"


# =============================================================================
# Test _create_outline_for_meeting
# =============================================================================

class TestCreateOutlineForMeeting:

    @pytest.mark.asyncio
    async def test_creates_outline_for_normal_mode(self):
        """Normal mode should create outline and schedule timers."""
        from schedulers.meeting_prep_scheduler import MeetingPrepScheduler

        scheduler = MeetingPrepScheduler.__new__(MeetingPrepScheduler)
        scheduler._prep_generated = set()
        scheduler._prep_in_progress = set()
        scheduler._reminders_sent = set()
        scheduler._pending_prep_timers = {}

        event = _make_event(hours_ahead=30)

        with patch("schedulers.meeting_prep_scheduler.classify_meeting_type") as mock_classify, \
             patch("schedulers.meeting_prep_scheduler.generate_prep_outline", new_callable=AsyncMock) as mock_outline, \
             patch("schedulers.meeting_prep_scheduler.submit_for_approval", new_callable=AsyncMock), \
             patch(SUPABASE_PATCH) as mock_db:

            mock_classify.return_value = ("founders_technical", "auto", ["title_match"])
            mock_outline.return_value = {
                "meeting_type": "founders_technical",
                "template_name": "Founders Technical Review",
                "event": event,
                "sections": [],
                "suggested_agenda": [],
                "event_start_time": event["start"],
            }
            mock_db.log_action.return_value = None
            mock_db.get_pending_approval.return_value = {"content": {}}
            mock_db.update_pending_approval.return_value = {}

            result = await scheduler._create_outline_for_meeting(event)

            assert result["status"] == "success"
            assert result["meeting_type"] == "founders_technical"
            assert result["timeline_mode"] == "normal"

        # Cleanup timers
        for tasks in scheduler._pending_prep_timers.values():
            for t in tasks:
                t.cancel()

    @pytest.mark.asyncio
    async def test_skip_mode_returns_skipped(self):
        """Meeting <2h away should be skipped."""
        from schedulers.meeting_prep_scheduler import MeetingPrepScheduler

        scheduler = MeetingPrepScheduler.__new__(MeetingPrepScheduler)
        scheduler._prep_generated = set()
        scheduler._prep_in_progress = set()
        scheduler._reminders_sent = set()
        scheduler._pending_prep_timers = {}

        event = _make_event(hours_ahead=1)

        with patch(SUPABASE_PATCH) as mock_db:
            mock_db.log_action.return_value = None
            result = await scheduler._create_outline_for_meeting(event)

        assert result["status"] == "skipped"
        assert result["reason"] == "too_late"

    @pytest.mark.asyncio
    async def test_emergency_mode_creates_quick_brief(self):
        """Emergency mode (<6h) should skip outline and generate quick brief directly."""
        from schedulers.meeting_prep_scheduler import MeetingPrepScheduler

        scheduler = MeetingPrepScheduler.__new__(MeetingPrepScheduler)
        scheduler._prep_generated = set()
        scheduler._prep_in_progress = set()
        scheduler._reminders_sent = set()
        scheduler._pending_prep_timers = {}

        event = _make_event(hours_ahead=4)

        with patch("schedulers.meeting_prep_scheduler.classify_meeting_type") as mock_classify, \
             patch("schedulers.meeting_prep_scheduler.generate_prep_outline", new_callable=AsyncMock) as mock_outline, \
             patch("schedulers.meeting_prep_scheduler.format_prep_document_v2") as mock_format, \
             patch("schedulers.meeting_prep_scheduler.submit_for_approval", new_callable=AsyncMock) as mock_submit, \
             patch(SUPABASE_PATCH) as mock_db, \
             patch("guardrails.sensitivity_classifier.classify_sensitivity") as mock_sens:

            mock_classify.return_value = ("founders_technical", "auto", ["title_match"])
            mock_outline.return_value = {
                "meeting_type": "founders_technical",
                "template_name": "Founders Technical Review",
                "event": event,
                "sections": [],
                "suggested_agenda": [],
                "event_start_time": event["start"],
            }
            mock_format.return_value = "# Prep doc content"
            mock_sens.return_value = "normal"
            mock_db.log_action.return_value = None

            result = await scheduler._create_outline_for_meeting(event)

            assert result["status"] == "success"
            assert result["timeline_mode"] == "emergency"
            # Should submit as meeting_prep (not prep_outline)
            mock_submit.assert_called_once()
            call_kwargs = mock_submit.call_args
            assert call_kwargs.kwargs.get("content_type") or call_kwargs[1].get("content_type") == "meeting_prep"
            # No background tasks (no race condition)
            assert len(scheduler._pending_prep_timers) == 0


# =============================================================================
# Test timer cancellation
# =============================================================================

class TestTimerCancellation:

    def test_cancel_prep_timers(self):
        """Should cancel all tasks for a given approval_id."""
        from schedulers.meeting_prep_scheduler import MeetingPrepScheduler

        scheduler = MeetingPrepScheduler.__new__(MeetingPrepScheduler)
        scheduler._pending_prep_timers = {}

        mock_task1 = MagicMock()
        mock_task2 = MagicMock()
        scheduler._pending_prep_timers["outline-123"] = [mock_task1, mock_task2]

        scheduler.cancel_prep_timers("outline-123")

        mock_task1.cancel.assert_called_once()
        mock_task2.cancel.assert_called_once()
        assert "outline-123" not in scheduler._pending_prep_timers

    def test_cancel_nonexistent_is_noop(self):
        """Cancelling timers for unknown ID should not raise."""
        from schedulers.meeting_prep_scheduler import MeetingPrepScheduler

        scheduler = MeetingPrepScheduler.__new__(MeetingPrepScheduler)
        scheduler._pending_prep_timers = {}

        scheduler.cancel_prep_timers("outline-nonexistent")  # Should not raise


# =============================================================================
# Test startup reconstruction
# =============================================================================

class TestReconstructPrepTimers:

    @pytest.mark.asyncio
    async def test_reconstructs_from_supabase(self):
        """Should rebuild timers for pending outlines on startup."""
        from schedulers.meeting_prep_scheduler import MeetingPrepScheduler

        scheduler = MeetingPrepScheduler.__new__(MeetingPrepScheduler)
        scheduler._prep_generated = set()
        scheduler._prep_in_progress = set()
        scheduler._pending_prep_timers = {}

        future_time = (datetime.now(timezone.utc) + timedelta(hours=20)).isoformat()

        with patch(SUPABASE_PATCH) as mock_db:
            mock_db.get_pending_prep_outlines.return_value = [{
                "approval_id": "outline-evt1",
                "content": {
                    "event_start_time": future_time,
                    "timeline_mode": "normal",
                    "outline": {"event": {"id": "evt1"}},
                },
            }]
            mock_db.get_pending_approval.return_value = {
                "content": {"event_start_time": future_time, "timeline_mode": "normal"},
            }
            mock_db.update_pending_approval.return_value = {}

            count = await scheduler.reconstruct_prep_timers()

        assert count == 1
        assert "evt1" in scheduler._prep_generated
        assert "outline-evt1" in scheduler._pending_prep_timers

        # Cleanup
        for tasks in scheduler._pending_prep_timers.values():
            for t in tasks:
                t.cancel()

    @pytest.mark.asyncio
    async def test_skips_past_meetings(self):
        """Should not reconstruct timers for meetings that have passed."""
        from schedulers.meeting_prep_scheduler import MeetingPrepScheduler

        scheduler = MeetingPrepScheduler.__new__(MeetingPrepScheduler)
        scheduler._prep_generated = set()
        scheduler._prep_in_progress = set()
        scheduler._pending_prep_timers = {}

        past_time = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()

        with patch(SUPABASE_PATCH) as mock_db:
            mock_db.get_pending_prep_outlines.return_value = [{
                "approval_id": "outline-evt-past",
                "content": {
                    "event_start_time": past_time,
                    "timeline_mode": "normal",
                    "outline": {"event": {"id": "evt-past"}},
                },
            }]

            count = await scheduler.reconstruct_prep_timers()

        assert count == 0

    @pytest.mark.asyncio
    async def test_no_outlines_returns_zero(self):
        """No pending outlines should return 0."""
        from schedulers.meeting_prep_scheduler import MeetingPrepScheduler

        scheduler = MeetingPrepScheduler.__new__(MeetingPrepScheduler)
        scheduler._prep_generated = set()
        scheduler._prep_in_progress = set()
        scheduler._pending_prep_timers = {}

        with patch(SUPABASE_PATCH) as mock_db:
            mock_db.get_pending_prep_outlines.return_value = []

            count = await scheduler.reconstruct_prep_timers()

        assert count == 0


# =============================================================================
# Test calendar re-verification
# =============================================================================

class TestReverifyPendingOutlines:

    @pytest.mark.asyncio
    async def test_event_deleted_expires_outline(self):
        """Deleted calendar event should expire the outline and notify."""
        from schedulers.meeting_prep_scheduler import MeetingPrepScheduler

        scheduler = MeetingPrepScheduler.__new__(MeetingPrepScheduler)
        scheduler._pending_prep_timers = {}

        with patch(SUPABASE_PATCH) as mock_db, \
             patch(CALENDAR_PATCH) as mock_cal, \
             patch(TELEGRAM_PATCH) as mock_tg:

            mock_db.get_pending_prep_outlines.return_value = [{
                "approval_id": "outline-evt1",
                "content": {
                    "outline": {"event": {"id": "evt1", "title": "Tech Review"}},
                    "event_start_time": "2026-03-17T10:00:00+02:00",
                },
            }]
            mock_cal.get_event = AsyncMock(return_value=None)  # Event deleted
            mock_db.update_pending_approval.return_value = {}
            mock_db.log_action.return_value = None
            mock_tg.send_to_eyal = AsyncMock(return_value=True)

            await scheduler._reverify_pending_outlines()

            mock_db.update_pending_approval.assert_called_with(
                "outline-evt1", status="expired"
            )
            mock_tg.send_to_eyal.assert_called_once()
            assert "removed from calendar" in mock_tg.send_to_eyal.call_args[0][0]

    @pytest.mark.asyncio
    async def test_event_rescheduled_updates_timeline(self):
        """Rescheduled event (>2h shift) should update timeline mode."""
        from schedulers.meeting_prep_scheduler import MeetingPrepScheduler

        scheduler = MeetingPrepScheduler.__new__(MeetingPrepScheduler)
        scheduler._pending_prep_timers = {}

        stored_start = "2026-03-17T10:00:00+00:00"
        new_start = "2026-03-17T16:00:00+00:00"  # 6h later

        with patch(SUPABASE_PATCH) as mock_db, \
             patch(CALENDAR_PATCH) as mock_cal, \
             patch(TELEGRAM_PATCH) as mock_tg:

            mock_db.get_pending_prep_outlines.return_value = [{
                "approval_id": "outline-evt1",
                "content": {
                    "outline": {"event": {"id": "evt1", "title": "Tech Review"}},
                    "event_start_time": stored_start,
                },
            }]
            mock_cal.get_event = AsyncMock(return_value={"id": "evt1", "start": new_start})
            mock_db.update_pending_approval.return_value = {}
            mock_tg.send_to_eyal = AsyncMock(return_value=True)

            await scheduler._reverify_pending_outlines()

            # Should have updated the approval content
            update_calls = mock_db.update_pending_approval.call_args_list
            assert len(update_calls) >= 1
            # Find the content update (not status update)
            content_update = None
            for call in update_calls:
                kwargs = call.kwargs if call.kwargs else {}
                if "content" in kwargs:
                    content_update = kwargs["content"]
                    break
            assert content_update is not None
            assert content_update["event_start_time"] == new_start

    @pytest.mark.asyncio
    async def test_minor_reschedule_ignored(self):
        """Small time shift (<2h) should not trigger update."""
        from schedulers.meeting_prep_scheduler import MeetingPrepScheduler

        scheduler = MeetingPrepScheduler.__new__(MeetingPrepScheduler)
        scheduler._pending_prep_timers = {}

        stored_start = "2026-03-17T10:00:00+00:00"
        new_start = "2026-03-17T10:30:00+00:00"  # Only 30min later

        with patch(SUPABASE_PATCH) as mock_db, \
             patch(CALENDAR_PATCH) as mock_cal, \
             patch(TELEGRAM_PATCH) as mock_tg:

            mock_db.get_pending_prep_outlines.return_value = [{
                "approval_id": "outline-evt1",
                "content": {
                    "outline": {"event": {"id": "evt1", "title": "Tech Review"}},
                    "event_start_time": stored_start,
                },
            }]
            mock_cal.get_event = AsyncMock(return_value={"id": "evt1", "start": new_start})
            mock_tg.send_to_eyal = AsyncMock()

            await scheduler._reverify_pending_outlines()

            # Should NOT have sent a notification
            mock_tg.send_to_eyal.assert_not_called()


# =============================================================================
# Test _check_and_generate_preps (integration-level)
# =============================================================================

class TestCheckAndGeneratePreps:

    @pytest.mark.asyncio
    async def test_two_meetings_get_separate_outlines(self):
        """Two meetings on same day should each get an outline."""
        from schedulers.meeting_prep_scheduler import MeetingPrepScheduler

        scheduler = MeetingPrepScheduler.__new__(MeetingPrepScheduler)
        scheduler._prep_generated = set()
        scheduler._prep_in_progress = set()
        scheduler._reminders_sent = set()
        scheduler._pending_prep_timers = {}
        scheduler.prep_hours_before = 24

        events = [
            _make_event(event_id="evt1", title="Tech Review", hours_ahead=20),
            _make_event(event_id="evt2", title="Business Sync", hours_ahead=22),
        ]

        with patch(CALENDAR_PATCH) as mock_cal, \
             patch(SUPABASE_PATCH) as mock_db, \
             patch("schedulers.meeting_prep_scheduler.is_cropsight_meeting", return_value=True), \
             patch("schedulers.meeting_prep_scheduler.classify_meeting_type") as mock_classify, \
             patch("schedulers.meeting_prep_scheduler.generate_prep_outline", new_callable=AsyncMock) as mock_outline, \
             patch("schedulers.meeting_prep_scheduler.submit_for_approval", new_callable=AsyncMock):

            mock_cal.get_events_needing_prep = AsyncMock(return_value=events)
            mock_db.get_pending_prep_outlines.return_value = []
            mock_db.log_action.return_value = None
            mock_db.get_pending_approval.return_value = {"content": {}}
            mock_db.update_pending_approval.return_value = {}

            mock_classify.return_value = ("founders_technical", "auto", ["title_match"])
            mock_outline.return_value = {
                "meeting_type": "founders_technical",
                "template_name": "Test",
                "event": {},
                "sections": [],
                "suggested_agenda": [],
                "event_start_time": "",
            }

            results = await scheduler._check_and_generate_preps()

        assert len(results) == 2
        assert all(r["status"] == "success" for r in results)
        assert "evt1" in scheduler._prep_generated
        assert "evt2" in scheduler._prep_generated

        # Cleanup
        for tasks in scheduler._pending_prep_timers.values():
            for t in tasks:
                t.cancel()

    @pytest.mark.asyncio
    async def test_existing_outline_skipped(self):
        """Event with existing outline should not create a duplicate."""
        from schedulers.meeting_prep_scheduler import MeetingPrepScheduler

        scheduler = MeetingPrepScheduler.__new__(MeetingPrepScheduler)
        scheduler._prep_generated = set()
        scheduler._prep_in_progress = set()
        scheduler._reminders_sent = set()
        scheduler._pending_prep_timers = {}
        scheduler.prep_hours_before = 24

        event = _make_event(event_id="evt1", hours_ahead=20)

        with patch(CALENDAR_PATCH) as mock_cal, \
             patch(SUPABASE_PATCH) as mock_db, \
             patch("schedulers.meeting_prep_scheduler.is_cropsight_meeting", return_value=True), \
             patch("schedulers.meeting_prep_scheduler.classify_meeting_type") as mock_classify:

            mock_cal.get_events_needing_prep = AsyncMock(return_value=[event])
            # Already has an outline
            mock_db.get_pending_prep_outlines.return_value = [{
                "approval_id": "outline-evt1",
                "content": {"outline": {"event": {"id": "evt1"}}},
            }]

            results = await scheduler._check_and_generate_preps()

        assert len(results) == 0
        mock_classify.assert_not_called()

    @pytest.mark.asyncio
    async def test_non_cropsight_skipped(self):
        """Non-CropSight meetings should be skipped."""
        from schedulers.meeting_prep_scheduler import MeetingPrepScheduler

        scheduler = MeetingPrepScheduler.__new__(MeetingPrepScheduler)
        scheduler._prep_generated = set()
        scheduler._prep_in_progress = set()
        scheduler._reminders_sent = set()
        scheduler._pending_prep_timers = {}
        scheduler.prep_hours_before = 24

        event = _make_event(event_id="evt1", hours_ahead=20)

        with patch(CALENDAR_PATCH) as mock_cal, \
             patch(SUPABASE_PATCH) as mock_db, \
             patch("schedulers.meeting_prep_scheduler.is_cropsight_meeting", return_value=False):

            mock_cal.get_events_needing_prep = AsyncMock(return_value=[event])
            mock_db.get_pending_prep_outlines.return_value = []

            results = await scheduler._check_and_generate_preps()

        assert len(results) == 0


# =============================================================================
# Test auto-generate and emergency background gen
# =============================================================================

class TestAutoGenerate:

    @pytest.mark.asyncio
    async def test_auto_generate_fires_when_pending(self):
        """Auto-generate should call generate_meeting_prep_from_outline."""
        from schedulers.meeting_prep_scheduler import MeetingPrepScheduler

        scheduler = MeetingPrepScheduler.__new__(MeetingPrepScheduler)
        scheduler._pending_prep_timers = {}

        with patch(SUPABASE_PATCH) as mock_db, \
             patch("schedulers.meeting_prep_scheduler.generate_meeting_prep_from_outline",
                   new_callable=AsyncMock) as mock_gen, \
             patch(TELEGRAM_PATCH) as mock_tg:

            # get_pending_approval is called to check status
            mock_db.get_pending_approval.return_value = {
                "approval_id": "outline-123",
                "status": "pending",
                "content": {
                    "outline": {"event": {"title": "Test Meeting"}},
                },
            }
            mock_gen.return_value = {"status": "success"}
            mock_db.log_action.return_value = None
            mock_tg.send_to_eyal = AsyncMock(return_value=True)

            # Call with 0 delay
            await scheduler._auto_generate_after_delay("outline-123", 0)

            mock_gen.assert_awaited_once_with("outline-123")

    @pytest.mark.asyncio
    async def test_auto_generate_skips_non_pending(self):
        """Auto-generate should not fire if outline is no longer pending."""
        from schedulers.meeting_prep_scheduler import MeetingPrepScheduler

        scheduler = MeetingPrepScheduler.__new__(MeetingPrepScheduler)

        with patch(SUPABASE_PATCH) as mock_db, \
             patch("schedulers.meeting_prep_scheduler.generate_meeting_prep_from_outline",
                   new_callable=AsyncMock) as mock_gen:

            mock_db.get_pending_approval.return_value = {
                "status": "generated",  # Already handled
                "content": {},
            }

            await scheduler._auto_generate_after_delay("outline-123", 0)

            mock_gen.assert_not_called()



# =============================================================================
# Test _hours_until helper
# =============================================================================

class TestHoursUntil:

    def test_future_event(self):
        from schedulers.meeting_prep_scheduler import MeetingPrepScheduler
        scheduler = MeetingPrepScheduler.__new__(MeetingPrepScheduler)

        future = (datetime.now(timezone.utc) + timedelta(hours=10)).isoformat()
        hours = scheduler._hours_until(future)
        assert 9.9 < hours < 10.1

    def test_past_event(self):
        from schedulers.meeting_prep_scheduler import MeetingPrepScheduler
        scheduler = MeetingPrepScheduler.__new__(MeetingPrepScheduler)

        past = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
        hours = scheduler._hours_until(past)
        assert hours == 0

    def test_empty_string(self):
        from schedulers.meeting_prep_scheduler import MeetingPrepScheduler
        scheduler = MeetingPrepScheduler.__new__(MeetingPrepScheduler)

        hours = scheduler._hours_until("")
        assert hours == 24.0


# =============================================================================
# Test startup readiness
# =============================================================================

class TestStartupReadiness:
    """Tests for scheduler waiting on Telegram bot readiness."""

    @pytest.mark.asyncio
    async def test_scheduler_waits_for_bot(self):
        """Scheduler should wait for Telegram bot readiness."""
        import asyncio
        from services.telegram_bot import TelegramBot

        bot = TelegramBot.__new__(TelegramBot)
        bot._ready = asyncio.Event()

        # Not ready yet
        assert not bot._ready.is_set()

        # Simulate bot becoming ready
        bot._ready.set()
        await bot.wait_until_ready(timeout=1)
        assert bot._ready.is_set()

    @pytest.mark.asyncio
    async def test_wait_timeout_doesnt_crash(self):
        """Timeout waiting for bot should not crash."""
        import asyncio
        from services.telegram_bot import TelegramBot

        bot = TelegramBot.__new__(TelegramBot)
        bot._ready = asyncio.Event()

        # Should timeout gracefully, not raise
        await bot.wait_until_ready(timeout=0.1)
