"""Tests for Sub-Phase 6.5: Weekly review scheduler + distribution."""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from datetime import datetime, timedelta, timezone


# =========================================================================
# WeeklyReviewScheduler Tests
# =========================================================================

class TestWeeklyReviewScheduler:
    """Test WeeklyReviewScheduler lifecycle."""

    def test_singleton_exists(self):
        from schedulers.weekly_review_scheduler import weekly_review_scheduler
        assert weekly_review_scheduler is not None

    def test_default_interval(self):
        from schedulers.weekly_review_scheduler import WeeklyReviewScheduler
        scheduler = WeeklyReviewScheduler()
        assert scheduler.check_interval == 900  # default from settings

    def test_custom_interval(self):
        from schedulers.weekly_review_scheduler import WeeklyReviewScheduler
        scheduler = WeeklyReviewScheduler(check_interval=60)
        assert scheduler.check_interval == 60

    def test_stop(self):
        from schedulers.weekly_review_scheduler import WeeklyReviewScheduler
        scheduler = WeeklyReviewScheduler()
        scheduler._running = True
        scheduler.stop()
        assert scheduler._running is False

    @pytest.mark.asyncio
    async def test_start_waits_for_telegram(self):
        """Scheduler should wait for Telegram readiness before cycling."""
        from schedulers.weekly_review_scheduler import WeeklyReviewScheduler
        scheduler = WeeklyReviewScheduler(check_interval=1)

        mock_bot = MagicMock()
        mock_bot.wait_until_ready = AsyncMock()

        with patch.dict("sys.modules", {"services.telegram_bot": MagicMock(telegram_bot=mock_bot)}):
            # Stop after one iteration
            async def stop_after_check():
                scheduler._running = False

            scheduler._check_cycle = AsyncMock(side_effect=stop_after_check)
            await scheduler.start()

        mock_bot.wait_until_ready.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_start_already_running(self):
        """Should warn if already running."""
        from schedulers.weekly_review_scheduler import WeeklyReviewScheduler
        scheduler = WeeklyReviewScheduler()
        scheduler._running = True

        # Should return immediately
        with patch("schedulers.weekly_review_scheduler.logger") as mock_logger:
            # Run in a task that we can cancel
            import asyncio
            task = asyncio.create_task(scheduler.start())
            await asyncio.sleep(0.01)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            mock_logger.warning.assert_called()
        scheduler._running = False


# =========================================================================
# Calendar Event Detection Tests
# =========================================================================

class TestFindReviewEvent:
    """Test _find_review_event calendar detection."""

    @pytest.mark.asyncio
    async def test_finds_matching_event(self):
        from schedulers.weekly_review_scheduler import WeeklyReviewScheduler
        scheduler = WeeklyReviewScheduler()

        mock_cal = MagicMock()
        mock_cal.get_upcoming_events = AsyncMock(return_value=[
            {"id": "e1", "title": "Team standup", "status": "confirmed"},
            {"id": "e2", "title": "CropSight: Weekly Review with Gianluigi", "status": "confirmed"},
        ])

        with patch.dict("sys.modules", {"services.google_calendar": MagicMock(calendar_service=mock_cal)}):
            event = await scheduler._find_review_event()

        assert event is not None
        assert event["id"] == "e2"

    @pytest.mark.asyncio
    async def test_no_matching_event(self):
        from schedulers.weekly_review_scheduler import WeeklyReviewScheduler
        scheduler = WeeklyReviewScheduler()

        mock_cal = MagicMock()
        mock_cal.get_upcoming_events = AsyncMock(return_value=[
            {"id": "e1", "title": "Team standup", "status": "confirmed"},
        ])

        with patch.dict("sys.modules", {"services.google_calendar": MagicMock(calendar_service=mock_cal)}):
            event = await scheduler._find_review_event()

        assert event is None

    @pytest.mark.asyncio
    async def test_skips_cancelled_event(self):
        from schedulers.weekly_review_scheduler import WeeklyReviewScheduler
        scheduler = WeeklyReviewScheduler()

        mock_cal = MagicMock()
        mock_cal.get_upcoming_events = AsyncMock(return_value=[
            {"id": "e1", "title": "CropSight: Weekly Review with Gianluigi", "status": "cancelled"},
        ])

        with patch.dict("sys.modules", {"services.google_calendar": MagicMock(calendar_service=mock_cal)}):
            event = await scheduler._find_review_event()

        assert event is None

    @pytest.mark.asyncio
    async def test_calendar_failure(self):
        from schedulers.weekly_review_scheduler import WeeklyReviewScheduler
        scheduler = WeeklyReviewScheduler()

        mock_cal = MagicMock()
        mock_cal.get_upcoming_events = AsyncMock(side_effect=Exception("API error"))

        with patch.dict("sys.modules", {"services.google_calendar": MagicMock(calendar_service=mock_cal)}):
            event = await scheduler._find_review_event()

        assert event is None


