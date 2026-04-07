"""
Tests for MCP tools — each tool returns correct data with mocked services.

Tests verify:
- Each tool calls the correct underlying function
- Response format matches the standard {status, data, metadata} envelope
- Error handling returns proper error responses
- Data sanitization (no raw transcript/email body in responses)
"""

import pytest
from datetime import datetime, timezone, timedelta
from unittest.mock import patch, MagicMock, AsyncMock

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
def mock_supabase():
    """Mock the supabase_client."""
    with patch("services.supabase_client.supabase_client") as mock:
        yield mock


@pytest.fixture
def mock_mcp_auth():
    """Mock the mcp_auth so log_call is a no-op."""
    with patch("services.mcp_server.mcp_auth") as mock:
        mock.log_call = MagicMock()
        yield mock


# =============================================================================
# Helper to call tools via FastMCP
# =============================================================================


async def call_tool(server, name: str, arguments: dict | None = None):
    """Call a registered MCP tool by name."""
    result = await server._mcp.call_tool(name, arguments or {})
    # FastMCP returns content blocks — extract the text content
    if isinstance(result, list):
        import json
        for block in result:
            if hasattr(block, "text"):
                return json.loads(block.text)
    if isinstance(result, dict):
        return result
    return result


# =============================================================================
# get_system_context
# =============================================================================


class TestGetSystemContext:
    @pytest.mark.asyncio
    async def test_returns_company_context(self, server, mock_mcp_auth):
        with patch("services.supabase_client.supabase_client") as mock_sb:
            mock_sb.get_tasks.return_value = [
                {"id": "1", "status": "pending", "title": "Test task"},
                {"id": "2", "status": "done", "title": "Done task"},
            ]
            mock_sb.list_meetings.return_value = [{"id": "m1"}]
            mock_sb.list_decisions.return_value = []
            mock_sb.get_pending_approval_summary.return_value = []
            mock_sb.get_latest_mcp_session.return_value = None

            with patch("processors.proactive_alerts.generate_alerts", return_value=[]):
                result = await call_tool(server, "get_system_context")

        assert result["status"] == "success"
        data = result["data"]
        assert "CropSight" in data["company"]
        assert len(data["team"]) == 4
        assert data["recent_activity"]["tasks_open"] == 1  # only pending
        assert data["personality_note"]

    @pytest.mark.asyncio
    async def test_includes_alerts(self, server, mock_mcp_auth):
        with patch("services.supabase_client.supabase_client") as mock_sb:
            mock_sb.get_tasks.return_value = []
            mock_sb.list_meetings.return_value = []
            mock_sb.list_decisions.return_value = []
            mock_sb.get_pending_approval_summary.return_value = []
            mock_sb.get_latest_mcp_session.return_value = None

            alerts = [{"severity": "high", "title": "2 tasks overdue"}]
            with patch("processors.proactive_alerts.generate_alerts", return_value=alerts):
                result = await call_tool(server, "get_system_context")

        assert "[HIGH] 2 tasks overdue" in result["data"]["attention_needed"]


# =============================================================================
# get_last_session_summary
# =============================================================================


class TestGetLastSessionSummary:
    @pytest.mark.asyncio
    async def test_returns_session(self, server, mock_mcp_auth):
        session = {"session_date": "2026-03-18", "summary": "Reviewed Q1 progress"}
        with patch("services.supabase_client.supabase_client") as mock_sb:
            mock_sb.get_latest_mcp_session.return_value = session
            result = await call_tool(server, "get_last_session_summary")

        assert result["status"] == "success"
        assert result["data"]["summary"] == "Reviewed Q1 progress"
        assert result["metadata"]["record_count"] == 1

    @pytest.mark.asyncio
    async def test_no_sessions(self, server, mock_mcp_auth):
        with patch("services.supabase_client.supabase_client") as mock_sb:
            mock_sb.get_latest_mcp_session.return_value = None
            result = await call_tool(server, "get_last_session_summary")

        assert result["status"] == "success"
        assert result["metadata"]["record_count"] == 0


# =============================================================================
# save_session_summary
# =============================================================================


