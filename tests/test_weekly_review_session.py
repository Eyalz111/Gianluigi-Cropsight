"""Tests for Sub-Phase 6.2: Interactive weekly review session."""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from datetime import datetime, timedelta


# =========================================================================
# Session Lifecycle Tests
# =========================================================================

class TestStartWeeklyReview:
    """Test start_weekly_review."""

    @pytest.mark.asyncio
    async def test_start_new_session(self):
        mock_compile = AsyncMock(return_value={"week_in_review": {"meetings_count": 2}})
        mock_weekly_review_module = MagicMock()
        mock_weekly_review_module.compile_weekly_review_data = mock_compile

        with patch("processors.weekly_review_session.supabase_client") as mock_db, \
             patch.dict("sys.modules", {"processors.weekly_review": mock_weekly_review_module}):

            mock_db.get_active_weekly_review_session.return_value = None
            mock_db.create_weekly_review_session.return_value = {
                "id": "session-1", "week_number": 12, "year": 2026
            }
            mock_db.get_weekly_review_session.return_value = {
                "id": "session-1", "agenda_data": {"week_in_review": {"meetings_count": 2}},
                "week_number": 12,
            }
            mock_db.update_weekly_review_session.return_value = {}

            from processors.weekly_review_session import start_weekly_review
            result = await start_weekly_review(user_id="eyal")

            assert result.get("session_id") == "session-1"
            assert result.get("current_part") == 1

    @pytest.mark.asyncio
    async def test_resume_same_week(self):
        now = datetime.now()
        week_number = now.isocalendar()[1]
        year = now.isocalendar()[0]

        with patch("processors.weekly_review_session.supabase_client") as mock_db:
            mock_db.get_active_weekly_review_session.return_value = {
                "id": "existing-1",
                "week_number": week_number,
                "year": year,
                "current_part": 2,
                "status": "in_progress",
                "agenda_data": {},
                "created_at": datetime.utcnow().isoformat(),
            }
            mock_db.get_weekly_review_session.return_value = {
                "id": "existing-1",
                "week_number": week_number,
                "agenda_data": {},
            }
            mock_db.update_weekly_review_session.return_value = {}

            from processors.weekly_review_session import start_weekly_review
            result = await start_weekly_review(user_id="eyal")

            assert result["session_id"] == "existing-1"
            assert "review_resumed" in result.get("action", "")

    @pytest.mark.asyncio
    async def test_expire_stale_session(self):
        """Session older than 48h should be expired and replaced."""
        mock_compile = AsyncMock(return_value={"week_in_review": {}})
        mock_weekly_review_module = MagicMock()
        mock_weekly_review_module.compile_weekly_review_data = mock_compile

        old_time = (datetime.utcnow() - timedelta(hours=72)).isoformat()

        with patch("processors.weekly_review_session.supabase_client") as mock_db, \
             patch.dict("sys.modules", {"processors.weekly_review": mock_weekly_review_module}):

            mock_db.get_active_weekly_review_session.return_value = {
                "id": "old-1",
                "week_number": 1,  # Different week
                "year": 2025,
                "status": "in_progress",
                "created_at": old_time,
            }
            mock_db.create_weekly_review_session.return_value = {
                "id": "new-1", "week_number": 12, "year": 2026
            }
            mock_db.get_weekly_review_session.return_value = {
                "id": "new-1", "agenda_data": {}, "week_number": 12,
            }
            mock_db.update_weekly_review_session.return_value = {}

            from processors.weekly_review_session import start_weekly_review
            result = await start_weekly_review(user_id="eyal")

            # Should have expired the old session
            mock_db.update_weekly_review_session.assert_any_call(
                "old-1", status="expired"
            )


# =========================================================================
# Navigation Tests
# =========================================================================

class TestNavigation:
    """Test 3-part navigation."""

    def test_detect_next(self):
        from processors.weekly_review_session import _detect_navigation
        assert _detect_navigation("next") == "next"
        assert _detect_navigation("continue") == "next"
        assert _detect_navigation(">>") == "next"

    def test_detect_back(self):
        from processors.weekly_review_session import _detect_navigation
        assert _detect_navigation("back") == "back"
        assert _detect_navigation("go back") == "back"
        assert _detect_navigation("<<") == "back"

    def test_detect_end(self):
        from processors.weekly_review_session import _detect_navigation
        assert _detect_navigation("end") == "end"
        assert _detect_navigation("end review") == "end"
        assert _detect_navigation("done") == "end"

    def test_long_message_not_navigation(self):
        from processors.weekly_review_session import _detect_navigation
        assert _detect_navigation("What happened with the next milestone?") is None

    def test_question_not_navigation(self):
        from processors.weekly_review_session import _detect_navigation
        assert _detect_navigation("Tell me more about it") is None


