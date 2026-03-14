"""
Router Agent — Intent classification for incoming messages.

Uses Haiku (model_simple) to classify user messages into intent
categories. The Router is the first step in the multi-agent pipeline:
Router → Conversation Agent → (Analyst | Operator) as needed.

Phase 1: Classifies intent but all intents route to Conversation Agent.
Phase 3+: Intent-based branching (debrief → DebriefFlow, etc.)
"""

import logging

from config.settings import settings
from core.llm import call_llm

logger = logging.getLogger(__name__)

# All valid intent types
VALID_INTENTS = {
    "question",
    "task_update",
    "information_injection",
    "gantt_request",
    "debrief",
    "approval_response",
    "weekly_review",
    "meeting_prep_request",
    "ambiguous",
}

# Conversation mode shortcuts — skip LLM when mode is set
_MODE_TO_INTENT = {
    "debrief": "debrief",
    "weekly_review": "weekly_review",
    "approval_review": "approval_response",
}

CLASSIFICATION_PROMPT = """Classify the user's message into exactly ONE intent category.

Categories:
- question: Asking about past meetings, tasks, decisions, or any knowledge query
- task_update: Reporting progress on a task, marking something done, or updating status
- information_injection: Sharing new information not from a meeting (e.g., "FYI, we signed the deal")
- gantt_request: Asking to view, update, or modify the operational Gantt chart
- debrief: Starting or continuing an end-of-day debrief session
- approval_response: Approving, rejecting, or commenting on a pending approval
- weekly_review: Starting or continuing a weekly review session
- meeting_prep_request: Asking for preparation materials for an upcoming meeting
- ambiguous: Cannot determine intent from the message

Reply with ONLY the category name, nothing else.

Message: {message}"""


async def classify_intent(
    message: str,
    conversation_mode: str | None = None,
    user_id: str | None = None,
) -> str:
    """
    Classify a user message into an intent category.

    Args:
        message: The user's message text.
        conversation_mode: Active mode (debrief/weekly_review/approval_review).
            If set, skips LLM and returns the matching intent directly.
        user_id: User identifier (for future per-user routing).

    Returns:
        Intent string: one of VALID_INTENTS.
    """
    # Shortcut: if conversation mode is active, return immediately
    if conversation_mode and conversation_mode in _MODE_TO_INTENT:
        intent = _MODE_TO_INTENT[conversation_mode]
        logger.debug(f"Router shortcut: mode={conversation_mode} → {intent}")
        return intent

    # LLM classification
    try:
        prompt = CLASSIFICATION_PROMPT.format(message=message[:500])
        text, _usage = call_llm(
            prompt=prompt,
            model=settings.model_simple,
            max_tokens=20,
            call_site="router",
        )

        intent = text.strip().lower().replace(" ", "_")

        if intent in VALID_INTENTS:
            return intent

        logger.warning(f"Router returned invalid intent '{intent}', falling back to 'question'")
        return "question"

    except Exception as e:
        logger.error(f"Router classification failed: {e}, falling back to 'question'")
        return "question"
