"""
Tests for Phase 5.6: Queue Awareness + Integration.

Tests cover:
- Morning brief includes prep outline mentions
- Debrief start surfaces pending prep outlines
- /status shows prep outlines with time info
- Expiry: future meeting → auto-generate
- Expiry: past meeting → expire silently
- Stale focus_active cleanup
"""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from datetime import datetime, timedelta, timezone


# =============================================================================
# Test Morning Brief includes prep outlines
# =============================================================================

class TestMorningBriefPrepOutlines:

    @pytest.mark.asyncio
    async def test_pending_preps_in_brief(self):
        """Morning brief should include pending prep outlines section."""
        with patch("processors.morning_brief.supabase_client") as mock_db:

            mock_db.get_unapproved_email_scans.return_value = []
            mock_db.get_pending_prep_outlines.return_value = [{
                "approval_id": "outline-evt1",
                "content": {
                    "outline": {
                        "event": {"title": "Tech Review", "start": "2026-03-17T10:00:00+02:00"},
                    },
                },
            }]

            from processors.morning_brief import compile_morning_brief

            brief = await compile_morning_brief()
            section_types = [s["type"] for s in brief["sections"]]
            assert "pending_prep_outlines" in section_types

            prep_section = [s for s in brief["sections"] if s["type"] == "pending_prep_outlines"][0]
            assert len(prep_section["items"]) == 1
            assert prep_section["items"][0]["title"] == "Tech Review"

    def test_format_pending_preps(self):
        """Format should include prep outline titles."""
        from processors.morning_brief import format_morning_brief

        brief = {
            "sections": [{
                "type": "pending_prep_outlines",
                "title": "Pending Prep Outlines",
                "items": [
                    {"title": "Tech Review", "time": "10:00"},
                    {"title": "Business Sync", "time": "14:00"},
                ],
            }],
            "stats": {},
        }

        text = format_morning_brief(brief)
        assert "Tech Review" in text
        assert "Business Sync" in text
        assert "prep pending" in text

    @pytest.mark.asyncio
    async def test_no_pending_preps_no_section(self):
        """No pending preps → no section added."""
        with patch("processors.morning_brief.supabase_client") as mock_db:

            mock_db.get_unapproved_email_scans.return_value = []
            mock_db.get_pending_prep_outlines.return_value = []

            from processors.morning_brief import compile_morning_brief

            brief = await compile_morning_brief()
            section_types = [s["type"] for s in brief["sections"]]
            assert "pending_prep_outlines" not in section_types


# =============================================================================
# Test Debrief surfaces pending preps
# =============================================================================

class TestDebriefPendingPreps:

    @pytest.mark.asyncio
    async def test_debrief_mentions_pending_preps(self):
        """Debrief start should mention pending prep outlines."""
        with patch("processors.debrief.supabase_client") as mock_db, \
             patch("services.google_calendar.calendar_service") as mock_cal:

            mock_db.get_active_debrief_session.return_value = None
            mock_db.create_debrief_session.return_value = {"id": "session-1"}
            mock_db.update_debrief_session.return_value = {}
            mock_db.get_pending_prep_outlines.return_value = [{
                "content": {
                    "outline": {"event": {"title": "Tech Review"}},
                },
            }]
            mock_cal.get_todays_events = AsyncMock(return_value=[])

            from processors.debrief import start_debrief

            result = await start_debrief(user_id="eyal")

            assert "pending prep outline" in result["response"]
            assert "Tech Review" in result["response"]

    @pytest.mark.asyncio
    async def test_debrief_no_pending_preps(self):
        """Debrief with no pending preps should not mention them."""
        with patch("processors.debrief.supabase_client") as mock_db, \
             patch("services.google_calendar.calendar_service") as mock_cal:

            mock_db.get_active_debrief_session.return_value = None
            mock_db.create_debrief_session.return_value = {"id": "session-1"}
            mock_db.update_debrief_session.return_value = {}
            mock_db.get_pending_prep_outlines.return_value = []
            mock_cal.get_todays_events = AsyncMock(return_value=[])

            from processors.debrief import start_debrief

            result = await start_debrief(user_id="eyal")

            assert "pending prep outline" not in result["response"]


