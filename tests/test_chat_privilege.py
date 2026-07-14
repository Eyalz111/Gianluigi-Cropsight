"""Chat-scoped privilege model — audit 2026-07 AC-01 / TS-01 / TS-02.

The office manager interacts only through the Telegram group. So:
- writes execute ONLY for a privileged (Eyal-DM) caller;
- the group is read-only and TEAM-capped — it never reaches write tools or
  sensitive-read tools, and write-capable intents are refused;
- filtering is audience-based (the group caps clearance even when Eyal asks there).
"""
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core.tools import (
    tools_for,
    WRITE_TOOL_NAMES,
    SENSITIVE_READ_TOOL_NAMES,
    RESTRICTED_READ_TOOL_NAMES,
)


# --------------------------------------------------------------------------- #
# tools_for — the allowed toolset per caller privilege
# --------------------------------------------------------------------------- #
class TestToolsFor:
    def test_eyal_gets_everything(self):
        names = {t["name"] for t in tools_for(True, 4)}
        assert "create_task" in names
        assert "search_memory" in names
        assert "list_decisions" in names

    def test_team_level_is_operational_reads_only(self):
        names = {t["name"] for t in tools_for(False, 2)}
        assert not (names & WRITE_TOOL_NAMES), "no write tools"
        assert not (names & SENSITIVE_READ_TOOL_NAMES), "no sensitive reads at TEAM"
        assert {"get_tasks", "get_gantt_status", "get_open_questions"} <= names

    def test_founders_group_can_query_but_not_write_or_touch_raw_email(self):
        # The office-manager group runs at FOUNDERS: it can query decisions/memory
        # (output tier-filtered elsewhere), stays read-only, and never gets raw
        # Gmail / email-intel tools.
        names = {t["name"] for t in tools_for(False, 3)}
        assert not (names & WRITE_TOOL_NAMES), "read-only"
        assert {"list_decisions", "search_memory", "get_tasks"} <= names, "can query"
        assert not (names & RESTRICTED_READ_TOOL_NAMES), "no raw gmail/email"


# --------------------------------------------------------------------------- #
# process_message — write intents refused for a read-only caller
# --------------------------------------------------------------------------- #
class TestProcessMessageReadOnly:
    @pytest.mark.parametrize("intent", ["debrief", "information_injection"])
    async def test_write_intents_blocked_for_readonly(self, intent):
        from core.agent import GianluigiAgent
        agent = GianluigiAgent()
        with patch("core.agent.classify_intent", AsyncMock(return_value=intent)), \
             patch("core.agent.supabase_client"):
            result = await agent.process_message("do it", "roye", allow_writes=False)
        assert result["action"] == "read_only"
        assert result["actions"] == []

    async def test_write_intent_allowed_for_privileged(self):
        from core.agent import GianluigiAgent
        agent = GianluigiAgent()
        fake = {"action": "quick_injection_confirm", "extracted_items": []}
        with patch("core.agent.classify_intent", AsyncMock(return_value="information_injection")), \
             patch("core.agent.supabase_client"), \
             patch("processors.debrief.process_quick_injection", AsyncMock(return_value=fake)):
            result = await agent.process_message("add a task", "eyal", allow_writes=True)
        assert result["action"] == "quick_injection_confirm"


# --------------------------------------------------------------------------- #
# ConversationAgent.respond — restricted toolset + guarded executor
# --------------------------------------------------------------------------- #
def _tool_use(name, tool_input):
    block = SimpleNamespace(type="tool_use", name=name, input=tool_input, id="t1")
    return SimpleNamespace(stop_reason="tool_use", content=[block])


def _end(text):
    block = SimpleNamespace(type="text", text=text)
    return SimpleNamespace(stop_reason="end_turn", content=[block])


