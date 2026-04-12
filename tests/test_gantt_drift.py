"""
Tests for Gantt drift detection and CEO Today view — Phase 5.

Tests:
- detect_gantt_drift: mismatch detection between Gantt and task reality
- get_full_status ceo_today view: additional sections returned
- get_full_status standard view: unchanged behavior
- Morning brief new sections: task_urgency, gantt_milestones, drift_alerts
"""

import pytest
from datetime import date, timedelta, datetime, timezone
from unittest.mock import patch, MagicMock, AsyncMock

from processors.gantt_intelligence import detect_gantt_drift


# =============================================================================
# Gantt Drift Detection
# =============================================================================


class TestDetectGanttDrift:
    @pytest.mark.asyncio
    async def test_detects_drift_when_tasks_mostly_overdue(self):
        yesterday = (date.today() - timedelta(days=1)).isoformat()
        gantt_data = {
            "items": [
                {"section": "Product & Tech", "status": "active"},
                {"section": "Product & Tech", "status": "active"},
            ]
        }
        tasks_pending = [
            {"category": "Product & Tech", "deadline": yesterday, "status": "pending"},
            {"category": "Product & Tech", "deadline": yesterday, "status": "pending"},
            {"category": "Product & Tech", "deadline": yesterday, "status": "pending"},
        ]
        tasks_in_progress = []

        with patch("services.gantt_manager.gantt_manager") as mock_gantt, \
             patch("services.supabase_client.supabase_client") as mock_sb:
            mock_gantt.get_gantt_status = AsyncMock(return_value=gantt_data)
            mock_sb.get_tasks.side_effect = [tasks_pending, tasks_in_progress]

            result = await detect_gantt_drift()

        assert len(result) == 1
        assert result[0]["section"] == "Product & Tech"
        assert result[0]["overdue_task_count"] == 3
        assert "100%" in result[0]["drift_description"]

    @pytest.mark.asyncio
    async def test_no_drift_when_tasks_on_track(self):
        tomorrow = (date.today() + timedelta(days=1)).isoformat()
        gantt_data = {
            "items": [
                {"section": "BD & Sales", "status": "active"},
            ]
        }
        tasks_pending = [
            {"category": "BD & Sales", "deadline": tomorrow, "status": "pending"},
        ]
        tasks_in_progress = []

        with patch("services.gantt_manager.gantt_manager") as mock_gantt, \
             patch("services.supabase_client.supabase_client") as mock_sb:
            mock_gantt.get_gantt_status = AsyncMock(return_value=gantt_data)
            mock_sb.get_tasks.side_effect = [tasks_pending, tasks_in_progress]

            result = await detect_gantt_drift()

        assert len(result) == 0

    @pytest.mark.asyncio
    async def test_no_drift_when_gantt_error(self):
        with patch("services.gantt_manager.gantt_manager") as mock_gantt:
            mock_gantt.get_gantt_status = AsyncMock(return_value={"error": "unavailable"})

            result = await detect_gantt_drift()

        assert result == []

    @pytest.mark.asyncio
    async def test_no_drift_with_no_matching_tasks(self):
        gantt_data = {
            "items": [
                {"section": "Legal & Compliance", "status": "active"},
            ]
        }

        with patch("services.gantt_manager.gantt_manager") as mock_gantt, \
             patch("services.supabase_client.supabase_client") as mock_sb:
            mock_gantt.get_gantt_status = AsyncMock(return_value=gantt_data)
            mock_sb.get_tasks.return_value = []

            result = await detect_gantt_drift()

        assert result == []

    @pytest.mark.asyncio
    async def test_ignores_completed_gantt_sections(self):
        gantt_data = {
            "items": [
                {"section": "Product & Tech", "status": "completed"},
            ]
        }

        with patch("services.gantt_manager.gantt_manager") as mock_gantt, \
             patch("services.supabase_client.supabase_client") as mock_sb:
            mock_gantt.get_gantt_status = AsyncMock(return_value=gantt_data)
            mock_sb.get_tasks.return_value = []

            result = await detect_gantt_drift()

        assert result == []

    @pytest.mark.asyncio
    async def test_drift_below_50_percent_not_flagged(self):
        yesterday = (date.today() - timedelta(days=1)).isoformat()
        tomorrow = (date.today() + timedelta(days=1)).isoformat()
        gantt_data = {
            "items": [
                {"section": "Product & Tech", "status": "active"},
            ]
        }
        # 1 overdue out of 3 = 33% — below threshold
        tasks_pending = [
            {"category": "Product & Tech", "deadline": yesterday, "status": "pending"},
            {"category": "Product & Tech", "deadline": tomorrow, "status": "pending"},
            {"category": "Product & Tech", "deadline": tomorrow, "status": "pending"},
        ]
        tasks_in_progress = []

        with patch("services.gantt_manager.gantt_manager") as mock_gantt, \
             patch("services.supabase_client.supabase_client") as mock_sb:
            mock_gantt.get_gantt_status = AsyncMock(return_value=gantt_data)
            mock_sb.get_tasks.side_effect = [tasks_pending, tasks_in_progress]

            result = await detect_gantt_drift()

        assert len(result) == 0

    @pytest.mark.asyncio
    async def test_handles_exception_gracefully(self):
        with patch("services.gantt_manager.gantt_manager") as mock_gantt:
            mock_gantt.get_gantt_status = AsyncMock(side_effect=Exception("network error"))

            result = await detect_gantt_drift()

        assert result == []


