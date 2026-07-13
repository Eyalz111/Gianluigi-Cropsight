"""Robustness fixes for the meeting-summary approval flow (2026-07-12 incident).

Covers: the parse-error plain-text fallback (bot never goes silent), the
free-text-looks-like-edit heuristic (edits sent before "Request Changes" aren't
lost), the reject-confirmation child counts, and the edit-flow revert-to-pending
(never strand a meeting in 'editing').
"""
import json
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


def _approve_env(monkeypatch, *, status, pending_info, db_tasks):
    """Wire process_response's approve arm with mocked internals; return (af, sc, captured_dist)."""
    import guardrails.approval_flow as af
    sc = MagicMock()
    sc.get_pending_approval.return_value = {"approval_id": "m1"} if pending_info is not None else None
    sc.get_meeting.return_value = {
        "id": "m1", "approval_status": status, "title": "M",
        "sensitivity": "team", "summary": "S", "date": "2026-07-12",
    }
    sc.get_tasks.return_value = db_tasks
    sc.list_decisions.return_value = []
    sc.list_follow_up_meetings.return_value = []
    sc.get_open_questions.return_value = []
    monkeypatch.setattr(af, "supabase_client", sc)
    monkeypatch.setattr(af, "_row_to_pending_info", lambda row: pending_info)
    monkeypatch.setattr(af, "cancel_auto_publish", lambda *a, **k: None)
    monkeypatch.setattr(af, "cancel_approval_reminders", lambda *a, **k: None)
    monkeypatch.setattr(af, "update_approval_status", AsyncMock())
    monkeypatch.setattr(af, "_promote_children_to_approved", lambda *a, **k: None)
    captured = {}

    async def fake_dist(meeting_id, content, sensitivity):
        captured["content"] = content
        captured["sensitivity"] = sensitivity
        return {"email_sent": True}

    monkeypatch.setattr(af, "distribute_approved_content", fake_dist)
    return af, sc, captured


class TestIdempotentDistribution:
    """#1 — a meeting distributes at most once; a second approve never re-sends."""

    async def test_second_approve_on_approved_meeting_is_noop(self, monkeypatch):
        af, sc, captured = _approve_env(
            monkeypatch, status="approved", pending_info=None, db_tasks=[],
        )
        res = await af.process_response("m1", "approve", force_action="approve")
        assert res["action"] == "already_approved"
        assert "content" not in captured                 # distribution never ran
        sc.delete_pending_approval.assert_not_called()   # didn't even consume the row

    async def test_first_approve_on_pending_meeting_distributes(self, monkeypatch):
        af, sc, captured = _approve_env(
            monkeypatch, status="pending", pending_info={"type": "meeting_summary", "content": {}},
            db_tasks=[{"meeting_id": "m1", "title": "T", "assignee": "Paolo", "priority": "H"}],
        )
        res = await af.process_response("m1", "approve", force_action="approve")
        assert res["action"] == "approved"
        assert captured["content"]["tasks"]              # distribution ran


class TestDistributeFromDB:
    """#3 — structured lists come from the DB, never the lossy edited pending copy."""

    async def test_uses_db_owners_not_blank_pending(self, monkeypatch):
        # pending copy has a BLANK owner (renders 'team'); DB has the real owner.
        lossy = {"type": "meeting_summary",
                 "content": {"title": "M", "summary": "S", "tasks": [{"title": "T", "assignee": ""}]}}
        af, sc, captured = _approve_env(
            monkeypatch, status="pending", pending_info=lossy,
            db_tasks=[{"meeting_id": "m1", "title": "T", "assignee": "Paolo Vailetti", "priority": "H"}],
        )
        res = await af.process_response("m1", "approve", force_action="approve")
        assert res["action"] == "approved"
        # distribution got the DB task (real owner), NOT the blank pending one
        assert captured["content"]["tasks"][0]["assignee"] == "Paolo Vailetti"
        assert all(t.get("assignee") for t in captured["content"]["tasks"])