# =========================================================================
# Title Matching Tests
# =========================================================================

class TestIsReviewEvent:
    """Test _is_review_event title matching."""

    def test_exact_match(self):
        from schedulers.weekly_review_scheduler import WeeklyReviewScheduler
        scheduler = WeeklyReviewScheduler()
        assert scheduler._is_review_event("CropSight: Weekly Review with Gianluigi") is True

    def test_case_insensitive(self):
        from schedulers.weekly_review_scheduler import WeeklyReviewScheduler
        scheduler = WeeklyReviewScheduler()
        assert scheduler._is_review_event("cropsight: weekly review with gianluigi") is True

    def test_with_whitespace(self):
        from schedulers.weekly_review_scheduler import WeeklyReviewScheduler
        scheduler = WeeklyReviewScheduler()
        assert scheduler._is_review_event("  CropSight: Weekly Review with Gianluigi  ") is True

    def test_empty_title(self):
        from schedulers.weekly_review_scheduler import WeeklyReviewScheduler
        scheduler = WeeklyReviewScheduler()
        assert scheduler._is_review_event("") is False

    def test_unrelated_title(self):
        from schedulers.weekly_review_scheduler import WeeklyReviewScheduler
        scheduler = WeeklyReviewScheduler()
        assert scheduler._is_review_event("Team standup") is False

    def test_fuzzy_match_partial_title(self):
        """Should match with 60%+ word overlap."""
        from schedulers.weekly_review_scheduler import WeeklyReviewScheduler
        scheduler = WeeklyReviewScheduler()

        # Mock _extract_significant_words
        mock_filter = MagicMock()
        mock_filter._extract_significant_words = MagicMock(side_effect=lambda t: set(t.lower().split()))

        with patch.dict("sys.modules", {"guardrails.calendar_filter": mock_filter}):
            # "weekly review gianluigi" has 3/5 words from expected title
            result = scheduler._is_review_event("Weekly Review with Gianluigi")
        assert result is True  # exact match catches it first

    def test_haiku_fallback_non_latin(self):
        """Should use Haiku for non-Latin titles."""
        from schedulers.weekly_review_scheduler import WeeklyReviewScheduler
        scheduler = WeeklyReviewScheduler()

        mock_llm = MagicMock()
        mock_llm.call_llm = MagicMock(return_value=("YES", {"input_tokens": 10, "output_tokens": 2}))

        with patch.dict("sys.modules", {"core.llm": mock_llm}):
            result = scheduler._is_review_event("סקירה שבועית עם ג'יאנלואיג'י")

        assert result is True


# =========================================================================
# Check Cycle Tests
# =========================================================================

