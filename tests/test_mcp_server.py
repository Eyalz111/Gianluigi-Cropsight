"""
Tests for MCP server lifecycle, routing, and integration.

Tests verify:
- Server builds correctly with all tools registered
- Custom health/ready/report routes work
- Tool count matches expected (15 tools)
- Server readiness state management
- SSE app structure
"""

import pytest
from unittest.mock import patch, MagicMock, AsyncMock
from starlette.testclient import TestClient

from services.mcp_server import MCPServer, _success, _error, _sanitize_records


# =============================================================================
# Response Helpers
# =============================================================================


class TestResponseHelpers:
    def test_success_with_list(self):
        data = [{"id": 1}, {"id": 2}]
        result = _success(data, source="supabase")
        assert result["status"] == "success"
        assert result["data"] == data
        assert result["metadata"]["record_count"] == 2
        assert result["metadata"]["source"] == "supabase"
        assert "freshness" in result["metadata"]

    def test_success_with_dict(self):
        data = {"key": "value"}
        result = _success(data, source="composite", record_count=5)
        assert result["metadata"]["record_count"] == 5

    def test_success_with_none(self):
        result = _success(None, record_count=0)
        assert result["data"] is None
        assert result["metadata"]["record_count"] == 0

    def test_error_format(self):
        result = _error("Something went wrong")
        assert result["status"] == "error"
        assert result["error"] == "Something went wrong"
        assert "freshness" in result["metadata"]

    def test_sanitize_records(self):
        records = [
            {"id": "1", "title": "Test", "raw_transcript": "FULL TEXT", "email_body": "raw email"},
            {"id": "2", "summary": "Good", "full_text": "should be removed"},
        ]
        sanitized = _sanitize_records(records)
        assert "raw_transcript" not in sanitized[0]
        assert "email_body" not in sanitized[0]
        assert sanitized[0]["title"] == "Test"
        assert "full_text" not in sanitized[1]
        assert sanitized[1]["summary"] == "Good"

    def test_sanitize_custom_exclude(self):
        records = [{"id": "1", "secret": "hidden", "public": "visible"}]
        sanitized = _sanitize_records(records, exclude_fields={"secret"})
        assert "secret" not in sanitized[0]
        assert sanitized[0]["public"] == "visible"


# =============================================================================
# Server Build
# =============================================================================


class TestServerBuild:
    def test_builds_mcp_instance(self):
        server = MCPServer()
        mcp = server._build_mcp()
        assert mcp is not None
        assert mcp.name == "gianluigi"

    @pytest.mark.asyncio
    async def test_registers_35_tools(self):
        server = MCPServer()
        mcp = server._build_mcp()
        tools = await mcp.list_tools()
        tool_names = [t.name for t in tools]

        expected_tools = [
            "get_system_context",
            "get_last_session_summary",
            "save_session_summary",
            "search_memory",
            "get_tasks",
            "get_decisions",
            "get_open_questions",
            "deal_ops",
            "get_stakeholder_info",
            "get_meeting_history",
            "get_pending_approvals",
            "get_gantt_status",
            "get_gantt_horizon",
            "get_upcoming_meetings",
            "get_weekly_summary",
            "get_full_status",
            "start_weekly_review",
            "confirm_weekly_review",
            "update_task",
            "create_task",
            "quick_inject",
            "confirm_quick_inject",
            "propose_gantt_update",
            "approve_gantt_proposal",
            "get_system_health",
            "get_cost_summary",
            "update_decision",
            "get_decisions_for_review",
            "get_topic_thread",
            "list_topic_threads",
            "merge_topic_threads",
            "rename_topic_thread",
            "get_gantt_metrics",
            # Phase 10: canonical projects
            "list_canonical_projects",
            "add_canonical_project",
            # Phase 12: decision chain
            "get_decision_chain",
            # Cross-cutting: QA check
            "run_qa_check",
        ]

        # Intelligence Signal tools
        expected_tools.extend([
            "get_intelligence_signal_status",
            "approve_intelligence_signal",
            "trigger_intelligence_signal",
            "get_competitor_watchlist",
            "add_competitor",
        ])

        for expected in expected_tools:
            assert expected in tool_names, f"Missing tool: {expected}"

        assert len(tool_names) == 43

    @pytest.mark.asyncio
    async def test_all_tools_have_descriptions(self):
        server = MCPServer()
        mcp = server._build_mcp()
        tools = await mcp.list_tools()
        for tool in tools:
            assert tool.description, f"Tool {tool.name} has no description"
            assert len(tool.description) > 10, f"Tool {tool.name} has a very short description"