class TestAdvanceToPart:
    """Test advance_to_part."""

    @pytest.mark.asyncio
    async def test_advance_to_part1(self):
        with patch("processors.weekly_review_session.supabase_client") as mock_db:
            mock_db.get_weekly_review_session.return_value = {
                "id": "s-1",
                "agenda_data": {"week_in_review": {"meetings_count": 3}},
                "week_number": 12,
            }
            mock_db.update_weekly_review_session.return_value = {}

            from processors.weekly_review_session import advance_to_part
            result = await advance_to_part("s-1", 1)
            assert "Part 1" in result["response"]
            assert result["current_part"] == 1

    @pytest.mark.asyncio
    async def test_advance_to_part2(self):
        with patch("processors.weekly_review_session.supabase_client") as mock_db:
            mock_db.get_weekly_review_session.return_value = {
                "id": "s-1",
                "agenda_data": {"gantt_proposals": {"proposals": [], "count": 0}},
                "week_number": 12,
            }
            mock_db.update_weekly_review_session.return_value = {}

            from processors.weekly_review_session import advance_to_part
            result = await advance_to_part("s-1", 2)
            assert "Part 2" in result["response"]
            assert result["current_part"] == 2

    @pytest.mark.asyncio
    async def test_advance_to_part3(self):
        with patch("processors.weekly_review_session.supabase_client") as mock_db:
            mock_db.get_weekly_review_session.return_value = {
                "id": "s-1",
                "agenda_data": {},
                "week_number": 12,
            }
            mock_db.update_weekly_review_session.return_value = {}

            from processors.weekly_review_session import advance_to_part
            result = await advance_to_part("s-1", 3)
            assert "Part 3" in result["response"]


# =========================================================================
# Process Message Tests
# =========================================================================

class TestProcessReviewMessage:
    """Test process_review_message."""

    @pytest.mark.asyncio
    async def test_navigation_next(self):
        with patch("processors.weekly_review_session.supabase_client") as mock_db:
            mock_db.get_weekly_review_session.return_value = {
                "id": "s-1",
                "created_at": datetime.utcnow().isoformat(),
                "current_part": 1,
                "raw_messages": [],
                "agenda_data": {"gantt_proposals": {"proposals": []}},
                "week_number": 12,
            }
            mock_db.update_weekly_review_session.return_value = {}

            from processors.weekly_review_session import process_review_message
            result = await process_review_message("s-1", "next", "eyal")
            assert result["current_part"] == 2

    @pytest.mark.asyncio
    async def test_ttl_expiry(self):
        with patch("processors.weekly_review_session.supabase_client") as mock_db:
            old_time = (datetime.utcnow() - timedelta(hours=49)).isoformat()
            mock_db.get_weekly_review_session.return_value = {
                "id": "s-1",
                "created_at": old_time,
                "current_part": 1,
                "raw_messages": [],
                "agenda_data": {},
            }
            mock_db.update_weekly_review_session.return_value = {}

            from processors.weekly_review_session import process_review_message
            result = await process_review_message("s-1", "hello", "eyal")
            assert result["action"] == "session_expired"

    @pytest.mark.asyncio
    async def test_session_not_found(self):
        with patch("processors.weekly_review_session.supabase_client") as mock_db:
            mock_db.get_weekly_review_session.return_value = None

            from processors.weekly_review_session import process_review_message
            result = await process_review_message("nonexistent", "hello", "eyal")
            assert result["action"] == "error"


# =========================================================================
# Finalize Tests
# =========================================================================

class TestFinalizeReview:
    """Test finalize_review."""

    @pytest.mark.asyncio
    async def test_finalize_generates_outputs(self):
        mock_html = AsyncMock(return_value={"report_url": "https://example.com/report/abc"})
        mock_pptx = AsyncMock(return_value=b"fake-pptx-bytes")

        mock_report_module = MagicMock()
        mock_report_module.generate_html_report = mock_html
        mock_slide_module = MagicMock()
        mock_slide_module.generate_gantt_slide = mock_pptx

        with patch("processors.weekly_review_session.supabase_client") as mock_db, \
             patch.dict("sys.modules", {
                 "processors.weekly_report": mock_report_module,
                 "processors.gantt_slide": mock_slide_module,
             }):

            mock_db.get_weekly_review_session.return_value = {
                "id": "s-1",
                "status": "in_progress",
                "agenda_data": {},
                "week_number": 12,
                "year": 2026,
            }
            mock_db.update_weekly_review_session.return_value = {}

            from processors.weekly_review_session import finalize_review
            result = await finalize_review("s-1")
            assert result["action"] == "review_finalize"
            assert "outputs" in result

    @pytest.mark.asyncio
    async def test_double_finalize_guard(self):
        with patch("processors.weekly_review_session.supabase_client") as mock_db:
            mock_db.get_weekly_review_session.return_value = {
                "id": "s-1", "status": "confirming",
            }

            from processors.weekly_review_session import finalize_review
            result = await finalize_review("s-1")
            assert "already" in result["response"].lower()


