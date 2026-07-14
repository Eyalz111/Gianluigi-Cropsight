"""
Tests for MCP authentication, rate limiting, and audit logging.

Tests:
- Token validation (valid, invalid, missing config)
- Rate limiting (within limit, exceeds limit, window reset)
- Audit logging
- Auth middleware (protected paths, unprotected paths)
"""

import time
import pytest
from unittest.mock import patch, MagicMock, AsyncMock
from starlette.testclient import TestClient
from starlette.applications import Starlette
from starlette.routing import Route
from starlette.responses import JSONResponse

from guardrails.mcp_auth import MCPAuth, MCPAuthMiddleware


# =============================================================================
# Token Validation
# =============================================================================


class TestTokenValidation:
    """Test bearer token validation."""

    @patch("guardrails.mcp_auth.settings")
    def test_valid_token(self, mock_settings):
        mock_settings.MCP_AUTH_TOKEN = "secret-token-123"
        auth = MCPAuth()
        assert auth.validate_token("secret-token-123") is True

    @patch("guardrails.mcp_auth.settings")
    def test_invalid_token(self, mock_settings):
        mock_settings.MCP_AUTH_TOKEN = "secret-token-123"
        auth = MCPAuth()
        assert auth.validate_token("wrong-token") is False

    @patch("guardrails.mcp_auth.settings")
    def test_empty_token(self, mock_settings):
        mock_settings.MCP_AUTH_TOKEN = "secret-token-123"
        auth = MCPAuth()
        assert auth.validate_token("") is False

    @patch("guardrails.mcp_auth.settings")
    def test_no_configured_token_rejects_all(self, mock_settings):
        mock_settings.MCP_AUTH_TOKEN = ""
        auth = MCPAuth()
        assert auth.validate_token("any-token") is False


# =============================================================================
# Rate Limiting
# =============================================================================


class TestRateLimiting:
    """Test in-memory rate limiting."""

    @patch("guardrails.mcp_auth.settings")
    def test_within_limit(self, mock_settings):
        mock_settings.MCP_RATE_LIMIT_PER_HOUR = 10
        auth = MCPAuth()
        for _ in range(10):
            assert auth.check_rate_limit("token-1") is True

    @patch("guardrails.mcp_auth.settings")
    def test_exceeds_limit(self, mock_settings):
        mock_settings.MCP_RATE_LIMIT_PER_HOUR = 5
        auth = MCPAuth()
        for _ in range(5):
            assert auth.check_rate_limit("token-1") is True
        # 6th call should fail
        assert auth.check_rate_limit("token-1") is False

    @patch("guardrails.mcp_auth.settings")
    def test_different_tokens_independent(self, mock_settings):
        mock_settings.MCP_RATE_LIMIT_PER_HOUR = 3
        auth = MCPAuth()
        for _ in range(3):
            auth.check_rate_limit("token-a")
        # token-a exhausted
        assert auth.check_rate_limit("token-a") is False
        # token-b still has budget
        assert auth.check_rate_limit("token-b") is True

    @patch("guardrails.mcp_auth.settings")
    def test_window_reset(self, mock_settings):
        mock_settings.MCP_RATE_LIMIT_PER_HOUR = 2
        auth = MCPAuth()

        # Use up the limit
        auth.check_rate_limit("token-1")
        auth.check_rate_limit("token-1")
        assert auth.check_rate_limit("token-1") is False

        # Simulate time passing (expire old entries)
        auth._call_counts["token-1"] = [time.time() - 3601]
        assert auth.check_rate_limit("token-1") is True


# =============================================================================
# Audit Logging
# =============================================================================


class TestAuditLogging:
    """Test MCP call audit logging."""

    @patch("guardrails.mcp_auth.settings")
    def test_log_call_success(self, mock_settings):
        mock_settings.MCP_RATE_LIMIT_PER_HOUR = 100
        auth = MCPAuth()
        mock_client = MagicMock()

        with patch("services.supabase_client.supabase_client", mock_client):
            auth.log_call("get_tasks", {"assignee": "Eyal"}, response_size=512)
            mock_client.log_action.assert_called_once()
            call_args = mock_client.log_action.call_args
            assert call_args.kwargs["action"] == "mcp_tool_call"
            assert call_args.kwargs["details"]["tool"] == "get_tasks"
            assert call_args.kwargs["details"]["success"] is True

    @patch("guardrails.mcp_auth.settings")
    def test_log_call_truncates_long_params(self, mock_settings):
        mock_settings.MCP_RATE_LIMIT_PER_HOUR = 100
        auth = MCPAuth()
        mock_client = MagicMock()

        long_query = "x" * 200
        with patch("services.supabase_client.supabase_client", mock_client):
            auth.log_call("search_memory", {"query": long_query})
            call_args = mock_client.log_action.call_args
            logged_query = call_args.kwargs["details"]["params"]["query"]
            assert len(logged_query) < 200
            assert logged_query.endswith("...")

    @patch("guardrails.mcp_auth.settings")
    def test_log_call_error_non_fatal(self, mock_settings):
        mock_settings.MCP_RATE_LIMIT_PER_HOUR = 100
        auth = MCPAuth()
        mock_client = MagicMock()
        mock_client.log_action.side_effect = Exception("DB down")

        with patch("services.supabase_client.supabase_client", mock_client):
            # Should not raise
            auth.log_call("get_tasks")