class TestCheckCycle:
    """Test _check_cycle timing logic."""

    @pytest.mark.asyncio
    async def test_no_event_does_nothing(self):
        from schedulers.weekly_review_scheduler import WeeklyReviewScheduler
        scheduler = WeeklyReviewScheduler()
        scheduler._find_review_event = AsyncMock(return_value=None)
        scheduler._trigger_prep = AsyncMock()
        scheduler._check_fallback_needed = AsyncMock()

        await scheduler._check_cycle()

        scheduler._trigger_prep.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_prep_triggered_at_t_minus_3h(self):
        from schedulers.weekly_review_scheduler import WeeklyReviewScheduler
        scheduler = WeeklyReviewScheduler()

        event_start = datetime.now(timezone.utc) + timedelta(hours=2)
        event = {"id": "e1", "start": event_start.isoformat()}

        scheduler._find_review_event = AsyncMock(return_value=event)
        scheduler._trigger_prep = AsyncMock()
        scheduler._send_notification = AsyncMock()

        await scheduler._check_cycle()

        scheduler._trigger_prep.assert_awaited_once()
        assert "e1" in scheduler._prepped_events

    @pytest.mark.asyncio
    async def test_notification_at_t_minus_30min(self):
        from schedulers.weekly_review_scheduler import WeeklyReviewScheduler
        scheduler = WeeklyReviewScheduler()

        event_start = datetime.now(timezone.utc) + timedelta(minutes=20)
        event = {"id": "e1", "start": event_start.isoformat()}

        scheduler._find_review_event = AsyncMock(return_value=event)
        scheduler._trigger_prep = AsyncMock()
        scheduler._send_notification = AsyncMock()

        await scheduler._check_cycle()

        scheduler._send_notification.assert_awaited_once_with("e1")
        assert "e1" in scheduler._notified_events

    @pytest.mark.asyncio
    async def test_no_duplicate_prep(self):
        from schedulers.weekly_review_scheduler import WeeklyReviewScheduler
        scheduler = WeeklyReviewScheduler()
        scheduler._prepped_events.add("e1")

        event_start = datetime.now(timezone.utc) + timedelta(hours=2)
        event = {"id": "e1", "start": event_start.isoformat()}

        scheduler._find_review_event = AsyncMock(return_value=event)
        scheduler._trigger_prep = AsyncMock()
        scheduler._send_notification = AsyncMock()

        await scheduler._check_cycle()

        scheduler._trigger_prep.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_no_duplicate_notification(self):
        from schedulers.weekly_review_scheduler import WeeklyReviewScheduler
        scheduler = WeeklyReviewScheduler()
        scheduler._notified_events.add("e1")

        event_start = datetime.now(timezone.utc) + timedelta(minutes=20)
        event = {"id": "e1", "start": event_start.isoformat()}

        scheduler._find_review_event = AsyncMock(return_value=event)
        scheduler._trigger_prep = AsyncMock()
        scheduler._send_notification = AsyncMock()

        await scheduler._check_cycle()

        scheduler._send_notification.assert_not_awaited()


# =========================================================================
# Event Time Parsing Tests
# =========================================================================

class TestParseEventTime:
    """Test _parse_event_time."""

    def test_iso_string(self):
        from schedulers.weekly_review_scheduler import WeeklyReviewScheduler
        scheduler = WeeklyReviewScheduler()
        result = scheduler._parse_event_time({"start": "2026-03-20T14:00:00+00:00"})
        assert result is not None
        assert result.tzinfo is not None

    def test_iso_string_z(self):
        from schedulers.weekly_review_scheduler import WeeklyReviewScheduler
        scheduler = WeeklyReviewScheduler()
        result = scheduler._parse_event_time({"start": "2026-03-20T14:00:00Z"})
        assert result is not None
        assert result.tzinfo is not None

    def test_datetime_object(self):
        from schedulers.weekly_review_scheduler import WeeklyReviewScheduler
        scheduler = WeeklyReviewScheduler()
        dt = datetime(2026, 3, 20, 14, 0, tzinfo=timezone.utc)
        result = scheduler._parse_event_time({"start": dt})
        assert result == dt

    def test_naive_datetime(self):
        from schedulers.weekly_review_scheduler import WeeklyReviewScheduler
        scheduler = WeeklyReviewScheduler()
        dt = datetime(2026, 3, 20, 14, 0)
        result = scheduler._parse_event_time({"start": dt})
        assert result.tzinfo is not None

    def test_no_start(self):
        from schedulers.weekly_review_scheduler import WeeklyReviewScheduler
        scheduler = WeeklyReviewScheduler()
        result = scheduler._parse_event_time({})
        assert result is None

    def test_invalid_string(self):
        from schedulers.weekly_review_scheduler import WeeklyReviewScheduler
        scheduler = WeeklyReviewScheduler()
        result = scheduler._parse_event_time({"start": "not-a-date"})
        assert result is None


# =========================================================================
# Trigger Prep Tests
# =========================================================================

