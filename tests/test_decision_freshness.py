"""Tests for Phase 12 A4: Decision freshness tracking.

Tests cover:
- touch_decision() in supabase_client
- get_stale_decisions() in supabase_client
- Touch calls from cross_reference (supersession detection)
- Touch calls from MCP get_decisions
- Stale decisions in weekly review attention_needed
"""

import pytest
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch, AsyncMock


def _make_decision(decision_id="d1", description="Use AWS", status="active",
                   last_referenced_at=None, created_at=None):
    return {
        "id": decision_id,
        "description": description,
        "decision_status": status,
        "last_referenced_at": last_referenced_at,
        "created_at": created_at or datetime.now(timezone.utc).isoformat(),
        "meetings": {"title": "Team Sync", "date": "2026-03-28"},
    }


# =========================================================================
# touch_decision
# =========================================================================

class TestTouchDecision:
    """Tests for supabase_client.touch_decision()."""

    @patch("services.supabase_client.supabase_client")
    def test_touch_updates_timestamp(self, mock_sc):
        mock_table = MagicMock()
        mock_sc.client.table.return_value = mock_table
        mock_update = MagicMock()
        mock_table.update.return_value = mock_update
        mock_eq = MagicMock()
        mock_update.eq.return_value = mock_eq
        mock_eq.execute.return_value = MagicMock(data=[{"id": "d1"}])

        # Call the real method on the mock's underlying class
        from services.supabase_client import SupabaseClient
        SupabaseClient.touch_decision(mock_sc, "d1")

        mock_table.update.assert_called_once()
        update_arg = mock_table.update.call_args[0][0]
        assert "last_referenced_at" in update_arg

    @patch("services.supabase_client.supabase_client")
    def test_touch_failure_non_fatal(self, mock_sc):
        """touch_decision should not raise on DB errors."""
        mock_sc.client.table.side_effect = Exception("DB error")

        from services.supabase_client import SupabaseClient
        # Should not raise
        SupabaseClient.touch_decision(mock_sc, "d1")


# =========================================================================
# get_stale_decisions
# =========================================================================

class TestGetStaleDecisions:
    """Tests for supabase_client.get_stale_decisions()."""

    @patch("services.supabase_client.supabase_client")
    def test_returns_combined_results(self, mock_sc):
        stale_result = MagicMock()
        stale_result.data = [_make_decision("d1", "Old referenced")]
        never_result = MagicMock()
        never_result.data = [_make_decision("d2", "Never referenced")]

        # Build mock chains for two table() calls
        call_count = [0]

        def make_chain():
            chain = MagicMock()
            if call_count[0] == 0:
                # First chain: stale_referenced path (not_.is_)
                chain.select.return_value = chain
                chain.eq.return_value = chain
                chain.not_.is_.return_value = chain
                chain.is_.return_value = chain
                chain.lt.return_value = chain
                chain.order.return_value = chain
                chain.limit.return_value = chain
                chain.execute.return_value = stale_result
                call_count[0] += 1
            else:
                # Second chain: never_referenced path (is_)
                chain.select.return_value = chain
                chain.eq.return_value = chain
                chain.is_.return_value = chain
                chain.lt.return_value = chain
                chain.order.return_value = chain
                chain.limit.return_value = chain
                chain.execute.return_value = never_result
            return chain

        mock_sc.client.table.side_effect = lambda name: make_chain()

        from services.supabase_client import SupabaseClient
        results = SupabaseClient.get_stale_decisions(mock_sc, days=28)
        assert len(results) == 2

    @patch("services.supabase_client.supabase_client")
    def test_returns_empty_when_no_stale(self, mock_sc):
        empty_result = MagicMock()
        empty_result.data = []

        chain = MagicMock()
        chain.select.return_value = chain
        chain.eq.return_value = chain
        chain.not_.is_.return_value = chain
        chain.is_.return_value = chain
        chain.lt.return_value = chain
        chain.order.return_value = chain
        chain.limit.return_value = chain
        chain.execute.return_value = empty_result
        mock_sc.client.table.return_value = chain

        from services.supabase_client import SupabaseClient
        results = SupabaseClient.get_stale_decisions(mock_sc, days=28)
        assert results == []

    @patch("services.supabase_client.supabase_client")
    def test_caps_at_20(self, mock_sc):
        big_result = MagicMock()
        big_result.data = [_make_decision(f"d{i}") for i in range(15)]

        chain = MagicMock()
        chain.select.return_value = chain
        chain.eq.return_value = chain
        chain.not_.is_.return_value = chain
        chain.is_.return_value = chain
        chain.lt.return_value = chain
        chain.order.return_value = chain
        chain.limit.return_value = chain
        chain.execute.return_value = big_result
        mock_sc.client.table.return_value = chain

        from services.supabase_client import SupabaseClient
        results = SupabaseClient.get_stale_decisions(mock_sc, days=28)
        assert len(results) <= 20


# =========================================================================
# Cross-reference: touch on supersession
# =========================================================================

