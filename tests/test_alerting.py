"""Tests for services/alerting.py — tiered system alerting."""

import pytest
from unittest.mock import AsyncMock, patch

from services.alerting import (
    AlertSeverity,
    send_system_alert,
    get_and_flush_warnings,
    _warning_buffer,
)


class TestAlertSeverity:
    """Tests for AlertSeverity enum."""

    def test_severity_values(self):
        assert AlertSeverity.CRITICAL.value == "critical"
        assert AlertSeverity.WARNING.value == "warning"
        assert AlertSeverity.INFO.value == "info"


class TestSendSystemAlert:
    """Tests for send_system_alert."""

    @pytest.mark.asyncio
    async def test_critical_sends_telegram(self):
        """CRITICAL alert should send Telegram DM to Eyal."""
        mock_bot = AsyncMock()
        mock_bot.send_to_eyal = AsyncMock(return_value=True)

        with patch("services.alerting.logger") as mock_logger:
            with patch(
                "services.telegram_bot.telegram_bot", mock_bot
            ):
                await send_system_alert(
                    AlertSeverity.CRITICAL,
                    "test_component",
                    "Something broke",
                )

            mock_bot.send_to_eyal.assert_awaited_once()
            call_text = mock_bot.send_to_eyal.call_args[0][0]
            assert "test_component" in call_text
            assert "Something broke" in call_text
            mock_logger.error.assert_called_once()

    @pytest.mark.asyncio
    async def test_critical_with_error_details(self):
        """CRITICAL alert with error should include error details."""
        mock_bot = AsyncMock()
        mock_bot.send_to_eyal = AsyncMock(return_value=True)

        with patch("services.telegram_bot.telegram_bot", mock_bot):
            await send_system_alert(
                AlertSeverity.CRITICAL,
                "sheets",
                "Write failed",
                error=ValueError("column not found"),
            )

        call_text = mock_bot.send_to_eyal.call_args[0][0]
        assert "ValueError" in call_text
        assert "column not found" in call_text

    @pytest.mark.asyncio
    async def test_critical_telegram_failure_does_not_crash(self):
        """If Telegram send fails, alerting should not raise."""
        mock_bot = AsyncMock()
        mock_bot.send_to_eyal = AsyncMock(side_effect=Exception("network error"))

        with patch("services.telegram_bot.telegram_bot", mock_bot):
            with patch("services.alerting.logger") as mock_logger:
                # Should not raise
                await send_system_alert(
                    AlertSeverity.CRITICAL,
                    "test",
                    "msg",
                )
                mock_logger.critical.assert_called_once()

    @pytest.mark.asyncio
    async def test_warning_buffers_not_sends(self):
        """WARNING alert should buffer, not send Telegram."""
        _warning_buffer.clear()

        with patch("services.alerting.logger"):
            await send_system_alert(
                AlertSeverity.WARNING,
                "scheduler",
                "Missed window",
            )

        assert len(_warning_buffer) == 1
        assert _warning_buffer[0]["component"] == "scheduler"
        assert _warning_buffer[0]["message"] == "Missed window"
        _warning_buffer.clear()

    @pytest.mark.asyncio
    async def test_info_logs_only(self):
        """INFO alert should only log, not buffer or send."""
        _warning_buffer.clear()

        with patch("services.alerting.logger") as mock_logger:
            await send_system_alert(
                AlertSeverity.INFO,
                "health",
                "All systems OK",
            )

        assert len(_warning_buffer) == 0
        mock_logger.info.assert_called_once()


class TestGetAndFlushWarnings:
    """Tests for get_and_flush_warnings."""

    def test_returns_and_clears_buffer(self):
        _warning_buffer.clear()
        _warning_buffer.append({"component": "a", "message": "m1", "timestamp": "10:00"})
        _warning_buffer.append({"component": "b", "message": "m2", "timestamp": "10:05"})

        result = get_and_flush_warnings()

        assert len(result) == 2
        assert result[0]["component"] == "a"
        assert len(_warning_buffer) == 0

    def test_empty_buffer_returns_empty(self):
        _warning_buffer.clear()
        result = get_and_flush_warnings()
        assert result == []
