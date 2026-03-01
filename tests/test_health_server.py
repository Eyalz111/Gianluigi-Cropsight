"""
Tests for the health check server (v0.4 — Cloud Run).

Tests cover:
- /health always returns 200
- /ready returns 503 before set_ready, 200 after
- Start/stop lifecycle
- set_ready toggles state
"""

import pytest
from aiohttp import web
from aiohttp.test_utils import AioHTTPTestCase, unittest_run_loop
from unittest.mock import patch, MagicMock


# =============================================================================
# Test: HealthServer unit tests (no real HTTP)
# =============================================================================

class TestHealthServerUnit:
    """Unit tests for HealthServer without starting a real server."""

    def test_initial_state_not_ready(self):
        """HealthServer should start in not-ready state."""
        from services.health_server import HealthServer
        server = HealthServer()
        assert server.is_ready is False

    def test_set_ready_true(self):
        """set_ready(True) should make is_ready return True."""
        from services.health_server import HealthServer
        server = HealthServer()
        server.set_ready(True)
        assert server.is_ready is True

    def test_set_ready_false_after_true(self):
        """set_ready(False) should revert to not-ready."""
        from services.health_server import HealthServer
        server = HealthServer()
        server.set_ready(True)
        server.set_ready(False)
        assert server.is_ready is False

    @pytest.mark.asyncio
    async def test_health_endpoint_returns_200(self):
        """GET /health should always return 200."""
        from services.health_server import HealthServer
        server = HealthServer()

        # Create a mock request
        mock_request = MagicMock(spec=web.Request)

        response = await server._handle_health(mock_request)
        assert response.status == 200

    @pytest.mark.asyncio
    async def test_ready_endpoint_returns_503_when_not_ready(self):
        """GET /ready should return 503 when not ready."""
        from services.health_server import HealthServer
        server = HealthServer()

        mock_request = MagicMock(spec=web.Request)

        response = await server._handle_ready(mock_request)
        assert response.status == 503

    @pytest.mark.asyncio
    async def test_ready_endpoint_returns_200_when_ready(self):
        """GET /ready should return 200 after set_ready(True)."""
        from services.health_server import HealthServer
        server = HealthServer()
        server.set_ready(True)

        mock_request = MagicMock(spec=web.Request)

        response = await server._handle_ready(mock_request)
        assert response.status == 200

    @pytest.mark.asyncio
    async def test_stop_without_start_does_not_raise(self):
        """Stopping a server that was never started should not raise."""
        from services.health_server import HealthServer
        server = HealthServer()
        # Should not raise
        await server.stop()
