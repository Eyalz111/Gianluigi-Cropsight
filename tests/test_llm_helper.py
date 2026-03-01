"""
Tests for core/llm.py — Centralized LLM Helper.

Tests cover:
- get_client() singleton behavior
- call_llm() return format, model/token routing, system prompt caching
- _log_usage() best-effort logging
- Model verification for migrated call sites
- /cost command in Telegram bot
"""

import pytest
from unittest.mock import MagicMock, patch, AsyncMock


# =============================================================================
# get_client() — Singleton
# =============================================================================

class TestGetClient:
    """Tests for the singleton Anthropic client."""

    def test_returns_same_instance(self):
        """get_client() should return the same instance on repeated calls."""
        import core.llm as llm_module
        # Reset singleton
        llm_module._client = None

        with patch("core.llm.Anthropic") as mock_cls:
            mock_cls.return_value = MagicMock()
            c1 = llm_module.get_client()
            c2 = llm_module.get_client()

            assert c1 is c2
            # Anthropic() should only be called once
            mock_cls.assert_called_once()

        # Clean up
        llm_module._client = None


# =============================================================================
# call_llm() — Core Helper
# =============================================================================

class TestCallLlm:
    """Tests for the call_llm() entry point."""

    def test_returns_text_and_usage_tuple(self):
        """call_llm should return (text, usage_dict) tuple."""
        mock_usage = MagicMock(
            input_tokens=100,
            output_tokens=50,
            cache_read_input_tokens=0,
            cache_creation_input_tokens=0,
        )
        mock_response = MagicMock()
        mock_response.content = [MagicMock(text="Hello world")]
        mock_response.usage = mock_usage

        import core.llm as llm_module
        llm_module._client = None

        with (
            patch("core.llm.Anthropic") as mock_cls,
            patch("core.llm._log_usage"),
        ):
            mock_client = MagicMock()
            mock_client.messages.create.return_value = mock_response
            mock_cls.return_value = mock_client

            text, usage = llm_module.call_llm(
                prompt="Test prompt",
                model="test-model",
                max_tokens=100,
                call_site="test",
            )

            assert text == "Hello world"
            assert usage["input_tokens"] == 100
            assert usage["output_tokens"] == 50
            assert usage["cache_read_input_tokens"] == 0
            assert usage["cache_creation_input_tokens"] == 0

        llm_module._client = None

    def test_system_prompt_adds_cache_control(self):
        """When system prompt is provided, cache_control block should be added."""
        mock_usage = MagicMock(
            input_tokens=100,
            output_tokens=50,
            cache_read_input_tokens=0,
            cache_creation_input_tokens=0,
        )
        mock_response = MagicMock()
        mock_response.content = [MagicMock(text="result")]
        mock_response.usage = mock_usage

        import core.llm as llm_module
        llm_module._client = None

        with (
            patch("core.llm.Anthropic") as mock_cls,
            patch("core.llm._log_usage"),
        ):
            mock_client = MagicMock()
            mock_client.messages.create.return_value = mock_response
            mock_cls.return_value = mock_client

            llm_module.call_llm(
                prompt="Test",
                model="test-model",
                max_tokens=100,
                call_site="test",
                system="You are a helpful assistant.",
            )

            # Check the system kwarg passed to messages.create
            call_kwargs = mock_client.messages.create.call_args[1]
            system_arg = call_kwargs["system"]
            assert isinstance(system_arg, list)
            assert len(system_arg) == 1
            assert system_arg[0]["type"] == "text"
            assert system_arg[0]["text"] == "You are a helpful assistant."
            assert system_arg[0]["cache_control"] == {"type": "ephemeral"}

        llm_module._client = None

    def test_no_system_prompt_sends_user_only(self):
        """Without system prompt, no system kwarg should be present."""
        mock_usage = MagicMock(
            input_tokens=50, output_tokens=25,
            cache_read_input_tokens=0, cache_creation_input_tokens=0,
        )
        mock_response = MagicMock()
        mock_response.content = [MagicMock(text="result")]
        mock_response.usage = mock_usage

        import core.llm as llm_module
        llm_module._client = None

        with (
            patch("core.llm.Anthropic") as mock_cls,
            patch("core.llm._log_usage"),
        ):
            mock_client = MagicMock()
            mock_client.messages.create.return_value = mock_response
            mock_cls.return_value = mock_client

            llm_module.call_llm(
                prompt="Test",
                model="test-model",
                max_tokens=100,
                call_site="test",
            )

            call_kwargs = mock_client.messages.create.call_args[1]
            assert "system" not in call_kwargs

        llm_module._client = None

    def test_passes_model_and_max_tokens(self):
        """Model and max_tokens should be forwarded to the API."""
        mock_usage = MagicMock(
            input_tokens=50, output_tokens=25,
            cache_read_input_tokens=0, cache_creation_input_tokens=0,
        )
        mock_response = MagicMock()
        mock_response.content = [MagicMock(text="result")]
        mock_response.usage = mock_usage

        import core.llm as llm_module
        llm_module._client = None

        with (
            patch("core.llm.Anthropic") as mock_cls,
            patch("core.llm._log_usage"),
        ):
            mock_client = MagicMock()
            mock_client.messages.create.return_value = mock_response
            mock_cls.return_value = mock_client

            llm_module.call_llm(
                prompt="Test",
                model="claude-haiku-4-5-20251001",
                max_tokens=2048,
                call_site="test",
            )

            call_kwargs = mock_client.messages.create.call_args[1]
            assert call_kwargs["model"] == "claude-haiku-4-5-20251001"
            assert call_kwargs["max_tokens"] == 2048

        llm_module._client = None


