"""Tests for core/health_monitor.py — daily health reporting."""

import pytest
from unittest.mock import patch, MagicMock, AsyncMock
from datetime import datetime


@pytest.fixture
def mock_settings():
    with patch("core.health_monitor.settings") as mock:
        mock.DAILY_HEALTH_REPORT_ENABLED = True
        mock.GOOGLE_REFRESH_TOKEN = "test-token"
        mock.GOOGLE_CLIENT_ID = "test-id"
        mock.GOOGLE_CLIENT_SECRET = "test-secret"
        mock.TELEGRAM_BOT_TOKEN = "test-bot-token"
        yield mock


@pytest.fixture
def mock_supabase():
    with patch("services.supabase_client.supabase_client") as mock:
        # action_log check
        mock.client.table.return_value.select.return_value.limit.return_value.execute.return_value = MagicMock(data=[{"id": 1}])
        # pending approvals
        mock.get_pending_approval_summary.return_value = []
        # error count
        mock.client.table.return_value.select.return_value.eq.return_value.gte.return_value.execute.return_value = MagicMock(count=0, data=[])
        # meetings
        mock.list_meetings.return_value = []
        yield mock


class TestCollectHealthData:
    def test_returns_expected_structure(self, mock_settings, mock_supabase):
        from core.health_monitor import collect_health_data
        data = collect_health_data()
        assert "timestamp" in data
        assert "components" in data
        assert "metrics" in data

    def test_pending_approvals_counted(self, mock_settings, mock_supabase):
        mock_supabase.get_pending_approval_summary.return_value = [
            {"approval_id": "a", "content_type": "meeting_summary"},
            {"approval_id": "b", "content_type": "weekly_digest"},
        ]
        from core.health_monitor import collect_health_data
        data = collect_health_data()
        assert data["metrics"]["pending_approvals"] == 2

    def test_google_oauth_configured(self, mock_settings, mock_supabase):
        from core.health_monitor import collect_health_data
        data = collect_health_data()
        assert data["components"]["google_oauth"] == "configured"

    def test_google_oauth_not_configured(self, mock_settings, mock_supabase):
        mock_settings.GOOGLE_REFRESH_TOKEN = ""
        from core.health_monitor import collect_health_data
        data = collect_health_data()
        assert data["components"]["google_oauth"] == "not configured"

    def test_telegram_configured(self, mock_settings, mock_supabase):
        from core.health_monitor import collect_health_data
        data = collect_health_data()
        assert data["components"]["telegram"] == "configured"


class TestFormatDailyHealthReport:
    def test_all_healthy(self, mock_settings):
        from core.health_monitor import format_daily_health_report
        data = {
            "timestamp": datetime.now().isoformat(),
            "components": {
                "supabase": "healthy",
                "google_oauth": "configured",
                "telegram": "configured",
            },
            "metrics": {
                "pending_approvals": 0,
                "errors_24h": 0,
                "meetings_7d": 3,
            },
        }
        report = format_daily_health_report(data)
        assert "All systems operational" in report
        assert "Meetings processed (7d): 3" in report

    def test_unhealthy_component(self, mock_settings):
        from core.health_monitor import format_daily_health_report
        data = {
            "timestamp": datetime.now().isoformat(),
            "components": {
                "supabase": "error: timeout",
                "google_oauth": "configured",
            },
            "metrics": {
                "pending_approvals": 0,
                "errors_24h": 0,
                "meetings_7d": 0,
            },
        }
        report = format_daily_health_report(data)
        assert "WARN" in report
        assert "supabase" in report

    def test_pending_approvals_shown(self, mock_settings):
        from core.health_monitor import format_daily_health_report
        data = {
            "timestamp": datetime.now().isoformat(),
            "components": {"supabase": "healthy"},
            "metrics": {
                "pending_approvals": 5,
                "errors_24h": 0,
                "meetings_7d": 0,
            },
        }
        report = format_daily_health_report(data)
        assert "Pending approvals: 5" in report

    def test_errors_shown(self, mock_settings):
        from core.health_monitor import format_daily_health_report
        data = {
            "timestamp": datetime.now().isoformat(),
            "components": {"supabase": "healthy"},
            "metrics": {
                "pending_approvals": 0,
                "errors_24h": 3,
                "meetings_7d": 0,
            },
        }
        report = format_daily_health_report(data)
        assert "Errors (24h): 3" in report


class TestSendDailyHealthReport:
    @pytest.mark.asyncio
    async def test_sends_report(self, mock_settings, mock_supabase):
        with patch("services.telegram_bot.telegram_bot") as mock_bot:
            mock_bot.send_to_eyal = AsyncMock()
            from core.health_monitor import send_daily_health_report
            result = await send_daily_health_report()
            assert result is True
            mock_bot.send_to_eyal.assert_called_once()

    @pytest.mark.asyncio
    async def test_disabled(self, mock_settings, mock_supabase):
        mock_settings.DAILY_HEALTH_REPORT_ENABLED = False
        from core.health_monitor import send_daily_health_report
        result = await send_daily_health_report()
        assert result is False

    @pytest.mark.asyncio
    async def test_handles_failure(self, mock_settings, mock_supabase):
        with patch("services.telegram_bot.telegram_bot") as mock_bot:
            mock_bot.send_to_eyal = AsyncMock(side_effect=Exception("send failed"))
            from core.health_monitor import send_daily_health_report
            result = await send_daily_health_report()
            assert result is False


class TestCheckAndAlert:
    @pytest.mark.asyncio
    async def test_delegates_to_error_alerting(self):
        with patch("core.error_alerting.alert_critical_error", new_callable=AsyncMock) as mock_alert:
            from core.health_monitor import check_and_alert
            error = RuntimeError("test error")
            await check_and_alert("test_component", error)
            mock_alert.assert_called_once_with("test_component", "test error")
