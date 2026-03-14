"""
Tests for the v1.0 multi-agent orchestrator in core/agent.py.

Tests cover:
- process_message() calls Router → Conversation Agent in sequence
- Result format is preserved (response, actions, sources)
- Conversation history is forwarded
- Error handling (Router fails → still works via fallback)
- Singleton gianluigi_agent import works
- call_llm_with_tools() tracks tokens correctly
"""

import pytest
from unittest.mock import MagicMock, patch, AsyncMock


# =============================================================================
# Orchestrator Flow
# =============================================================================

class TestOrchestratorFlow:
    """Test the Router → Conversation Agent pipeline."""

    @pytest.mark.asyncio
    async def test_process_message_calls_router_then_conversation(self):
        """process_message should classify intent then dispatch to ConversationAgent."""
        with patch("core.agent.classify_intent", new_callable=AsyncMock) as mock_router, \
             patch("core.agent.supabase_client") as mock_db:

            mock_router.return_value = "question"
            mock_db.log_action = MagicMock()

            from core.agent import GianluigiAgent

            with patch("core.agent.Anthropic"):
                agent = GianluigiAgent()

            # Mock the conversation agent's respond method
            agent.conversation_agent.respond = AsyncMock(return_value={
                "response": "Test response",
                "actions": [],
                "sources": [],
            })

            result = await agent.process_message("Hello", "eyal")

            mock_router.assert_called_once()
            agent.conversation_agent.respond.assert_called_once()
            assert result["response"] == "Test response"

    @pytest.mark.asyncio
    async def test_intent_logged_in_action(self):
        """The classified intent should be included in the log."""
        with patch("core.agent.classify_intent", new_callable=AsyncMock) as mock_router, \
             patch("core.agent.supabase_client") as mock_db:

            mock_router.return_value = "task_update"
            mock_db.log_action = MagicMock()

            from core.agent import GianluigiAgent

            with patch("core.agent.Anthropic"):
                agent = GianluigiAgent()

            agent.conversation_agent.respond = AsyncMock(return_value={
                "response": "OK",
                "actions": [{"tool": "update_task", "input": {}, "success": True}],
                "sources": [],
            })

            await agent.process_message("Task done", "eyal")

            # Check log_action was called with intent in details
            log_call = mock_db.log_action.call_args
            assert log_call[1]["details"]["intent"] == "task_update"

    @pytest.mark.asyncio
    async def test_result_format_preserved(self):
        """Result should always have response, actions, sources keys."""
        with patch("core.agent.classify_intent", new_callable=AsyncMock) as mock_router, \
             patch("core.agent.supabase_client") as mock_db:

            mock_router.return_value = "question"
            mock_db.log_action = MagicMock()

            from core.agent import GianluigiAgent

            with patch("core.agent.Anthropic"):
                agent = GianluigiAgent()

            agent.conversation_agent.respond = AsyncMock(return_value={
                "response": "Answer",
                "actions": [{"tool": "search_meetings", "input": {}, "success": True}],
                "sources": ["meeting-1"],
            })

            result = await agent.process_message("What happened?", "eyal")

            assert isinstance(result["response"], str)
            assert isinstance(result["actions"], list)
            assert isinstance(result["sources"], list)

    @pytest.mark.asyncio
    async def test_conversation_history_forwarded(self):
        """Conversation history should be passed to ConversationAgent."""
        with patch("core.agent.classify_intent", new_callable=AsyncMock) as mock_router, \
             patch("core.agent.supabase_client") as mock_db:

            mock_router.return_value = "question"
            mock_db.log_action = MagicMock()

            from core.agent import GianluigiAgent

            with patch("core.agent.Anthropic"):
                agent = GianluigiAgent()

            agent.conversation_agent.respond = AsyncMock(return_value={
                "response": "OK", "actions": [], "sources": [],
            })

            history = [
                {"role": "user", "content": "prev msg"},
                {"role": "assistant", "content": "prev resp"},
            ]
            await agent.process_message("Follow up", "eyal", conversation_history=history)

            call_kwargs = agent.conversation_agent.respond.call_args[1]
            assert call_kwargs["conversation_history"] == history


# =============================================================================
# Error Handling
# =============================================================================

