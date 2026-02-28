"""
Tests for conversation memory (services/conversation_memory.py).

Covers:
1. add_message + get_history basics
2. TTL expiry
3. Max messages cap
4. inject_approval_context
5. clear
6. Multi-chat isolation
7. String coercion of chat_id (int vs str)
8. Empty history returns []
9. Mixed roles preserved in order
10. Prune on get_history
"""

import pytest
from datetime import datetime, timedelta
from unittest.mock import patch

from services.conversation_memory import ConversationMemory, ConversationEntry


class TestAddAndGetHistory:
    """Basic add/get operations."""

    def test_add_and_get_single_message(self):
        mem = ConversationMemory()
        mem.add_message("chat1", "user", "Hello")
        history = mem.get_history("chat1")
        assert len(history) == 1
        assert history[0] == {"role": "user", "content": "Hello"}

    def test_get_empty_history_returns_empty_list(self):
        mem = ConversationMemory()
        assert mem.get_history("nonexistent") == []

    def test_multiple_messages_preserved_in_order(self):
        mem = ConversationMemory()
        mem.add_message("chat1", "user", "Q1")
        mem.add_message("chat1", "assistant", "A1")
        mem.add_message("chat1", "user", "Q2")

        history = mem.get_history("chat1")
        assert len(history) == 3
        assert history[0]["role"] == "user"
        assert history[0]["content"] == "Q1"
        assert history[1]["role"] == "assistant"
        assert history[1]["content"] == "A1"
        assert history[2]["role"] == "user"
        assert history[2]["content"] == "Q2"

    def test_chat_id_coerced_to_string(self):
        """int chat_id should match str chat_id."""
        mem = ConversationMemory()
        mem.add_message(12345, "user", "Hi")
        history = mem.get_history("12345")
        assert len(history) == 1
        assert history[0]["content"] == "Hi"


class TestTTLExpiry:
    """Messages older than TTL should be pruned."""

    def test_expired_messages_pruned_on_get(self):
        mem = ConversationMemory(ttl_minutes=5)
        # Add an old message by backdating its timestamp
        old_entry = ConversationEntry(
            role="user",
            content="old message",
            timestamp=datetime.now() - timedelta(minutes=10),
        )
        mem._histories["chat1"] = [old_entry]

        # Add a fresh message
        mem.add_message("chat1", "user", "new message")

        history = mem.get_history("chat1")
        assert len(history) == 1
        assert history[0]["content"] == "new message"

    def test_fresh_messages_not_pruned(self):
        mem = ConversationMemory(ttl_minutes=30)
        mem.add_message("chat1", "user", "recent")
        history = mem.get_history("chat1")
        assert len(history) == 1


class TestMaxMessages:
    """Excess messages should be trimmed (oldest dropped)."""

    def test_cap_at_max_messages(self):
        mem = ConversationMemory(max_messages=3)
        for i in range(5):
            mem.add_message("chat1", "user", f"msg-{i}")

        history = mem.get_history("chat1")
        assert len(history) == 3
        # Should keep the 3 most recent
        assert history[0]["content"] == "msg-2"
        assert history[1]["content"] == "msg-3"
        assert history[2]["content"] == "msg-4"


class TestInjectApprovalContext:
    """inject_approval_context adds a synthetic assistant message."""

    def test_inject_creates_assistant_message(self):
        mem = ConversationMemory()
        mem.inject_approval_context(
            chat_id="chat1",
            meeting_id="uuid-123",
            title="MVP Focus #2",
            preview="Key decisions were made about...",
        )

        history = mem.get_history("chat1")
        assert len(history) == 1
        assert history[0]["role"] == "assistant"
        assert "MVP Focus #2" in history[0]["content"]
        assert "uuid-123" in history[0]["content"]
        assert "Key decisions" in history[0]["content"]

    def test_inject_truncates_long_preview(self):
        mem = ConversationMemory()
        long_preview = "x" * 2000
        mem.inject_approval_context("chat1", "id", "Title", long_preview)

        history = mem.get_history("chat1")
        # Preview should be truncated to 800 chars
        assert len(history[0]["content"]) < 1000


class TestClear:
    """clear() should wipe the chat's history."""

    def test_clear_removes_all_messages(self):
        mem = ConversationMemory()
        mem.add_message("chat1", "user", "msg1")
        mem.add_message("chat1", "assistant", "msg2")
        mem.clear("chat1")
        assert mem.get_history("chat1") == []

    def test_clear_nonexistent_chat_is_noop(self):
        mem = ConversationMemory()
        mem.clear("nonexistent")  # Should not raise


class TestMultiChatIsolation:
    """Different chats should not see each other's messages."""

    def test_separate_histories(self):
        mem = ConversationMemory()
        mem.add_message("chat1", "user", "msg-A")
        mem.add_message("chat2", "user", "msg-B")

        h1 = mem.get_history("chat1")
        h2 = mem.get_history("chat2")

        assert len(h1) == 1
        assert h1[0]["content"] == "msg-A"
        assert len(h2) == 1
        assert h2[0]["content"] == "msg-B"

    def test_clear_one_does_not_affect_other(self):
        mem = ConversationMemory()
        mem.add_message("chat1", "user", "msg-A")
        mem.add_message("chat2", "user", "msg-B")

        mem.clear("chat1")

        assert mem.get_history("chat1") == []
        assert len(mem.get_history("chat2")) == 1
