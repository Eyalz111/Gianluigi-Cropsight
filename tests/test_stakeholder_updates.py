"""
Tests for Phase 4: Stakeholder Tracker Updates with Approval.

Tests:
1. submit_stakeholder_updates_for_approval() — new stakeholder
2. submit_stakeholder_updates_for_approval() — existing stakeholder
3. send_stakeholder_approval_request() — message format with buttons
4. apply_stakeholder_update() — update existing row
5. apply_stakeholder_update() — add new row
6. Callback handling — stakeholder_approve and stakeholder_reject
"""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch


# =============================================================================
# Test: submit_stakeholder_updates_for_approval — new stakeholder
# =============================================================================

class TestSubmitStakeholderForApprovalNew:
    """Test submitting a new stakeholder (not existing) for approval."""

    @pytest.mark.asyncio
    async def test_new_stakeholder_submission(self):
        """Should detect that stakeholder is new and submit for approval."""
        with (
            patch(
                "guardrails.approval_flow.sheets_service"
            ) as mock_sheets,
            patch(
                "guardrails.approval_flow.telegram_bot"
            ) as mock_telegram,
            patch(
                "guardrails.approval_flow.supabase_client"
            ) as mock_supabase,
        ):
            # Stakeholder does NOT exist
            mock_sheets.get_stakeholder_info = AsyncMock(return_value=[])
            mock_telegram.send_stakeholder_approval_request = AsyncMock(
                return_value=True
            )
            mock_supabase.log_action = MagicMock(return_value={"id": "log-1"})

            from guardrails.approval_flow import (
                submit_stakeholder_updates_for_approval,
            )

            result = await submit_stakeholder_updates_for_approval(
                stakeholder_name="Rita Gonzalez",
                organization="AgriTech Labs",
                updates={
                    "contact_person": "Rita Gonzalez",
                    "type": "Partner",
                    "priority": "H",
                },
                source_meeting_id="meeting-123",
            )

            assert result["status"] == "pending"
            assert result["is_new"] is True
            assert result["action"] == "add"
            assert result["telegram_sent"] is True
            assert result["approval_id"] == "stakeholder:AgriTech Labs"

            # Verify sheets was checked
            mock_sheets.get_stakeholder_info.assert_awaited_once_with(
                name="Rita Gonzalez"
            )
            # Verify telegram was called with is_new=True
            mock_telegram.send_stakeholder_approval_request.assert_awaited_once()
            call_kwargs = mock_telegram.send_stakeholder_approval_request.call_args.kwargs
            assert call_kwargs["is_new"] is True
            assert call_kwargs["organization"] == "AgriTech Labs"

            # Verify audit log (sync call, no await)
            mock_supabase.log_action.assert_called_once()
            log_kwargs = mock_supabase.log_action.call_args.kwargs
            assert log_kwargs["action"] == "stakeholder_update_requested"
            assert log_kwargs["details"]["action"] == "add"


# =============================================================================
# Test: submit_stakeholder_updates_for_approval — existing stakeholder
# =============================================================================

class TestSubmitStakeholderForApprovalExisting:
    """Test submitting an update for an existing stakeholder."""

    @pytest.mark.asyncio
    async def test_existing_stakeholder_submission(self):
        """Should detect existing stakeholder and submit update for approval."""
        with (
            patch(
                "guardrails.approval_flow.sheets_service"
            ) as mock_sheets,
            patch(
                "guardrails.approval_flow.telegram_bot"
            ) as mock_telegram,
            patch(
                "guardrails.approval_flow.supabase_client"
            ) as mock_supabase,
        ):
            # Stakeholder DOES exist
            mock_sheets.get_stakeholder_info = AsyncMock(
                return_value=[
                    {
                        "row_number": 5,
                        "organization_name": "AgriTech Labs",
                        "contact_person": "Rita Gonzalez",
                        "status": "Active",
                    }
                ]
            )
            mock_telegram.send_stakeholder_approval_request = AsyncMock(
                return_value=True
            )
            mock_supabase.log_action = MagicMock(return_value={"id": "log-2"})

            from guardrails.approval_flow import (
                submit_stakeholder_updates_for_approval,
            )

            result = await submit_stakeholder_updates_for_approval(
                stakeholder_name="Rita Gonzalez",
                organization="AgriTech Labs",
                updates={"next_action": "Schedule demo", "priority": "H"},
            )

            assert result["status"] == "pending"
            assert result["is_new"] is False
            assert result["action"] == "update"
            assert result["telegram_sent"] is True

            # Verify telegram was called with is_new=False
            call_kwargs = mock_telegram.send_stakeholder_approval_request.call_args.kwargs
            assert call_kwargs["is_new"] is False

            # Verify audit log records "update" action
            log_kwargs = mock_supabase.log_action.call_args.kwargs
            assert log_kwargs["details"]["action"] == "update"


