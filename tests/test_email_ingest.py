"""Email ingestion (processors/email_ingest). [2026-07-25]

Pure helpers (quote stripper, References parsing, participants) + the orchestrator
across new-thread / delta / idempotency / gap / nothing-actionable — all mocked,
no live Gmail/DB/LLM.
"""
from unittest.mock import MagicMock, AsyncMock

import pytest

import processors.email_ingest as ei
from services.gmail import strip_quoted_text, extract_quoted_text


class TestStripQuoted:
    def test_gmail_on_wrote(self):
        b = ("New ask here.\n\nOn Mon, Jul 21, 2026 at 3:00 PM Paolo <p@x.com> "
             "wrote:\n> old stuff\n> more old")
        assert strip_quoted_text(b) == "New ask here."
        assert "old stuff" in extract_quoted_text(b)

    def test_angle_quoted_lines(self):
        assert strip_quoted_text("Do the thing.\n> quoted reply") == "Do the thing."

    def test_outlook_original_message(self):
        assert strip_quoted_text("Reply text.\n-----Original Message-----\nFrom: x") == "Reply text."

    def test_signature_dropped(self):
        assert strip_quoted_text("Content here.\n-- \nPaolo\nBD, CropSight") == "Content here."

    def test_plain_message_unchanged(self):
        assert strip_quoted_text("Just a plain message, no quotes.") == "Just a plain message, no quotes."

    def test_empty(self):
        assert strip_quoted_text("") == "" and extract_quoted_text("") == ""


class TestHelpers:
    def test_participants(self):
        msg = {"from": "Paolo Vailetti <paolo@x.com>", "to": "Eyal <eyal@x.com>, g@x.com", "cc": ""}
        people = ei._participants(msg)
        assert "Paolo Vailetti" in people and "Eyal" in people

    def test_parse_references(self):
        assert ei._parse_references("<a@x> <b@y>  <c@z>") == ["<a@x>", "<b@y>", "<c@z>"]
        assert ei._parse_references("") == []

    def test_pending_filter(self):
        rows = [{"id": 1, "approval_status": "pending"}, {"id": 2, "approval_status": "approved"},
                {"id": 3}]  # missing => treated as pending
        assert [r["id"] for r in ei._pending(rows)] == [1, 3]


def _patch(monkeypatch, thread, thread_msgs, pending_tasks=None):
    from services.supabase_client import supabase_client as sc
    from services.gmail import gmail_service
    import guardrails.approval_flow as af
    calls = {"processed": [], "upsert": [], "submit": [], "scan": []}
    monkeypatch.setattr(sc, "get_email_thread", lambda tk: thread)
    monkeypatch.setattr(sc, "upsert_email_thread", lambda tk, **k: calls["upsert"].append((tk, k)) or {"thread_key": tk})
    monkeypatch.setattr(sc, "mark_message_processed", lambda tk, mid: calls["processed"].append(mid))
    # The orphan-recovery probe (review #2) runs .table().select().eq().eq().limit()
    # .execute().data — default it to [] so a NEW thread (no email_threads row) takes
    # the process_transcript branch rather than adopting a phantom MagicMock orphan.
    _client = MagicMock()
    _client.table.return_value.select.return_value.eq.return_value.eq.return_value.limit.return_value.execute.return_value.data = []
    monkeypatch.setattr(sc, "_client", _client)
    monkeypatch.setattr(sc, "get_meeting", lambda mid: {"sensitivity": "founders"})
    monkeypatch.setattr(sc, "get_tasks", lambda **k: pending_tasks or [])
    monkeypatch.setattr(sc, "list_decisions", lambda **k: [])
    monkeypatch.setattr(sc, "list_follow_up_meetings", lambda **k: [])
    monkeypatch.setattr(sc, "get_open_questions", lambda **k: [])
    monkeypatch.setattr(sc, "create_email_scan", lambda **k: calls["scan"].append(k) or {})
    monkeypatch.setattr(gmail_service, "get_thread", AsyncMock(return_value=thread_msgs))

    async def _submit(ct, content, mid):
        calls["submit"].append({"ct": ct, "mid": mid, "content": content})
    monkeypatch.setattr(af, "submit_for_approval", _submit)
    return calls, sc