# =========================================================================
# Confirm Tests
# =========================================================================

class TestConfirmReview:
    """Test confirm_review."""

    @pytest.mark.asyncio
    async def test_approve(self):
        mock_dist = AsyncMock(return_value={"drive_uploaded": True})
        mock_approval_module = MagicMock()
        mock_approval_module.distribute_approved_review = mock_dist

        with patch("processors.weekly_review_session.supabase_client") as mock_db, \
             patch.dict("sys.modules", {"guardrails.approval_flow": mock_approval_module}):

            mock_db.get_weekly_review_session.return_value = {
                "id": "s-1",
                "status": "confirming",
                "agenda_data": {},
                "week_number": 12,
                "year": 2026,
                "gantt_proposals": [],
            }
            mock_db.client.table.return_value.update.return_value.eq.return_value.eq.return_value.execute.return_value = MagicMock(
                data=[{"id": "s-1"}]
            )
            mock_db.update_weekly_review_session.return_value = {}

            from processors.weekly_review_session import confirm_review
            result = await confirm_review("s-1", approved=True)
            assert result["action"] == "review_approved"

    @pytest.mark.asyncio
    async def test_reject(self):
        with patch("processors.weekly_review_session.supabase_client") as mock_db:
            mock_db.get_weekly_review_session.return_value = {
                "id": "s-1", "status": "confirming",
            }
            mock_db.update_weekly_review_session.return_value = {}

            from processors.weekly_review_session import confirm_review
            result = await confirm_review("s-1", approved=False)
            assert result["action"] == "review_cancelled"

    @pytest.mark.asyncio
    async def test_double_approve_guard(self):
        with patch("processors.weekly_review_session.supabase_client") as mock_db:
            mock_db.get_weekly_review_session.return_value = {
                "id": "s-1", "status": "approved",
            }

            from processors.weekly_review_session import confirm_review
            result = await confirm_review("s-1", approved=True)
            assert "already" in result["response"].lower()


# =========================================================================
# Correction Tests
# =========================================================================

class TestCorrections:
    """Test process_correction."""

    @pytest.mark.asyncio
    async def test_correction_applied(self):
        with patch("processors.weekly_review_session.supabase_client") as mock_db, \
             patch("processors.weekly_review_session.call_llm") as mock_llm:

            mock_db.get_weekly_review_session.return_value = {
                "id": "s-1",
                "corrections": [],
            }
            mock_db.update_weekly_review_session.return_value = {}
            mock_llm.return_value = (
                '{"corrections": [{"target": "html", "instruction": "Fix title"}], "response_text": "Fixed."}',
                {},
            )

            from processors.weekly_review_session import process_correction
            result = await process_correction("s-1", "Fix the title", "eyal")
            assert result["action"] == "correction_applied"

    @pytest.mark.asyncio
    async def test_max_corrections_cap(self):
        with patch("processors.weekly_review_session.supabase_client") as mock_db, \
             patch("processors.weekly_review_session.settings") as mock_settings:

            mock_settings.WEEKLY_REVIEW_MAX_CORRECTIONS = 2
            mock_db.get_weekly_review_session.return_value = {
                "id": "s-1",
                "corrections": [{"target": "html"}, {"target": "pptx"}],
            }

            from processors.weekly_review_session import process_correction
            result = await process_correction("s-1", "Another fix", "eyal")
            assert result["action"] == "max_corrections"


# =========================================================================
# Session Stack Tests
# =========================================================================