# =============================================================================
# Test: send_stakeholder_approval_request — message format with buttons
# =============================================================================

class TestSendStakeholderApprovalRequest:
    """Test the Telegram message formatting and button layout."""

    @pytest.mark.asyncio
    async def test_new_stakeholder_message_format(self):
        """Should format message with 'New Stakeholder' header and buttons."""
        with patch(
            "services.telegram_bot.settings"
        ) as mock_settings:
            mock_settings.TELEGRAM_BOT_TOKEN = "test-token"
            mock_settings.TELEGRAM_GROUP_CHAT_ID = "-100123"
            mock_settings.TELEGRAM_EYAL_CHAT_ID = "123456789"

            from services.telegram_bot import TelegramBot

            bot = TelegramBot()
            bot.send_to_eyal = AsyncMock(return_value=True)

            result = await bot.send_stakeholder_approval_request(
                stakeholder_name="Rita Gonzalez",
                organization="AgriTech Labs",
                updates={
                    "contact_person": "Rita Gonzalez",
                    "type": "Partner",
                },
                is_new=True,
                source_meeting_id="meeting-123",
            )

            assert result is True
            bot.send_to_eyal.assert_awaited_once()

            # Check message content
            call_args = bot.send_to_eyal.call_args
            message_text = call_args.args[0]
            assert "New Stakeholder" in message_text
            assert "AgriTech Labs" in message_text
            assert "Rita Gonzalez" in message_text
            assert "meeting-123" in message_text

            # Check reply_markup has buttons
            reply_markup = call_args.kwargs.get("reply_markup")
            assert reply_markup is not None

            # Check parse_mode is HTML
            parse_mode = call_args.kwargs.get("parse_mode")
            assert parse_mode == "HTML"

    @pytest.mark.asyncio
    async def test_update_stakeholder_message_format(self):
        """Should format message with 'Update Stakeholder' header."""
        with patch(
            "services.telegram_bot.settings"
        ) as mock_settings:
            mock_settings.TELEGRAM_BOT_TOKEN = "test-token"
            mock_settings.TELEGRAM_GROUP_CHAT_ID = "-100123"
            mock_settings.TELEGRAM_EYAL_CHAT_ID = "123456789"

            from services.telegram_bot import TelegramBot

            bot = TelegramBot()
            bot.send_to_eyal = AsyncMock(return_value=True)

            result = await bot.send_stakeholder_approval_request(
                stakeholder_name="Rita Gonzalez",
                organization="AgriTech Labs",
                updates={"next_action": "Schedule demo"},
                is_new=False,
            )

            assert result is True
            message_text = bot.send_to_eyal.call_args.args[0]
            assert "Update Stakeholder" in message_text

    @pytest.mark.asyncio
    async def test_callback_data_truncation(self):
        """Should truncate org name in callback data to fit Telegram limit."""
        with patch(
            "services.telegram_bot.settings"
        ) as mock_settings:
            mock_settings.TELEGRAM_BOT_TOKEN = "test-token"
            mock_settings.TELEGRAM_GROUP_CHAT_ID = "-100123"
            mock_settings.TELEGRAM_EYAL_CHAT_ID = "123456789"

            from services.telegram_bot import TelegramBot

            bot = TelegramBot()
            bot.send_to_eyal = AsyncMock(return_value=True)

            long_org = "A" * 60  # Very long org name
            await bot.send_stakeholder_approval_request(
                stakeholder_name="Contact",
                organization=long_org,
                updates={"type": "Partner"},
                is_new=True,
            )

            # Verify the reply_markup callback_data is within limits
            call_kwargs = bot.send_to_eyal.call_args.kwargs
            reply_markup = call_kwargs["reply_markup"]
            # InlineKeyboardMarkup.inline_keyboard is a list of rows
            approve_button = reply_markup.inline_keyboard[0][0]
            # "stakeholder_approve:" = 20 chars + 30 chars = 50 chars max
            assert len(approve_button.callback_data) <= 64


