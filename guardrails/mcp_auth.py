"""
MCP authentication, rate limiting, and audit logging.

Provides:
- Bearer token validation (single token, single user)
- In-memory rate limiting (configurable calls/hour)
- Audit logging of every MCP tool call via supabase_client.log_action()
- Pure ASGI middleware that protects /sse and /messages/ paths

Usage:
    from guardrails.mcp_auth import mcp_auth, MCPAuthMiddleware

    # Check token
    if not mcp_auth.validate_token(token):
        return 401

    # Add middleware to Starlette app
    app.add_middleware(MCPAuthMiddleware)
"""

import json
import logging
import time
from collections import defaultdict

from config.settings import settings

logger = logging.getLogger(__name__)

# Paths that require MCP auth (SSE connection + message posting)
_PROTECTED_PREFIXES = ("/sse", "/messages")


class MCPAuth:
    """Bearer token validation, rate limiting, and audit logging for MCP."""

    def __init__(self):
        self._call_counts: dict[str, list[float]] = defaultdict(list)

    def validate_token(self, token: str) -> bool:
        """Validate a bearer token against the configured MCP_AUTH_TOKEN."""
        if not settings.MCP_AUTH_TOKEN:
            logger.warning("MCP_AUTH_TOKEN not configured — rejecting all MCP requests")
            return False
        return token == settings.MCP_AUTH_TOKEN

    def check_rate_limit(self, token: str) -> bool:
        """
        Check if the token is within the rate limit.

        Uses a sliding window: keeps timestamps of calls in the last hour,
        prunes old entries, and checks against the limit.

        Returns:
            True if within limit, False if rate limited.
        """
        now = time.time()
        window = 3600  # 1 hour
        limit = settings.MCP_RATE_LIMIT_PER_HOUR

        # Prune old entries
        self._call_counts[token] = [
            ts for ts in self._call_counts[token] if now - ts < window
        ]

        if len(self._call_counts[token]) >= limit:
            return False

        self._call_counts[token].append(now)
        return True

    def log_call(
        self,
        tool_name: str,
        params: dict | None = None,
        response_size: int = 0,
        success: bool = True,
        error: str | None = None,
    ) -> None:
        """
        Log an MCP tool call to the audit trail.

        Uses supabase_client.log_action() — same audit system as all
        other Gianluigi operations.
        """
        try:
            from services.supabase_client import supabase_client

            # Summarize params (don't log full query text for privacy)
            param_summary = {}
            if params:
                for k, v in params.items():
                    if isinstance(v, str) and len(v) > 100:
                        param_summary[k] = v[:100] + "..."
                    else:
                        param_summary[k] = v

            supabase_client.log_action(
                action="mcp_tool_call",
                details={
                    "tool": tool_name,
                    "params": param_summary,
                    "response_size": response_size,
                    "success": success,
                    "error": error,
                },
                triggered_by="mcp",
            )
        except Exception as e:
            logger.debug(f"MCP audit log failed (non-fatal): {e}")


class MCPAuthMiddleware:
    """
    Pure ASGI middleware that enforces Bearer token auth on MCP paths.

    Uses raw ASGI protocol instead of BaseHTTPMiddleware to avoid
    breaking SSE streaming responses.

    Protects /sse and /messages/ endpoints.
    Passes through health, ready, and report endpoints without auth.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "")

        # Only protect MCP-specific paths
        if not any(path.startswith(prefix) for prefix in _PROTECTED_PREFIXES):
            await self.app(scope, receive, send)
            return

        # Extract Authorization header
        headers = dict(scope.get("headers", []))
        auth_header = headers.get(b"authorization", b"").decode()

        if not auth_header.startswith("Bearer "):
            logger.warning(f"MCP auth: missing Bearer token for {path}")
            await self._send_json_error(
                send, 401, {"error": "Missing or invalid Authorization header"}
            )
            return

        token = auth_header[7:]  # Strip "Bearer "

        if not mcp_auth.validate_token(token):
            logger.warning(f"MCP auth: invalid token for {path}")
            await self._send_json_error(send, 401, {"error": "Invalid token"})
            return

        if not mcp_auth.check_rate_limit(token):
            logger.warning(f"MCP auth: rate limit exceeded for {path}")
            await self._send_json_error(
                send, 429, {"error": "Rate limit exceeded", "retry_after_seconds": 3600}
            )
            return

        # Auth passed — forward to the app
        await self.app(scope, receive, send)

    @staticmethod
    async def _send_json_error(send, status: int, body: dict):
        """Send a JSON error response via raw ASGI."""
        payload = json.dumps(body).encode()
        await send({
            "type": "http.response.start",
            "status": status,
            "headers": [
                [b"content-type", b"application/json"],
                [b"content-length", str(len(payload)).encode()],
            ],
        })
        await send({
            "type": "http.response.body",
            "body": payload,
        })


# Singleton
mcp_auth = MCPAuth()
