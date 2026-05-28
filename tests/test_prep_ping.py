"""
Tests for the meeting-prep "Prep Ping" rebuild (v2.5 Phase 3, chunk 3):
email-resolved participant anchoring, deterministic ping (no LLM), the
Eyal-attendance gate, the scheduler window/floor/fire-once/reschedule, restart
reconstruction, and the Tier-2 synth (single LLM + fallback).

Patches MODULE-level attrs (never the global settings object).
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import processors.prep_ping as pp


def _iso(minutes_from_now: float) -> str:
    return (datetime.now(timezone.utc) + timedelta(minutes=minutes_from_now)).isoformat()


# =========================================================================
# anchoring + helpers
# =========================================================================

class TestAnchorAndHelpers:
    def test_first_name_strips_title(self):
        assert pp._first_name({"name": "Prof. Yoram Weiss"}) == "Yoram"
        assert pp._first_name({"name": "Roye Tadmor"}) == "Roye"

    def test_eyal_is_attendee(self):
        with patch.object(pp, "EYAL_IDENTITIES", {"eyalz111@gmail.com"}):
            assert pp.eyal_is_attendee({"attendees": [{"email": "eyalz111@gmail.com"}], "organizer": ""})
            assert not pp.eyal_is_attendee({"attendees": [{"email": "a@gmail.com"}], "organizer": "b@gmail.com"})

    def test_anchor_resolves_by_email_not_displayname(self):
        # Hebrew displayName, but email resolves to the team member → first name "Roye".
        members = {"roye@cropsight.io": {"name": "Roye Tadmor"}}
        event = {
            "title": "1:1",
            "attendees": [
                {"email": "roye@cropsight.io", "displayName": "רועי תדמור"},
                {"email": "stranger@external.com", "displayName": "Stranger"},  # external → skip
            ],
        }
        with patch.object(pp, "EYAL_IDENTITIES", set()), \
             patch.object(pp, "get_team_member_by_email", side_effect=lambda e: members.get(e)), \
             patch("processors.topic_threading._match_canonical_name", return_value=None), \
             patch("processors.topic_threading._find_thread_by_name", return_value=None):
            out = pp.anchor_event(event)
        assert len(out["participants"]) == 1
        assert out["participants"][0]["first"] == "Roye"
        assert out["participants"][0]["display"] == "רועי תדמור"  # Hebrew kept for rendering
        assert out["topic"] is None

    def test_string_attendees_dont_crash(self):
        """Regression: some calendar events return attendees as plain email strings,
        not dicts. Pre-fix this raised `'str' object has no attribute 'get'` in
        prep_ping_scheduler (live error 2026-05-28)."""
        event = {
            "title": "Roye 1:1",
            "attendees": ["roye@cropsight.io", "external@x.com"],  # strings, not dicts
            "organizer": "eyalz111@gmail.com",
        }
        members = {"roye@cropsight.io": {"name": "Roye Tadmor"}}
        with patch.object(pp, "EYAL_IDENTITIES", {"eyalz111@gmail.com"}), \
             patch.object(pp, "get_team_member_by_email", side_effect=lambda e: members.get(e)), \
             patch("processors.topic_threading._match_canonical_name", return_value=None), \
             patch("processors.topic_threading._find_thread_by_name", return_value=None):
            assert pp.eyal_is_attendee(event) is True
            out = pp.anchor_event(event)
        # Roye still resolves (by email); display falls back to first name (no displayName on a string attendee).
        assert [p["first"] for p in out["participants"]] == ["Roye"]
        assert out["participants"][0]["display"] == "Roye"

    def test_anchor_excludes_eyal(self):
        with patch.object(pp, "EYAL_IDENTITIES", {"eyalz111@gmail.com"}), \
             patch.object(pp, "get_team_member_by_email", return_value={"name": "Eyal Zror"}), \
             patch("processors.topic_threading._match_canonical_name", return_value=None), \
             patch("processors.topic_threading._find_thread_by_name", return_value=None):
            out = pp.anchor_event({"title": "x", "attendees": [{"email": "eyalz111@gmail.com"}]})
        assert out["participants"] == []


# =========================================================================
# gather_ping_context (deterministic, NO LLM)
# =========================================================================

class TestGatherPingContext:
    def _event(self, mins=90):
        return {"title": "Roye 1:1", "start": _iso(mins),
                "attendees": [{"email": "roye@cropsight.io", "displayName": "Roye"}]}

    def test_participant_tasks_and_no_llm(self):
        mock_sc = MagicMock()
        mock_sc.get_tasks.side_effect = [
            [{"title": "A", "deadline": "2000-01-01"}, {"title": "B"}],  # pending (A overdue)
            [],  # in_progress
        ]
        members = {"roye@cropsight.io": {"name": "Roye Tadmor"}}
        with patch.object(pp, "EYAL_IDENTITIES", set()), \
             patch.object(pp, "supabase_client", mock_sc), \
             patch.object(pp, "get_team_member_by_email", side_effect=lambda e: members.get(e)), \
             patch("processors.topic_threading._match_canonical_name", return_value=None), \
             patch("processors.topic_threading._find_thread_by_name", return_value=None), \
             patch("core.llm.call_llm", side_effect=AssertionError("Tier-1 must not call the LLM")):
            ctx = pp.gather_ping_context(self._event())
        assert ctx["give_up"] is False
        assert ctx["people"][0]["display"] == "Roye"
        assert ctx["people"][0]["open"] == 2
        assert ctx["people"][0]["overdue"] == ["A"]
        assert ctx["change_flag"] is True  # overdue present

    def test_give_up_when_nothing(self):
        mock_sc = MagicMock()
        mock_sc.get_tasks.return_value = []
        members = {"roye@cropsight.io": {"name": "Roye Tadmor"}}
        with patch.object(pp, "EYAL_IDENTITIES", set()), \
             patch.object(pp, "supabase_client", mock_sc), \
             patch.object(pp, "get_team_member_by_email", side_effect=lambda e: members.get(e)), \
             patch("processors.topic_threading._match_canonical_name", return_value=None), \
             patch("processors.topic_threading._find_thread_by_name", return_value=None):
            ctx = pp.gather_ping_context(self._event())
        assert ctx["give_up"] is True
        assert pp.format_ping_text(ctx).endswith("Nothing flagged on this one.")

    def test_topic_enrichment_no_participants(self):
        mock_sc = MagicMock()
        mock_sc.get_tasks.return_value = []
        thread = {"topic_name": "ML accuracy",
                  "brief_json": {"current_status": "blocked", "open_items": [{"description": "x"}], "sensitivity": "team"}}
        with patch.object(pp, "EYAL_IDENTITIES", set()), \
             patch.object(pp, "supabase_client", mock_sc), \
             patch.object(pp, "get_team_member_by_email", return_value=None), \
             patch("processors.topic_threading._match_canonical_name", return_value="ML accuracy"), \
             patch("processors.topic_threading._find_thread_by_name", return_value=thread):
            ctx = pp.gather_ping_context(self._event())
        assert ctx["give_up"] is False
        assert ctx["topic"]["status"] == "blocked"
        assert ctx["change_flag"] is True  # blocked topic


# =========================================================================
# scheduler: window / floor / fire-once / reschedule / gates
# =========================================================================

class TestPrepPingScheduler:
    def _sched(self):
        from schedulers.prep_ping_scheduler import PrepPingScheduler
        return PrepPingScheduler()

    def _patches(self, events):
        import schedulers.prep_ping_scheduler as mod
        return [
            patch.object(mod.calendar_service, "get_events_needing_prep", AsyncMock(return_value=events)),
            patch.object(mod, "is_cropsight_meeting", return_value=True),
            patch.object(mod, "eyal_is_attendee", return_value=True),
            patch.object(mod, "gather_ping_context", return_value={"give_up": False}),
            patch.object(mod, "format_ping_text", return_value="ping"),
            patch.object(mod.comms_spine, "send_to_eyal", AsyncMock(return_value=True)),
            patch.object(mod.supabase_client, "log_action", MagicMock()),
        ]

    async def _run(self, sched, events):
        from contextlib import ExitStack
        with ExitStack() as stack:
            for p in self._patches(events):
                stack.enter_context(p)
            return await sched._check_and_ping()

    async def test_window_and_floor(self):
        sched = self._sched()
        # 30 min out (in window 15..90) → ping; 10 min (< floor 15) → skip; 100 min (> lead) → skip
        events = [
            {"id": "in", "title": "A", "start": _iso(30)},
            {"id": "late", "title": "B", "start": _iso(10)},
            {"id": "early", "title": "C", "start": _iso(100)},
        ]
        sent = await self._run(sched, events)
        assert [s["event_id"] for s in sent] == ["in"]

    async def test_fire_once(self):
        sched = self._sched()
        events = [{"id": "m1", "title": "A", "start": _iso(45)}]
        first = await self._run(sched, events)
        second = await self._run(sched, events)  # same event_id + start
        assert len(first) == 1 and len(second) == 0

    async def test_reschedule_repings(self):
        sched = self._sched()
        start1 = _iso(45)
        await self._run(sched, [{"id": "m1", "title": "A", "start": start1}])
        # moved to a new time → new composite key → re-ping
        sent2 = await self._run(sched, [{"id": "m1", "title": "A", "start": _iso(80)}])
        assert len(sent2) == 1

    async def test_eyal_gate_blocks(self):
        sched = self._sched()
        import schedulers.prep_ping_scheduler as mod
        from contextlib import ExitStack
        events = [{"id": "m1", "title": "Roye<>Paolo", "start": _iso(45)}]
        with ExitStack() as stack:
            for p in self._patches(events):
                stack.enter_context(p)
            stack.enter_context(patch.object(mod, "eyal_is_attendee", return_value=False))
            sent = await sched._check_and_ping()
        assert sent == []

    async def test_reconstruct_rebuilds_pinged(self):
        sched = self._sched()
        import schedulers.prep_ping_scheduler as mod
        rows = [{"event_id": "m1", "event_start": "2026-05-27T10:00:00+00:00"}]
        with patch.object(mod.supabase_client, "get_recent_prep_pings", MagicMock(return_value=rows)):
            n = await sched.reconstruct_prep_timers()
        assert n == 1
        assert sched._key("m1", "2026-05-27T10:00:00+00:00") in sched._pinged


# =========================================================================
# Tier-2 synthesis (single LLM + fallback)
# =========================================================================

class TestSynthesizeBrief:
    def _setup(self):
        members = {"roye@cropsight.io": {"name": "Roye Tadmor"}}
        mock_sc = MagicMock()
        mock_sc.get_tasks.return_value = [{"title": "A", "deadline": "2000-01-01"}]
        return members, mock_sc

    async def test_single_llm_call(self):
        members, mock_sc = self._setup()
        event = {"title": "Roye 1:1", "attendees": [{"email": "roye@cropsight.io", "displayName": "Roye"}]}
        mock_llm = MagicMock(return_value=("Tight brief here", {}))
        with patch.object(pp, "EYAL_IDENTITIES", set()), \
             patch.object(pp, "supabase_client", mock_sc), \
             patch.object(pp, "get_team_member_by_email", side_effect=lambda e: members.get(e)), \
             patch("processors.topic_threading._match_canonical_name", return_value=None), \
             patch("processors.topic_threading._find_thread_by_name", return_value=None), \
             patch("processors.meeting_prep.find_relevant_decisions", AsyncMock(return_value=[])), \
             patch("processors.meeting_prep._find_open_questions", return_value=[]), \
             patch("processors.meeting_continuity.build_meeting_continuity_context", return_value=""), \
             patch("core.llm.call_llm", mock_llm):
            out = await pp.synthesize_prepare_brief(event)
        assert out == "Tight brief here"
        assert mock_llm.call_count == 1

    async def test_fallback_on_llm_failure(self):
        members, mock_sc = self._setup()
        event = {"title": "Roye 1:1", "attendees": [{"email": "roye@cropsight.io", "displayName": "Roye"}]}
        with patch.object(pp, "EYAL_IDENTITIES", set()), \
             patch.object(pp, "supabase_client", mock_sc), \
             patch.object(pp, "get_team_member_by_email", side_effect=lambda e: members.get(e)), \
             patch("processors.topic_threading._match_canonical_name", return_value=None), \
             patch("processors.topic_threading._find_thread_by_name", return_value=None), \
             patch("processors.meeting_prep.find_relevant_decisions", AsyncMock(return_value=[])), \
             patch("processors.meeting_prep._find_open_questions", return_value=[]), \
             patch("processors.meeting_continuity.build_meeting_continuity_context", return_value=""), \
             patch("core.llm.call_llm", MagicMock(side_effect=RuntimeError("boom"))):
            out = await pp.synthesize_prepare_brief(event)
        assert "Prep — Roye 1:1" in out and "open task" in out  # deterministic fallback
