"""Robustness fixes for the meeting-summary approval flow (2026-07-12 incident).

Covers: the parse-error plain-text fallback (bot never goes silent), the
free-text-looks-like-edit heuristic (edits sent before "Request Changes" aren't
lost), the reject-confirmation child counts, and the edit-flow revert-to-pending
(never strand a meeting in 'editing').
"""
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def _bot():
    from services.telegram_bot import TelegramBot
    b = TelegramBot.__new__(TelegramBot)
    b.eyal_chat_id = "123"
    return b


class TestLooksLikeEdit:
    def test_edit_phrases_detected(self):
        from services.telegram_bot import TelegramBot
        for t in ["change task 3 deadline to Friday", "remove the second decision",
                  "add a task for Roye to call AWS", "the owner should be Paolo",
                  "fix the typo in decision 2", "make it shorter"]:
            assert TelegramBot._looks_like_edit(t), t

    def test_questions_and_chat_not_flagged(self):
        from services.telegram_bot import TelegramBot
        for t in ["what decisions were made?", "how many tasks are there?",
                  "is the summary ready", "ok", "", "thanks"]:
            assert not TelegramBot._looks_like_edit(t), t


class TestParseErrorFallback:
    async def test_send_message_falls_back_to_plain_text(self):
        from telegram.error import BadRequest
        bot = _bot()
        seen = []

        async def fake_send(**k):
            seen.append(k.get("parse_mode"))
            if k.get("parse_mode") is not None:
                raise BadRequest("Can't parse entities: can't find end of the entity starting at byte offset 15")
            return MagicMock()

        bot._bot_send_message = fake_send
        ok = await bot.send_message("123", "a short *broken markdown msg", parse_mode="Markdown")
        assert ok is True
        assert seen == ["Markdown", None]   # retried once as plain text

    async def test_send_message_non_parse_error_not_retried(self):
        from telegram.error import BadRequest
        bot = _bot()
        seen = []

        async def fake_send(**k):
            seen.append(k.get("parse_mode"))
            raise BadRequest("chat not found")   # a different BadRequest

        bot._bot_send_message = fake_send
        ok = await bot.send_message("123", "hello", parse_mode="Markdown")
        assert ok is False
        assert seen == ["Markdown"]   # NOT retried as plain


class TestRejectConfirmCounts:
    def test_child_counts(self):
        bot = _bot()
        chain = MagicMock()
        chain.select.return_value.eq.return_value.execute.return_value.count = 7
        sc = SimpleNamespace(client=SimpleNamespace(table=lambda *a, **k: chain))
        with patch("services.supabase_client.supabase_client", sc):
            counts = bot._meeting_child_counts("m1")
        assert counts["tasks"] == 7 and counts["decisions"] == 7
        assert set(counts) == {"tasks", "decisions", "open_questions", "follow_up_meetings"}


class TestEditRevertsToPending:
    async def _run_edit(self, monkeypatch, apply_result, parse_result="do the edit"):
        """Drive process_response's edit arm with mocked internals."""
        import guardrails.approval_flow as af
        monkeypatch.setattr(af, "parse_edit_instructions_with_claude",
                            AsyncMock(return_value=parse_result))
        monkeypatch.setattr(af, "apply_edits", AsyncMock(return_value=apply_result))
        monkeypatch.setattr(af, "submit_for_approval", AsyncMock(return_value=None))
        status_calls = []
        monkeypatch.setattr(af, "update_approval_status",
                            AsyncMock(side_effect=lambda mid, st: status_calls.append(st)))
        sc = MagicMock()
        sc.get_meeting.return_value = {"id": "m1", "summary": "S", "title": "M"}
        sc.get_pending_approval.return_value = {"content_type": "meeting_summary", "content": {}}
        sc.log_approval_observation.return_value = None
        monkeypatch.setattr(af, "supabase_client", sc)
        res = await af.process_response(meeting_id="m1", response="make it shorter",
                                        response_source="telegram", force_action="edit")
        return res, status_calls

    async def test_apply_edits_error_reverts_to_pending(self, monkeypatch):
        from guardrails.approval_flow import ApprovalStatus
        res, status_calls = await self._run_edit(monkeypatch, {"error": "boom"})
        assert res["resubmitted"] is False
        # editing was set, then reverted to pending
        assert ApprovalStatus.EDITING in status_calls and ApprovalStatus.PENDING in status_calls
        assert status_calls[-1] == ApprovalStatus.PENDING

    async def test_unparseable_edits_revert_to_pending(self, monkeypatch):
        from guardrails.approval_flow import ApprovalStatus
        res, status_calls = await self._run_edit(monkeypatch, {}, parse_result=None)
        assert res["resubmitted"] is False
        assert status_calls[-1] == ApprovalStatus.PENDING

    async def test_edit_crash_reverts_to_pending(self, monkeypatch):
        import guardrails.approval_flow as af
        from guardrails.approval_flow import ApprovalStatus
        monkeypatch.setattr(af, "parse_edit_instructions_with_claude",
                            AsyncMock(side_effect=RuntimeError("llm down")))
        status_calls = []
        monkeypatch.setattr(af, "update_approval_status",
                            AsyncMock(side_effect=lambda mid, st: status_calls.append(st)))
        sc = MagicMock()
        sc.get_meeting.return_value = {"id": "m1", "summary": "S", "title": "M"}
        sc.get_pending_approval.return_value = {"content_type": "meeting_summary", "content": {}}
        monkeypatch.setattr(af, "supabase_client", sc)
        res = await af.process_response(meeting_id="m1", response="x",
                                        response_source="telegram", force_action="edit")
        assert res["resubmitted"] is False
        assert status_calls[-1] == ApprovalStatus.PENDING   # never stranded in 'editing'