# =============================================================================
# Test expire_past_prep_approvals — the meeting-time sweep (supabase method)
# =============================================================================

class TestExpirePastPrepApprovals:

    def _mock_client(self, monkeypatch, rows):
        from services.supabase_client import supabase_client as sc
        table = MagicMock()
        # SELECT chain -> the pending prep rows
        (table.select.return_value.eq.return_value
              .in_.return_value.execute.return_value.data) = rows
        # UPDATE chain -> echo a row back so it counts as expired
        (table.update.return_value.eq.return_value
              .eq.return_value.execute.return_value.data) = [{"ok": True}]
        client = MagicMock()
        client.table.return_value = table
        monkeypatch.setattr(sc, "_client", client)
        return sc, table

    def test_past_meeting_prep_is_expired(self, monkeypatch):
        past = (datetime.now(timezone.utc) - timedelta(hours=12)).isoformat()
        sc, table = self._mock_client(monkeypatch, [
            {"approval_id": "prep-past", "content": {"start_time": past}},
        ])
        expired = sc.expire_past_prep_approvals()
        assert len(expired) == 1
        table.update.assert_called_once()   # the past card was updated to expired

    def test_future_meeting_prep_is_kept(self, monkeypatch):
        future = (datetime.now(timezone.utc) + timedelta(hours=12)).isoformat()
        sc, table = self._mock_client(monkeypatch, [
            {"approval_id": "prep-future", "content": {"start_time": future}},
        ])
        expired = sc.expire_past_prep_approvals()
        assert expired == []
        table.update.assert_not_called()    # a still-useful prep is never swept

    def test_unparseable_start_is_kept(self, monkeypatch):
        sc, table = self._mock_client(monkeypatch, [
            {"approval_id": "prep-bad", "content": {"start_time": "not-a-date"}},
            {"approval_id": "prep-none", "content": {}},
        ])
        expired = sc.expire_past_prep_approvals()
        assert expired == []                 # no meeting time -> never wrongly expire
        table.update.assert_not_called()


# =============================================================================
# Test expire_stale_approvals with prep_outline
# =============================================================================

