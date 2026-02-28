"""
In-memory conversation history for Telegram and email interactions.

Stores recent messages per chat so the agent has context about what was
just discussed (e.g., the approval summary it sent). Entries expire after
TTL_MINUTES and the list is capped at MAX_HISTORY_MESSAGES.

Usage:
    from services.conversation_memory import conversation_memory

    # Store messages
    conversation_memory.add_message(chat_id, "user", "Make it shorter")
    conversation_memory.add_message(chat_id, "assistant", "Done, here's the updated version...")

    # Get history for Claude API
    history = conversation_memory.get_history(chat_id)
    # -> [{"role": "user", "content": "..."}, {"role": "assistant", "content": "..."}]

    # Inject approval context so the agent knows what summary it just sent
    conversation_memory.inject_approval_context(chat_id, meeting_id, title, preview)

    # Clear on approve/reject
    conversation_memory.clear(chat_id)
"""

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from config.settings import settings

logger = logging.getLogger(__name__)



@dataclass
class ConversationEntry:
    """A single message in the conversation history."""
    role: str           # "user" or "assistant"
    content: str
    timestamp: datetime = field(default_factory=datetime.now)


class ConversationMemory:
    """
    Per-chat conversation history store.

    Keys are strings — Telegram chat IDs or email addresses.
    Values are ordered lists of ConversationEntry (oldest first).
    """

    def __init__(
        self,
        max_messages: int | None = None,
        ttl_minutes: int | None = None,
    ):
        self.max_messages = max_messages or settings.CONVERSATION_MAX_MESSAGES
        self.ttl_minutes = ttl_minutes or settings.CONVERSATION_TTL_MINUTES
        self._histories: dict[str, list[ConversationEntry]] = {}

    def add_message(self, chat_id: str, role: str, content: str) -> None:
        """
        Append a message to the chat's history.

        Prunes expired entries and caps at max_messages.

        Args:
            chat_id: Chat identifier (Telegram chat ID or email address).
            role: "user" or "assistant".
            content: The message text.
        """
        chat_id = str(chat_id)
        if chat_id not in self._histories:
            self._histories[chat_id] = []

        self._histories[chat_id].append(
            ConversationEntry(role=role, content=content)
        )

        # Prune expired + cap
        self._prune(chat_id)

    def get_history(self, chat_id: str) -> list[dict]:
        """
        Get conversation history in Claude API message format.

        Returns only non-expired entries, up to max_messages.

        Args:
            chat_id: Chat identifier.

        Returns:
            List of {"role": "user"|"assistant", "content": "..."} dicts.
        """
        chat_id = str(chat_id)
        if chat_id not in self._histories:
            return []

        self._prune(chat_id)

        return [
            {"role": entry.role, "content": entry.content}
            for entry in self._histories[chat_id]
        ]

    def inject_approval_context(
        self,
        chat_id: str,
        meeting_id: str,
        title: str,
        preview: str,
    ) -> None:
        """
        Add a synthetic assistant message so the agent knows what it just sent.

        Called after an approval request is delivered. Creates context like:
        "I just sent you the summary for 'MVP Focus #2' for approval: ..."

        Args:
            chat_id: Chat identifier (Eyal's Telegram ID or email).
            meeting_id: UUID of the meeting.
            title: Meeting title.
            preview: Truncated summary text.
        """
        context_msg = (
            f"[I just sent you the summary for '{title}' (meeting ID: {meeting_id}) "
            f"for your approval. Here's what it contains:]\n\n{preview[:800]}"
        )
        self.add_message(str(chat_id), "assistant", context_msg)
        logger.debug(f"Injected approval context for chat {chat_id}: {title}")

    def clear(self, chat_id: str) -> None:
        """
        Wipe conversation history for a chat.

        Called after approve/reject to reset context.

        Args:
            chat_id: Chat identifier.
        """
        chat_id = str(chat_id)
        self._histories.pop(chat_id, None)
        logger.debug(f"Cleared conversation history for chat {chat_id}")

    def _prune(self, chat_id: str) -> None:
        """Remove expired entries and cap at max_messages."""
        entries = self._histories.get(chat_id, [])
        if not entries:
            return

        cutoff = datetime.now() - timedelta(minutes=self.ttl_minutes)

        # Remove expired
        entries = [e for e in entries if e.timestamp >= cutoff]

        # Cap at max_messages (keep most recent)
        if len(entries) > self.max_messages:
            entries = entries[-self.max_messages:]

        self._histories[chat_id] = entries


# Singleton instance
conversation_memory = ConversationMemory()