class TestRespondGating:
    async def test_readonly_never_executes_a_write_tool(self):
        from core.conversation_agent import ConversationAgent
        executor = AsyncMock(return_value="ok")
        agent = ConversationAgent(tool_executor=executor)
        # Force a write tool_use even though it's not offered — the guard must block it.
        responses = [_tool_use("create_task", {"title": "x"}), _end("done")]
        with patch("core.conversation_agent.call_llm_with_tools", side_effect=responses) as m:
            out = await agent.respond("add task", "roye", allow_writes=False, max_sensitivity_level=2)
        executor.assert_not_awaited()                      # write never executed
        offered = {t["name"] for t in m.call_args_list[0].kwargs["tools"]}
        assert "create_task" not in offered               # not even offered to Claude
        assert out["response"] == "done"

    async def test_readonly_executes_allowed_read_tool(self):
        from core.conversation_agent import ConversationAgent
        executor = AsyncMock(return_value="tasks...")
        agent = ConversationAgent(tool_executor=executor)
        responses = [_tool_use("get_tasks", {}), _end("here you go")]
        with patch("core.conversation_agent.call_llm_with_tools", side_effect=responses):
            out = await agent.respond("my tasks", "roye", allow_writes=False, max_sensitivity_level=3)
        executor.assert_awaited_once_with("get_tasks", {}, 3)   # level threaded through
        assert out["response"] == "here you go"


# --------------------------------------------------------------------------- #
# TelegramBot._chat_privilege — audience-based access control
# --------------------------------------------------------------------------- #
def _update(user_id, chat_id):
    u = MagicMock()
    u.effective_user.id = user_id
    u.effective_chat.id = chat_id
    return u


class TestChatPrivilege:
    def _bot(self):
        from services.telegram_bot import TelegramBot
        bot = TelegramBot.__new__(TelegramBot)   # skip heavy __init__
        bot.eyal_chat_id = 8190904141
        return bot

    def test_eyal_dm_is_full_privilege(self):
        is_eyal, allow_writes, lvl = self._bot()._chat_privilege(_update(8190904141, 8190904141))
        assert is_eyal and allow_writes and lvl == 4

    def test_eyal_in_group_is_readonly_founders_capped(self):
        # Audience-based: the group caps clearance (FOUNDERS) and blocks writes
        # even for Eyal — CEO content must not land in the shared chat.
        is_eyal, allow_writes, lvl = self._bot()._chat_privilege(_update(8190904141, -5187389631))
        assert is_eyal and not allow_writes and lvl == 3

    def test_other_member_in_group_is_readonly_founders(self):
        is_eyal, allow_writes, lvl = self._bot()._chat_privilege(_update(999, -5187389631))
        assert not is_eyal and not allow_writes and lvl == 3

    def test_other_member_dm_is_readonly_founders(self):
        is_eyal, allow_writes, lvl = self._bot()._chat_privilege(_update(999, 999))
        assert not is_eyal and not allow_writes and lvl == 3


class TestApplyTierFilter:
    def _agent(self):
        from core.agent import GianluigiAgent
        return GianluigiAgent()

    def test_founders_caller_drops_ceo_decisions(self):
        result = {"decisions": [
            {"description": "team one", "sensitivity": "team"},
            {"description": "ceo one", "sensitivity": "ceo"},
        ], "count": 2}
        out = self._agent()._apply_tier_filter("list_decisions", result, 3)
        assert out["count"] == 1
        assert out["decisions"][0]["sensitivity"] == "team"

    def test_ceo_caller_sees_everything(self):
        result = {"decisions": [{"description": "x", "sensitivity": "ceo"}], "count": 1}
        out = self._agent()._apply_tier_filter("list_decisions", result, 4)
        assert out["count"] == 1

    def test_meeting_summary_above_level_is_gated(self):
        result = {"title": "Board", "summary": "secret", "sensitivity": "ceo"}
        out = self._agent()._apply_tier_filter("get_meeting_summary", result, 3)
        assert "error" in out and "summary" not in out