class TestExpirePrepOutline:

    @pytest.mark.asyncio
    async def test_future_meeting_auto_generates(self):
        """Expired prep_outline for future meeting should auto-generate."""
        future_time = (datetime.now(timezone.utc) + timedelta(hours=5)).isoformat()

        with patch("guardrails.approval_flow.supabase_client") as mock_db, \
             patch("guardrails.approval_flow.comms_spine") as mock_tg, \
             patch("guardrails.approval_flow.settings") as mock_settings, \
             patch("processors.meeting_prep.generate_meeting_prep_from_outline",
                   new_callable=AsyncMock) as mock_gen:

            mock_db.expire_pending_approvals.return_value = [{
                "approval_id": "outline-evt1",
                "content_type": "prep_outline",
                "content": {
                    "title": "Tech Review",
                    "event_start_time": future_time,
                    "outline": {"event": {"title": "Tech Review"}},
                },
            }]
            mock_db.update_pending_approval.return_value = {}
            mock_db.get_pending_prep_outlines.return_value = []
            mock_tg.send_to_eyal = AsyncMock(return_value=True)
            mock_settings.MEETING_PREP_FOCUS_TIMEOUT_MINUTES = 30
            mock_gen.return_value = {"status": "success"}

            from guardrails.approval_flow import expire_stale_approvals

            result = await expire_stale_approvals()

            assert len(result) == 1
            # Should have re-set to pending then auto-generated
            mock_db.update_pending_approval.assert_called()

    @pytest.mark.asyncio
    async def test_past_meeting_expires_silently(self):
        """Expired prep_outline for past meeting should just expire."""
        past_time = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()

        with patch("guardrails.approval_flow.supabase_client") as mock_db, \
             patch("guardrails.approval_flow.comms_spine") as mock_tg, \
             patch("guardrails.approval_flow.settings") as mock_settings:

            mock_db.expire_pending_approvals.return_value = [{
                "approval_id": "outline-evt2",
                "content_type": "prep_outline",
                "content": {
                    "title": "Past Meeting",
                    "event_start_time": past_time,
                    "outline": {"event": {"title": "Past Meeting"}},
                },
            }]
            mock_db.get_pending_prep_outlines.return_value = []
            mock_tg.send_to_eyal = AsyncMock(return_value=True)
            mock_settings.MEETING_PREP_FOCUS_TIMEOUT_MINUTES = 30

            from guardrails.approval_flow import expire_stale_approvals

            result = await expire_stale_approvals()

            assert len(result) == 1
            # Should NOT have tried to auto-generate
            mock_db.update_pending_approval.assert_not_called()

    @pytest.mark.asyncio
    async def test_past_meeting_prep_card_is_swept(self):
        """A meeting_prep card whose meeting has passed (no expires_at) must be
        expired by the prep sweep and flow through the same cancel/notify loop.
        [2026-07-25 audit — the 5 stale prep cards for already-held meetings]"""
        with patch("guardrails.approval_flow.supabase_client") as mock_db, \
             patch("guardrails.approval_flow.comms_spine") as mock_tg, \
             patch("guardrails.approval_flow.settings") as mock_settings, \
             patch("guardrails.approval_flow.cancel_approval_reminders") as mock_cancel:

            mock_db.expire_pending_approvals.return_value = []
            mock_db.expire_past_prep_approvals.return_value = [{
                "approval_id": "prep-evt-past",
                "content_type": "meeting_prep",
                "content": {"title": "Held Meeting"},
            }]
            mock_db.get_pending_prep_outlines.return_value = []
            mock_tg.send_to_eyal = AsyncMock(return_value=True)
            mock_settings.MEETING_PREP_FOCUS_TIMEOUT_MINUTES = 30

            from guardrails.approval_flow import expire_stale_approvals

            result = await expire_stale_approvals()

            assert any(r["approval_id"] == "prep-evt-past" for r in result)
            mock_cancel.assert_any_call("prep-evt-past")   # reminders cancelled

    @pytest.mark.asyncio
    async def test_stale_focus_active_cleared(self):
        """Stale focus_active flags should be cleared."""
        old_time = (datetime.now(timezone.utc) - timedelta(minutes=60)).isoformat()

        with patch("guardrails.approval_flow.supabase_client") as mock_db, \
             patch("guardrails.approval_flow.comms_spine") as mock_tg, \
             patch("guardrails.approval_flow.settings") as mock_settings:

            mock_db.expire_pending_approvals.return_value = []
            mock_db.get_pending_prep_outlines.return_value = [{
                "approval_id": "outline-stale",
                "updated_at": old_time,
                "content": {
                    "focus_active": True,
                    "outline": {},
                },
            }]
            mock_db.update_pending_approval.return_value = {}
            mock_settings.MEETING_PREP_FOCUS_TIMEOUT_MINUTES = 30

            from guardrails.approval_flow import expire_stale_approvals

            await expire_stale_approvals()

            # Should have cleared focus_active
            update_calls = mock_db.update_pending_approval.call_args_list
            assert len(update_calls) >= 1
            content_arg = update_calls[0].kwargs.get("content") or update_calls[0][1].get("content")
            assert content_arg["focus_active"] is False

    @pytest.mark.asyncio
    async def test_recent_focus_not_cleared(self):
        """Recent focus_active flags should not be cleared."""
        recent_time = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()

        with patch("guardrails.approval_flow.supabase_client") as mock_db, \
             patch("guardrails.approval_flow.comms_spine") as mock_tg, \
             patch("guardrails.approval_flow.settings") as mock_settings:

            mock_db.expire_pending_approvals.return_value = []
            mock_db.get_pending_prep_outlines.return_value = [{
                "approval_id": "outline-recent",
                "updated_at": recent_time,
                "content": {
                    "focus_active": True,
                    "outline": {},
                },
            }]
            mock_settings.MEETING_PREP_FOCUS_TIMEOUT_MINUTES = 30

            from guardrails.approval_flow import expire_stale_approvals

            await expire_stale_approvals()

            # Should NOT have updated
            mock_db.update_pending_approval.assert_not_called()