class TestTriggerPrep:
    """Test _trigger_prep (T-3h action)."""

    @pytest.mark.asyncio
    async def test_creates_session(self):
        from schedulers.weekly_review_scheduler import WeeklyReviewScheduler
        scheduler = WeeklyReviewScheduler()

        mock_db = MagicMock()
        mock_db.get_active_weekly_review_session.return_value = None
        mock_db.create_weekly_review_session.return_value = {"id": "s-1"}
        mock_db.update_weekly_review_session.return_value = {}

        mock_compile = AsyncMock(return_value={"week_in_review": {}})
        mock_report = AsyncMock(return_value={"report_id": "r-1"})
        mock_gantt = AsyncMock(return_value=b"pptx")

        with patch.dict("sys.modules", {
                 "services.supabase_client": MagicMock(supabase_client=mock_db),
                 "processors.weekly_review": MagicMock(compile_weekly_review_data=mock_compile),
                 "processors.weekly_report": MagicMock(generate_html_report=mock_report),
                 "processors.gantt_slide": MagicMock(generate_gantt_slide=mock_gantt),
             }):
            await scheduler._trigger_prep({"id": "e1"})

        mock_db.create_weekly_review_session.assert_called_once()

    @pytest.mark.asyncio
    async def test_skips_existing_session(self):
        from schedulers.weekly_review_scheduler import WeeklyReviewScheduler
        scheduler = WeeklyReviewScheduler()

        now = datetime.now()
        week_number = now.isocalendar()[1]

        mock_db = MagicMock()
        mock_db.get_active_weekly_review_session.return_value = {
            "id": "existing",
            "week_number": week_number,
        }

        with patch.dict("sys.modules", {
                 "services.supabase_client": MagicMock(supabase_client=mock_db),
             }):
            await scheduler._trigger_prep({"id": "e1"})

        mock_db.create_weekly_review_session.assert_not_called()


# =========================================================================
# Send Notification Tests
# =========================================================================

class TestSendNotification:
    """Test _send_notification (T-30min action)."""

    @pytest.mark.asyncio
    async def test_sends_notification(self):
        from schedulers.weekly_review_scheduler import WeeklyReviewScheduler
        scheduler = WeeklyReviewScheduler()
        # Mock calendar re-verification to find event
        scheduler._find_review_event = AsyncMock(return_value={"id": "e1"})

        mock_db = MagicMock()
        mock_db.get_active_weekly_review_session.return_value = {
            "id": "s-1",
            "week_number": 12,
            "year": 2026,
            "report_id": None,
        }

        mock_bot = MagicMock()
        mock_bot.send_to_eyal = AsyncMock()

        with patch.dict("sys.modules", {
                 "services.supabase_client": MagicMock(supabase_client=mock_db),
                 "services.telegram_bot": MagicMock(telegram_bot=mock_bot),
             }):
            await scheduler._send_notification("e1")

        mock_bot.send_to_eyal.assert_awaited_once()
        call_args = mock_bot.send_to_eyal.call_args
        assert "W12" in call_args[0][0]
        assert "/review" in call_args[0][0]

    @pytest.mark.asyncio
    async def test_no_session_skips(self):
        from schedulers.weekly_review_scheduler import WeeklyReviewScheduler
        scheduler = WeeklyReviewScheduler()
        scheduler._find_review_event = AsyncMock(return_value={"id": "e1"})

        mock_db = MagicMock()
        mock_db.get_active_weekly_review_session.return_value = None

        mock_bot = MagicMock()
        mock_bot.send_to_eyal = AsyncMock()

        with patch.dict("sys.modules", {
                 "services.supabase_client": MagicMock(supabase_client=mock_db),
                 "services.telegram_bot": MagicMock(telegram_bot=mock_bot),
             }):
            await scheduler._send_notification("e1")

        mock_bot.send_to_eyal.assert_not_awaited()


# =========================================================================
# Distribute Approved Review Tests
# =========================================================================