# =============================================================================
# Streamable HTTP App Structure
# =============================================================================


class TestStreamableHTTPApp:
    def test_streamable_http_app_has_routes(self):
        server = MCPServer()
        server._mcp = server._build_mcp()
        app = server._mcp.streamable_http_app()

        route_paths = []
        for route in app.routes:
            if hasattr(route, "path"):
                route_paths.append(route.path)

        # Should have MCP route
        assert "/mcp" in route_paths
        # Should have custom health routes
        assert "/health" in route_paths
        assert "/ready" in route_paths


# =============================================================================
# Health/Ready Routes via Starlette TestClient
# =============================================================================


class TestHealthRoutes:
    def _make_app(self):
        server = MCPServer()
        server._mcp = server._build_mcp()
        app = server._mcp.sse_app()
        # Don't add auth middleware for health route testing
        return app, server

    def test_health_returns_200(self):
        app, _ = self._make_app()
        client = TestClient(app, raise_server_exceptions=False)
        response = client.get("/health")
        assert response.status_code == 200
        assert response.json()["status"] == "alive"

    def test_ready_returns_503_when_not_ready(self):
        app, server = self._make_app()
        server._ready = False
        client = TestClient(app, raise_server_exceptions=False)
        response = client.get("/ready")
        assert response.status_code == 503
        assert response.json()["status"] == "not_ready"

    def test_ready_returns_200_when_ready(self):
        app, server = self._make_app()
        server._ready = True
        client = TestClient(app, raise_server_exceptions=False)
        response = client.get("/ready")
        assert response.status_code == 200
        assert response.json()["status"] == "ready"


# =============================================================================
# Weekly Report Route
# =============================================================================


class TestWeeklyReportRoute:
    def _make_app(self):
        server = MCPServer()
        server._mcp = server._build_mcp()
        app = server._mcp.sse_app()
        return app

    def test_report_not_found(self):
        app = self._make_app()
        client = TestClient(app, raise_server_exceptions=False)
        with patch("services.supabase_client.supabase_client") as mock_sb:
            mock_sb.get_weekly_report_by_token.return_value = None
            response = client.get("/reports/weekly/invalid-token")
        assert response.status_code == 404

    def test_report_found(self):
        app = self._make_app()
        client = TestClient(app, raise_server_exceptions=False)
        with patch("services.supabase_client.supabase_client") as mock_sb:
            mock_sb.get_weekly_report_by_token.return_value = {
                "id": "r1",
                "html_content": "<h1>Weekly Report</h1>",
                "expires_at": None,
                "access_count": 0,
            }
            mock_sb.update_weekly_report.return_value = {}
            response = client.get("/reports/weekly/valid-token")
        assert response.status_code == 200
        assert "<h1>Weekly Report</h1>" in response.text

    def test_expired_report(self):
        app = self._make_app()
        client = TestClient(app, raise_server_exceptions=False)
        with patch("services.supabase_client.supabase_client") as mock_sb:
            mock_sb.get_weekly_report_by_token.return_value = {
                "id": "r1",
                "html_content": "<h1>Old Report</h1>",
                "expires_at": "2020-01-01T00:00:00Z",
                "access_count": 5,
            }
            response = client.get("/reports/weekly/expired-token")
        assert response.status_code == 200
        assert "Expired" in response.text


# =============================================================================
# Readiness State
# =============================================================================


class TestReadinessState:
    def test_initial_state_not_ready(self):
        server = MCPServer()
        assert server.is_ready is False

    def test_set_ready_true(self):
        server = MCPServer()
        server.set_ready(True)
        assert server.is_ready is True

    def test_set_ready_false_after_true(self):
        server = MCPServer()
        server.set_ready(True)
        server.set_ready(False)
        assert server.is_ready is False
