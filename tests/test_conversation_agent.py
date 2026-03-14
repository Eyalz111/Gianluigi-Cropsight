"""
Tests for core/conversation_agent.py — Conversation Agent.

Tests cover:
- respond() returns correct {"response", "actions", "sources"} format
- Tool-use loop works (mock tool_use response → tool execution → continue)
- Max iterations safety
- Intent and extra_context passed to Claude correctly
- Error handling in tool execution
"""

import pytest
from unittest.mock import MagicMock, patch, AsyncMock


def _make_text_response(text="Hello from Gianluigi"):
    """Create a mock Claude response with end_turn and text."""
    mock_block = MagicMock()
    mock_block.type = "text"
    mock_block.text = text

    mock_response = MagicMock()
    mock_response.stop_reason = "end_turn"
    mock_response.content = [mock_block]
    mock_response.usage = MagicMock(
        input_tokens=100, output_tokens=50,
        cache_read_input_tokens=0, cache_creation_input_tokens=0,
    )
    return mock_response


def _make_tool_use_response(tool_name="search_meetings", tool_input=None):
    """Create a mock Claude response with a tool_use block."""
    tool_block = MagicMock()
    tool_block.type = "tool_use"
    tool_block.name = tool_name
    tool_block.input = tool_input or {"query": "test"}
    tool_block.id = "tool_123"

    mock_response = MagicMock()
    mock_response.stop_reason = "tool_use"
    mock_response.content = [tool_block]
    mock_response.usage = MagicMock(
        input_tokens=100, output_tokens=50,
        cache_read_input_tokens=0, cache_creation_input_tokens=0,
    )
    return mock_response


# =============================================================================
# Basic Response Format
# =============================================================================

class TestRespondFormat:
    """Test that respond() returns the expected format."""

    @pytest.mark.asyncio
    async def test_returns_response_dict(self):
        """respond() should return dict with response, actions, sources."""
        mock_executor = AsyncMock()

        with patch("core.conversation_agent.call_llm_with_tools") as mock_llm, \
             patch("core.conversation_agent.get_team_member", return_value=None):
            mock_llm.return_value = _make_text_response("Test response")

            from core.conversation_agent import ConversationAgent
            agent = ConversationAgent(tool_executor=mock_executor)
            result = await agent.respond(
                user_message="Hello",
                user_id="eyal",
            )

            assert "response" in result
            assert "actions" in result
            assert "sources" in result
            assert result["response"] == "Test response"
            assert result["actions"] == []
            assert result["sources"] == []

    @pytest.mark.asyncio
    async def test_response_with_no_tools(self):
        """Simple question should return text without tool calls."""
        mock_executor = AsyncMock()

        with patch("core.conversation_agent.call_llm_with_tools") as mock_llm, \
             patch("core.conversation_agent.get_team_member", return_value=None):
            mock_llm.return_value = _make_text_response("I'm doing well!")

            from core.conversation_agent import ConversationAgent
            agent = ConversationAgent(tool_executor=mock_executor)
            result = await agent.respond("How are you?", "eyal")

            assert result["response"] == "I'm doing well!"
            mock_executor.assert_not_called()


# =============================================================================
# Tool Use Loop
# =============================================================================

class TestToolUseLoop:
    """Test the tool-use loop functionality."""

    @pytest.mark.asyncio
    async def test_single_tool_call(self):
        """Should execute tool and return final response."""
        mock_executor = AsyncMock(return_value={"results": [], "count": 0})

        with patch("core.conversation_agent.call_llm_with_tools") as mock_llm, \
             patch("core.conversation_agent.get_team_member", return_value=None):
            # First call: tool_use, Second call: end_turn
            mock_llm.side_effect = [
                _make_tool_use_response("search_meetings", {"query": "Moldova"}),
                _make_text_response("No results found for Moldova."),
            ]

            from core.conversation_agent import ConversationAgent
            agent = ConversationAgent(tool_executor=mock_executor)
            result = await agent.respond("Tell me about Moldova", "eyal")

            assert result["response"] == "No results found for Moldova."
            assert len(result["actions"]) == 1
            assert result["actions"][0]["tool"] == "search_meetings"
            assert result["actions"][0]["success"] is True
            mock_executor.assert_called_once_with("search_meetings", {"query": "Moldova"})

    @pytest.mark.asyncio
    async def test_multiple_tool_calls(self):
        """Should handle multiple sequential tool calls."""
        mock_executor = AsyncMock(return_value={"results": []})

        with patch("core.conversation_agent.call_llm_with_tools") as mock_llm, \
             patch("core.conversation_agent.get_team_member", return_value=None):
            mock_llm.side_effect = [
                _make_tool_use_response("search_meetings", {"query": "Q1"}),
                _make_tool_use_response("get_tasks", {"assignee": "eyal"}),
                _make_text_response("Here's what I found."),
            ]

            from core.conversation_agent import ConversationAgent
            agent = ConversationAgent(tool_executor=mock_executor)
            result = await agent.respond("What's my status?", "eyal")

            assert len(result["actions"]) == 2
            assert result["response"] == "Here's what I found."

    @pytest.mark.asyncio
    async def test_tool_execution_error(self):
        """Should handle tool execution errors gracefully."""
        mock_executor = AsyncMock(side_effect=ValueError("Tool failed"))

        with patch("core.conversation_agent.call_llm_with_tools") as mock_llm, \
             patch("core.conversation_agent.get_team_member", return_value=None):
            mock_llm.side_effect = [
                _make_tool_use_response("search_meetings"),
                _make_text_response("Sorry, I encountered an error."),
            ]

            from core.conversation_agent import ConversationAgent
            agent = ConversationAgent(tool_executor=mock_executor)
            result = await agent.respond("Search for things", "eyal")

            assert len(result["actions"]) == 1
            assert result["actions"][0]["success"] is False
            assert "Tool failed" in result["actions"][0]["error"]

    @pytest.mark.asyncio
    async def test_max_iterations_safety(self):
        """Should stop after max_tool_iterations to prevent infinite loops."""
        mock_executor = AsyncMock(return_value={"results": []})

        with patch("core.conversation_agent.call_llm_with_tools") as mock_llm, \
             patch("core.conversation_agent.get_team_member", return_value=None):
            # Always return tool_use — never ends
            mock_llm.return_value = _make_tool_use_response()

            from core.conversation_agent import ConversationAgent
            agent = ConversationAgent(tool_executor=mock_executor)
            agent.max_tool_iterations = 3  # Lower for testing

            result = await agent.respond("Infinite loop test", "eyal")

            assert "sorry" in result["response"].lower() or "unable" in result["response"].lower()
            assert mock_llm.call_count == 3