# =============================================================================
# Auth Middleware
# =============================================================================


class TestAuthMiddleware:
    """Test the Starlette auth middleware."""

    def _make_test_app(self):
        """Create a minimal Starlette app with the auth middleware."""
        async def health(request):
            return JSONResponse({"status": "alive"})

        async def sse_endpoint(request):
            return JSONResponse({"status": "connected"})

        async def messages_endpoint(request):
            return JSONResponse({"status": "ok"})

        # /mcp = Streamable HTTP transport (the protected path; SSE was retired)
        async def mcp_endpoint(request):
            return JSONResponse({"status": "connected"})

        app = Starlette(
            routes=[
                Route("/health", health),
                Route("/sse", sse_endpoint),
                Route("/messages/test", messages_endpoint),
                Route("/mcp", mcp_endpoint),
            ],
        )
        app.add_middleware(MCPAuthMiddleware)
        return app

    @patch("guardrails.mcp_auth.mcp_auth")
    def test_health_no_auth_required(self, mock_auth):
        app = self._make_test_app()
        client = TestClient(app)
        response = client.get("/health")
        assert response.status_code == 200
        # validate_token should NOT have been called
        mock_auth.validate_token.assert_not_called()

    @patch("guardrails.mcp_auth.mcp_auth")
    def test_sse_authless_allowed(self, mock_auth):
        """No token = authless mode (Claude.ai connector). Should pass through."""
        mock_auth.check_rate_limit.return_value = True
        app = self._make_test_app()
        client = TestClient(app)
        response = client.get("/sse")
        assert response.status_code == 200
        mock_auth.validate_token.assert_not_called()

    @patch("guardrails.mcp_auth.mcp_auth")
    def test_messages_authless_allowed(self, mock_auth):
        """No token on /messages = authless mode. Should pass through."""
        mock_auth.check_rate_limit.return_value = True
        app = self._make_test_app()
        client = TestClient(app)
        response = client.get("/messages/test")
        assert response.status_code == 200

    # /mcp = Streamable HTTP transport (the protected path; SSE+/messages were retired)
    @patch("guardrails.mcp_auth.mcp_auth")
    def test_mcp_with_valid_token(self, mock_auth):
        mock_auth.validate_token.return_value = True
        mock_auth.check_rate_limit.return_value = True
        app = self._make_test_app()
        client = TestClient(app)
        response = client.get(
            "/mcp",
            headers={"Authorization": "Bearer valid-token"},
        )
        assert response.status_code == 200
        mock_auth.validate_token.assert_called_with("valid-token")

    @patch("guardrails.mcp_auth.mcp_auth")
    def test_mcp_with_invalid_token_rejected(self, mock_auth):
        """Bad token provided = reject (don't fall through to authless)."""
        mock_auth.validate_token.return_value = False
        app = self._make_test_app()
        client = TestClient(app)
        response = client.get(
            "/mcp",
            headers={"Authorization": "Bearer bad-token"},
        )
        assert response.status_code == 401

    @patch("guardrails.mcp_auth.mcp_auth")
    def test_mcp_rate_limited_with_token(self, mock_auth):
        mock_auth.validate_token.return_value = True
        mock_auth.check_rate_limit.return_value = False
        app = self._make_test_app()
        client = TestClient(app)
        response = client.get(
            "/mcp",
            headers={"Authorization": "Bearer valid-token"},
        )
        assert response.status_code == 429
        assert "Rate limit" in response.json()["error"]

    @patch("guardrails.mcp_auth.settings")
    @patch("guardrails.mcp_auth.mcp_auth")
    def test_mcp_rate_limited_authless(self, mock_auth, mock_settings):
        """When authless is explicitly allowed, connections are still rate-limited by IP."""
        mock_settings.MCP_ALLOW_AUTHLESS = True
        mock_auth.check_rate_limit.return_value = False
        app = self._make_test_app()
        client = TestClient(app)
        response = client.get("/mcp")
        assert response.status_code == 429

    @patch("guardrails.mcp_auth.settings")
    @patch("guardrails.mcp_auth.mcp_auth")
    def test_mcp_tokenless_rejected_by_default(self, mock_auth, mock_settings):
        """Secure default (June P3-01 closed): no token + authless disabled => 401,
        rejected BEFORE the rate-limit check."""
        mock_settings.MCP_ALLOW_AUTHLESS = False
        app = self._make_test_app()
        client = TestClient(app)
        response = client.get("/mcp")
        assert response.status_code == 401
        assert "Authentication required" in response.json()["error"]
        mock_auth.check_rate_limit.assert_not_called()

    @patch("guardrails.mcp_auth.settings")
    @patch("guardrails.mcp_auth.mcp_auth")
    def test_mcp_authless_allowed_when_flag_set(self, mock_auth, mock_settings):
        """Safety valve: MCP_ALLOW_AUTHLESS=True restores the old authless pass-through
        (instant env-var escape hatch if a legit client gets locked out)."""
        mock_settings.MCP_ALLOW_AUTHLESS = True
        mock_auth.check_rate_limit.return_value = True
        app = self._make_test_app()
        client = TestClient(app)
        response = client.get("/mcp")
        assert response.status_code == 200
        mock_auth.validate_token.assert_not_called()