class TestSessionStack:
    """Test Telegram bot session stack."""

    def test_backward_compat_property_get(self):
        from services.telegram_bot import TelegramBot
        bot = TelegramBot()
        assert bot._active_interactive_session is None

    def test_backward_compat_property_set(self):
        from services.telegram_bot import TelegramBot
        bot = TelegramBot()
        bot._active_interactive_session = "debrief"
        assert bot._active_interactive_session == "debrief"
        assert bot._session_stack == ["debrief"]

    def test_backward_compat_property_clear(self):
        from services.telegram_bot import TelegramBot
        bot = TelegramBot()
        bot._active_interactive_session = "debrief"
        bot._active_interactive_session = None
        assert bot._active_interactive_session is None
        assert bot._session_stack == []

    def test_stack_push_pop(self):
        from services.telegram_bot import TelegramBot
        bot = TelegramBot()
        bot._session_stack.append("weekly_review")
        bot._session_stack.append("debrief")
        assert bot._active_interactive_session == "debrief"
        bot._session_stack.pop()
        assert bot._active_interactive_session == "weekly_review"

    def test_debrief_interrupts_review(self):
        """Debrief should be able to push onto stack during review."""
        from services.telegram_bot import TelegramBot
        bot = TelegramBot()
        bot._session_stack.append("weekly_review")
        bot._session_stack.append("debrief")
        assert bot._active_interactive_session == "debrief"
        # Pop debrief
        bot._session_stack.pop()
        assert bot._active_interactive_session == "weekly_review"


# =========================================================================
# Formatting Tests
# =========================================================================

class TestFormatting:
    """Test HTML formatting in parts."""

    def test_part1_uses_html(self):
        from processors.weekly_review_session import _format_part1
        result = _format_part1({"week_in_review": {"meetings_count": 3}}, 12)
        assert "<b>" in result
        assert "Part 1" in result

    def test_part2_uses_html(self):
        from processors.weekly_review_session import _format_part2
        result = _format_part2({"gantt_proposals": {"proposals": [], "count": 0}}, 12)
        assert "<b>" in result
        assert "Part 2" in result

    def test_part3_uses_html(self):
        from processors.weekly_review_session import _format_part3
        result = _format_part3({}, 12)
        assert "<b>" in result
        assert "Part 3" in result

    def test_part1_truncates_long_output(self):
        from processors.weekly_review_session import _format_part1
        # Create data that would produce a very long output
        agenda = {
            "week_in_review": {"meetings_count": 100},
            "attention_needed": {
                "stale_tasks": [{"title": "x" * 100, "assignee": "Eyal"} for _ in range(100)],
                "alerts": [],
            },
        }
        result = _format_part1(agenda, 12)
        assert len(result) <= 4000

    def test_escape_html(self):
        from processors.weekly_review_session import _escape_html
        assert _escape_html("a < b & c > d") == "a &lt; b &amp; c &gt; d"


# =========================================================================
# TTL Tests
# =========================================================================

class TestSessionTTL:
    """Test session expiry."""

    def test_not_expired(self):
        from processors.weekly_review_session import _is_session_expired
        recent = datetime.utcnow().isoformat()
        assert _is_session_expired(recent) is False

    def test_expired(self):
        from processors.weekly_review_session import _is_session_expired
        old = (datetime.utcnow() - timedelta(hours=49)).isoformat()
        assert _is_session_expired(old) is True

    def test_empty_string(self):
        from processors.weekly_review_session import _is_session_expired
        assert _is_session_expired("") is False


# =========================================================================
# Resume After Debrief Tests
# =========================================================================

class TestResumeAfterDebrief:
    """Test resume_after_debrief."""

    @pytest.mark.asyncio
    async def test_resume_refreshes_data(self):
        mock_compile = AsyncMock(return_value={"week_in_review": {"meetings_count": 5}})
        mock_weekly_review_module = MagicMock()
        mock_weekly_review_module.compile_weekly_review_data = mock_compile

        with patch("processors.weekly_review_session.supabase_client") as mock_db, \
             patch.dict("sys.modules", {"processors.weekly_review": mock_weekly_review_module}):

            mock_db.get_weekly_review_session.return_value = {
                "id": "s-1",
                "week_number": 12,
                "year": 2026,
                "current_part": 2,
                "agenda_data": {},
            }
            mock_db.update_weekly_review_session.return_value = {}

            from processors.weekly_review_session import resume_after_debrief
            result = await resume_after_debrief("s-1")
            assert result["action"] == "review_resumed_after_debrief"


# =========================================================================
# Gantt Backup Order Tests (Phase 6 Batch A)
# =========================================================================

