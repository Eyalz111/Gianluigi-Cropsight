"""Tests for intelligence signal MCP tools."""

import pytest
import json
from unittest.mock import patch, MagicMock, AsyncMock

from services.mcp_server import MCPServer


@pytest.fixture
def server():
    """Create an MCPServer with tools registered."""
    srv = MCPServer()
    srv._mcp = srv._build_mcp()
    return srv


@pytest.fixture
def mock_mcp_auth():
    with patch("services.mcp_server.mcp_auth") as mock:
        mock.log_call = MagicMock()
        yield mock


async def call_tool(server, name: str, arguments: dict | None = None):
    """Call a registered MCP tool by name."""
    result = await server._mcp.call_tool(name, arguments or {})
    if isinstance(result, list):
        for block in result:
            if hasattr(block, "text"):
                return json.loads(block.text)
    if isinstance(result, dict):
        return result
    return result


class TestGetIntelligenceSignalStatus:
    @pytest.mark.asyncio
    async def test_returns_latest_signal(self, server, mock_mcp_auth):
        with patch("services.supabase_client.supabase_client") as mock_sc:
            mock_sc.get_latest_intelligence_signal.return_value = {
                "signal_id": "signal-w14-2026",
                "week_number": 14,
                "year": 2026,
                "status": "pending_approval",
                "flags": [{"flag": "Test", "urgency": "high"}],
                "research_source": "perplexity",
                "drive_doc_url": "https://example.com/doc",
                "drive_video_url": None,
                "created_at": "2026-04-02T18:00:00Z",
                "distributed_at": None,
                "recipients": None,
            }
            mock_sc.get_intelligence_signals.return_value = [
                {"signal_id": "signal-w14-2026", "status": "pending_approval", "week_number": 14},
            ]

            result = await call_tool(server, "get_intelligence_signal_status")

        assert result["status"] == "success"
        assert result["data"]["signal_id"] == "signal-w14-2026"
        assert len(result["data"]["flags"]) == 1

    @pytest.mark.asyncio
    async def test_returns_specific_signal(self, server, mock_mcp_auth):
        with patch("services.supabase_client.supabase_client") as mock_sc:
            mock_sc.get_intelligence_signal.return_value = {
                "signal_id": "signal-w13-2026",
                "week_number": 13,
                "year": 2026,
                "status": "distributed",
                "flags": [],
                "research_source": "perplexity",
                "drive_doc_url": "https://example.com/doc",
                "drive_video_url": None,
                "created_at": "2026-03-26T18:00:00Z",
                "distributed_at": "2026-03-27T10:00:00Z",
                "recipients": ["eyal@cropsight.com"],
            }
            mock_sc.get_intelligence_signals.return_value = []

            result = await call_tool(
                server,
                "get_intelligence_signal_status",
                {"signal_id": "signal-w13-2026"},
            )

        assert result["status"] == "success"
        assert result["data"]["signal_id"] == "signal-w13-2026"

    @pytest.mark.asyncio
    async def test_no_signals(self, server, mock_mcp_auth):
        with patch("services.supabase_client.supabase_client") as mock_sc:
            mock_sc.get_latest_intelligence_signal.return_value = None

            result = await call_tool(server, "get_intelligence_signal_status")

        assert result["status"] == "success"
        assert "No intelligence signals" in result["data"]["message"]


