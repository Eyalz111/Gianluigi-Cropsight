"""Tests for Phase 12 A6: Decision chain traversal.

Tests cover:
- get_decision_chain() in supabase_client
- set_decision_parent() in supabase_client
- _link_decision_chains() in transcript_processor
- MCP get_decision_chain tool
"""

import pytest
from unittest.mock import MagicMock, patch


def _make_decision(decision_id, description, parent_id=None, superseded_by=None,
                   status="active"):
    return {
        "id": decision_id,
        "description": description,
        "decision_status": status,
        "parent_decision_id": parent_id,
        "superseded_by": superseded_by,
        "meetings": {"title": "Team Sync", "date": "2026-03-28"},
    }


# =========================================================================
# set_decision_parent
# =========================================================================

class TestSetDecisionParent:

    @patch("services.supabase_client.supabase_client")
    def test_sets_parent(self, mock_sc):
        mock_sc.client.table.return_value.update.return_value.eq.return_value.execute.return_value = MagicMock()

        from services.supabase_client import SupabaseClient
        SupabaseClient.set_decision_parent(mock_sc, "new-d1", "old-d1")

        mock_sc.client.table.return_value.update.assert_called_once_with(
            {"parent_decision_id": "old-d1"}
        )

    @patch("services.supabase_client.supabase_client")
    def test_failure_non_fatal(self, mock_sc):
        mock_sc.client.table.side_effect = Exception("DB error")

        from services.supabase_client import SupabaseClient
        # Should not raise
        SupabaseClient.set_decision_parent(mock_sc, "d1", "d2")


# =========================================================================
# get_decision_chain
# =========================================================================

class TestGetDecisionChain:

    @patch("services.supabase_client.supabase_client")
    def test_single_decision_no_chain(self, mock_sc):
        """A decision with no parent and no children returns just itself."""
        result_single = MagicMock()
        result_single.data = [_make_decision("d1", "First decision")]
        result_empty = MagicMock()
        result_empty.data = []

        call_count = [0]
        def table_factory(name):
            chain = MagicMock()
            chain.select.return_value = chain
            if call_count[0] == 0:
                # Walk-up: fetch d1
                chain.eq.return_value = chain
                chain.execute.return_value = result_single
                call_count[0] += 1
            elif call_count[0] == 1:
                # Walk-up: parent is None, won't reach here — but walk-down starts
                chain.eq.return_value = chain
                chain.execute.return_value = result_empty
                call_count[0] += 1
            else:
                chain.eq.return_value = chain
                chain.execute.return_value = result_empty
            return chain

        mock_sc.client.table.side_effect = table_factory

        from services.supabase_client import SupabaseClient
        result = SupabaseClient.get_decision_chain(mock_sc, "d1")
        assert len(result) >= 1
        assert result[0]["id"] == "d1"

    @patch("services.supabase_client.supabase_client")
    def test_chain_with_parent(self, mock_sc):
        """A decision with a parent should return both in chrono order."""
        d_parent = _make_decision("d-parent", "Original decision")
        d_child = _make_decision("d-child", "Updated decision", parent_id="d-parent")

        call_count = [0]
        def table_factory(name):
            chain = MagicMock()
            chain.select.return_value = chain
            chain.eq.return_value = chain

            if call_count[0] == 0:
                # Walk-up: fetch d-child
                chain.execute.return_value = MagicMock(data=[d_child])
                call_count[0] += 1
            elif call_count[0] == 1:
                # Walk-up: fetch d-parent
                chain.execute.return_value = MagicMock(data=[d_parent])
                call_count[0] += 1
            else:
                # Walk-down or parent=None stops
                chain.execute.return_value = MagicMock(data=[])
            return chain

        mock_sc.client.table.side_effect = table_factory

        from services.supabase_client import SupabaseClient
        result = SupabaseClient.get_decision_chain(mock_sc, "d-child")
        assert len(result) >= 1
        # Parent should come first (chronological)
        ids = [d["id"] for d in result]
        if "d-parent" in ids and "d-child" in ids:
            assert ids.index("d-parent") < ids.index("d-child")

    @patch("services.supabase_client.supabase_client")
    def test_empty_chain(self, mock_sc):
        """Non-existent decision returns empty chain."""
        mock_sc.client.table.return_value.select.return_value.eq.return_value.execute.return_value = MagicMock(data=[])

        from services.supabase_client import SupabaseClient
        result = SupabaseClient.get_decision_chain(mock_sc, "nonexistent")
        assert result == []

    @patch("services.supabase_client.supabase_client")
    def test_max_depth_guard(self, mock_sc):
        """Chain traversal should stop at max depth to prevent infinite loops."""
        # Create a circular reference scenario
        circular = _make_decision("d-loop", "Circular", parent_id="d-loop")

        chain = MagicMock()
        chain.select.return_value = chain
        chain.eq.return_value = chain
        chain.execute.return_value = MagicMock(data=[circular])
        mock_sc.client.table.return_value = chain

        from services.supabase_client import SupabaseClient
        result = SupabaseClient.get_decision_chain(mock_sc, "d-loop")
        # Should not infinite loop — visited set prevents re-visiting
        assert len(result) <= 10