class TestGanttBackupOrder:
    """Test Gantt backup happens before proposal execution."""

    @pytest.mark.asyncio
    async def test_backup_before_execute(self):
        """Backup should be called before proposals are executed."""
        call_order = []

        async def mock_backup():
            call_order.append("backup")

        async def mock_execute(pid):
            call_order.append(f"execute_{pid}")

        mock_gantt = MagicMock()
        mock_gantt.backup_full_gantt = mock_backup
        mock_gantt.execute_approved_proposal = mock_execute
        mock_gantt_module = MagicMock()
        mock_gantt_module.gantt_manager = mock_gantt

        mock_dist = AsyncMock(return_value={"drive_uploaded": True})
        mock_approval_module = MagicMock()
        mock_approval_module.distribute_approved_review = mock_dist

        with patch("processors.weekly_review_session.supabase_client") as mock_db, \
             patch.dict("sys.modules", {
                 "services.gantt_manager": mock_gantt_module,
                 "guardrails.approval_flow": mock_approval_module,
             }):
            mock_db.get_weekly_review_session.return_value = {
                "id": "s-1", "status": "confirming",
                "agenda_data": {}, "week_number": 12, "year": 2026,
                "gantt_proposals": [{"id": "p1", "approved": True}],
            }
            mock_db.client.table.return_value.update.return_value.eq.return_value.eq.return_value.execute.return_value = MagicMock(data=[{"id": "s-1"}])
            mock_db.update_weekly_review_session.return_value = {}

            from processors.weekly_review_session import confirm_review
            result = await confirm_review("s-1", approved=True)

            assert call_order == ["backup", "execute_p1"]
            assert result["action"] == "review_approved"

    @pytest.mark.asyncio
    async def test_backup_fails_continues(self):
        """If backup fails, proposals should still execute."""
        async def mock_backup():
            raise Exception("Sheets API down")

        async def mock_execute(pid):
            pass

        mock_gantt = MagicMock()
        mock_gantt.backup_full_gantt = mock_backup
        mock_gantt.execute_approved_proposal = mock_execute
        mock_gantt_module = MagicMock()
        mock_gantt_module.gantt_manager = mock_gantt

        mock_dist = AsyncMock(return_value={"drive_uploaded": True})
        mock_approval_module = MagicMock()
        mock_approval_module.distribute_approved_review = mock_dist

        with patch("processors.weekly_review_session.supabase_client") as mock_db, \
             patch.dict("sys.modules", {
                 "services.gantt_manager": mock_gantt_module,
                 "guardrails.approval_flow": mock_approval_module,
             }):
            mock_db.get_weekly_review_session.return_value = {
                "id": "s-1", "status": "confirming",
                "agenda_data": {}, "week_number": 12, "year": 2026,
                "gantt_proposals": [{"id": "p1", "approved": True}],
            }
            mock_db.client.table.return_value.update.return_value.eq.return_value.eq.return_value.execute.return_value = MagicMock(data=[{"id": "s-1"}])
            mock_db.update_weekly_review_session.return_value = {}

            from processors.weekly_review_session import confirm_review
            result = await confirm_review("s-1", approved=True)
            assert result["action"] == "review_approved"

    @pytest.mark.asyncio
    async def test_no_proposals_skips_backup(self):
        """No approved proposals = no backup call."""
        mock_gantt = MagicMock()
        mock_gantt.backup_full_gantt = AsyncMock()
        mock_gantt_module = MagicMock()
        mock_gantt_module.gantt_manager = mock_gantt

        mock_dist = AsyncMock(return_value={})
        mock_approval_module = MagicMock()
        mock_approval_module.distribute_approved_review = mock_dist

        with patch("processors.weekly_review_session.supabase_client") as mock_db, \
             patch.dict("sys.modules", {
                 "services.gantt_manager": mock_gantt_module,
                 "guardrails.approval_flow": mock_approval_module,
             }):
            mock_db.get_weekly_review_session.return_value = {
                "id": "s-1", "status": "confirming",
                "agenda_data": {}, "week_number": 12, "year": 2026,
                "gantt_proposals": [],
            }
            mock_db.client.table.return_value.update.return_value.eq.return_value.eq.return_value.execute.return_value = MagicMock(data=[{"id": "s-1"}])
            mock_db.update_weekly_review_session.return_value = {}

            from processors.weekly_review_session import confirm_review
            await confirm_review("s-1", approved=True)
            mock_gantt.backup_full_gantt.assert_not_called()

    @pytest.mark.asyncio
    async def test_gantt_failed_returns_backup_available(self):
        """gantt_failed action should include backup_available=True."""
        async def mock_backup():
            pass

        async def mock_execute(pid):
            raise Exception("execute failed")

        mock_gantt = MagicMock()
        mock_gantt.backup_full_gantt = mock_backup
        mock_gantt.execute_approved_proposal = mock_execute
        mock_gantt_module = MagicMock()
        mock_gantt_module.gantt_manager = mock_gantt

        with patch("processors.weekly_review_session.supabase_client") as mock_db, \
             patch.dict("sys.modules", {"services.gantt_manager": mock_gantt_module}):
            mock_db.get_weekly_review_session.return_value = {
                "id": "s-1", "status": "confirming",
                "agenda_data": {}, "week_number": 12, "year": 2026,
                "gantt_proposals": [{"id": "p1", "approved": True}],
            }
            mock_db.client.table.return_value.update.return_value.eq.return_value.eq.return_value.execute.return_value = MagicMock(data=[{"id": "s-1"}])
            mock_db.update_weekly_review_session.return_value = {}

            from processors.weekly_review_session import confirm_review
            result = await confirm_review("s-1", approved=True)
            assert result["action"] == "gantt_failed"
            assert result.get("backup_available") is True

    @pytest.mark.asyncio
    async def test_unapproved_proposals_skip_backup(self):
        """Proposals exist but none approved — no backup needed."""
        mock_gantt = MagicMock()
        mock_gantt.backup_full_gantt = AsyncMock()
        mock_gantt_module = MagicMock()
        mock_gantt_module.gantt_manager = mock_gantt

        mock_dist = AsyncMock(return_value={})
        mock_approval_module = MagicMock()
        mock_approval_module.distribute_approved_review = mock_dist

        with patch("processors.weekly_review_session.supabase_client") as mock_db, \
             patch.dict("sys.modules", {
                 "services.gantt_manager": mock_gantt_module,
                 "guardrails.approval_flow": mock_approval_module,
             }):
            mock_db.get_weekly_review_session.return_value = {
                "id": "s-1", "status": "confirming",
                "agenda_data": {}, "week_number": 12, "year": 2026,
                "gantt_proposals": [{"id": "p1", "approved": False}],
            }
            mock_db.client.table.return_value.update.return_value.eq.return_value.eq.return_value.execute.return_value = MagicMock(data=[{"id": "s-1"}])
            mock_db.update_weekly_review_session.return_value = {}

            from processors.weekly_review_session import confirm_review
            await confirm_review("s-1", approved=True)
            mock_gantt.backup_full_gantt.assert_not_called()