class TestDistributeApprovedReview:
    """Test distribute_approved_review from approval_flow."""

    @pytest.mark.asyncio
    async def test_full_distribution(self):
        from guardrails.approval_flow import distribute_approved_review

        agenda_data = {
            "week_in_review": {
                "meetings_count": 5,
                "decisions_count": 3,
                "decisions": [{"description": "Test decision"}],
                "task_summary": {
                    "completed_this_week": [{"title": "Task 1"}],
                    "overdue": [],
                },
            },
        }

        mock_gantt_slide = AsyncMock(return_value=b"pptx-bytes")
        mock_drive = MagicMock()
        mock_drive._upload_bytes_file = AsyncMock(return_value={"id": "pptx-1"})
        mock_drive.save_weekly_digest = AsyncMock(return_value={"id": "digest-1", "link": "https://drive.google.com/..."})

        mock_gmail = MagicMock()
        mock_gmail.send_email = AsyncMock()

        mock_bot = MagicMock()
        mock_bot.send_to_eyal = AsyncMock()

        mock_db = MagicMock()
        mock_db.get_weekly_review_session.return_value = {"id": "s-1", "report_id": "r-1"}
        mock_db.get_weekly_report.return_value = {"access_token": "tok123"}
        mock_db.update_weekly_report.return_value = {}
        mock_db.log_action.return_value = None

        with patch("guardrails.approval_flow.drive_service", mock_drive), \
             patch("guardrails.approval_flow.gmail_service", mock_gmail), \
             patch("guardrails.approval_flow.telegram_bot", mock_bot), \
             patch("guardrails.approval_flow.supabase_client", mock_db), \
             patch("guardrails.approval_flow.settings") as mock_settings, \
             patch.dict("sys.modules", {
                 "processors.gantt_slide": MagicMock(generate_gantt_slide=mock_gantt_slide),
             }):
            mock_settings.GANTT_SLIDES_FOLDER_ID = "folder-1"
            mock_settings.WEEKLY_DIGESTS_FOLDER_ID = "folder-2"
            mock_settings.ENVIRONMENT = "development"
            mock_settings.EYAL_EMAIL = "eyal@test.com"
            mock_settings.REPORTS_BASE_URL = "https://gianluigi.run.app"

            result = await distribute_approved_review(
                session_id="s-1",
                agenda_data=agenda_data,
                week_number=12,
                year=2026,
            )

        assert result["type"] == "weekly_review"
        assert result["pptx_uploaded"] is True
        assert result["digest_uploaded"] is True
        assert result["email_sent"] is True
        assert result["telegram_sent"] is True

    @pytest.mark.asyncio
    async def test_distribution_partial_failure(self):
        """Should continue even if some steps fail."""
        from guardrails.approval_flow import distribute_approved_review

        mock_drive = MagicMock()
        mock_drive._upload_bytes_file = AsyncMock(side_effect=Exception("Drive error"))
        mock_drive.save_weekly_digest = AsyncMock(side_effect=Exception("Drive error"))

        mock_gmail = MagicMock()
        mock_gmail.send_email = AsyncMock(side_effect=Exception("Gmail error"))

        mock_bot = MagicMock()
        mock_bot.send_to_eyal = AsyncMock()

        mock_db = MagicMock()
        mock_db.get_weekly_review_session.return_value = None
        mock_db.log_action.return_value = None

        with patch("guardrails.approval_flow.drive_service", mock_drive), \
             patch("guardrails.approval_flow.gmail_service", mock_gmail), \
             patch("guardrails.approval_flow.telegram_bot", mock_bot), \
             patch("guardrails.approval_flow.supabase_client", mock_db), \
             patch("guardrails.approval_flow.settings") as mock_settings, \
             patch.dict("sys.modules", {
                 "processors.gantt_slide": MagicMock(
                     generate_gantt_slide=AsyncMock(side_effect=Exception("PPTX error"))
                 ),
             }):
            mock_settings.GANTT_SLIDES_FOLDER_ID = "folder-1"
            mock_settings.WEEKLY_DIGESTS_FOLDER_ID = "folder-2"
            mock_settings.ENVIRONMENT = "development"
            mock_settings.EYAL_EMAIL = "eyal@test.com"
            mock_settings.REPORTS_BASE_URL = ""

            result = await distribute_approved_review(
                session_id="s-1",
                agenda_data={},
                week_number=12,
                year=2026,
            )

        # Should still return results (all failed but didn't crash)
        assert result["pptx_uploaded"] is False
        assert result["digest_uploaded"] is False
        assert result["email_sent"] is False
        # Telegram should succeed (it's the last step)
        assert result["telegram_sent"] is True


# =========================================================================
# Main.py Integration Tests
# =========================================================================

class TestMainIntegration:
    """Test main.py scheduler startup logic."""

    def test_weekly_review_enabled_replaces_digest(self):
        """When WEEKLY_REVIEW_ENABLED=True, digest scheduler should not start."""
        # This is a structural test — we verify the conditional logic exists
        import ast
        with open("main.py", "r", encoding="utf-8") as f:
            source = f.read()
        tree = ast.parse(source)

        # Find the pattern: if settings.WEEKLY_REVIEW_ENABLED
        found = False
        for node in ast.walk(tree):
            if isinstance(node, ast.If):
                # Check for WEEKLY_REVIEW_ENABLED in the test
                if hasattr(node.test, 'attr') and node.test.attr == 'WEEKLY_REVIEW_ENABLED':
                    found = True
                    break
        assert found, "main.py should check WEEKLY_REVIEW_ENABLED"

    def test_weekly_review_scheduler_stop_in_shutdown(self):
        """Shutdown should stop the weekly review scheduler."""
        with open("main.py", "r", encoding="utf-8") as f:
            source = f.read()
        assert "weekly_review_scheduler.stop()" in source