# =============================================================================
# Context Injection
# =============================================================================

class TestContextInjection:
    """Test that user context and extra_context are injected correctly."""

    @pytest.mark.asyncio
    async def test_team_member_context_injected(self):
        """Should add [Message from Name (Role)] prefix."""
        mock_executor = AsyncMock()

        with patch("core.conversation_agent.call_llm_with_tools") as mock_llm, \
             patch("core.conversation_agent.get_team_member") as mock_team:
            mock_team.return_value = {"name": "Eyal Zror", "role": "CEO"}
            mock_llm.return_value = _make_text_response("OK")

            from core.conversation_agent import ConversationAgent
            agent = ConversationAgent(tool_executor=mock_executor)
            await agent.respond("Hello", "eyal")

            # Check the messages passed to LLM
            call_kwargs = mock_llm.call_args[1]
            messages = call_kwargs["messages"]
            user_msg = messages[-1]["content"]
            assert "[Message from Eyal Zror (CEO)]" in user_msg

    @pytest.mark.asyncio
    async def test_extra_context_appended(self):
        """Should append extra_context to the user message."""
        mock_executor = AsyncMock()

        with patch("core.conversation_agent.call_llm_with_tools") as mock_llm, \
             patch("core.conversation_agent.get_team_member", return_value=None):
            mock_llm.return_value = _make_text_response("OK")

            from core.conversation_agent import ConversationAgent
            agent = ConversationAgent(tool_executor=mock_executor)
            await agent.respond(
                "What about task X?", "eyal",
                extra_context="[TASK STATUS CONTEXT]\n- Task X (pending)"
            )

            call_kwargs = mock_llm.call_args[1]
            messages = call_kwargs["messages"]
            user_msg = messages[-1]["content"]
            assert "[TASK STATUS CONTEXT]" in user_msg
            assert "Task X (pending)" in user_msg

    @pytest.mark.asyncio
    async def test_conversation_history_prepended(self):
        """Should prepend conversation_history to messages."""
        mock_executor = AsyncMock()

        with patch("core.conversation_agent.call_llm_with_tools") as mock_llm, \
             patch("core.conversation_agent.get_team_member", return_value=None):
            mock_llm.return_value = _make_text_response("OK")

            history = [
                {"role": "user", "content": "Previous message"},
                {"role": "assistant", "content": "Previous response"},
            ]

            from core.conversation_agent import ConversationAgent
            agent = ConversationAgent(tool_executor=mock_executor)
            await agent.respond("Follow-up", "eyal", conversation_history=history)

            call_kwargs = mock_llm.call_args[1]
            messages = call_kwargs["messages"]
            assert len(messages) == 3  # 2 history + 1 new
            assert messages[0]["content"] == "Previous message"

    @pytest.mark.asyncio
    async def test_uses_call_site_conversation_agent(self):
        """Should pass call_site='conversation_agent' to call_llm_with_tools."""
        mock_executor = AsyncMock()

        with patch("core.conversation_agent.call_llm_with_tools") as mock_llm, \
             patch("core.conversation_agent.get_team_member", return_value=None):
            mock_llm.return_value = _make_text_response("OK")

            from core.conversation_agent import ConversationAgent
            agent = ConversationAgent(tool_executor=mock_executor)
            await agent.respond("Test", "eyal")

            call_kwargs = mock_llm.call_args[1]
            assert call_kwargs["call_site"] == "conversation_agent"

    @pytest.mark.asyncio
    async def test_unexpected_stop_reason(self):
        """Should handle unexpected stop_reason gracefully."""
        mock_executor = AsyncMock()

        with patch("core.conversation_agent.call_llm_with_tools") as mock_llm, \
             patch("core.conversation_agent.get_team_member", return_value=None):
            resp = _make_text_response("Partial response")
            resp.stop_reason = "max_tokens"
            mock_llm.return_value = resp

            from core.conversation_agent import ConversationAgent
            agent = ConversationAgent(tool_executor=mock_executor)
            result = await agent.respond("Test", "eyal")

            assert result["response"] == "Partial response"