# =========================================================================
# Session Expiry Tests (Phase 6 Batch A)
# =========================================================================

class TestSessionExpiry:
    """Test 48h session expiry (replaces week-boundary logic)."""

    @pytest.mark.asyncio
    async def test_resume_within_48h(self):
        """Session within 48h should resume."""
        recent_time = (datetime.utcnow() - timedelta(hours=10)).isoformat()

        with patch("processors.weekly_review_session.supabase_client") as mock_db:
            mock_db.get_active_weekly_review_session.return_value = {
                "id": "existing-1",
                "week_number": 12,
                "year": 2026,
                "current_part": 2,
                "status": "in_progress",
                "created_at": recent_time,
                "agenda_data": {},
            }
            mock_db.get_weekly_review_session.return_value = {
                "id": "existing-1", "week_number": 12, "agenda_data": {},
            }
            mock_db.update_weekly_review_session.return_value = {}

            from processors.weekly_review_session import start_weekly_review
            result = await start_weekly_review(user_id="eyal")
            assert result["session_id"] == "existing-1"
            assert "review_resumed" in result.get("action", "")

    @pytest.mark.asyncio
    async def test_expire_after_48h(self):
        """Session older than 48h should be expired and new one created."""
        old_time = (datetime.utcnow() - timedelta(hours=50)).isoformat()
        mock_compile = AsyncMock(return_value={"week_in_review": {}})
        mock_weekly_review_module = MagicMock()
        mock_weekly_review_module.compile_weekly_review_data = mock_compile

        with patch("processors.weekly_review_session.supabase_client") as mock_db, \
             patch.dict("sys.modules", {"processors.weekly_review": mock_weekly_review_module}):
            mock_db.get_active_weekly_review_session.return_value = {
                "id": "old-1",
                "week_number": 12,
                "year": 2026,
                "status": "in_progress",
                "created_at": old_time,
            }
            mock_db.create_weekly_review_session.return_value = {
                "id": "new-1", "week_number": 12, "year": 2026,
            }
            mock_db.get_weekly_review_session.return_value = {
                "id": "new-1", "agenda_data": {}, "week_number": 12,
            }
            mock_db.update_weekly_review_session.return_value = {}

            from processors.weekly_review_session import start_weekly_review
            result = await start_weekly_review(user_id="eyal")

            # Old session should be expired
            mock_db.update_weekly_review_session.assert_any_call("old-1", status="expired")
            # New session should be created
            assert result["session_id"] == "new-1"

    @pytest.mark.asyncio
    async def test_cross_week_within_48h_resumes(self):
        """Session from different week but within 48h should resume (not cancel)."""
        recent_time = (datetime.utcnow() - timedelta(hours=20)).isoformat()

        with patch("processors.weekly_review_session.supabase_client") as mock_db:
            mock_db.get_active_weekly_review_session.return_value = {
                "id": "cross-week-1",
                "week_number": 11,  # different week
                "year": 2026,
                "current_part": 1,
                "status": "in_progress",
                "created_at": recent_time,
                "agenda_data": {},
            }
            mock_db.get_weekly_review_session.return_value = {
                "id": "cross-week-1", "week_number": 11, "agenda_data": {},
            }
            mock_db.update_weekly_review_session.return_value = {}

            from processors.weekly_review_session import start_weekly_review
            result = await start_weekly_review(user_id="eyal")
            assert result["session_id"] == "cross-week-1"
            assert "review_resumed" in result.get("action", "")

    @pytest.mark.asyncio
    async def test_stale_data_warning_shown(self):
        """Session >4h old should show stale data warning on resume."""
        old_time = (datetime.utcnow() - timedelta(hours=8)).isoformat()

        with patch("processors.weekly_review_session.supabase_client") as mock_db:
            mock_db.get_active_weekly_review_session.return_value = {
                "id": "stale-1",
                "week_number": 12,
                "year": 2026,
                "current_part": 2,
                "status": "in_progress",
                "created_at": old_time,
                "agenda_data": {},
            }
            mock_db.count_items_since.return_value = 3
            mock_db.get_weekly_review_session.return_value = {
                "id": "stale-1", "week_number": 12, "agenda_data": {},
            }
            mock_db.update_weekly_review_session.return_value = {}

            from processors.weekly_review_session import start_weekly_review
            result = await start_weekly_review(user_id="eyal")
            assert "compiled" in result["response"].lower() or "ago" in result["response"].lower()
            assert "/review --fresh" in result["response"]

    @pytest.mark.asyncio
    async def test_orphan_sweep_expires_old_sessions(self):
        """Orphan cleanup should expire sessions older than 48h."""
        with patch("schedulers.orphan_cleanup_scheduler.supabase_client") as mock_db, \
             patch("schedulers.orphan_cleanup_scheduler.telegram_bot") as mock_tg:
            # Setup: orphan cleanup's _run_cleanup
            mock_db.client.table.return_value.update.return_value.in_.return_value.lt.return_value.execute.return_value = MagicMock(data=[{"id": "old-1"}])
            mock_db.get_stale_pending_approvals.return_value = []
            mock_db.get_orphan_embedding_ids.return_value = []
            mock_db.log_action.return_value = {}

            # Mock expire_stale_approvals (imported locally inside _run_cleanup)
            mock_approval_flow = MagicMock()
            mock_approval_flow.expire_stale_approvals = AsyncMock(return_value=[])
            with patch.dict("sys.modules", {"guardrails.approval_flow": mock_approval_flow}):
                from schedulers.orphan_cleanup_scheduler import OrphanCleanupScheduler
                scheduler = OrphanCleanupScheduler()
                result = await scheduler._run_cleanup()
                assert result.get("expired_review_sessions") == 1

    @pytest.mark.asyncio
    async def test_configurable_expiry_setting(self):
        """WEEKLY_REVIEW_SESSION_EXPIRY_HOURS should be configurable."""
        # 24h expiry, session is 30h old -> should expire
        old_time = (datetime.utcnow() - timedelta(hours=30)).isoformat()
        mock_compile = AsyncMock(return_value={"week_in_review": {}})
        mock_weekly_review_module = MagicMock()
        mock_weekly_review_module.compile_weekly_review_data = mock_compile

        with patch("processors.weekly_review_session.supabase_client") as mock_db, \
             patch("processors.weekly_review_session.settings") as mock_settings, \
             patch.dict("sys.modules", {"processors.weekly_review": mock_weekly_review_module}):
            mock_settings.WEEKLY_REVIEW_SESSION_EXPIRY_HOURS = 24
            mock_settings.model_agent = "claude-sonnet-4-6"
            mock_db.get_active_weekly_review_session.return_value = {
                "id": "old-1",
                "week_number": 12, "year": 2026,
                "status": "in_progress",
                "created_at": old_time,
            }
            mock_db.create_weekly_review_session.return_value = {
                "id": "new-1", "week_number": 12, "year": 2026,
            }
            mock_db.get_weekly_review_session.return_value = {
                "id": "new-1", "agenda_data": {}, "week_number": 12,
            }
            mock_db.update_weekly_review_session.return_value = {}

            from processors.weekly_review_session import start_weekly_review
            result = await start_weekly_review(user_id="eyal")
            mock_db.update_weekly_review_session.assert_any_call("old-1", status="expired")

    @pytest.mark.asyncio
    async def test_no_stale_warning_under_4h(self):
        """Session under 4h old should NOT show stale data warning."""
        recent_time = (datetime.utcnow() - timedelta(hours=2)).isoformat()

        with patch("processors.weekly_review_session.supabase_client") as mock_db:
            mock_db.get_active_weekly_review_session.return_value = {
                "id": "recent-1",
                "week_number": 12, "year": 2026,
                "current_part": 2,
                "status": "in_progress",
                "created_at": recent_time,
                "agenda_data": {},
            }
            mock_db.get_weekly_review_session.return_value = {
                "id": "recent-1", "week_number": 12, "agenda_data": {},
            }
            mock_db.update_weekly_review_session.return_value = {}

            from processors.weekly_review_session import start_weekly_review
            result = await start_weekly_review(user_id="eyal")
            assert "/review --fresh" not in result.get("response", "")


