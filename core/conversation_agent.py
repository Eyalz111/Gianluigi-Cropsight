"""
Conversation Agent — Handles dialogue with users via tool use.

The Conversation Agent is the primary handler for user interactions.
It receives classified intents from the Router and uses Claude (Sonnet)
with tools to answer questions, look up data, and perform actions.

The tool-use loop is extracted from the original GianluigiAgent.process_message().
Tools are executed via a dependency-injected tool_executor callable, so all
existing _tool_* methods stay on GianluigiAgent for test compatibility.
"""

import logging
from typing import Any, Callable, Awaitable

from config.settings import settings
from config.team import get_team_member
from core.llm import call_llm_with_tools, get_client
from core.system_prompt import get_system_prompt
from core.tools import TOOL_DEFINITIONS

logger = logging.getLogger(__name__)


class ConversationAgent:
    """
    Dialogue handler with tool-use capabilities.

    Uses Claude Sonnet to converse with users, calling tools as needed
    to retrieve data or perform actions. The tool loop runs up to
    max_tool_iterations before stopping.
    """

    def __init__(self, tool_executor: Callable[[str, dict], Awaitable[Any]]):
        """
        Initialize the Conversation Agent.

        Args:
            tool_executor: Async callable that executes tools by name.
                Signature: async def(tool_name: str, tool_input: dict) -> Any
                Typically GianluigiAgent._execute_tool_call.
        """
        self.model = settings.model_agent
        self.system_prompt = get_system_prompt()
        self.system_prompt_cached = [{
            "type": "text",
            "text": self.system_prompt,
            "cache_control": {"type": "ephemeral"},
        }]
        self.tools = TOOL_DEFINITIONS
        self.tool_executor = tool_executor
        self.max_tool_iterations = 10

    async def respond(
        self,
        user_message: str,
        user_id: str,
        conversation_history: list | None = None,
        intent: str | None = None,
        extra_context: str = "",
    ) -> dict:
        """
        Process a user message and return a response.

        Runs the tool-use loop: sends the message to Claude, executes
        any requested tools, and continues until Claude produces a
        final text response or max iterations are reached.

        Args:
            user_message: The message from the user.
            user_id: User identifier (eyal, roye, paolo, yoram).
            conversation_history: Optional previous messages for context.
            intent: Classified intent from Router (for logging/future use).
            extra_context: Pre-fetched context from query routing.

        Returns:
            Dict with "response" (str), "actions" (list), "sources" (list).
        """
        # Build messages array
        messages = []

        if conversation_history:
            messages.extend(conversation_history)

        # Add user context
        team_member = get_team_member(user_id)
        user_context = ""
        if team_member:
            user_context = f"[Message from {team_member['name']} ({team_member['role']})]"

        # Build the full user message with any pre-fetched context
        full_message = user_message
        if user_context:
            full_message = f"{user_context}\n\n{user_message}"
        if extra_context:
            full_message = f"{full_message}\n\n{extra_context}"

        messages.append({
            "role": "user",
            "content": full_message,
        })

        # Track actions taken
        actions_taken = []
        sources_cited = []
        final_text = ""

        # Tool use loop
        iterations = 0
        while iterations < self.max_tool_iterations:
            iterations += 1

            # Call Claude API via centralized helper
            response = call_llm_with_tools(
                messages=messages,
                model=self.model,
                max_tokens=4096,
                system=self.system_prompt_cached,
                tools=self.tools,
                call_site="conversation_agent",
            )

            # Check stop reason
            if response.stop_reason == "end_turn":
                final_text = self._extract_text_response(response)
                break

            elif response.stop_reason == "tool_use":
                tool_results = []

                for content_block in response.content:
                    if content_block.type == "tool_use":
                        tool_name = content_block.name
                        tool_input = content_block.input
                        tool_id = content_block.id

                        logger.info(f"Executing tool: {tool_name}")

                        try:
                            result = await self.tool_executor(tool_name, tool_input)
                            tool_results.append({
                                "type": "tool_result",
                                "tool_use_id": tool_id,
                                "content": str(result),
                            })
                            actions_taken.append({
                                "tool": tool_name,
                                "input": tool_input,
                                "success": True,
                            })
                        except Exception as e:
                            logger.error(f"Tool execution error: {e}")
                            tool_results.append({
                                "type": "tool_result",
                                "tool_use_id": tool_id,
                                "content": f"Error: {str(e)}",
                                "is_error": True,
                            })
                            actions_taken.append({
                                "tool": tool_name,
                                "input": tool_input,
                                "success": False,
                                "error": str(e),
                            })

                # Add assistant response and tool results to messages
                messages.append({"role": "assistant", "content": response.content})
                messages.append({"role": "user", "content": tool_results})

            else:
                logger.warning(f"Unexpected stop reason: {response.stop_reason}")
                final_text = self._extract_text_response(response)
                break

        else:
            # Max iterations reached
            logger.warning("Max tool iterations reached")
            final_text = "I'm sorry, I wasn't able to complete your request. Please try rephrasing."

        return {
            "response": final_text,
            "actions": actions_taken,
            "sources": sources_cited,
        }

    def _extract_text_response(self, response) -> str:
        """Extract text content from Claude response."""
        text_parts = []
        for block in response.content:
            if hasattr(block, "text"):
                text_parts.append(block.text)
        return "\n".join(text_parts)