class TestSaveSessionSummary:
    @pytest.mark.asyncio
    async def test_saves_session(self, server, mock_mcp_auth):
        created = {"id": "abc", "session_date": "2026-03-21", "summary": "Test"}
        with patch("services.supabase_client.supabase_client") as mock_sb:
            mock_sb.create_mcp_session.return_value = created
            result = await call_tool(server, "save_session_summary", {
                "summary": "Discussed funding timeline",
                "decisions": ["Push pre-seed to April"],
                "pending": ["Deck revision"],
            })

        assert result["status"] == "success"
        assert result["data"]["id"] == "abc"


# =============================================================================
# search_memory
# =============================================================================


class TestSearchMemory:
    @pytest.mark.asyncio
    async def test_returns_search_results(self, server, mock_mcp_auth):
        with patch("services.supabase_client.supabase_client") as mock_sb:
            mock_sb.search_memory.return_value = {
                "embeddings": [
                    {"content": "Moldova pilot update", "source_type": "meeting"},
                ],
                "decisions": [],
                "tasks": [],
            }
            with patch("services.embeddings.embedding_service") as mock_embed:
                mock_embed.embed_text = AsyncMock(return_value=[0.1] * 1536)
                result = await call_tool(server, "search_memory", {"query": "Moldova"})

        assert result["status"] == "success"
        assert result["metadata"]["source"] == "hybrid_rag"
        assert len(result["data"]["embeddings"]) == 1

    @pytest.mark.asyncio
    async def test_sanitizes_raw_text(self, server, mock_mcp_auth):
        with patch("services.supabase_client.supabase_client") as mock_sb:
            mock_sb.search_memory.return_value = {
                "embeddings": [
                    {"content": "summary", "raw_transcript": "FULL TEXT HERE", "email_body": "raw"},
                ],
                "decisions": [],
                "tasks": [],
            }
            with patch("services.embeddings.embedding_service") as mock_embed:
                mock_embed.embed_text = AsyncMock(return_value=[0.1] * 1536)
                result = await call_tool(server, "search_memory", {"query": "test"})

        # raw_transcript and email_body should be stripped
        embedding = result["data"]["embeddings"][0]
        assert "raw_transcript" not in embedding
        assert "email_body" not in embedding

    @pytest.mark.asyncio
    async def test_filter_by_source_type(self, server, mock_mcp_auth):
        with patch("services.supabase_client.supabase_client") as mock_sb:
            mock_sb.search_memory.return_value = {
                "embeddings": [
                    {"content": "a", "source_type": "meeting"},
                    {"content": "b", "source_type": "email"},
                ],
                "decisions": [],
                "tasks": [],
            }
            with patch("services.embeddings.embedding_service") as mock_embed:
                mock_embed.embed_text = AsyncMock(return_value=[0.1] * 1536)
                result = await call_tool(server, "search_memory", {
                    "query": "test",
                    "source_types": ["meeting"],
                })

        assert len(result["data"]["embeddings"]) == 1
        assert result["data"]["embeddings"][0]["source_type"] == "meeting"


# =============================================================================
# get_tasks
# =============================================================================


class TestGetTasks:
    @pytest.mark.asyncio
    async def test_returns_tasks(self, server, mock_mcp_auth):
        tasks = [
            {"id": "1", "title": "API Gateway", "assignee": "Roye", "status": "in_progress"},
        ]
        with patch("services.supabase_client.supabase_client") as mock_sb:
            mock_sb.get_tasks.return_value = tasks
            result = await call_tool(server, "get_tasks", {"assignee": "Roye"})

        assert result["status"] == "success"
        assert result["metadata"]["record_count"] == 1
        assert result["data"][0]["assignee"] == "Roye"

    @pytest.mark.asyncio
    async def test_passes_filters(self, server, mock_mcp_auth):
        with patch("services.supabase_client.supabase_client") as mock_sb:
            mock_sb.get_tasks.return_value = []
            await call_tool(server, "get_tasks", {
                "assignee": "Paolo",
                "status": "pending",
                "category": "BD",
                "limit": 10,
            })
            mock_sb.get_tasks.assert_called_once_with(
                assignee="Paolo", status="pending", category="BD", limit=10,
            )


# =============================================================================
# get_decisions
# =============================================================================


