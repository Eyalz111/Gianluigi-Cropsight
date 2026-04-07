"""
Tests for deal_ops MCP tool — Phase 4.

Tests each action of the composite deal_ops tool:
- list, get, create, update, timeline
- commitment_list, commitment_create, commitment_update
- pulse
- error handling for missing parameters
- unknown action handling
"""

import pytest
from unittest.mock import patch, MagicMock

from services.mcp_server import MCPServer


# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture
def server():
    """Create an MCPServer with tools registered."""
    srv = MCPServer()
    srv._mcp = srv._build_mcp()
    return srv


@pytest.fixture
def mock_mcp_auth():
    """Mock the mcp_auth so log_call is a no-op."""
    with patch("services.mcp_server.mcp_auth") as mock:
        mock.log_call = MagicMock()
        yield mock


async def call_tool(server, name: str, arguments: dict | None = None):
    """Call a registered MCP tool by name."""
    result = await server._mcp.call_tool(name, arguments or {})
    if isinstance(result, list):
        import json
        for block in result:
            if hasattr(block, "text"):
                return json.loads(block.text)
    return result


# =============================================================================
# deal_ops — list
# =============================================================================


class TestDealOpsList:
    @pytest.mark.asyncio
    async def test_list_all_deals(self, server, mock_mcp_auth):
        deals = [{"id": "1", "name": "Deal A"}, {"id": "2", "name": "Deal B"}]
        with patch("services.supabase_client.supabase_client") as mock_sb:
            mock_sb.get_deals.return_value = deals
            result = await call_tool(server, "deal_ops", {"action": "list"})

        assert result["status"] == "success"
        assert len(result["data"]) == 2

    @pytest.mark.asyncio
    async def test_list_filtered_by_stage(self, server, mock_mcp_auth):
        with patch("services.supabase_client.supabase_client") as mock_sb:
            mock_sb.get_deals.return_value = [{"id": "1", "stage": "lead"}]
            result = await call_tool(server, "deal_ops", {"action": "list", "stage": "lead"})

        assert result["status"] == "success"
        mock_sb.get_deals.assert_called_with(stage="lead")


# =============================================================================
# deal_ops — get
# =============================================================================


class TestDealOpsGet:
    @pytest.mark.asyncio
    async def test_get_deal_with_timeline(self, server, mock_mcp_auth):
        deal = {"id": "deal-1", "name": "Test Deal"}
        timeline = [{"id": "int-1", "interaction_type": "meeting"}]
        with patch("services.supabase_client.supabase_client") as mock_sb:
            mock_sb.get_deal.return_value = deal
            mock_sb.get_deal_timeline.return_value = timeline
            result = await call_tool(server, "deal_ops", {"action": "get", "deal_id": "deal-1"})

        assert result["status"] == "success"
        assert result["data"]["deal"]["name"] == "Test Deal"
        assert len(result["data"]["timeline"]) == 1

    @pytest.mark.asyncio
    async def test_get_missing_deal_id(self, server, mock_mcp_auth):
        result = await call_tool(server, "deal_ops", {"action": "get"})
        assert result["status"] == "error"
        assert "deal_id required" in result["error"]

    @pytest.mark.asyncio
    async def test_get_nonexistent_deal(self, server, mock_mcp_auth):
        with patch("services.supabase_client.supabase_client") as mock_sb:
            mock_sb.get_deal.return_value = None
            result = await call_tool(server, "deal_ops", {"action": "get", "deal_id": "fake"})

        assert result["status"] == "error"
        assert "not found" in result["error"]


# =============================================================================
# deal_ops — create
# =============================================================================


class TestDealOpsCreate:
    @pytest.mark.asyncio
    async def test_create_deal(self, server, mock_mcp_auth):
        with patch("services.supabase_client.supabase_client") as mock_sb:
            mock_sb.create_deal.return_value = {"id": "new-1", "name": "New Deal", "stage": "lead"}
            result = await call_tool(server, "deal_ops", {
                "action": "create",
                "name": "New Deal",
                "organization": "NewOrg",
            })

        assert result["status"] == "success"
        assert result["data"]["id"] == "new-1"

    @pytest.mark.asyncio
    async def test_create_missing_name(self, server, mock_mcp_auth):
        result = await call_tool(server, "deal_ops", {"action": "create", "organization": "Org"})
        assert result["status"] == "error"

    @pytest.mark.asyncio
    async def test_create_missing_organization(self, server, mock_mcp_auth):
        result = await call_tool(server, "deal_ops", {"action": "create", "name": "Deal"})
        assert result["status"] == "error"


# =============================================================================
# deal_ops — update
# =============================================================================


