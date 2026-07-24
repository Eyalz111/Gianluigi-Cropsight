"""Email ingestion — Phase 1 (foundation). [2026-07-25]

Covers the pure quote-stripper and the first-message ingest orchestration
(mocked: no live Gmail/DB/LLM). Thread delta + gap recovery land in Phase 2/3.
"""
from unittest.mock import MagicMock

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
        assert strip_quoted_text("") == ""
        assert extract_quoted_text("") == ""


class TestParticipants:
    def test_names_and_addresses(self):
        msg = {"from": "Paolo Vailetti <paolo@x.com>", "to": "Eyal <eyal@x.com>, gianluigi@x.com", "cc": ""}
        people = ei._participants(msg)
        assert "Paolo Vailetti" in people and "Eyal" in people

    def test_addresses_deduped(self):
        assert ei._addresses("a@x.com, B@x.com", "a@x.com") == ["a@x.com", "b@x.com"]


def _patch_sc(monkeypatch, thread):
    from services.supabase_client import supabase_client as sc
    calls = {"processed": [], "upsert": [], "tagged": []}
    monkeypatch.setattr(sc, "get_email_thread", lambda tk: thread)
    monkeypatch.setattr(sc, "upsert_email_thread",
                        lambda tk, **k: calls["upsert"].append((tk, k)) or {"thread_key": tk})
    monkeypatch.setattr(sc, "mark_message_processed",
                        lambda tk, mid: calls["processed"].append((tk, mid)))
    # meetings tag update goes through the read-only `client` property -> _client
    fake = MagicMock()
    monkeypatch.setattr(sc, "_client", fake)
    return calls, fake


class TestIngestOrchestration:
    async def test_new_thread_extracts_and_cards(self, monkeypatch):
        import processors.transcript_processor as tp
        import guardrails.approval_flow as af
        calls, _ = _patch_sc(monkeypatch, thread=None)
        submitted = {}

        async def _pt(**kw):
            return {"meeting_id": "m-new", "summary": "s",
                    "tasks": [{"title": "Send the NDA"}], "decisions": [],
                    "follow_ups": [], "open_questions": []}
        monkeypatch.setattr(tp, "process_transcript", _pt)

        async def _submit(ct, content, mid):
            submitted["ct"] = ct; submitted["mid"] = mid; submitted["content"] = content
            return {"approval_id": mid}
        monkeypatch.setattr(af, "submit_for_approval", _submit)

        msg = {"threadId": "t1", "message_id": "<a@x>", "subject": "Re: Moldova",
               "from": "Paolo <paolo@x.com>", "to": "g@x", "cc": "",
               "body": "Please send the NDA.\n\nOn Mon, X wrote:\n> old"}
        res = await ei.ingest_email_thread(msg)

        assert res["ingested"] is True and res["meeting_id"] == "m-new"
        assert submitted["ct"] == "email_thread" and submitted["mid"] == "m-new"
        assert calls["processed"] == [("t1", "<a@x>")]
        assert calls["upsert"] and calls["upsert"][0][0] == "t1"

    async def test_already_processed_is_noop(self, monkeypatch):
        _patch_sc(monkeypatch, thread={"meeting_id": "m1", "processed_message_ids": ["<a@x>"]})
        res = await ei.ingest_email_thread(
            {"threadId": "t1", "message_id": "<a@x>", "subject": "s", "body": "b"})
        assert res["skipped"] == "already_processed"

    async def test_existing_thread_defers_delta(self, monkeypatch):
        _patch_sc(monkeypatch, thread={"meeting_id": "m1", "processed_message_ids": []})
        res = await ei.ingest_email_thread(
            {"threadId": "t1", "message_id": "<b@x>", "subject": "s", "body": "b"})
        assert res["skipped"] == "delta_phase2"

    async def test_missing_ids_skipped(self, monkeypatch):
        res = await ei.ingest_email_thread(
            {"threadId": "", "message_id": "", "subject": "s", "body": "b"})
        assert "missing" in res["skipped"]

    async def test_no_actionable_items_no_card(self, monkeypatch):
        import processors.transcript_processor as tp
        import guardrails.approval_flow as af
        _patch_sc(monkeypatch, thread=None)

        async def _pt(**kw):
            return {"meeting_id": "m-empty", "summary": "fyi",
                    "tasks": [], "decisions": [], "follow_ups": [], "open_questions": []}
        monkeypatch.setattr(tp, "process_transcript", _pt)
        called = {"n": 0}

        async def _submit(*a, **k):
            called["n"] += 1
        monkeypatch.setattr(af, "submit_for_approval", _submit)

        res = await ei.ingest_email_thread(
            {"threadId": "t9", "message_id": "<z@x>", "subject": "FYI", "from": "P <p@x>",
             "to": "g@x", "cc": "", "body": "Just an update, nothing to do."})
        assert res.get("no_actionable") is True
        assert called["n"] == 0, "no approval card when nothing actionable"