class TestGetDecisions:
    @pytest.mark.asyncio
    async def test_returns_decisions(self, server, mock_mcp_auth):
        decisions = [{"id": "d1", "description": "Go with AWS", "topic": "infra"}]
        with patch("services.supabase_client.supabase_client") as mock_sb:
            mock_sb.list_decisions.return_value = decisions
            result = await call_tool(server, "get_decisions", {"topic": "infra"})

        assert result["status"] == "success"
        assert result["data"][0]["description"] == "Go with AWS"


# =============================================================================
# get_open_questions
# =============================================================================


class TestGetOpenQuestions:
    @pytest.mark.asyncio
    async def test_returns_open_questions(self, server, mock_mcp_auth):
        questions = [{"id": "q1", "question": "Timeline for Moldova?", "status": "open"}]
        with patch("services.supabase_client.supabase_client") as mock_sb:
            mock_sb.get_open_questions.return_value = questions
            result = await call_tool(server, "get_open_questions")

        assert result["status"] == "success"
        assert result["metadata"]["record_count"] == 1


# =============================================================================
# get_commitments
# =============================================================================


class TestDealOps:
    @pytest.mark.asyncio
    async def test_deal_ops_list(self, server, mock_mcp_auth):
        deals = [{"id": "d1", "name": "Test Deal", "organization": "TestOrg"}]
        with patch("services.supabase_client.supabase_client") as mock_sb:
            mock_sb.get_deals.return_value = deals
            result = await call_tool(server, "deal_ops", {"action": "list"})

        assert result["status"] == "success"
        assert len(result["data"]) == 1
        mock_sb.get_deals.assert_called_once_with(stage=None)


# =============================================================================
# get_stakeholder_info
# =============================================================================


class TestGetStakeholderInfo:
    @pytest.mark.asyncio
    async def test_returns_stakeholders(self, server, mock_mcp_auth):
        stakeholders = [{"name": "IIA", "type": "Government"}]
        with patch("services.google_sheets.sheets_service") as mock_sheets:
            mock_sheets.get_stakeholder_info = AsyncMock(return_value=stakeholders)
            result = await call_tool(server, "get_stakeholder_info", {"name": "IIA"})

        assert result["status"] == "success"
        assert result["metadata"]["source"] == "google_sheets"


# =============================================================================
# get_meeting_history
# =============================================================================


class TestGetMeetingHistory:
    @pytest.mark.asyncio
    async def test_returns_meetings(self, server, mock_mcp_auth):
        meetings = [{"id": "m1", "title": "Weekly Sync", "summary": "Discussed roadmap"}]
        with patch("services.supabase_client.supabase_client") as mock_sb:
            mock_sb.list_meetings.return_value = meetings
            result = await call_tool(server, "get_meeting_history", {"limit": 5})

        assert result["status"] == "success"
        assert result["metadata"]["record_count"] == 1

    @pytest.mark.asyncio
    async def test_topic_filter(self, server, mock_mcp_auth):
        meetings = [
            {"id": "m1", "title": "Moldova Review", "summary": "Good progress"},
            {"id": "m2", "title": "Investor Call", "summary": "Discussed funding"},
        ]
        with patch("services.supabase_client.supabase_client") as mock_sb:
            mock_sb.list_meetings.return_value = meetings
            result = await call_tool(server, "get_meeting_history", {"topic": "moldova"})

        assert result["metadata"]["record_count"] == 1
        assert result["data"][0]["title"] == "Moldova Review"

    @pytest.mark.asyncio
    async def test_sanitizes_raw_transcript(self, server, mock_mcp_auth):
        meetings = [{"id": "m1", "title": "Test", "raw_transcript": "FULL TRANSCRIPT"}]
        with patch("services.supabase_client.supabase_client") as mock_sb:
            mock_sb.list_meetings.return_value = meetings
            result = await call_tool(server, "get_meeting_history")

        assert "raw_transcript" not in result["data"][0]


# =============================================================================
# get_pending_approvals
# =============================================================================