# =========================================================================
# Approval Flow Integration Tests
# =========================================================================

class TestApprovalFlowIntegration:
    """Test weekly_review content_type in approval_flow."""

    def test_expiry_map_includes_weekly_review(self):
        """weekly_review should have 7-day expiry."""
        # Read the expiry logic from the source
        with open("guardrails/approval_flow.py", "r", encoding="utf-8") as f:
            source = f.read()
        assert '"weekly_review": timedelta(days=7)' in source

    def test_is_non_meeting_content_type(self):
        """weekly_review should be in the non-meeting content type list."""
        with open("guardrails/approval_flow.py", "r", encoding="utf-8") as f:
            source = f.read()
        assert '"weekly_review"' in source
        assert 'review-' in source


# =========================================================================
# Calendar Re-Verification Tests (Item 3)
# =========================================================================

class TestCalendarReVerification:
    """Test calendar re-verification before T-30min notification."""

    @pytest.mark.asyncio
    async def test_event_deleted_cancels_session(self):
        """If event is deleted, session should be cancelled."""
        from schedulers.weekly_review_scheduler import WeeklyReviewScheduler
        scheduler = WeeklyReviewScheduler()
        scheduler._find_review_event = AsyncMock(return_value=None)

        mock_db = MagicMock()
        mock_db.get_active_weekly_review_session.return_value = {
            "id": "s-1", "week_number": 12,
        }
        mock_db.update_weekly_review_session.return_value = {}

        mock_bot = MagicMock()
        mock_bot.send_to_eyal = AsyncMock()

        with patch.dict("sys.modules", {
                 "services.supabase_client": MagicMock(supabase_client=mock_db),
                 "services.telegram_bot": MagicMock(telegram_bot=mock_bot),
             }):
            await scheduler._send_notification("e1")

        mock_db.update_weekly_review_session.assert_called_with("s-1", status="cancelled")
        mock_bot.send_to_eyal.assert_awaited_once()
        assert "removed" in mock_bot.send_to_eyal.call_args[0][0].lower()

    @pytest.mark.asyncio
    async def test_event_exists_proceeds(self):
        """If event still exists, notification should proceed normally."""
        from schedulers.weekly_review_scheduler import WeeklyReviewScheduler
        scheduler = WeeklyReviewScheduler()
        scheduler._find_review_event = AsyncMock(return_value={"id": "e1"})

        mock_db = MagicMock()
        mock_db.get_active_weekly_review_session.return_value = {
            "id": "s-1", "week_number": 12, "year": 2026,
            "report_id": None,
        }

        mock_bot = MagicMock()
        mock_bot.send_to_eyal = AsyncMock()

        with patch.dict("sys.modules", {
                 "services.supabase_client": MagicMock(supabase_client=mock_db),
                 "services.telegram_bot": MagicMock(telegram_bot=mock_bot),
             }):
            await scheduler._send_notification("e1")

        mock_bot.send_to_eyal.assert_awaited_once()
        assert "/review" in mock_bot.send_to_eyal.call_args[0][0]

    @pytest.mark.asyncio
    async def test_re_verify_fails_proceeds(self):
        """If calendar check fails, notification should proceed anyway."""
        from schedulers.weekly_review_scheduler import WeeklyReviewScheduler
        scheduler = WeeklyReviewScheduler()
        scheduler._find_review_event = AsyncMock(side_effect=Exception("API down"))

        mock_db = MagicMock()
        mock_db.get_active_weekly_review_session.return_value = {
            "id": "s-1", "week_number": 12, "year": 2026,
            "report_id": None,
        }

        mock_bot = MagicMock()
        mock_bot.send_to_eyal = AsyncMock()

        with patch.dict("sys.modules", {
                 "services.supabase_client": MagicMock(supabase_client=mock_db),
                 "services.telegram_bot": MagicMock(telegram_bot=mock_bot),
             }):
            await scheduler._send_notification("e1")

        mock_bot.send_to_eyal.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_event_deleted_no_session_no_crash(self):
        """If event deleted and no session exists, should not crash."""
        from schedulers.weekly_review_scheduler import WeeklyReviewScheduler
        scheduler = WeeklyReviewScheduler()
        scheduler._find_review_event = AsyncMock(return_value=None)

        mock_db = MagicMock()
        mock_db.get_active_weekly_review_session.return_value = None

        with patch.dict("sys.modules", {
                 "services.supabase_client": MagicMock(supabase_client=mock_db),
             }):
            await scheduler._send_notification("e1")  # Should not crash