# =============================================================================
# Test: apply_stakeholder_update — update existing row
# =============================================================================

class TestApplyStakeholderUpdateExisting:
    """Test applying an approved update to an existing stakeholder."""

    @pytest.mark.asyncio
    async def test_update_existing_stakeholder(self):
        """Should update cells for an existing stakeholder row."""
        with patch(
            "services.google_sheets.settings"
        ) as mock_settings:
            mock_settings.STAKEHOLDER_TRACKER_SHEET_ID = "sheet-456"

            from services.google_sheets import GoogleSheetsService

            svc = GoogleSheetsService()

            # Mock get_all_stakeholders to return existing data
            svc.get_all_stakeholders = AsyncMock(
                return_value=[
                    {
                        "row_number": 3,
                        "organization_name": "AgriTech Labs",
                        "contact_person": "Rita Gonzalez",
                        "status": "Active",
                    },
                ]
            )
            svc._update_cell = AsyncMock()

            result = await svc.apply_stakeholder_update(
                organization="AgriTech Labs",
                updates={
                    "next_action": "Schedule demo",
                    "priority": "H",
                },
            )

            assert result is True

            # Should have called _update_cell for each field
            assert svc._update_cell.await_count == 2

            # Check the calls were made with correct column mappings
            calls = svc._update_cell.call_args_list
            call_ranges = [c.kwargs["range_name"] for c in calls]
            # next_action -> I, priority -> F
            assert "I3" in call_ranges
            assert "F3" in call_ranges

    @pytest.mark.asyncio
    async def test_update_case_insensitive_match(self):
        """Should match organization name case-insensitively."""
        with patch(
            "services.google_sheets.settings"
        ) as mock_settings:
            mock_settings.STAKEHOLDER_TRACKER_SHEET_ID = "sheet-456"

            from services.google_sheets import GoogleSheetsService

            svc = GoogleSheetsService()
            svc.get_all_stakeholders = AsyncMock(
                return_value=[
                    {
                        "row_number": 5,
                        "organization_name": "AGRITECH LABS",
                        "contact_person": "Rita",
                    },
                ]
            )
            svc._update_cell = AsyncMock()

            result = await svc.apply_stakeholder_update(
                organization="agritech labs",
                updates={"status": "In Progress"},
            )

            assert result is True
            svc._update_cell.assert_awaited_once()


# =============================================================================
# Test: apply_stakeholder_update — add new row
# =============================================================================

class TestApplyStakeholderUpdateNew:
    """Test applying an approved update for a new stakeholder (append row)."""

    @pytest.mark.asyncio
    async def test_add_new_stakeholder_row(self):
        """Should append a new row when stakeholder not found."""
        with patch(
            "services.google_sheets.settings"
        ) as mock_settings:
            mock_settings.STAKEHOLDER_TRACKER_SHEET_ID = "sheet-456"

            from services.google_sheets import GoogleSheetsService

            svc = GoogleSheetsService()

            # No existing stakeholders
            svc.get_all_stakeholders = AsyncMock(return_value=[])
            svc._append_row_to_range = AsyncMock()

            result = await svc.apply_stakeholder_update(
                organization="New Partner Inc",
                updates={
                    "organization_name": "New Partner Inc",
                    "contact_person": "John Doe",
                    "type": "Investor",
                    "priority": "M",
                    "status": "New",
                    "notes": "Met at conference",
                },
            )

            assert result is True
            svc._append_row_to_range.assert_awaited_once()

            # Check the row data
            call_kwargs = svc._append_row_to_range.call_args.kwargs
            assert call_kwargs["sheet_id"] == "sheet-456"
            assert call_kwargs["range_name"] == "A:P"
            row_values = call_kwargs["values"]
            assert row_values[0] == "New Partner Inc"  # organization_name
            assert row_values[1] == "Investor"          # type
            assert row_values[3] == "John Doe"          # contact_person
            assert row_values[14] == "New"               # status
            assert row_values[15] == "Met at conference"  # notes

    @pytest.mark.asyncio
    async def test_add_new_with_defaults(self):
        """Should use defaults for missing fields in new stakeholder."""
        with patch(
            "services.google_sheets.settings"
        ) as mock_settings:
            mock_settings.STAKEHOLDER_TRACKER_SHEET_ID = "sheet-456"

            from services.google_sheets import GoogleSheetsService

            svc = GoogleSheetsService()
            svc.get_all_stakeholders = AsyncMock(return_value=[])
            svc._append_row_to_range = AsyncMock()

            result = await svc.apply_stakeholder_update(
                organization="Minimal Org",
                updates={},  # No updates — use all defaults
            )

            assert result is True
            row_values = svc._append_row_to_range.call_args.kwargs["values"]
            # organization_name defaults to the organization arg
            assert row_values[0] == "Minimal Org"
            # status defaults to "New"
            assert row_values[14] == "New"

    @pytest.mark.asyncio
    async def test_no_sheet_id_returns_false(self):
        """Should return False when STAKEHOLDER_TRACKER_SHEET_ID is not set."""
        with patch(
            "services.google_sheets.settings"
        ) as mock_settings:
            mock_settings.STAKEHOLDER_TRACKER_SHEET_ID = ""

            from services.google_sheets import GoogleSheetsService

            svc = GoogleSheetsService()
            result = await svc.apply_stakeholder_update(
                organization="Test",
                updates={"status": "Active"},
            )
            assert result is False