class TestGetPendingApprovals:
    @pytest.mark.asyncio
    async def test_returns_approvals(self, server, mock_mcp_auth):
        approvals = [{"approval_id": "a1", "content_type": "meeting_summary"}]
        with patch("services.supabase_client.supabase_client") as mock_sb:
            mock_sb.get_pending_approval_summary.return_value = approvals
            result = await call_tool(server, "get_pending_approvals")

        assert result["status"] == "success"
        assert result["metadata"]["record_count"] == 1


# =============================================================================
# get_gantt_status
# =============================================================================


class TestGetGanttStatus:
    @pytest.mark.asyncio
    async def test_returns_gantt_status(self, server, mock_mcp_auth):
        gantt_data = {"sections": {"Product": {"status": "in_progress"}}}
        with patch("services.gantt_manager.gantt_manager") as mock_gantt:
            mock_gantt.get_gantt_status = AsyncMock(return_value=gantt_data)
            result = await call_tool(server, "get_gantt_status", {"week": 12})

        assert result["status"] == "success"
        assert result["metadata"]["source"] == "google_sheets"
        mock_gantt.get_gantt_status.assert_called_once_with(week=12)


# =============================================================================
# get_gantt_horizon
# =============================================================================


class TestGetGanttHorizon:
    @pytest.mark.asyncio
    async def test_returns_horizon(self, server, mock_mcp_auth):
        horizon = {"milestones": [{"week": 14, "event": "MVP release"}]}
        with patch("services.gantt_manager.gantt_manager") as mock_gantt:
            mock_gantt.get_gantt_horizon = AsyncMock(return_value=horizon)
            result = await call_tool(server, "get_gantt_horizon", {"weeks_ahead": 4})

        assert result["status"] == "success"
        mock_gantt.get_gantt_horizon.assert_called_once_with(weeks_ahead=4)


# =============================================================================
# get_upcoming_meetings
# =============================================================================


class TestGetUpcomingMeetings:
    @pytest.mark.asyncio
    async def test_returns_enriched_events(self, server, mock_mcp_auth):
        events = [{"title": "Weekly Sync", "start": "2026-03-22T10:00:00Z", "end": "2026-03-22T11:00:00Z", "location": "", "attendees": []}]
        prep_history = [{"meeting_title": "Weekly Sync", "status": "generated"}]

        with patch("services.google_calendar.calendar_service") as mock_cal:
            mock_cal.get_upcoming_events = AsyncMock(return_value=events)
            with patch("services.supabase_client.supabase_client") as mock_sb:
                mock_sb.get_meeting_prep_history.return_value = prep_history
                result = await call_tool(server, "get_upcoming_meetings", {"days": 3})

        assert result["status"] == "success"
        assert result["data"][0]["prep_status"] == "generated"
        assert result["metadata"]["source"] == "google_calendar"


# =============================================================================
# get_weekly_summary
# =============================================================================


class TestGetWeeklySummary:
    @pytest.mark.asyncio
    async def test_returns_weekly_data(self, server, mock_mcp_auth):
        weekly_data = {
            "week_in_review": {"meetings": 3},
            "gantt_proposals": [],
            "attention_needed": [],
        }
        with patch("processors.weekly_review.compile_weekly_review_data", new_callable=AsyncMock) as mock_compile:
            mock_compile.return_value = weekly_data
            result = await call_tool(server, "get_weekly_summary")

        assert result["status"] == "success"
        assert result["data"]["week_in_review"]["meetings"] == 3
        assert result["metadata"]["source"] == "composite"


# =============================================================================
# Error Handling
# =============================================================================


class TestErrorHandling:
    @pytest.mark.asyncio
    async def test_tool_error_returns_error_envelope(self, server, mock_mcp_auth):
        with patch("services.supabase_client.supabase_client") as mock_sb:
            mock_sb.get_tasks.side_effect = Exception("DB connection lost")
            result = await call_tool(server, "get_tasks")

        assert result["status"] == "error"
        assert "DB connection lost" in result["error"]

    @pytest.mark.asyncio
    async def test_system_context_error(self, server, mock_mcp_auth):
        with patch("services.supabase_client.supabase_client") as mock_sb:
            mock_sb.get_tasks.side_effect = Exception("timeout")
            result = await call_tool(server, "get_system_context")

        assert result["status"] == "error"