# =============================================================================
# CEO Today View (MCP get_full_status)
# =============================================================================


class TestCeoTodayView:
    @pytest.fixture
    def server(self):
        from services.mcp_server import MCPServer
        srv = MCPServer()
        srv._mcp = srv._build_mcp()
        return srv

    @pytest.fixture
    def mock_mcp_auth(self):
        with patch("services.mcp_server.mcp_auth") as mock:
            mock.log_call = MagicMock()
            yield mock

    async def call_tool(self, server, name, arguments=None):
        result = await server._mcp.call_tool(name, arguments or {})
        if isinstance(result, list):
            import json
            for block in result:
                if hasattr(block, "text"):
                    return json.loads(block.text)
        return result

    @pytest.mark.asyncio
    async def test_standard_view_no_extra_sections(self, server, mock_mcp_auth):
        with patch("services.supabase_client.supabase_client") as mock_sb, \
             patch("services.gantt_manager.gantt_manager") as mock_gantt, \
             patch("services.google_calendar.calendar_service") as mock_cal, \
             patch("processors.proactive_alerts.generate_alerts", return_value=[]):
            mock_sb.get_tasks.return_value = []
            mock_sb.get_pending_approval_summary.return_value = []
            mock_gantt.get_gantt_status = AsyncMock(return_value={"items": []})
            mock_cal.get_upcoming_events = AsyncMock(return_value=[])

            result = await self.call_tool(server, "get_full_status", {"view": "standard"})

        assert result["status"] == "success"
        assert "overdue_tasks" not in result["data"]
        assert "deal_pulse" not in result["data"]
        assert "drift_alerts" not in result["data"]

    @pytest.mark.asyncio
    async def test_ceo_today_includes_overdue_tasks(self, server, mock_mcp_auth):
        yesterday = (date.today() - timedelta(days=1)).isoformat()
        tasks = [
            {"title": "Overdue task", "assignee": "Eyal", "deadline": yesterday, "status": "pending", "priority": "H"},
            {"title": "Future task", "assignee": "Roye", "deadline": "2099-01-01", "status": "pending", "priority": "M"},
        ]
        with patch("services.supabase_client.supabase_client") as mock_sb, \
             patch("services.gantt_manager.gantt_manager") as mock_gantt, \
             patch("services.google_calendar.calendar_service") as mock_cal, \
             patch("processors.proactive_alerts.generate_alerts", return_value=[]), \
             patch("processors.gantt_intelligence.compute_gantt_metrics", new_callable=AsyncMock, return_value={"milestone_risks": []}), \
             patch("processors.deal_intelligence.generate_deal_pulse", return_value=[]), \
             patch("processors.deal_intelligence.generate_commitments_due", return_value=[]), \
             patch("processors.gantt_intelligence.detect_gantt_drift", new_callable=AsyncMock, return_value=[]):
            mock_sb.get_tasks.return_value = tasks
            mock_sb.get_pending_approval_summary.return_value = []
            mock_gantt.get_gantt_status = AsyncMock(return_value={"items": []})
            mock_cal.get_upcoming_events = AsyncMock(return_value=[])

            result = await self.call_tool(server, "get_full_status", {"view": "ceo_today"})

        assert result["status"] == "success"
        assert len(result["data"]["overdue_tasks"]) == 1
        assert result["data"]["overdue_tasks"][0]["title"] == "Overdue task"

    @pytest.mark.asyncio
    async def test_ceo_today_includes_deal_pulse(self, server, mock_mcp_auth):
        with patch("services.supabase_client.supabase_client") as mock_sb, \
             patch("services.gantt_manager.gantt_manager") as mock_gantt, \
             patch("services.google_calendar.calendar_service") as mock_cal, \
             patch("processors.proactive_alerts.generate_alerts", return_value=[]), \
             patch("processors.gantt_intelligence.compute_gantt_metrics", new_callable=AsyncMock, return_value={"milestone_risks": []}), \
             patch("processors.deal_intelligence.generate_deal_pulse", return_value=[{"type": "stale", "name": "D1"}]), \
             patch("processors.deal_intelligence.generate_commitments_due", return_value=[]), \
             patch("processors.gantt_intelligence.detect_gantt_drift", new_callable=AsyncMock, return_value=[]):
            mock_sb.get_tasks.return_value = []
            mock_sb.get_pending_approval_summary.return_value = []
            mock_gantt.get_gantt_status = AsyncMock(return_value={"items": []})
            mock_cal.get_upcoming_events = AsyncMock(return_value=[])

            result = await self.call_tool(server, "get_full_status", {"view": "ceo_today"})

        assert result["status"] == "success"
        assert len(result["data"]["deal_pulse"]) == 1

    @pytest.mark.asyncio
    async def test_ceo_today_includes_drift_alerts(self, server, mock_mcp_auth):
        drift = [{"section": "Product & Tech", "drift_description": "50% overdue"}]
        with patch("services.supabase_client.supabase_client") as mock_sb, \
             patch("services.gantt_manager.gantt_manager") as mock_gantt, \
             patch("services.google_calendar.calendar_service") as mock_cal, \
             patch("processors.proactive_alerts.generate_alerts", return_value=[]), \
             patch("processors.gantt_intelligence.compute_gantt_metrics", new_callable=AsyncMock, return_value={"milestone_risks": []}), \
             patch("processors.deal_intelligence.generate_deal_pulse", return_value=[]), \
             patch("processors.deal_intelligence.generate_commitments_due", return_value=[]), \
             patch("processors.gantt_intelligence.detect_gantt_drift", new_callable=AsyncMock, return_value=drift):
            mock_sb.get_tasks.return_value = []
            mock_sb.get_pending_approval_summary.return_value = []
            mock_gantt.get_gantt_status = AsyncMock(return_value={"items": []})
            mock_cal.get_upcoming_events = AsyncMock(return_value=[])

            result = await self.call_tool(server, "get_full_status", {"view": "ceo_today"})

        assert result["status"] == "success"
        assert len(result["data"]["drift_alerts"]) == 1