class TestDealOpsUpdate:
    @pytest.mark.asyncio
    async def test_update_deal_stage(self, server, mock_mcp_auth):
        with patch("services.supabase_client.supabase_client") as mock_sb:
            mock_sb.update_deal.return_value = {"id": "deal-1", "stage": "proposal"}
            result = await call_tool(server, "deal_ops", {
                "action": "update",
                "deal_id": "deal-1",
                "stage": "proposal",
            })

        assert result["status"] == "success"
        assert result["data"]["stage"] == "proposal"

    @pytest.mark.asyncio
    async def test_update_missing_deal_id(self, server, mock_mcp_auth):
        result = await call_tool(server, "deal_ops", {"action": "update", "stage": "lead"})
        assert result["status"] == "error"

    @pytest.mark.asyncio
    async def test_update_no_fields(self, server, mock_mcp_auth):
        result = await call_tool(server, "deal_ops", {"action": "update", "deal_id": "deal-1"})
        assert result["status"] == "error"
        assert "No fields" in result["error"]


# =============================================================================
# deal_ops — timeline
# =============================================================================


class TestDealOpsTimeline:
    @pytest.mark.asyncio
    async def test_get_timeline(self, server, mock_mcp_auth):
        with patch("services.supabase_client.supabase_client") as mock_sb:
            mock_sb.get_deal_timeline.return_value = [
                {"id": "1", "interaction_type": "meeting"},
                {"id": "2", "interaction_type": "email"},
            ]
            result = await call_tool(server, "deal_ops", {"action": "timeline", "deal_id": "deal-1"})

        assert result["status"] == "success"
        assert len(result["data"]) == 2


# =============================================================================
# deal_ops — commitment operations
# =============================================================================


class TestDealOpsCommitments:
    @pytest.mark.asyncio
    async def test_commitment_list(self, server, mock_mcp_auth):
        with patch("services.supabase_client.supabase_client") as mock_sb:
            mock_sb.get_external_commitments.return_value = [
                {"id": "ec-1", "commitment": "Send report"},
            ]
            result = await call_tool(server, "deal_ops", {"action": "commitment_list"})

        assert result["status"] == "success"
        assert len(result["data"]) == 1

    @pytest.mark.asyncio
    async def test_commitment_create(self, server, mock_mcp_auth):
        with patch("services.supabase_client.supabase_client") as mock_sb:
            mock_sb.create_external_commitment.return_value = {"id": "ec-new", "commitment": "Deliver data"}
            result = await call_tool(server, "deal_ops", {
                "action": "commitment_create",
                "organization": "PartnerCo",
                "commitment": "Deliver data by April 15",
            })

        assert result["status"] == "success"
        assert result["data"]["id"] == "ec-new"

    @pytest.mark.asyncio
    async def test_commitment_create_missing_fields(self, server, mock_mcp_auth):
        result = await call_tool(server, "deal_ops", {"action": "commitment_create"})
        assert result["status"] == "error"

    @pytest.mark.asyncio
    async def test_commitment_update(self, server, mock_mcp_auth):
        with patch("services.supabase_client.supabase_client") as mock_sb:
            mock_sb.update_external_commitment.return_value = {"id": "ec-1", "status": "fulfilled"}
            result = await call_tool(server, "deal_ops", {
                "action": "commitment_update",
                "commitment_id": "ec-1",
                "status": "fulfilled",
            })

        assert result["status"] == "success"

    @pytest.mark.asyncio
    async def test_commitment_update_missing_id(self, server, mock_mcp_auth):
        result = await call_tool(server, "deal_ops", {"action": "commitment_update", "status": "fulfilled"})
        assert result["status"] == "error"


# =============================================================================
# deal_ops — pulse
# =============================================================================


class TestDealOpsPulse:
    @pytest.mark.asyncio
    async def test_pulse_returns_both_sections(self, server, mock_mcp_auth):
        with patch("processors.deal_intelligence.generate_deal_pulse") as mock_pulse, \
             patch("processors.deal_intelligence.generate_commitments_due") as mock_commit, \
             patch("services.supabase_client.supabase_client"):
            mock_pulse.return_value = [{"type": "overdue", "name": "D1"}]
            mock_commit.return_value = [{"organization": "Org1", "commitment": "Test"}]

            result = await call_tool(server, "deal_ops", {"action": "pulse"})

        assert result["status"] == "success"
        assert len(result["data"]["deal_pulse"]) == 1
        assert len(result["data"]["commitments_due"]) == 1


# =============================================================================
# deal_ops — unknown action
# =============================================================================


class TestDealOpsUnknown:
    @pytest.mark.asyncio
    async def test_unknown_action(self, server, mock_mcp_auth):
        result = await call_tool(server, "deal_ops", {"action": "foobar"})
        assert result["status"] == "error"
        assert "Unknown action" in result["error"]


# =============================================================================
# Verify get_commitments is removed
# =============================================================================


class TestGetCommitmentsRemoved:
    @pytest.mark.asyncio
    async def test_get_commitments_tool_not_registered(self, server):
        """Verify the deprecated get_commitments tool was removed."""
        tools = await server._mcp.list_tools()
        tool_names = [t.name for t in tools]
        assert "get_commitments" not in tool_names
        assert "deal_ops" in tool_names
