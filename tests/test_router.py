"""
Tests for core/router.py — Router Agent intent classification.

Tests cover:
- Each of 9 intent categories with example messages
- Conversation mode shortcuts (skip LLM)
- Fallback to "question" on LLM failure
- Edge cases: empty message, Hebrew text, emoji-only
"""

import pytest
from unittest.mock import patch, MagicMock


# =============================================================================
# Conversation Mode Shortcuts
# =============================================================================

class TestConversationModeShortcuts:
    """When conversation_mode is set, Router should skip LLM."""

    @pytest.mark.asyncio
    async def test_debrief_mode_returns_debrief(self):
        """Debrief mode should return 'debrief' without LLM call."""
        from core.router import classify_intent

        with patch("core.router.call_llm") as mock_llm:
            result = await classify_intent("anything", conversation_mode="debrief")
            assert result == "debrief"
            mock_llm.assert_not_called()

    @pytest.mark.asyncio
    async def test_weekly_review_mode_returns_weekly_review(self):
        """Weekly review mode should return 'weekly_review' without LLM call."""
        from core.router import classify_intent

        with patch("core.router.call_llm") as mock_llm:
            result = await classify_intent("anything", conversation_mode="weekly_review")
            assert result == "weekly_review"
            mock_llm.assert_not_called()

    @pytest.mark.asyncio
    async def test_approval_review_mode_returns_approval_response(self):
        """Approval review mode should return 'approval_response' without LLM call."""
        from core.router import classify_intent

        with patch("core.router.call_llm") as mock_llm:
            result = await classify_intent("looks good", conversation_mode="approval_review")
            assert result == "approval_response"
            mock_llm.assert_not_called()

    @pytest.mark.asyncio
    async def test_none_mode_calls_llm(self):
        """When mode is None, Router should call LLM."""
        from core.router import classify_intent

        with patch("core.router.call_llm") as mock_llm:
            mock_llm.return_value = ("question", {"input_tokens": 10, "output_tokens": 1,
                                                    "cache_read_input_tokens": 0,
                                                    "cache_creation_input_tokens": 0})
            result = await classify_intent("What happened in the last meeting?")
            assert result == "question"
            mock_llm.assert_called_once()


# =============================================================================
# Intent Classification via LLM
# =============================================================================

class TestIntentClassification:
    """Test that LLM responses map to correct intents."""

    def _mock_llm_response(self, intent_text):
        return (intent_text, {"input_tokens": 10, "output_tokens": 1,
                               "cache_read_input_tokens": 0,
                               "cache_creation_input_tokens": 0})

    @pytest.mark.asyncio
    async def test_question_intent(self):
        from core.router import classify_intent
        with patch("core.router.call_llm") as mock_llm:
            mock_llm.return_value = self._mock_llm_response("question")
            result = await classify_intent("What did we decide about cloud providers?")
            assert result == "question"

    @pytest.mark.asyncio
    async def test_task_update_intent(self):
        from core.router import classify_intent
        with patch("core.router.call_llm") as mock_llm:
            mock_llm.return_value = self._mock_llm_response("task_update")
            result = await classify_intent("I finished the MVP requirements doc")
            assert result == "task_update"

    @pytest.mark.asyncio
    async def test_information_injection_intent(self):
        from core.router import classify_intent
        with patch("core.router.call_llm") as mock_llm:
            mock_llm.return_value = self._mock_llm_response("information_injection")
            result = await classify_intent("FYI we signed the deal with Lavazza")
            assert result == "information_injection"

    @pytest.mark.asyncio
    async def test_gantt_request_intent(self):
        from core.router import classify_intent
        with patch("core.router.call_llm") as mock_llm:
            mock_llm.return_value = self._mock_llm_response("gantt_request")
            result = await classify_intent("Update the Gantt chart for Q2")
            assert result == "gantt_request"

    @pytest.mark.asyncio
    async def test_debrief_intent(self):
        from core.router import classify_intent
        with patch("core.router.call_llm") as mock_llm:
            mock_llm.return_value = self._mock_llm_response("debrief")
            result = await classify_intent("Let's do my end of day debrief")
            assert result == "debrief"

    @pytest.mark.asyncio
    async def test_approval_response_intent(self):
        from core.router import classify_intent
        with patch("core.router.call_llm") as mock_llm:
            mock_llm.return_value = self._mock_llm_response("approval_response")
            result = await classify_intent("Approve the summary")
            assert result == "approval_response"

    @pytest.mark.asyncio
    async def test_weekly_review_intent(self):
        from core.router import classify_intent
        with patch("core.router.call_llm") as mock_llm:
            mock_llm.return_value = self._mock_llm_response("weekly_review")
            result = await classify_intent("Start the weekly review")
            assert result == "weekly_review"

    @pytest.mark.asyncio
    async def test_meeting_prep_request_intent(self):
        from core.router import classify_intent
        with patch("core.router.call_llm") as mock_llm:
            mock_llm.return_value = self._mock_llm_response("meeting_prep_request")
            result = await classify_intent("Prepare me for the investor meeting tomorrow")
            assert result == "meeting_prep_request"

    @pytest.mark.asyncio
    async def test_ambiguous_intent(self):
        from core.router import classify_intent
        with patch("core.router.call_llm") as mock_llm:
            mock_llm.return_value = self._mock_llm_response("ambiguous")
            result = await classify_intent("hmm")
            assert result == "ambiguous"