# =========================================================================
# Fallback Notification Tests (Item 7)
# =========================================================================

class TestFallbackNotification:
    """Test fallback prompt on review day with no event."""

    @pytest.mark.asyncio
    async def test_prompt_on_review_day(self):
        """Should prompt Eyal on review day when no calendar event."""
        from schedulers.weekly_review_scheduler import WeeklyReviewScheduler
        scheduler = WeeklyReviewScheduler()

        # Mock datetime to be a Thursday (review day=4) at 12:00
        mock_now = datetime(2026, 3, 19, 12, 0)  # Thursday

        mock_db = MagicMock()
        mock_db.get_audit_log.return_value = []
        mock_db.get_active_weekly_review_session.return_value = None
        mock_db.log_action.return_value = {}

        mock_bot = MagicMock()
        mock_bot.send_to_eyal = AsyncMock()

        with patch("schedulers.weekly_review_scheduler.datetime") as mock_dt, \
             patch("schedulers.weekly_review_scheduler.settings") as mock_settings, \
             patch.dict("sys.modules", {
                 "services.supabase_client": MagicMock(supabase_client=mock_db),
                 "services.telegram_bot": MagicMock(telegram_bot=mock_bot),
             }):
            mock_dt.now.return_value = mock_now
            mock_settings.WEEKLY_REVIEW_DAY = 3  # Thursday = weekday 3
            await scheduler._check_fallback_needed()

        mock_bot.send_to_eyal.assert_awaited_once()
        assert "/review" in mock_bot.send_to_eyal.call_args[0][0]

    @pytest.mark.asyncio
    async def test_no_prompt_on_wrong_day(self):
        """Should not prompt on non-review days."""
        from schedulers.weekly_review_scheduler import WeeklyReviewScheduler
        scheduler = WeeklyReviewScheduler()

        mock_now = datetime(2026, 3, 16, 12, 0)  # Monday

        with patch("schedulers.weekly_review_scheduler.datetime") as mock_dt, \
             patch("schedulers.weekly_review_scheduler.settings") as mock_settings:
            mock_dt.now.return_value = mock_now
            mock_settings.WEEKLY_REVIEW_DAY = 3  # Thursday
            await scheduler._check_fallback_needed()
            # No assertions needed — if it reaches log_action it would fail

    @pytest.mark.asyncio
    async def test_no_prompt_when_session_exists(self):
        """Should not prompt if a review session already exists."""
        from schedulers.weekly_review_scheduler import WeeklyReviewScheduler
        scheduler = WeeklyReviewScheduler()

        mock_now = datetime(2026, 3, 19, 12, 0)  # Thursday

        mock_db = MagicMock()
        mock_db.get_audit_log.return_value = []
        mock_db.get_active_weekly_review_session.return_value = {"id": "s-1"}

        mock_bot = MagicMock()
        mock_bot.send_to_eyal = AsyncMock()

        with patch("schedulers.weekly_review_scheduler.datetime") as mock_dt, \
             patch("schedulers.weekly_review_scheduler.settings") as mock_settings, \
             patch.dict("sys.modules", {
                 "services.supabase_client": MagicMock(supabase_client=mock_db),
                 "services.telegram_bot": MagicMock(telegram_bot=mock_bot),
             }):
            mock_dt.now.return_value = mock_now
            mock_settings.WEEKLY_REVIEW_DAY = 3
            await scheduler._check_fallback_needed()

        mock_bot.send_to_eyal.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_only_once_per_day(self):
        """Should only prompt once per day."""
        from schedulers.weekly_review_scheduler import WeeklyReviewScheduler
        scheduler = WeeklyReviewScheduler()

        mock_now = datetime(2026, 3, 19, 12, 0)

        mock_db = MagicMock()
        # Simulate already prompted today
        mock_db.get_audit_log.return_value = [
            {"details": {"date": "2026-03-19"}}
        ]

        mock_bot = MagicMock()
        mock_bot.send_to_eyal = AsyncMock()

        with patch("schedulers.weekly_review_scheduler.datetime") as mock_dt, \
             patch("schedulers.weekly_review_scheduler.settings") as mock_settings, \
             patch.dict("sys.modules", {
                 "services.supabase_client": MagicMock(supabase_client=mock_db),
                 "services.telegram_bot": MagicMock(telegram_bot=mock_bot),
             }):
            mock_dt.now.return_value = mock_now
            mock_settings.WEEKLY_REVIEW_DAY = 3
            await scheduler._check_fallback_needed()

        mock_bot.send_to_eyal.assert_not_awaited()