# =============================================================================
# Morning Brief New Sections Formatting
# =============================================================================


class TestMorningBriefNewSections:
    def test_format_task_urgency(self):
        from processors.morning_brief import format_morning_brief

        brief = {
            "sections": [{
                "type": "task_urgency",
                "title": "Task Urgency",
                "items": [
                    {"title": "Send proposal", "assignee": "Eyal", "deadline": "2026-04-05"},
                ],
            }],
            "stats": {},
            "scan_ids": [],
        }
        result = format_morning_brief(brief)
        assert "Needs attention" in result
        assert "Send proposal" in result
        assert "Eyal" in result

    def test_format_gantt_milestones(self):
        from processors.morning_brief import format_morning_brief

        brief = {
            "sections": [{
                "type": "gantt_milestones",
                "title": "Gantt Milestones",
                "items": [
                    {"milestone": "PoC completion", "section": "Product & Tech", "weeks_away": 2},
                ],
            }],
            "stats": {},
            "scan_ids": [],
        }
        result = format_morning_brief(brief)
        assert "Milestones" in result
        assert "PoC completion" in result
        assert "2w away" in result

    def test_format_drift_alerts(self):
        from processors.morning_brief import format_morning_brief

        brief = {
            "sections": [{
                "type": "drift_alerts",
                "title": "Drift Alerts",
                "items": [
                    {"drift_description": "Product & Tech: 3/4 tasks overdue (75%)"},
                ],
            }],
            "stats": {},
            "scan_ids": [],
        }
        result = format_morning_brief(brief)
        assert "Needs attention" in result
        assert "75%" in result

    def test_empty_sections_omitted(self):
        from processors.morning_brief import format_morning_brief

        brief = {
            "sections": [
                {"type": "task_urgency", "title": "Task Urgency", "items": []},
                {"type": "gantt_milestones", "title": "Gantt Milestones", "items": []},
                {"type": "drift_alerts", "title": "Drift Alerts", "items": []},
            ],
            "stats": {},
            "scan_ids": [],
        }
        result = format_morning_brief(brief)
        assert "Task Urgency" not in result
        assert "Gantt Milestones" not in result
        assert "Drift Alerts" not in result