# =========================================================================
# Stack Reconstruction Tests (Phase 6 Batch A)
# =========================================================================

class TestStackReconstruction:
    """Test session stack reconstruction on startup."""

    @pytest.mark.asyncio
    async def test_no_sessions(self):
        from services.telegram_bot import TelegramBot
        bot = TelegramBot()

        with patch("services.supabase_client.supabase_client") as mock_db:
            mock_db.get_active_weekly_review_session.return_value = None
            mock_db.get_active_debrief_session.return_value = None

            count = await bot._reconstruct_session_stack()
            assert count == 0
            assert bot._session_stack == []

    @pytest.mark.asyncio
    async def test_debrief_only(self):
        from services.telegram_bot import TelegramBot
        bot = TelegramBot()

        with patch("services.supabase_client.supabase_client") as mock_db:
            mock_db.get_active_weekly_review_session.return_value = None
            mock_db.get_active_debrief_session.return_value = {
                "id": "d-1", "created_at": "2026-03-18T10:00:00",
            }

            count = await bot._reconstruct_session_stack()
            assert count == 1
            assert bot._session_stack == ["debrief"]

    @pytest.mark.asyncio
    async def test_review_only(self):
        from services.telegram_bot import TelegramBot
        bot = TelegramBot()

        with patch("services.supabase_client.supabase_client") as mock_db:
            mock_db.get_active_weekly_review_session.return_value = {
                "id": "r-1", "created_at": "2026-03-18T09:00:00",
            }
            mock_db.get_active_debrief_session.return_value = None

            count = await bot._reconstruct_session_stack()
            assert count == 1
            assert bot._session_stack == ["weekly_review"]

    @pytest.mark.asyncio
    async def test_both_ordered_by_created_at(self):
        from services.telegram_bot import TelegramBot
        bot = TelegramBot()

        with patch("services.supabase_client.supabase_client") as mock_db:
            mock_db.get_active_weekly_review_session.return_value = {
                "id": "r-1", "created_at": "2026-03-18T09:00:00",
            }
            mock_db.get_active_debrief_session.return_value = {
                "id": "d-1", "created_at": "2026-03-18T10:00:00",
            }

            count = await bot._reconstruct_session_stack()
            assert count == 2
            # Review was created first (older = bottom of stack), debrief on top
            assert bot._session_stack == ["weekly_review", "debrief"]
            assert bot._active_interactive_session == "debrief"

    @pytest.mark.asyncio
    async def test_debrief_error_ignored(self):
        """If debrief table query fails, only review is reconstructed."""
        from services.telegram_bot import TelegramBot
        bot = TelegramBot()

        with patch("services.supabase_client.supabase_client") as mock_db:
            mock_db.get_active_weekly_review_session.return_value = {
                "id": "r-1", "created_at": "2026-03-18T09:00:00",
            }
            mock_db.get_active_debrief_session.side_effect = Exception("table missing")

            count = await bot._reconstruct_session_stack()
            assert count == 1
            assert bot._session_stack == ["weekly_review"]
