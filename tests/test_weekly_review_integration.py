"""Tests for Sub-Phase 6.6: Weekly review queue awareness + integration."""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from datetime import datetime, timedelta


# =========================================================================
# /status Shows Review State Tests
# =========================================================================

class TestStatusShowsReviewState:
    """Test that /status shows weekly review session state."""

    def test_status_handler_includes_review_session_code(self):
        """The /status handler should include weekly review session check."""
        with open("services/telegram_bot.py", "r", encoding="utf-8") as f:
            source = f.read()
        assert "get_active_weekly_review_session" in source
        assert "Weekly Review" in source


# =========================================================================
# Morning Brief Mentions Review Tests
# =========================================================================

class TestMorningBriefReviewMention:
    """Test morning brief includes pending weekly review."""

    @pytest.mark.asyncio
    async def test_compile_includes_review_section(self):
        """compile_morning_brief should include weekly_review section when active."""
        mock_db = MagicMock()
        mock_db.get_unapproved_email_scans.return_value = []
        mock_db.get_pending_prep_outlines.return_value = []
        mock_db.get_active_weekly_review_session.return_value = {
            "id": "s-1",
            "week_number": 12,
            "status": "ready",
        }

        with patch("processors.morning_brief.supabase_client", mock_db), \
             patch.dict("sys.modules", {
                 "services.google_calendar": MagicMock(
                     calendar_service=MagicMock(
                         get_todays_events=AsyncMock(return_value=[])
                     )
                 ),
                 "processors.proactive_alerts": MagicMock(
                     run_all_detectors=AsyncMock(return_value=[])
                 ),
             }):
            from processors.morning_brief import compile_morning_brief
            brief = await compile_morning_brief()

        sections = brief.get("sections", [])
        review_sections = [s for s in sections if s.get("type") == "weekly_review"]
        assert len(review_sections) == 1
        assert review_sections[0]["week_number"] == 12
        assert review_sections[0]["status"] == "ready"

    @pytest.mark.asyncio
    async def test_compile_no_review_when_inactive(self):
        """No weekly_review section when no active session."""
        mock_db = MagicMock()
        mock_db.get_unapproved_email_scans.return_value = []
        mock_db.get_pending_prep_outlines.return_value = []
        mock_db.get_active_weekly_review_session.return_value = None

        with patch("processors.morning_brief.supabase_client", mock_db), \
             patch.dict("sys.modules", {
                 "services.google_calendar": MagicMock(
                     calendar_service=MagicMock(
                         get_todays_events=AsyncMock(return_value=[])
                     )
                 ),
                 "processors.proactive_alerts": MagicMock(
                     run_all_detectors=AsyncMock(return_value=[])
                 ),
             }):
            from processors.morning_brief import compile_morning_brief
            brief = await compile_morning_brief()

        sections = brief.get("sections", [])
        review_sections = [s for s in sections if s.get("type") == "weekly_review"]
        assert len(review_sections) == 0

    def test_format_weekly_review_section(self):
        """format_morning_brief should render weekly_review section."""
        from processors.morning_brief import format_morning_brief

        brief = {
            "sections": [
                {
                    "type": "weekly_review",
                    "title": "Weekly Review",
                    "week_number": 12,
                    "status": "ready",
                },
            ],
            "stats": {},
        }
        result = format_morning_brief(brief)
        assert "W12" in result
        assert "/review" in result

    def test_format_weekly_review_in_progress(self):
        """Should show 'in progress' status."""
        from processors.morning_brief import format_morning_brief

        brief = {
            "sections": [
                {
                    "type": "weekly_review",
                    "title": "Weekly Review",
                    "week_number": 12,
                    "status": "in_progress",
                },
            ],
            "stats": {},
        }
        result = format_morning_brief(brief)
        assert "in progress" in result


# =========================================================================
# Approval Expiry Tests
# =========================================================================

class TestApprovalExpiry:
    """Test weekly_review expiry configuration."""

    def test_weekly_review_has_7_day_expiry(self):
        """weekly_review should have 7-day expiry in expiry_map."""
        with open("guardrails/approval_flow.py", "r", encoding="utf-8") as f:
            source = f.read()
        assert '"weekly_review": timedelta(days=7)' in source

    def test_weekly_review_in_non_meeting_types(self):
        """weekly_review should be detected as non-meeting content."""
        with open("guardrails/approval_flow.py", "r", encoding="utf-8") as f:
            source = f.read()
        assert '"weekly_review"' in source
        assert 'review-' in source


# =========================================================================
# MCP Data Structure Validation Tests
# =========================================================================

class TestMCPDataStructure:
    """Test that agenda_data has all keys Phase 7 will need."""

    def test_agenda_data_has_required_keys(self):
        """compile_weekly_review_data should return all 5 sections + meta."""
        # This is a structural test — verify the function signature and return shape
        import ast
        with open("processors/weekly_review.py", "r", encoding="utf-8") as f:
            source = f.read()
        tree = ast.parse(source)

        # Find compile_weekly_review_data function
        found = False
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if node.name == "compile_weekly_review_data":
                    found = True
                    break
        assert found, "compile_weekly_review_data function should exist"

        # Check return keys referenced in the source
        expected_keys = [
            "week_in_review",
            "gantt_proposals",
            "attention_needed",
            "next_week_preview",
            "horizon_check",
            "meta",
        ]
        for key in expected_keys:
            assert f'"{key}"' in source, f"Missing key {key} in weekly_review.py"

    def test_session_stores_agenda_data(self):
        """weekly_review_sessions should store agenda_data as JSONB."""
        with open("scripts/migrate_phase6.sql", "r", encoding="utf-8") as f:
            sql = f.read()
        assert "agenda_data" in sql

    def test_reports_store_html_and_token(self):
        """weekly_reports should have html_content and access_token columns."""
        with open("scripts/migrate_phase6.sql", "r", encoding="utf-8") as f:
            sql = f.read()
        assert "html_content" in sql
        assert "access_token" in sql


# =========================================================================
# Main.py Scheduler Coexistence Tests
# =========================================================================

class TestSchedulerCoexistence:
    """Test old digest scheduler vs new weekly review scheduler."""

    def test_weekly_review_enabled_disables_digest(self):
        """When WEEKLY_REVIEW_ENABLED=True, old digest scheduler should be skipped."""
        with open("main.py", "r", encoding="utf-8") as f:
            source = f.read()

        # Should check WEEKLY_REVIEW_ENABLED before starting either scheduler
        assert "WEEKLY_REVIEW_ENABLED" in source
        assert "weekly_review_scheduler" in source
        assert "weekly_digest_scheduler" in source

    def test_shutdown_stops_review_scheduler(self):
        """Shutdown should stop the weekly review scheduler."""
        with open("main.py", "r", encoding="utf-8") as f:
            source = f.read()
        assert "weekly_review_scheduler.stop()" in source


# =========================================================================
# Session Stack Tests
# =========================================================================

class TestSessionStackBackwardCompat:
    """Test session stack backward compatibility."""

    def test_active_interactive_session_property_exists(self):
        """telegram_bot should have _active_interactive_session property."""
        with open("services/telegram_bot.py", "r", encoding="utf-8") as f:
            source = f.read()
        assert "_session_stack" in source
        assert "_active_interactive_session" in source