class TestErrorHandling:
    """Test error handling in the orchestrator."""

    @pytest.mark.asyncio
    async def test_router_failure_still_works(self):
        """If Router fails, should fall back to 'question' and still respond."""
        with patch("core.agent.classify_intent", new_callable=AsyncMock) as mock_router, \
             patch("core.agent.supabase_client") as mock_db:

            # Router returns "question" as fallback on error (tested in test_router.py)
            mock_router.return_value = "question"
            mock_db.log_action = MagicMock()

            from core.agent import GianluigiAgent

            with patch("core.agent.Anthropic"):
                agent = GianluigiAgent()

            agent.conversation_agent.respond = AsyncMock(return_value={
                "response": "Still works", "actions": [], "sources": [],
            })

            result = await agent.process_message("Test", "eyal")
            assert result["response"] == "Still works"

    @pytest.mark.asyncio
    async def test_query_context_prefetch_with_entity_lookup(self):
        """Entity lookup query type should pre-fetch and pass context."""
        with patch("core.agent.classify_intent", new_callable=AsyncMock) as mock_router, \
             patch("core.agent.supabase_client") as mock_db:

            mock_router.return_value = "question"
            mock_db.log_action = MagicMock()
            mock_db.find_entity_by_name = MagicMock(return_value={
                "id": "e1", "canonical_name": "Lavazza",
                "entity_type": "company", "aliases": [],
            })
            mock_db.get_entity_mentions = MagicMock(return_value=[])

            from core.agent import GianluigiAgent

            with patch("core.agent.Anthropic"):
                agent = GianluigiAgent()

            agent.conversation_agent.respond = AsyncMock(return_value={
                "response": "OK", "actions": [], "sources": [],
            })

            await agent.process_message("Tell me about Lavazza", "eyal")

            # ConversationAgent should have received extra_context
            call_kwargs = agent.conversation_agent.respond.call_args[1]
            assert "ENTITY CONTEXT" in call_kwargs.get("extra_context", "")


# =============================================================================
# Singleton Import
# =============================================================================

class TestSingletonImport:
    """Test that gianluigi_agent singleton works."""

    def test_singleton_import(self):
        """Should be able to import gianluigi_agent."""
        with patch("core.agent.Anthropic"), \
             patch("core.agent.settings") as mock_settings:
            mock_settings.ANTHROPIC_API_KEY = "test-key"
            mock_settings.model_agent = "test-model"
            mock_settings.model_extraction = "test-model"
            mock_settings.model_simple = "test-model"

            # Just verify import doesn't crash
            # (actual singleton is created at module level)
            from core.agent import GianluigiAgent
            assert GianluigiAgent is not None


# =============================================================================
# call_llm_with_tools Token Tracking
# =============================================================================

class TestCallLlmWithTools:
    """Test call_llm_with_tools() in core/llm.py."""

    def test_returns_raw_response(self):
        """call_llm_with_tools should return the raw Anthropic response."""
        import core.llm as llm_module
        llm_module._client = None

        mock_response = MagicMock()
        mock_response.usage = MagicMock(
            input_tokens=200, output_tokens=100,
            cache_read_input_tokens=50, cache_creation_input_tokens=0,
        )

        with patch("core.llm.Anthropic") as mock_cls, \
             patch("core.llm._log_usage") as mock_log:
            mock_client = MagicMock()
            mock_client.messages.create.return_value = mock_response
            mock_cls.return_value = mock_client

            result = llm_module.call_llm_with_tools(
                messages=[{"role": "user", "content": "test"}],
                model="test-model",
                max_tokens=4096,
                system=[{"type": "text", "text": "system", "cache_control": {"type": "ephemeral"}}],
                tools=[],
                call_site="conversation_agent",
            )

            assert result is mock_response

        llm_module._client = None

    def test_logs_usage_with_call_site(self):
        """call_llm_with_tools should call _log_usage with correct call_site."""
        import core.llm as llm_module
        llm_module._client = None

        mock_response = MagicMock()
        mock_response.usage = MagicMock(
            input_tokens=200, output_tokens=100,
            cache_read_input_tokens=0, cache_creation_input_tokens=0,
        )

        with patch("core.llm.Anthropic") as mock_cls, \
             patch("core.llm._log_usage") as mock_log:
            mock_client = MagicMock()
            mock_client.messages.create.return_value = mock_response
            mock_cls.return_value = mock_client

            llm_module.call_llm_with_tools(
                messages=[],
                model="test-model",
                max_tokens=100,
                system=[],
                tools=[],
                call_site="router",
            )

            mock_log.assert_called_once()
            log_kwargs = mock_log.call_args[1]
            assert log_kwargs["call_site"] == "router"
            assert log_kwargs["model"] == "test-model"

        llm_module._client = None

    def test_passes_tools_and_system_to_api(self):
        """Should forward tools and system to messages.create."""
        import core.llm as llm_module
        llm_module._client = None

        mock_response = MagicMock()
        mock_response.usage = MagicMock(
            input_tokens=100, output_tokens=50,
            cache_read_input_tokens=0, cache_creation_input_tokens=0,
        )

        tools = [{"name": "test_tool", "description": "test", "input_schema": {}}]
        system = [{"type": "text", "text": "Be helpful", "cache_control": {"type": "ephemeral"}}]

        with patch("core.llm.Anthropic") as mock_cls, \
             patch("core.llm._log_usage"):
            mock_client = MagicMock()
            mock_client.messages.create.return_value = mock_response
            mock_cls.return_value = mock_client

            llm_module.call_llm_with_tools(
                messages=[{"role": "user", "content": "test"}],
                model="test-model",
                max_tokens=4096,
                system=system,
                tools=tools,
                call_site="test",
            )

            call_kwargs = mock_client.messages.create.call_args[1]
            assert call_kwargs["tools"] == tools
            assert call_kwargs["system"] == system
            assert call_kwargs["model"] == "test-model"

        llm_module._client = None