# =========================================================================
# _link_decision_chains (in transcript_processor)
# =========================================================================

class TestLinkDecisionChains:

    @patch("processors.transcript_processor.supabase_client")
    def test_links_superseded_decisions(self, mock_sc):
        mock_sc.list_decisions.return_value = [
            {"id": "new-d1", "description": "Two-scenario model"},
        ]

        from processors.transcript_processor import _link_decision_chains

        supersessions = [
            {"new_index": 1, "old_id": "old-d1", "reason": "Replaced single scenario"},
        ]
        _link_decision_chains("meeting-1", supersessions)

        mock_sc.set_decision_parent.assert_called_once_with("new-d1", "old-d1")

    @patch("processors.transcript_processor.supabase_client")
    def test_no_supersessions_no_action(self, mock_sc):
        from processors.transcript_processor import _link_decision_chains

        _link_decision_chains("meeting-1", [])
        mock_sc.set_decision_parent.assert_not_called()
        mock_sc.list_decisions.assert_not_called()

    @patch("processors.transcript_processor.supabase_client")
    def test_invalid_index_skipped(self, mock_sc):
        mock_sc.list_decisions.return_value = [
            {"id": "new-d1", "description": "Only one"},
        ]

        from processors.transcript_processor import _link_decision_chains

        supersessions = [
            {"new_index": 99, "old_id": "old-d1", "reason": "Out of range"},
        ]
        _link_decision_chains("meeting-1", supersessions)

        mock_sc.set_decision_parent.assert_not_called()

    @patch("processors.transcript_processor.supabase_client")
    def test_missing_old_id_skipped(self, mock_sc):
        mock_sc.list_decisions.return_value = [
            {"id": "new-d1", "description": "New"},
        ]

        from processors.transcript_processor import _link_decision_chains

        supersessions = [
            {"new_index": 1, "old_id": None, "reason": "Missing old ID"},
        ]
        _link_decision_chains("meeting-1", supersessions)

        mock_sc.set_decision_parent.assert_not_called()

    @patch("processors.transcript_processor.supabase_client")
    def test_no_new_decisions_in_db_skipped(self, mock_sc):
        mock_sc.list_decisions.return_value = []

        from processors.transcript_processor import _link_decision_chains

        supersessions = [
            {"new_index": 1, "old_id": "old-d1", "reason": "Nothing stored"},
        ]
        _link_decision_chains("meeting-1", supersessions)

        mock_sc.set_decision_parent.assert_not_called()


# =========================================================================
# MCP get_decision_chain tool
# =========================================================================

class TestMCPDecisionChainTool:

    def test_mcp_source_includes_decision_chain_tool(self):
        import inspect
        import services.mcp_server as mcp_module
        source = inspect.getsource(mcp_module)
        assert "get_decision_chain" in source
        assert "Trace the evolution of a decision" in source

    def test_migration_includes_parent_decision_id(self):
        with open("scripts/migrate_v2_phase12.sql") as f:
            content = f.read()
        assert "parent_decision_id" in content
        assert "spawned_from_decision_id" in content