# =============================================================================
# _log_usage() — Best-Effort Logging
# =============================================================================

class TestLogUsage:
    """Tests for _log_usage() token tracking."""

    def test_writes_to_supabase(self):
        """_log_usage should insert a row into the token_usage table."""
        mock_table = MagicMock()
        mock_client = MagicMock()
        mock_client.table.return_value = mock_table
        mock_table.insert.return_value = mock_table
        mock_table.execute.return_value = MagicMock()

        with patch("core.llm.supabase_client", create=True) as mock_sb:
            # We need to patch within _log_usage's import
            with patch.dict("sys.modules", {}):
                pass

        # Use a more direct approach — patch the import inside _log_usage
        import core.llm as llm_module
        mock_supabase = MagicMock()
        mock_supabase.client.table.return_value.insert.return_value.execute.return_value = MagicMock()

        with patch("services.supabase_client.supabase_client", mock_supabase):
            llm_module._log_usage(
                call_site="test_site",
                model="test-model",
                usage={"input_tokens": 100, "output_tokens": 50,
                       "cache_read_input_tokens": 10, "cache_creation_input_tokens": 5},
                meeting_id="meeting-123",
            )

            mock_supabase.client.table.assert_called_with("token_usage")

    def test_silently_fails_on_error(self):
        """_log_usage should not raise exceptions on failure."""
        import core.llm as llm_module

        # Patch supabase_client to raise
        mock_supabase = MagicMock()
        mock_supabase.client.table.side_effect = Exception("DB down")

        with patch("services.supabase_client.supabase_client", mock_supabase):
            # Should not raise
            llm_module._log_usage(
                call_site="test",
                model="test",
                usage={"input_tokens": 0, "output_tokens": 0},
            )


# =============================================================================
# Model Verification — Migrated Call Sites
# =============================================================================

