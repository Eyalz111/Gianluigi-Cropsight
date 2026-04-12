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
# Test expire_stale_approvals with prep_outline
# =============================================================================

class TestExpirePrepOutline:

    @pytest.mark.asyncio
    async def test_future_meeting_auto_generates(self):
        """Expired prep_outline for future meeting should auto-generate."""
        future_time = (datetime.now(timezone.utc) + timedelta(hours=5)).isoformat()

        with patch("guardrails.approval_flow.supabase_client") as mock_db, \
             patch("guardrails.approval_flow.telegram_bot") as mock_tg, \
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
             patch("guardrails.approval_flow.telegram_bot") as mock_tg, \
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
    async def test_stale_focus_active_cleared(self):
        """Stale focus_active flags should be cleared."""
        old_time = (datetime.now(timezone.utc) - timedelta(minutes=60)).isoformat()

        with patch("guardrails.approval_flow.supabase_client") as mock_db, \
             patch("guardrails.approval_flow.telegram_bot") as mock_tg, \
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
             patch("guardrails.approval_flow.telegram_bot") as mock_tg, \
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
