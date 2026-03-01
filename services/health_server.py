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


# Singleton instance
health_server = HealthServer()