# =============================================================================
# Fallback and Edge Cases
# =============================================================================

class TestFallbackBehavior:
    """Test fallback behavior on errors and edge cases."""

    @pytest.mark.asyncio
    async def test_llm_failure_falls_back_to_question(self):
        """If LLM call fails, should return 'question' as safe default."""
        from core.router import classify_intent
        with patch("core.router.call_llm") as mock_llm:
            mock_llm.side_effect = Exception("API error")
            result = await classify_intent("Tell me about the Moldova pilot")
            assert result == "question"

    @pytest.mark.asyncio
    async def test_invalid_llm_response_falls_back_to_question(self):
        """If LLM returns an invalid intent, should fall back to 'question'."""
        from core.router import classify_intent
        with patch("core.router.call_llm") as mock_llm:
            mock_llm.return_value = ("invalid_category", {"input_tokens": 10, "output_tokens": 1,
                                                            "cache_read_input_tokens": 0,
                                                            "cache_creation_input_tokens": 0})
            result = await classify_intent("test message")
            assert result == "question"

    @pytest.mark.asyncio
    async def test_empty_message(self):
        """Empty message should still classify (likely ambiguous or question)."""
        from core.router import classify_intent
        with patch("core.router.call_llm") as mock_llm:
            mock_llm.return_value = ("ambiguous", {"input_tokens": 10, "output_tokens": 1,
                                                     "cache_read_input_tokens": 0,
                                                     "cache_creation_input_tokens": 0})
            result = await classify_intent("")
            assert result in {"ambiguous", "question"}

    @pytest.mark.asyncio
    async def test_hebrew_text(self):
        """Hebrew text should be handled without errors."""
        from core.router import classify_intent
        with patch("core.router.call_llm") as mock_llm:
            mock_llm.return_value = ("question", {"input_tokens": 10, "output_tokens": 1,
                                                    "cache_read_input_tokens": 0,
                                                    "cache_creation_input_tokens": 0})
            result = await classify_intent("מה קרה בפגישה האחרונה?")
            assert result == "question"

    @pytest.mark.asyncio
    async def test_emoji_only_message(self):
        """Emoji-only message should classify without error."""
        from core.router import classify_intent
        with patch("core.router.call_llm") as mock_llm:
            mock_llm.return_value = ("ambiguous", {"input_tokens": 10, "output_tokens": 1,
                                                     "cache_read_input_tokens": 0,
                                                     "cache_creation_input_tokens": 0})
            result = await classify_intent("👍")
            assert result in {"ambiguous", "question"}

    @pytest.mark.asyncio
    async def test_uses_model_simple(self):
        """Router should use settings.model_simple (Haiku)."""
        from core.router import classify_intent
        with patch("core.router.call_llm") as mock_llm, \
             patch("core.router.settings") as mock_settings:
            mock_settings.model_simple = "claude-haiku-4-5-20251001"
            mock_llm.return_value = ("question", {"input_tokens": 10, "output_tokens": 1,
                                                    "cache_read_input_tokens": 0,
                                                    "cache_creation_input_tokens": 0})
            await classify_intent("test")
            call_kwargs = mock_llm.call_args[1]
            assert call_kwargs["model"] == "claude-haiku-4-5-20251001"
            assert call_kwargs["call_site"] == "router"

    @pytest.mark.asyncio
    async def test_long_message_truncated(self):
        """Messages longer than 500 chars should be truncated in the prompt."""
        from core.router import classify_intent
        with patch("core.router.call_llm") as mock_llm:
            mock_llm.return_value = ("question", {"input_tokens": 10, "output_tokens": 1,
                                                    "cache_read_input_tokens": 0,
                                                    "cache_creation_input_tokens": 0})
            long_msg = "a" * 1000
            await classify_intent(long_msg)
            call_args = mock_llm.call_args[1]
            # The prompt should contain the truncated message
            assert len(call_args["prompt"]) < len(long_msg) + 500


# =============================================================================
# Valid Intents Set
# =============================================================================

class TestValidIntents:
    """Test that VALID_INTENTS contains all expected values."""

    def test_all_intents_present(self):
        from core.router import VALID_INTENTS
        expected = {
            "question", "task_update", "information_injection",
            "gantt_request", "debrief", "approval_response",
            "weekly_review", "meeting_prep_request", "ambiguous",
        }
        assert VALID_INTENTS == expected