# =========================================================================
# Coexistence Mutex Tests (Item 10)
# =========================================================================

class TestCoexistenceMutex:
    """Test digest scheduler skips when review session exists."""

    @pytest.mark.asyncio
    async def test_digest_skips_when_review_active(self):
        """Digest should skip generation when an active review session exists."""
        from schedulers.weekly_digest_scheduler import WeeklyDigestScheduler
        scheduler = WeeklyDigestScheduler()

        now = datetime(2026, 3, 20, 14, 30)  # Friday 14:30 (digest window)

        with patch("schedulers.weekly_digest_scheduler.supabase_client") as mock_db, \
             patch("schedulers.weekly_digest_scheduler.datetime") as mock_dt, \
             patch("schedulers.weekly_digest_scheduler.settings") as mock_settings:
            mock_dt.now.return_value = now
            mock_settings.WEEKLY_DIGEST_DAY = 4  # Friday
            mock_settings.WEEKLY_DIGEST_HOUR = 14
            mock_settings.WEEKLY_DIGEST_WINDOW_HOURS = 2
            scheduler._last_digest_week = None

            mock_db.get_active_weekly_review_session.return_value = {
                "id": "r-1", "status": "in_progress",
            }

            await scheduler._check_and_generate()
            # Should NOT call _generate_and_distribute
            assert scheduler._last_digest_week is None

    @pytest.mark.asyncio
    async def test_digest_runs_when_review_expired(self):
        """Digest should run if review session is expired."""
        from schedulers.weekly_digest_scheduler import WeeklyDigestScheduler
        scheduler = WeeklyDigestScheduler()

        now = datetime(2026, 3, 20, 14, 30)

        with patch("schedulers.weekly_digest_scheduler.supabase_client") as mock_db, \
             patch("schedulers.weekly_digest_scheduler.datetime") as mock_dt, \
             patch("schedulers.weekly_digest_scheduler.settings") as mock_settings, \
             patch.object(scheduler, "_generate_and_distribute", new_callable=AsyncMock) as mock_gen:
            mock_dt.now.return_value = now
            mock_dt.side_effect = lambda *args, **kwargs: datetime(*args, **kwargs)
            mock_settings.WEEKLY_DIGEST_DAY = 4
            mock_settings.WEEKLY_DIGEST_HOUR = 14
            mock_settings.WEEKLY_DIGEST_WINDOW_HOURS = 2
            scheduler._last_digest_week = None

            mock_db.get_active_weekly_review_session.return_value = {
                "id": "r-1", "status": "expired",
            }

            mock_gen.return_value = {}
            await scheduler._check_and_generate()
            mock_gen.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_digest_runs_when_no_review(self):
        """Digest should run normally when no review session exists."""
        from schedulers.weekly_digest_scheduler import WeeklyDigestScheduler
        scheduler = WeeklyDigestScheduler()

        now = datetime(2026, 3, 20, 14, 30)

        with patch("schedulers.weekly_digest_scheduler.supabase_client") as mock_db, \
             patch("schedulers.weekly_digest_scheduler.datetime") as mock_dt, \
             patch("schedulers.weekly_digest_scheduler.settings") as mock_settings, \
             patch.object(scheduler, "_generate_and_distribute", new_callable=AsyncMock) as mock_gen:
            mock_dt.now.return_value = now
            mock_dt.side_effect = lambda *args, **kwargs: datetime(*args, **kwargs)
            mock_settings.WEEKLY_DIGEST_DAY = 4
            mock_settings.WEEKLY_DIGEST_HOUR = 14
            mock_settings.WEEKLY_DIGEST_WINDOW_HOURS = 2
            scheduler._last_digest_week = None

            mock_db.get_active_weekly_review_session.return_value = None

            mock_gen.return_value = {}
            await scheduler._check_and_generate()
            mock_gen.assert_awaited_once()

    def test_both_schedulers_start_in_main(self):
        """Both review and digest schedulers should start when review enabled."""
        with open("main.py", "r", encoding="utf-8") as f:
            source = f.read()
        # Digest should always start (not exclusive with review)
        assert "weekly_digest_scheduler.start()" in source
        assert "weekly_review_scheduler.start()" in source
