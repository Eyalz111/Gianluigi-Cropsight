"""Repeatable meeting-chain QA (processors/meeting_qa). [2026-07-24]

Pins the DB<->Sheet consistency checks that used to be a manual spot-check:
PRESENCE (every DB child on its tab), DUPLICATE (no UUID twice on Meetings), and
DRIFT (DB title == sheet title). The comparison is pure — driven by a fabricated
sheet_index — so it needs no live Sheet.
"""
from unittest.mock import MagicMock

import pytest

import processors.meeting_qa as mq


def _cg(db, sheet_map, kind="task", extra=None):
    return mq._check_group(
        db, lambda r: r.get("id"), lambda r: r.get("title"), sheet_map, kind, extra_maps=extra)


class TestCheckGroup:
    def test_present_and_matching_is_clean(self):
        assert _cg([{"id": "a", "title": "Do X"}], {"a": ["Do X"]}) == []

    def test_missing_from_sheet_flagged(self):
        iss = _cg([{"id": "a", "title": "Do X"}], {})
        assert len(iss) == 1 and "missing" in iss[0]

    def test_duplicate_uuid_flagged(self):
        iss = _cg([{"id": "a", "title": "Meet Z"}], {"a": ["Meet Z", "Meet Z"]}, kind="follow-up")
        assert any("DUPLICATED" in i for i in iss)

    def test_title_drift_flagged(self):
        # the exact 2026-07-24 case: DB says 2SID, sheet still says To Seed
        iss = _cg([{"id": "a", "title": "2SID VC"}], {"a": ["To Seed VC"]}, kind="follow-up")
        assert any("DRIFT" in i for i in iss)

    def test_present_in_extra_map_counts(self):
        # a follow-up archived to Past Meetings is still "present" — not missing
        iss = _cg([{"id": "a", "title": "Held"}], {}, kind="follow-up", extra=[{"a": ["Held"]}])
        assert iss == []

    def test_whitespace_case_not_drift(self):
        iss = _cg([{"id": "a", "title": "Do  X"}], {"a": ["do x"]})
        assert iss == []


def _mock_sc(monkeypatch, tasks, decisions, follow_ups, meeting=True):
    from services.supabase_client import supabase_client as sc
    tbl = MagicMock()
    tbl.select.return_value.eq.return_value.execute.return_value.data = (
        [{"title": "Weekly", "approval_status": "approved"}] if meeting else [])
    # `client` is a read-only property backed by `_client` — patch the backing attr.
    monkeypatch.setattr(sc, "_client", MagicMock(table=lambda n: tbl))
    monkeypatch.setattr(sc, "get_tasks", lambda **k: tasks)
    monkeypatch.setattr(sc, "list_decisions", lambda **k: decisions)
    monkeypatch.setattr(sc, "list_follow_up_meetings", lambda **k: follow_ups)


class TestVerifyMeetingChain:
    async def test_all_consistent_is_ok(self, monkeypatch):
        _mock_sc(monkeypatch,
                 tasks=[{"id": "t1", "title": "Do X", "status": "pending"}],
                 decisions=[{"id": "d1", "description": "Decide Y"}],
                 follow_ups=[{"id": "f1", "title": "Meet Z"}])
        idx = {"tasks": {"t1": ["Do X"]}, "decisions": {"d1": ["Decide Y"]},
               "meetings": {"f1": ["Meet Z"]}, "past_meetings": {}}
        r = await mq.verify_meeting_chain("m1", sheet_index=idx)
        assert r["ok"] is True and r["issues"] == []
        assert r["counts"] == {"tasks": 1, "decisions": 1, "follow_ups": 1}

    async def test_flags_missing_task_and_dup_followup(self, monkeypatch):
        _mock_sc(monkeypatch,
                 tasks=[{"id": "t1", "title": "Do X", "status": "pending"}],
                 decisions=[],
                 follow_ups=[{"id": "f1", "title": "Meet Z"}])
        idx = {"tasks": {}, "decisions": {},
               "meetings": {"f1": ["Meet Z", "Meet Z"]}, "past_meetings": {}}
        r = await mq.verify_meeting_chain("m1", sheet_index=idx)
        assert r["ok"] is False
        joined = " ".join(r["issues"])
        assert "missing" in joined and "DUPLICATED" in joined

    async def test_archived_task_not_expected_on_sheet(self, monkeypatch):
        # an archived task legitimately left the Tasks tab — must NOT be "missing"
        _mock_sc(monkeypatch,
                 tasks=[{"id": "t1", "title": "Old", "status": "archived"}],
                 decisions=[], follow_ups=[])
        idx = {"tasks": {}, "decisions": {}, "meetings": {}, "past_meetings": {}}
        r = await mq.verify_meeting_chain("m1", sheet_index=idx)
        assert r["ok"] is True

    async def test_missing_meeting_row(self, monkeypatch):
        _mock_sc(monkeypatch, tasks=[], decisions=[], follow_ups=[], meeting=False)
        idx = {"tasks": {}, "decisions": {}, "meetings": {}, "past_meetings": {}}
        r = await mq.verify_meeting_chain("gone", sheet_index=idx)
        assert r["ok"] is False and "not found" in r["issues"][0]