class TestApproveIntelligenceSignal:
    @pytest.mark.asyncio
    async def test_approve_and_distribute(self, server, mock_mcp_auth):
        with patch("services.supabase_client.supabase_client") as mock_sc:
            mock_sc.get_intelligence_signal.return_value = {
                "signal_id": "signal-w14-2026",
                "signal_content": "Report content.",
                "status": "pending_approval",
            }
            mock_sc.update_pending_approval.return_value = {}
            mock_sc.update_intelligence_signal.return_value = {}
            mock_sc.log_action.return_value = {}

            with patch(
                "processors.intelligence_signal_agent.distribute_intelligence_signal",
                new_callable=AsyncMock,
            ) as mock_dist:
                mock_dist.return_value = {
                    "signal_id": "signal-w14-2026",
                    "status": "distributed",
                    "recipients": ["eyal@cropsight.com"],
                }

                result = await call_tool(
                    server,
                    "approve_intelligence_signal",
                    {"signal_id": "signal-w14-2026"},
                )

        assert result["status"] == "success"
        assert result["data"]["status"] == "distributed"

    @pytest.mark.asyncio
    async def test_cancel_signal(self, server, mock_mcp_auth):
        with patch("services.supabase_client.supabase_client") as mock_sc:
            mock_sc.get_intelligence_signal.return_value = {
                "signal_id": "signal-w14-2026",
                "status": "pending_approval",
            }
            mock_sc.update_intelligence_signal.return_value = {}
            mock_sc.update_pending_approval.return_value = {}
            mock_sc.log_action.return_value = {}

            result = await call_tool(
                server,
                "approve_intelligence_signal",
                {"signal_id": "signal-w14-2026", "cancel": True},
            )

        assert result["status"] == "success"
        assert result["data"]["status"] == "cancelled"

    @pytest.mark.asyncio
    async def test_not_found(self, server, mock_mcp_auth):
        with patch("services.supabase_client.supabase_client") as mock_sc:
            mock_sc.get_intelligence_signal.return_value = None

            result = await call_tool(
                server,
                "approve_intelligence_signal",
                {"signal_id": "nonexistent"},
            )

        assert result["status"] == "error"


class TestTriggerIntelligenceSignal:
    @pytest.mark.asyncio
    async def test_triggers_generation(self, server, mock_mcp_auth):
        with patch(
            "processors.intelligence_signal_agent.generate_intelligence_signal",
            new_callable=AsyncMock,
        ) as mock_gen:
            mock_gen.return_value = {
                "signal_id": "signal-w14-2026",
                "status": "pending_approval",
            }

            with patch("services.supabase_client.supabase_client") as mock_sc:
                mock_sc.log_action.return_value = {}

                result = await call_tool(server, "trigger_intelligence_signal")

        assert result["status"] == "success"
        assert result["data"]["signal_id"] == "signal-w14-2026"


class TestGetCompetitorWatchlist:
    @pytest.mark.asyncio
    async def test_returns_watchlist(self, server, mock_mcp_auth):
        with patch("services.supabase_client.supabase_client") as mock_sc:
            mock_sc.get_competitor_watchlist.return_value = [
                {"name": "SatYield", "category": "known", "is_active": True},
                {"name": "EOSDA", "category": "known", "is_active": True},
            ]

            result = await call_tool(server, "get_competitor_watchlist")

        assert result["status"] == "success"
        assert len(result["data"]) == 2

    @pytest.mark.asyncio
    async def test_include_deactivated(self, server, mock_mcp_auth):
        with patch("services.supabase_client.supabase_client") as mock_sc:
            mock_sc.get_competitor_watchlist.return_value = [
                {"name": "SatYield", "category": "known", "is_active": True},
                {"name": "OldCo", "category": "known", "is_active": False},
            ]

            result = await call_tool(
                server,
                "get_competitor_watchlist",
                {"include_deactivated": True},
            )

        assert result["status"] == "success"
        mock_sc.get_competitor_watchlist.assert_called_with(include_deactivated=True)


class TestAddCompetitor:
    @pytest.mark.asyncio
    async def test_adds_competitor(self, server, mock_mcp_auth):
        with patch("services.supabase_client.supabase_client") as mock_sc:
            mock_sc.upsert_competitor.return_value = {
                "name": "NewCo",
                "category": "watching",
            }
            mock_sc.log_action.return_value = {}

            result = await call_tool(
                server,
                "add_competitor",
                {
                    "name": "NewCo",
                    "category": "watching",
                    "funding": "$10M",
                    "notes": "New entrant",
                },
            )

        assert result["status"] == "success"
        mock_sc.upsert_competitor.assert_called_once()
        call_data = mock_sc.upsert_competitor.call_args[0][0]
        assert call_data["name"] == "NewCo"
        assert call_data["added_by"] == "eyal"

    @pytest.mark.asyncio
    async def test_add_competitor_error(self, server, mock_mcp_auth):
        with patch("services.supabase_client.supabase_client") as mock_sc:
            mock_sc.upsert_competitor.side_effect = Exception("DB error")

            result = await call_tool(
                server,
                "add_competitor",
                {"name": "FailCo"},
            )

        assert result["status"] == "error"