class TestCrossReferenceTouchDecision:
    """Test that cross_reference touches decisions during supersession detection."""

    @pytest.mark.asyncio
    @patch("processors.cross_reference.settings")
    @patch("processors.cross_reference.supabase_client")
    async def test_supersession_touches_old_decision(self, mock_sc, mock_settings):
        import json

        mock_settings.CONTINUITY_AUTO_APPLY_ENABLED = False
        mock_settings.model_simple = "haiku"
        mock_settings.model_agent = "sonnet"

        # No open tasks/questions
        mock_sc.get_tasks.return_value = []
        mock_sc.get_open_questions.return_value = []

        # Existing decisions for supersession check
        mock_sc.list_decisions.return_value = [
            {
                "id": "old-d1",
                "description": "Single optimistic projection",
                "decision_status": "active",
                "meeting_id": "old-meeting",
                "label": "Fundraising",
            },
        ]

        # Mock detect_supersessions directly
        from processors.cross_reference import run_cross_reference

        with patch("processors.cross_reference.detect_supersessions", new_callable=AsyncMock) as mock_detect:
            mock_detect.return_value = [
                {"new_index": 1, "old_id": "old-d1", "reason": "Replaced by two-scenario model"}
            ]

            await run_cross_reference(
                meeting_id="new-meeting",
                transcript="test",
                new_tasks=[],
                new_decisions=[{"description": "Two scenarios", "label": "Fundraising"}],
            )

        # Verify touch_decision was called for the superseded decision
        mock_sc.touch_decision.assert_called_with("old-d1")

    @pytest.mark.asyncio
    @patch("processors.cross_reference.settings")
    @patch("processors.cross_reference.supabase_client")
    async def test_no_supersessions_no_touch(self, mock_sc, mock_settings):
        mock_settings.CONTINUITY_AUTO_APPLY_ENABLED = False
        mock_settings.model_simple = "haiku"
        mock_settings.model_agent = "sonnet"

        mock_sc.get_tasks.return_value = []
        mock_sc.get_open_questions.return_value = []
        mock_sc.list_decisions.return_value = []

        from processors.cross_reference import run_cross_reference

        with patch("processors.cross_reference.detect_supersessions", new_callable=AsyncMock) as mock_detect:
            mock_detect.return_value = []

            await run_cross_reference(
                meeting_id="new-meeting",
                transcript="test",
                new_tasks=[],
                new_decisions=[],
            )

        mock_sc.touch_decision.assert_not_called()


# =========================================================================
# Weekly review: stale decisions section
# =========================================================================

class TestWeeklyReviewStaleDecisions:

    @pytest.mark.asyncio
    @patch("processors.weekly_review.supabase_client")
    async def test_attention_needed_includes_stale_decisions(self, mock_sc):
        """_compile_attention_needed should include stale_decisions."""
        mock_sc.get_stale_tasks.return_value = []
        mock_sc.get_tasks_without_assignee.return_value = []
        mock_sc.get_tasks_without_deadline.return_value = []
        mock_sc.get_stale_decisions.return_value = [
            _make_decision("d-stale", "Old decision nobody talks about"),
        ]

        with patch("processors.proactive_alerts.generate_alerts", return_value=[]), \
             patch("services.gantt_manager.gantt_manager") as mock_gantt, \
             patch("processors.proactive_alerts.get_escalation_items", return_value=[]), \
             patch("processors.decision_review.get_decisions_due_for_review", return_value=[]):

            mock_gantt.get_gantt_horizon = AsyncMock(return_value={"milestones": []})

            from processors.weekly_review import _compile_attention_needed
            result = await _compile_attention_needed()

        assert "stale_decisions" in result
        assert len(result["stale_decisions"]) == 1
        assert result["stale_decisions"][0]["id"] == "d-stale"

    @pytest.mark.asyncio
    @patch("processors.weekly_review.supabase_client")
    async def test_stale_decisions_failure_non_fatal(self, mock_sc):
        """If get_stale_decisions fails, should not crash attention_needed."""
        mock_sc.get_stale_tasks.return_value = []
        mock_sc.get_tasks_without_assignee.return_value = []
        mock_sc.get_tasks_without_deadline.return_value = []
        mock_sc.get_stale_decisions.side_effect = Exception("DB error")

        with patch("processors.proactive_alerts.generate_alerts", return_value=[]), \
             patch("services.gantt_manager.gantt_manager") as mock_gantt, \
             patch("processors.proactive_alerts.get_escalation_items", return_value=[]), \
             patch("processors.decision_review.get_decisions_due_for_review", return_value=[]):

            mock_gantt.get_gantt_horizon = AsyncMock(return_value={"milestones": []})

            from processors.weekly_review import _compile_attention_needed
            result = await _compile_attention_needed()

        assert result["stale_decisions"] == []


# =========================================================================
# MCP get_decisions: touch on query
# =========================================================================

class TestMCPGetDecisionsTouch:
    """Test that MCP get_decisions touches queried decisions."""

    def test_mcp_source_includes_touch_call(self):
        """Verify the MCP server source code calls touch_decision in get_decisions."""
        import inspect
        import services.mcp_server as mcp_module
        source = inspect.getsource(mcp_module)
        assert "touch_decision" in source

    def test_touch_within_get_decisions_context(self):
        """Verify touch_decision is called in the get_decisions tool block."""
        import inspect
        import services.mcp_server as mcp_module
        source = inspect.getsource(mcp_module)
        # Find the get_decisions function and verify touch is in its body
        idx = source.find("async def get_decisions(")
        assert idx > 0
        # Get the function body (up to next tool)
        next_tool = source.find("# ====", idx + 10)
        func_body = source[idx:next_tool]
        assert "touch_decision" in func_body


# =========================================================================
# Migration file
# =========================================================================

class TestPhase12Migration:

    def test_migration_file_exists(self):
        import os
        path = os.path.join("scripts", "migrate_v2_phase12.sql")
        assert os.path.exists(path)

    def test_migration_includes_last_referenced_at(self):
        with open("scripts/migrate_v2_phase12.sql") as f:
            content = f.read()
        assert "last_referenced_at" in content
        assert "TIMESTAMPTZ" in content