class TestModelVerification:
    """Verify each migrated call site uses the correct model tier."""

    @pytest.mark.asyncio
    async def test_transcript_processor_uses_model_extraction(self):
        """transcript_processor should use model_extraction + system prompt."""
        with (
            patch("processors.transcript_processor.call_llm") as mock_llm,
            patch("processors.transcript_processor.settings") as mock_settings,
        ):
            mock_settings.model_extraction = "claude-opus-4-6"
            mock_settings.ANTHROPIC_API_KEY = "test"
            mock_llm.return_value = (
                '{"decisions":[],"tasks":[],"follow_ups":[],"open_questions":[],'
                '"stakeholders":[],"commitments":[],"discussion_summary":"Test"}',
                {"input_tokens": 100, "output_tokens": 50,
                 "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0},
            )

            from processors.transcript_processor import extract_structured_data

            await extract_structured_data(
                transcript="Test transcript",
                meeting_title="Test Meeting",
                participants=["Eyal"],
                meeting_date="2026-01-01",
            )

            mock_llm.assert_called_once()
            call_kwargs = mock_llm.call_args[1]
            assert call_kwargs["model"] == "claude-opus-4-6"
            assert call_kwargs["call_site"] == "transcript_extraction"
            assert call_kwargs["system"] is not None  # Has system prompt

    @pytest.mark.asyncio
    async def test_infer_status_uses_model_simple(self):
        """infer_task_status_changes should use model_simple (not model_agent)."""
        with (
            patch("processors.cross_reference.call_llm") as mock_llm,
            patch("processors.cross_reference.supabase_client") as mock_db,
            patch("processors.cross_reference.settings") as mock_settings,
        ):
            mock_settings.model_simple = "claude-haiku-4-5-20251001"
            mock_settings.ANTHROPIC_API_KEY = "test"
            mock_db.get_tasks = MagicMock(return_value=[
                {"id": "t1", "title": "Test task", "assignee": "Eyal",
                 "category": "Product & Tech", "status": "pending",
                 "created_at": "2026-01-01"},
            ])
            mock_llm.return_value = (
                '{"status_changes": []}',
                {"input_tokens": 50, "output_tokens": 25,
                 "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0},
            )

            from processors.cross_reference import infer_task_status_changes

            await infer_task_status_changes("meeting-1", "transcript text")

            call_kwargs = mock_llm.call_args[1]
            assert call_kwargs["model"] == "claude-haiku-4-5-20251001"
            assert call_kwargs["call_site"] == "status_inference"

    @pytest.mark.asyncio
    async def test_deduplicate_tasks_uses_model_simple(self):
        """deduplicate_tasks should use model_simple."""
        with (
            patch("processors.cross_reference.call_llm") as mock_llm,
            patch("processors.cross_reference.supabase_client") as mock_db,
            patch("processors.cross_reference.settings") as mock_settings,
        ):
            mock_settings.model_simple = "claude-haiku-4-5-20251001"
            mock_db.get_tasks = MagicMock(return_value=[
                {"id": "t1", "title": "Existing task", "assignee": "Eyal",
                 "category": "Product & Tech", "status": "pending"},
            ])
            mock_llm.return_value = (
                '{"classifications": [{"new_task_index": "A", "type": "NEW", "existing_task_id": null, "new_status": null, "evidence": null, "reason": "New task"}]}',
                {"input_tokens": 50, "output_tokens": 25,
                 "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0},
            )

            from processors.cross_reference import deduplicate_tasks

            await deduplicate_tasks(
                [{"title": "New task", "assignee": "Roye", "category": "BD & Sales"}],
                "meeting-1", "transcript",
            )

            call_kwargs = mock_llm.call_args[1]
            assert call_kwargs["model"] == "claude-haiku-4-5-20251001"
            assert call_kwargs["call_site"] == "task_dedup"

    @pytest.mark.asyncio
    async def test_apply_edits_uses_model_background(self):
        """apply_edits should use model_background."""
        with (
            patch("guardrails.approval_flow.call_llm") as mock_llm,
            patch("guardrails.approval_flow.supabase_client") as mock_db,
            patch("guardrails.approval_flow.settings") as mock_settings,
        ):
            mock_settings.model_background = "claude-sonnet-4-6"
            mock_db.get_meeting = MagicMock(return_value={
                "summary": "Test summary", "title": "Test", "date": "2026-01-01",
            })
            mock_db.update_meeting = MagicMock()
            mock_llm.return_value = (
                "Updated summary text",
                {"input_tokens": 100, "output_tokens": 200,
                 "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0},
            )

            from guardrails.approval_flow import apply_edits

            await apply_edits("meeting-1", [{"type": "modify", "section": "summary",
                                              "target": "full", "change": "shorter"}])

            call_kwargs = mock_llm.call_args[1]
            assert call_kwargs["model"] == "claude-sonnet-4-6"
            assert call_kwargs["call_site"] == "edit_application"

    @pytest.mark.asyncio
    async def test_classify_review_intent_uses_model_simple(self):
        """_classify_review_intent should use model_simple."""
        with (
            patch("core.llm.call_llm") as mock_llm,
        ):
            mock_llm.return_value = ("edit", {"input_tokens": 10, "output_tokens": 1,
                                              "cache_read_input_tokens": 0,
                                              "cache_creation_input_tokens": 0})

            # Create a minimal bot instance to test the method
            with (
                patch("services.telegram_bot.Application"),
                patch("services.telegram_bot.settings") as mock_settings,
            ):
                mock_settings.TELEGRAM_BOT_TOKEN = "test-token"
                mock_settings.TELEGRAM_EYAL_CHAT_ID = "12345"
                mock_settings.EYAL_TELEGRAM_ID = 12345
                mock_settings.model_simple = "claude-haiku-4-5-20251001"

                from services.telegram_bot import TelegramBot
                bot = TelegramBot.__new__(TelegramBot)
                bot._stop_event = MagicMock()

                result = await bot._classify_review_intent("make it shorter")

                mock_llm.assert_called_once()
                call_kwargs = mock_llm.call_args[1]
                assert call_kwargs["model"] == "claude-haiku-4-5-20251001"
                assert call_kwargs["call_site"] == "review_intent"
                assert result == "edit"


# =============================================================================
# /cost Command
# =============================================================================

class TestCostCommand:
    """Tests for the /cost Telegram command."""

    @pytest.mark.asyncio
    async def test_cost_admin_only(self):
        """Non-admin users should be rejected."""
        with (
            patch("services.telegram_bot.Application"),
            patch("services.telegram_bot.settings") as mock_settings,
        ):
            mock_settings.TELEGRAM_BOT_TOKEN = "test-token"
            mock_settings.TELEGRAM_EYAL_CHAT_ID = "12345"
            mock_settings.EYAL_TELEGRAM_ID = 12345

            from services.telegram_bot import TelegramBot
            bot = TelegramBot.__new__(TelegramBot)
            bot._stop_event = MagicMock()
            bot.send_message = AsyncMock()
            bot._is_admin = MagicMock(return_value=False)

            mock_update = MagicMock()
            mock_update.effective_user.id = 99999
            mock_update.effective_chat.id = 99999

            await bot._handle_cost(mock_update, MagicMock())

            bot.send_message.assert_called_once()
            msg = bot.send_message.call_args[0][1]
            assert "Only Eyal" in msg

    @pytest.mark.asyncio
    async def test_cost_with_data(self):
        """Should return formatted summary when data exists."""
        with (
            patch("services.telegram_bot.Application"),
            patch("services.telegram_bot.settings") as mock_settings,
        ):
            mock_settings.TELEGRAM_BOT_TOKEN = "test-token"
            mock_settings.TELEGRAM_EYAL_CHAT_ID = "12345"
            mock_settings.EYAL_TELEGRAM_ID = 12345

            from services.telegram_bot import TelegramBot
            bot = TelegramBot.__new__(TelegramBot)
            bot._stop_event = MagicMock()
            bot.send_message = AsyncMock()
            bot._is_admin = MagicMock(return_value=True)

            mock_update = MagicMock()
            mock_update.effective_user.id = 12345
            mock_update.effective_chat.id = 12345

            # Mock supabase data
            mock_table = MagicMock()
            mock_table.select.return_value = mock_table
            mock_table.gte.return_value = mock_table
            mock_table.execute.return_value = MagicMock(data=[
                {"call_site": "task_dedup", "model": "claude-haiku-4-5-20251001",
                 "input_tokens": 500, "output_tokens": 100,
                 "cache_read_tokens": 0, "cache_creation_tokens": 0},
                {"call_site": "transcript_extraction", "model": "claude-opus-4-6",
                 "input_tokens": 3000, "output_tokens": 1500,
                 "cache_read_tokens": 1000, "cache_creation_tokens": 500},
            ])

            mock_supabase = MagicMock()
            mock_supabase.client.table.return_value = mock_table

            with patch("services.supabase_client.supabase_client", mock_supabase):
                await bot._handle_cost(mock_update, MagicMock())

            bot.send_message.assert_called_once()
            msg = bot.send_message.call_args[0][1]
            assert "API Usage" in msg
            assert "task_dedup" in msg
            assert "transcript_extraction" in msg
            assert "Totals" in msg

    @pytest.mark.asyncio
    async def test_cost_no_data(self):
        """Should return 'No API usage' message when table is empty."""
        with (
            patch("services.telegram_bot.Application"),
            patch("services.telegram_bot.settings") as mock_settings,
        ):
            mock_settings.TELEGRAM_BOT_TOKEN = "test-token"
            mock_settings.TELEGRAM_EYAL_CHAT_ID = "12345"
            mock_settings.EYAL_TELEGRAM_ID = 12345

            from services.telegram_bot import TelegramBot
            bot = TelegramBot.__new__(TelegramBot)
            bot._stop_event = MagicMock()
            bot.send_message = AsyncMock()
            bot._is_admin = MagicMock(return_value=True)

            mock_update = MagicMock()
            mock_update.effective_user.id = 12345
            mock_update.effective_chat.id = 12345

            mock_table = MagicMock()
            mock_table.select.return_value = mock_table
            mock_table.gte.return_value = mock_table
            mock_table.execute.return_value = MagicMock(data=[])

            mock_supabase = MagicMock()
            mock_supabase.client.table.return_value = mock_table

            with patch("services.supabase_client.supabase_client", mock_supabase):
                await bot._handle_cost(mock_update, MagicMock())

            msg = bot.send_message.call_args[0][1]
            assert "No API usage recorded" in msg