# =============================================================================
# Test: Callback handling — stakeholder_approve and stakeholder_reject
# =============================================================================

class TestStakeholderCallbackHandling:
    """Test the callback query handler for stakeholder approve/reject buttons."""

    @pytest.mark.asyncio
    async def test_stakeholder_approve_callback(self):
        """Should process stakeholder_approve callback and log action."""
        mock_supabase = MagicMock()
        mock_supabase.log_action = MagicMock(return_value={"id": "log-3"})
        mock_supabase.update_meeting = MagicMock()

        from services.telegram_bot import TelegramBot

        bot = TelegramBot()

        # Create mock update for callback query
        query = AsyncMock()
        query.data = "stakeholder_approve:AgriTech Labs"
        query.answer = AsyncMock()
        query.edit_message_text = AsyncMock()

        update = MagicMock()
        update.callback_query = query

        context = MagicMock()
        context.user_data = {}

        # Patch supabase_client in the module where it gets imported from
        with patch(
            "services.supabase_client.supabase_client",
            mock_supabase,
        ):
            await bot._handle_callback_query(update, context)

        query.answer.assert_awaited_once()
        query.edit_message_text.assert_awaited_once_with(
            "Approved stakeholder update for: AgriTech Labs"
        )
        mock_supabase.log_action.assert_called_once()
        log_kwargs = mock_supabase.log_action.call_args.kwargs
        assert log_kwargs["action"] == "stakeholder_approved"
        assert log_kwargs["details"]["organization"] == "AgriTech Labs"
        assert log_kwargs["triggered_by"] == "eyal"

    @pytest.mark.asyncio
    async def test_stakeholder_reject_callback(self):
        """Should process stakeholder_reject callback and log action."""
        mock_supabase = MagicMock()
        mock_supabase.log_action = MagicMock(return_value={"id": "log-4"})
        mock_supabase.update_meeting = MagicMock()

        from services.telegram_bot import TelegramBot

        bot = TelegramBot()

        query = AsyncMock()
        query.data = "stakeholder_reject:AgriTech Labs"
        query.answer = AsyncMock()
        query.edit_message_text = AsyncMock()

        update = MagicMock()
        update.callback_query = query

        context = MagicMock()
        context.user_data = {}

        with patch(
            "services.supabase_client.supabase_client",
            mock_supabase,
        ):
            await bot._handle_callback_query(update, context)

        query.answer.assert_awaited_once()
        query.edit_message_text.assert_awaited_once_with(
            "Rejected stakeholder update for: AgriTech Labs"
        )
        mock_supabase.log_action.assert_called_once()
        log_kwargs = mock_supabase.log_action.call_args.kwargs
        assert log_kwargs["action"] == "stakeholder_rejected"
        assert log_kwargs["details"]["organization"] == "AgriTech Labs"
        assert log_kwargs["triggered_by"] == "eyal"