class TestIngestOrchestration:
    async def test_new_thread_extracts_and_cards(self, monkeypatch):
        import processors.transcript_processor as tp
        calls, _ = _patch(monkeypatch, thread=None, thread_msgs=[],
                          pending_tasks=[{"title": "Send the NDA", "approval_status": "pending"}])

        async def _pt(**kw):
            return {"meeting_id": "m-new", "tasks": [{"title": "Send the NDA"}],
                    "decisions": [], "follow_ups": [], "open_questions": []}
        monkeypatch.setattr(tp, "process_transcript", _pt)

        msg = {"threadId": "t1", "message_id": "<a@x>", "subject": "Re: Moldova",
               "from": "Paolo <paolo@x.com>", "to": "g@x", "cc": "", "references": "",
               "body": "Please send the NDA.\n\nOn Mon, X wrote:\n> old"}
        res = await ei.ingest_email_thread(msg)

        assert res["ingested"] is True and res["meeting_id"] == "m-new" and res["delta"] is False
        assert calls["submit"] and calls["submit"][0]["ct"] == "email_thread"
        assert "<a@x>" in calls["processed"] and calls["upsert"]

    async def test_delta_appends_to_existing_source(self, monkeypatch):
        calls, _ = _patch(
            monkeypatch,
            thread={"meeting_id": "m1", "processed_message_ids": ["<a@x>"]},
            thread_msgs=[{"message_id": "<a@x>", "body": "old", "references": ""},
                         {"message_id": "<b@x>", "from": "P <p@x>", "date": "d",
                          "references": "<a@x>", "body": "Also please book a call."}],
            pending_tasks=[{"title": "Book a call", "approval_status": "pending"}])

        async def _delta(meeting_id, delta_text, subject):
            return {"tasks": [{"title": "Book a call"}], "decisions": [], "follow_ups": [], "open_questions": []}
        monkeypatch.setattr(ei, "_extract_and_store_delta", _delta)

        msg = {"threadId": "t1", "message_id": "<b@x>", "subject": "Re: Moldova",
               "from": "P <p@x>", "to": "g@x", "cc": "", "references": "<a@x>", "body": "Also please book a call."}
        res = await ei.ingest_email_thread(msg)

        assert res["ingested"] is True and res["delta"] is True and res["meeting_id"] == "m1"
        assert "<b@x>" in calls["processed"]
        assert calls["submit"] and calls["submit"][0]["ct"] == "email_thread"

    async def test_gap_recovered_from_quoted_history(self, monkeypatch):
        # We saw <a@x>, were dropped for <b@x>, re-added at <c@x> which quotes <b@x>.
        calls, _ = _patch(
            monkeypatch,
            thread={"meeting_id": "m1", "processed_message_ids": ["<a@x>"]},
            thread_msgs=[{"message_id": "<a@x>", "body": "first", "references": ""},
                         {"message_id": "<c@x>", "from": "P <p@x>", "date": "d",
                          "references": "<a@x> <b@x> <c@x>",
                          "body": "Re-looping you in.\n\nOn Tue, X wrote:\n> (b) we agreed to hire a lawyer"}],
            pending_tasks=[{"title": "Hire a lawyer", "approval_status": "pending"}])

        captured = {}

        async def _delta(meeting_id, delta_text, subject):
            captured["delta_text"] = delta_text
            return {"tasks": [{"title": "Hire a lawyer"}], "decisions": [], "follow_ups": [], "open_questions": []}
        monkeypatch.setattr(ei, "_extract_and_store_delta", _delta)

        msg = {"threadId": "t1", "message_id": "<c@x>", "subject": "Re: Legal",
               "from": "P <p@x>", "references": "<a@x> <b@x> <c@x>",
               "body": "Re-looping you in.\n\nOn Tue, X wrote:\n> (b) we agreed to hire a lawyer"}
        res = await ei.ingest_email_thread(msg)

        assert res["gap"] == 1, "the message we were never sent (<b@x>) is detected as a gap"
        assert "<b@x>" in calls["processed"], "gap id marked so it isn't re-backfilled"
        assert "hire a lawyer" in captured["delta_text"].lower(), "gap content recovered from quotes"

    async def test_already_processed_is_noop(self, monkeypatch):
        _patch(monkeypatch, thread={"meeting_id": "m1", "processed_message_ids": ["<a@x>"]}, thread_msgs=[])
        res = await ei.ingest_email_thread({"threadId": "t1", "message_id": "<a@x>", "subject": "s", "body": "b"})
        assert res["skipped"] == "already_processed"

    async def test_missing_ids_skipped(self, monkeypatch):
        res = await ei.ingest_email_thread({"threadId": "", "message_id": "", "subject": "s", "body": "b"})
        assert "missing" in res["skipped"]

    async def test_nothing_actionable_indexes_no_card(self, monkeypatch):
        import processors.transcript_processor as tp
        calls, _ = _patch(monkeypatch, thread=None, thread_msgs=[], pending_tasks=[])  # no pending items

        async def _pt(**kw):
            return {"meeting_id": "m-empty", "tasks": [], "decisions": [], "follow_ups": [], "open_questions": []}
        monkeypatch.setattr(tp, "process_transcript", _pt)

        res = await ei.ingest_email_thread(
            {"threadId": "t9", "message_id": "<z@x>", "subject": "FYI", "from": "P <p@x>",
             "to": "g@x", "cc": "", "references": "", "body": "Just an update, nothing to do."})
        assert res.get("no_actionable") is True
        assert not calls["submit"], "no approval card when nothing pending"
        assert calls["scan"] and calls["scan"][0]["thread_id"] == "t9", "indexed to the brain"
