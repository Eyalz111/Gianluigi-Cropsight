"""
Tests for output-rendering robustness (audit PR-G): a malformed item or a
transient failure must not silently suppress the whole output or under-report.

Covers:
  P2-07 — morning-brief deal/commitment renders use .get() (a bad item can't
          KeyError and, via the caller's broad except, kill the whole brief).
  P2-08 — generate_deal_pulse tolerates a bad next_action_date.
  P2-06 — debrief injection surfaces failed items instead of under-counting.
  P2-04 — an approved intelligence signal that fails to email alerts Eyal.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# =============================================================================
# P2-07 — morning-brief deal/commitment renders survive a malformed item
# =============================================================================

class TestMorningBriefRenderRobustness:
    def test_format_morning_brief_tolerates_missing_deal_keys(self):
        from processors.morning_brief import format_morning_brief
        brief = {
            "sections": [
                # deal item missing organization + detail; commitment missing days_overdue
                {"type": "deal_pulse", "items": [{"name": "Acme", "type": "overdue"}]},
                {"type": "commitments_due", "items": [{"commitment": "Send NDA"}]},
            ],
            "stats": {},
        }
        out = format_morning_brief(brief)  # must NOT raise KeyError
        assert "Acme" in out
        assert "Send NDA" in out

    def test_v2_assemble_tolerates_missing_deal_keys(self):
        from processors.morning_brief import _assemble_v2_groups
        sections = [
            {"type": "deal_pulse", "items": [{"name": "Beta Corp"}]},
            {"type": "commitments_due", "items": [{"commitment": "Term sheet"}]},
        ]
        groups = _assemble_v2_groups(sections)  # must NOT raise
        deals_blob = " ".join(groups.get("deals", []))
        assert "Beta Corp" in deals_blob
        assert "Term sheet" in deals_blob


# =============================================================================
# P2-08 — generate_deal_pulse tolerates a bad next_action_date
# =============================================================================

class TestDealPulseDateGuard:
    def test_time_component_date_is_parsed_not_crashed(self):
        from processors import deal_intelligence
        deals = [{
            "name": "Acme", "organization": "Acme Inc",
            "next_action": "Follow up",
            "next_action_date": "2026-06-01T00:00:00",  # time component → old code crashed
        }]
        with patch.object(deal_intelligence.supabase_client,
                           "get_overdue_deal_actions", return_value=deals), \
             patch.object(deal_intelligence.supabase_client,
                           "get_stale_deals", return_value=[]):
            items = deal_intelligence.generate_deal_pulse()
        assert len(items) == 1
        assert items[0]["name"] == "Acme"

    def test_unparseable_date_is_skipped_not_crashed(self):
        from processors import deal_intelligence
        deals = [
            {"name": "Bad", "organization": "X", "next_action_date": "not-a-date"},
            {"name": "Null", "organization": "Y", "next_action_date": None},
        ]
        with patch.object(deal_intelligence.supabase_client,
                           "get_overdue_deal_actions", return_value=deals), \
             patch.object(deal_intelligence.supabase_client,
                           "get_stale_deals", return_value=[]):
            items = deal_intelligence.generate_deal_pulse()  # must NOT raise
        # Both overdue rows skipped; no crash.
        assert all(i.get("type") != "overdue" for i in items)


# =============================================================================
# P2-06 — debrief injection surfaces failed items
# =============================================================================

class TestDebriefFailureSurfacing:
    @pytest.mark.asyncio
    async def test_failed_item_counted_and_surfaced(self):
        from processors import debrief
        items = [
            {"type": "task", "title": "good task"},
            {"type": "task", "title": "bad task"},
        ]
        with patch.object(debrief, "supabase_client") as mock_db, \
             patch("services.embeddings.embedding_service") as mock_embed:
            mock_db.create_meeting.return_value = {"id": "m-1"}
            mock_db.get_areas.return_value = []
            # First task saves; second raises.
            mock_db.create_task.side_effect = [{"id": "t-1"}, RuntimeError("db down")]
            mock_embed.chunk_and_embed_document = AsyncMock(return_value=[])

            result = await debrief._inject_debrief_items(None, items, "2026-06-12")

        assert result["counts"]["tasks"] == 1
        assert result["counts"]["failed"] == 1
        assert "FAILED" in result["summary"]


# =============================================================================
# P2-04 — approved intelligence signal that fails to email alerts Eyal
# =============================================================================

class TestIntelligenceSignalSendFailureAlert:
    @pytest.mark.asyncio
    async def test_email_failure_alerts_eyal(self):
        from processors import intelligence_signal_agent as isa

        signal = {
            "signal_id": "signal-w14-2026",
            "signal_content": "Report body.",
            "flags": [],
            "drive_doc_url": "https://x/doc",
            "week_number": 14,
            "year": 2026,
        }

        mock_gmail = MagicMock()
        mock_gmail.send_email_with_attachments = AsyncMock(return_value=False)  # send FAILS
        mock_spine = MagicMock()
        mock_spine.send_to_eyal = AsyncMock(return_value=True)

        with patch.object(isa, "supabase_client") as mock_sc, \
             patch.object(isa, "settings") as mock_s, \
             patch("services.word_generator.generate_signal_docx", return_value=b"docx"), \
             patch("services.gmail.gmail_service", mock_gmail), \
             patch("services.orchestrator.spine.comms_spine", mock_spine):
            mock_sc.get_intelligence_signal.return_value = signal
            mock_s.INTELLIGENCE_SIGNAL_SAFE_DISTRIBUTE = False
            mock_s.intelligence_signal_recipients_list = ["eyal@cropsight.io"]

            result = await isa.distribute_intelligence_signal("signal-w14-2026")

        assert result["status"] == "error"
        # The approved-but-undelivered signal must trigger a real-time alert.
        mock_spine.send_to_eyal.assert_awaited_once()


# =============================================================================
# P2-12 — a task_urgency fetch failure surfaces as an alert, not silence
# =============================================================================

class TestMorningBriefTaskUrgencyAlert:
    @pytest.mark.asyncio
    async def test_get_tasks_failure_surfaces_alert(self):
        from config.settings import settings

        with patch("processors.morning_brief.supabase_client") as mock_sc, \
             patch("services.supabase_client.supabase_client") as mock_sc_src, \
             patch("services.google_calendar.calendar_service") as mock_cal, \
             patch("processors.sheets_sync.compute_sheets_diff", new=AsyncMock(return_value=None)), \
             patch("processors.sheets_sync.format_sync_summary", return_value=""), \
             patch("processors.meeting_continuity.build_daily_continuity_context", return_value=None), \
             patch.object(settings, "MORNING_BRIEF_ENABLED", True), \
             patch.object(settings, "EMAIL_DAILY_SCAN_ENABLED", True):
            mock_sc.get_unapproved_email_scans.return_value = []
            mock_sc.get_pending_prep_outlines.return_value = []
            mock_sc.get_active_weekly_review_session.return_value = None
            mock_sc_src.get_pending_approvals_by_status.return_value = []
            mock_cal.get_todays_events = AsyncMock(return_value=[])
            # The urgency section's get_tasks blows up.
            mock_sc.get_tasks.side_effect = RuntimeError("supabase blip")

            from processors.morning_brief import compile_morning_brief
            brief = await compile_morning_brief()

        alert_msgs = [
            a.get("message", "")
            for s in brief["sections"] if s.get("type") == "alerts"
            for a in s.get("alerts", [])
        ]
        assert any("Task urgency check failed" in m for m in alert_msgs), (
            f"expected a task-urgency alert; sections="
            f"{[s.get('type') for s in brief['sections']]}"
        )