class TestApplyEditsDedup:
    """#4 — a task/decision the edit LLM emits twice must not create a duplicate row."""

    async def test_duplicate_llm_task_dropped(self, monkeypatch):
        import guardrails.approval_flow as af
        llm = json.dumps({
            "summary": "s",
            "tasks": [
                {"title": "Same task", "assignee": "A", "priority": "H", "index": 1},
                {"title": "Same task", "assignee": "A", "priority": "H"},  # dup, no index
            ],
            "decisions": [], "follow_ups": [], "open_questions": [],
        })
        monkeypatch.setattr(af, "call_llm", lambda **k: (llm, {}))
        sc = MagicMock()
        sc._serialize_datetime.return_value = None
        monkeypatch.setattr(af, "supabase_client", sc)
        structured = {"tasks": [{"id": "t1", "title": "Same task", "assignee": "A"}],
                      "decisions": [], "follow_ups": [], "open_questions": []}
        await af.apply_edits("m1", [{"type": "noop"}], structured_data=structured)
        # dup collapsed -> original updated in place once, NO new row created
        sc.create_tasks_batch.assert_not_called()
        assert sc.update_task.call_count == 1


class TestCardMessageIdPersistence:
    """#5/#6 — approval-card message-ids round-trip through pending_approvals content."""

    def test_set_then_get_roundtrip(self, monkeypatch):
        from services import supabase_client as scmod
        sc = scmod.supabase_client
        written = {}
        monkeypatch.setattr(sc, "get_pending_approval", lambda aid: {"content": {"title": "M"}})
        monkeypatch.setattr(sc, "update_pending_approval",
                            lambda aid, content=None, **k: written.update({"content": content}))
        sc.set_card_message_ids("m1", [101, 102])
        assert written["content"]["_card_message_ids"] == [101, 102]
        assert written["content"]["title"] == "M"        # merged, didn't clobber content

        monkeypatch.setattr(sc, "get_pending_approval",
                            lambda aid: {"content": {"_card_message_ids": [101, 102]}})
        assert sc.get_card_message_ids("m1") == [101, 102]

    def test_missing_row_returns_empty(self, monkeypatch):
        from services import supabase_client as scmod
        sc = scmod.supabase_client
        monkeypatch.setattr(sc, "get_pending_approval", lambda aid: None)
        assert sc.get_card_message_ids("nope") == []       # never raises


class TestNonMeetingRejectNoUuidError:
    """2026-07-13 regression: rejecting a prep/digest (prefixed id, not a UUID)
    threw 22P02 because the stale-card guard ran get_meeting() on the prep id.
    Non-meeting ids must skip every UUID-keyed query and discard directly."""

    async def test_prep_reject_never_calls_get_meeting(self, monkeypatch):
        from services.telegram_bot import TelegramBot
        import guardrails.approval_flow as af
        import services.telegram_bot as tb

        bot = TelegramBot.__new__(TelegramBot)
        bot.eyal_chat_id = "123"
        bot._approval_message_ids = {}          # empty -> _cleanup_approval_parts is a no-op
        bot._app = MagicMock()                  # 'app' is a property backed by _app

        sc = MagicMock()
        monkeypatch.setattr("services.supabase_client.supabase_client", sc)
        pr = AsyncMock(return_value={"next_step": "Content discarded"})
        monkeypatch.setattr(af, "process_response", pr)
        monkeypatch.setattr(tb, "conversation_memory", MagicMock())

        q = MagicMock()
        q.data = "reject:prep-b8qifkej29plnbgn9a9dt9fl1g_20260713T170000Z"
        q.from_user.id = "123"
        q.message.message_id = 999
        q.answer = AsyncMock()
        q.edit_message_text = AsyncMock()
        update = SimpleNamespace(callback_query=q)
        ctx = SimpleNamespace(user_data={})

        await bot._handle_callback_query(update, ctx)

        sc.get_meeting.assert_not_called()   # the bug — would 400 on a prep- id
        pr.assert_awaited_once()
        assert pr.await_args.kwargs.get("force_action") == "reject"
