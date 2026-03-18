"""
Lightweight HTTP health check server for Cloud Run.

Cloud Run requires an HTTP server on PORT (default 8080) for liveness
and readiness probes. This server runs alongside the main Gianluigi
services and exposes two endpoints:

- GET /health  → 200 always (liveness probe)
- GET /ready   → 200 when services initialized, 503 otherwise (readiness probe)

Usage:
    from services.health_server import health_server

    await health_server.start()
    health_server.set_ready(True)
    # ... later ...
    await health_server.stop()
"""

import logging
from aiohttp import web

from config.settings import settings

logger = logging.getLogger(__name__)


class HealthServer:
    """
    Minimal aiohttp server for Cloud Run health checks.

    Starts on the PORT env var (default 8080) and responds to
    /health (liveness) and /ready (readiness) probes.
    """

    def __init__(self):
        self._app: web.Application | None = None
        self._runner: web.AppRunner | None = None
        self._site: web.TCPSite | None = None
        self._ready: bool = False

    async def start(self) -> None:
        """Start the health check HTTP server."""
        self._app = web.Application()
        self._app.router.add_get("/health", self._handle_health)
        self._app.router.add_get("/ready", self._handle_ready)
        self._app.router.add_get(
            "/reports/weekly/{access_token}", self._handle_weekly_report
        )

        self._runner = web.AppRunner(self._app)
        await self._runner.setup()

        port = settings.PORT
        self._site = web.TCPSite(self._runner, "0.0.0.0", port)
        await self._site.start()
        logger.info(f"Health server listening on port {port}")

    async def stop(self) -> None:
        """Stop the health check HTTP server."""
        if self._runner:
            await self._runner.cleanup()
            logger.info("Health server stopped")

    def set_ready(self, ready: bool) -> None:
        """Set the readiness state (called after services initialize)."""
        self._ready = ready

    @property
    def is_ready(self) -> bool:
        """Check if the server reports ready."""
        return self._ready

    async def _handle_health(self, request: web.Request) -> web.Response:
        """Liveness probe — always returns 200."""
        return web.json_response({"status": "alive"})

    async def _handle_ready(self, request: web.Request) -> web.Response:
        """Readiness probe — returns 200 when ready, 503 otherwise."""
        if self._ready:
            return web.json_response({"status": "ready"})
        return web.json_response(
            {"status": "not_ready"},
            status=503,
        )

    async def _handle_weekly_report(self, request: web.Request) -> web.Response:
        """Serve HTML weekly report by per-report access token."""
        access_token = request.match_info.get("access_token", "")
        if not access_token:
            return web.json_response({"error": "Not found"}, status=404)

        try:
            from services.supabase_client import supabase_client
            from datetime import datetime, timezone

            report = supabase_client.get_weekly_report_by_token(access_token)
            if not report:
                return web.json_response({"error": "Not found"}, status=404)

            # Check expiry
            expires_at = report.get("expires_at")
            if expires_at:
                try:
                    exp_dt = datetime.fromisoformat(
                        str(expires_at).replace("Z", "+00:00")
                    )
                    if exp_dt.tzinfo is None:
                        exp_dt = exp_dt.replace(tzinfo=timezone.utc)
                    if datetime.now(timezone.utc) > exp_dt:
                        return web.Response(
                            text=self._expired_report_html(),
                            content_type="text/html",
                        )
                except (ValueError, TypeError):
                    pass

            html_content = report.get("html_content", "")
            if not html_content:
                return web.json_response({"error": "Report not generated"}, status=404)

            # Log access
            try:
                report_id = report.get("id", "")
                access_count = (report.get("access_count") or 0) + 1
                supabase_client.update_weekly_report(
                    report_id,
                    last_accessed_at=datetime.now(timezone.utc).isoformat(),
                    access_count=access_count,
                )
            except Exception as e:
                logger.debug(f"Access logging failed: {e}")

            return web.Response(
                text=html_content,
                content_type="text/html",
            )
        except Exception as e:
            logger.error(f"Error serving weekly report: {e}")
            return web.json_response({"error": "Internal error"}, status=500)

    @staticmethod
    def _expired_report_html() -> str:
        """Return a friendly HTML page for expired reports."""
        return """<!DOCTYPE html>
<html><head><title>Report Expired</title>
<style>body{font-family:sans-serif;display:flex;justify-content:center;align-items:center;height:100vh;margin:0;background:#f5f5f5}
.box{text-align:center;padding:40px;background:#fff;border-radius:8px;box-shadow:0 2px 8px rgba(0,0,0,.1)}
h1{color:#666;margin:0 0 12px}p{color:#999}</style></head>
<body><div class="box"><h1>Report Expired</h1><p>This weekly report link has expired.<br>Please request a new report from Gianluigi.</p></div></body></html>"""


# Singleton instance
health_server = HealthServer()
