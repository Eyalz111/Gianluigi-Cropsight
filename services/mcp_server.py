"""
MCP server for Gianluigi — Claude.ai as CEO dashboard.

Provides 15 read-only tools as thin wrappers around existing brain functions.
Uses the official MCP Python SDK with SSE transport on port 8080, sharing
the port with health check and report endpoints.

Architecture:
    Claude.ai --SSE--> MCP Server --> Auth Middleware --> Tool Handler --> Existing Brain Function

All tools return structured JSON with status, data, and metadata fields.
No new business logic — tools call the same functions Telegram and agents use.

Usage:
    from services.mcp_server import mcp_server

    await mcp_server.start()
    # ... later ...
    await mcp_server.stop()
"""

import logging
from datetime import datetime, timezone, timedelta

import uvicorn
from mcp.server.fastmcp import FastMCP
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from config.settings import settings
from guardrails.mcp_auth import MCPAuthMiddleware, mcp_auth

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helper: standard response wrapper
# ---------------------------------------------------------------------------

def _success(data, source: str = "supabase", record_count: int | None = None) -> dict:
    """Wrap data in standard MCP tool response format."""
    meta = {
        "source": source,
        "freshness": datetime.now(timezone.utc).isoformat(),
    }
    if record_count is not None:
        meta["record_count"] = record_count
    elif isinstance(data, list):
        meta["record_count"] = len(data)
    return {"status": "success", "data": data, "metadata": meta}


def _error(message: str, source: str = "internal") -> dict:
    """Wrap error in standard MCP tool response format."""
    return {
        "status": "error",
        "error": message,
        "metadata": {
            "source": source,
            "freshness": datetime.now(timezone.utc).isoformat(),
        },
    }


def _sanitize_records(records: list[dict], exclude_fields: set | None = None) -> list[dict]:
    """Remove raw text fields that shouldn't be returned via MCP."""
    exclude = exclude_fields or {"raw_transcript", "email_body", "full_text"}
    sanitized = []
    for record in records:
        sanitized.append({k: v for k, v in record.items() if k not in exclude})
    return sanitized


# ---------------------------------------------------------------------------
# MCP Server class
# ---------------------------------------------------------------------------

class MCPServer:
    """
    MCP server with SSE transport, auth middleware, and 15 read-only tools.

    Extends the health server's port (8080) by replacing the aiohttp server
    with a Starlette app that serves both health endpoints and MCP SSE.
    """

    def __init__(self):
        self._mcp: FastMCP | None = None
        self._server: uvicorn.Server | None = None
        self._ready: bool = False

    def _build_mcp(self) -> FastMCP:
        """Build the FastMCP instance with all tools and custom routes."""
        mcp = FastMCP(
            "gianluigi",
            instructions=(
                "Gianluigi is CropSight's AI operations assistant. "
                "Call get_system_context() first to load company context, "
                "then use other tools to query operational data."
            ),
        )

        self._register_health_routes(mcp)
        self._register_tools(mcp)
        return mcp

    # ------------------------------------------------------------------
    # Health / Ready / Report routes (replaces aiohttp health_server)
    # ------------------------------------------------------------------

    def _register_health_routes(self, mcp: FastMCP) -> None:
        """Add health, ready, and report routes as custom HTTP endpoints."""

        server_ref = self  # capture for closures

        @mcp.custom_route("/health", methods=["GET"])
        async def handle_health(request: Request) -> JSONResponse:
            return JSONResponse({"status": "alive"})

        @mcp.custom_route("/ready", methods=["GET"])
        async def handle_ready(request: Request) -> JSONResponse:
            if server_ref._ready:
                return JSONResponse({"status": "ready"})
            return JSONResponse({"status": "not_ready"}, status_code=503)

        @mcp.custom_route("/reports/weekly/{access_token}", methods=["GET"])
        async def handle_weekly_report(request: Request) -> Response:
            access_token = request.path_params.get("access_token", "")
            if not access_token:
                return JSONResponse({"error": "Not found"}, status_code=404)

            try:
                from services.supabase_client import supabase_client

                report = supabase_client.get_weekly_report_by_token(access_token)
                if not report:
                    return JSONResponse({"error": "Not found"}, status_code=404)

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
                            return Response(
                                content=_expired_report_html(),
                                media_type="text/html",
                            )
                    except (ValueError, TypeError):
                        pass

                html_content = report.get("html_content", "")
                if not html_content:
                    return JSONResponse({"error": "Report not generated"}, status_code=404)

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

                return Response(content=html_content, media_type="text/html")
            except Exception as e:
                logger.error(f"Error serving weekly report: {e}")
                return JSONResponse({"error": "Internal error"}, status_code=500)

    # ------------------------------------------------------------------
    # MCP Tools (15 read-only tools)
    # ------------------------------------------------------------------

    def _register_tools(self, mcp: FastMCP) -> None:
        """Register all 15 MCP tools on the FastMCP instance."""

        # ============================================================
        # 1. get_system_context — Onboarding tool
        # ============================================================
        @mcp.tool(
            name="get_system_context",
            description=(
                "Load CropSight company context, current operational state, "
                "pending items, and attention flags. Call this FIRST in every session."
            ),
        )
        async def get_system_context() -> dict:
            try:
                from services.supabase_client import supabase_client
                from config.team import TEAM_MEMBERS
                from processors.proactive_alerts import generate_alerts

                # Team info
                team = [
                    {"name": m["name"], "role": m["role"]}
                    for m in TEAM_MEMBERS.values()
                ]

                # Current state counts
                now = datetime.now(timezone.utc)
                week_start = now - timedelta(days=now.weekday())
                week_start = week_start.replace(hour=0, minute=0, second=0, microsecond=0)

                tasks = supabase_client.get_tasks(limit=500)
                open_tasks = [t for t in tasks if t.get("status") not in ("done", "cancelled")]
                overdue_tasks = [t for t in tasks if t.get("status") == "overdue"]

                meetings = supabase_client.list_meetings(date_from=week_start, limit=50)
                decisions = supabase_client.list_decisions(limit=50)
                # Filter decisions from this week
                decisions_this_week = [
                    d for d in decisions
                    if d.get("created_at", "") >= week_start.isoformat()
                ]

                pending_approvals = supabase_client.get_pending_approval_summary()

                # Alerts
                alerts = generate_alerts()
                attention_items = [
                    f"[{a['severity'].upper()}] {a['title']}" for a in alerts[:5]
                ]

                # Last MCP session
                last_session = supabase_client.get_latest_mcp_session()
                last_session_info = None
                if last_session:
                    last_session_info = {
                        "date": last_session.get("session_date", ""),
                        "summary": last_session.get("summary", ""),
                    }

                iso_cal = now.isocalendar()

                context = {
                    "company": "CropSight — ML crop yield forecasting, Israeli AgTech, pre-revenue PoC",
                    "team": team,
                    "current_week": iso_cal.week,
                    "current_date": now.strftime("%Y-%m-%d"),
                    "gantt_period": "Q1-Q2 2026",
                    "recent_activity": {
                        "meetings_this_week": len(meetings),
                        "decisions_this_week": len(decisions_this_week),
                        "tasks_open": len(open_tasks),
                        "tasks_overdue": len(overdue_tasks),
                    },
                    "attention_needed": attention_items if attention_items else ["No urgent items"],
                    "pending_approvals": len(pending_approvals),
                    "last_session": last_session_info,
                    "personality_note": (
                        "I'm Gianluigi, CropSight's AI operations assistant. "
                        "I track everything, propose actions for Eyal's approval, "
                        "and never distribute to the team without explicit OK."
                    ),
                }

                mcp_auth.log_call("get_system_context", response_size=len(str(context)))
                return _success(context, source="composite")

            except Exception as e:
                logger.error(f"get_system_context error: {e}")
                mcp_auth.log_call("get_system_context", success=False, error=str(e))
                return _error(str(e))

        # ============================================================
        # 2. get_last_session_summary
        # ============================================================
        @mcp.tool(
            name="get_last_session_summary",
            description="Get the most recent MCP session summary for continuity across conversations.",
        )
        async def get_last_session_summary() -> dict:
            try:
                from services.supabase_client import supabase_client

                session = supabase_client.get_latest_mcp_session()
                mcp_auth.log_call("get_last_session_summary")
                if session:
                    return _success(session, record_count=1)
                return _success(None, record_count=0)

            except Exception as e:
                logger.error(f"get_last_session_summary error: {e}")
                mcp_auth.log_call("get_last_session_summary", success=False, error=str(e))
                return _error(str(e))

        # ============================================================
        # 3. save_session_summary
        # ============================================================
        @mcp.tool(
            name="save_session_summary",
            description=(
                "Save a session summary for continuity. Call at the end of each "
                "Claude.ai session with a summary of what was discussed and decided."
            ),
        )
        async def save_session_summary(
            summary: str,
            decisions: list[str] | None = None,
            pending: list[str] | None = None,
        ) -> dict:
            try:
                from services.supabase_client import supabase_client

                session = supabase_client.create_mcp_session(
                    session_date=datetime.now(timezone.utc).strftime("%Y-%m-%d"),
                    summary=summary,
                    decisions_made=decisions,
                    pending_items=pending,
                )
                mcp_auth.log_call("save_session_summary", {"summary_length": len(summary)})
                return _success(session, record_count=1)

            except Exception as e:
                logger.error(f"save_session_summary error: {e}")
                mcp_auth.log_call("save_session_summary", success=False, error=str(e))
                return _error(str(e))

        # ============================================================
        # 4. search_memory
        # ============================================================
        @mcp.tool(
            name="search_memory",
            description=(
                "Search Gianluigi's memory using hybrid RAG (semantic + keyword). "
                "Returns relevant context from meetings, decisions, tasks, and documents."
            ),
        )
        async def search_memory(
            query: str,
            source_types: list[str] | None = None,
            limit: int = 10,
        ) -> dict:
            try:
                from services.supabase_client import supabase_client
                from services.embeddings import embedding_service

                # Generate embedding for semantic search
                query_embedding = await embedding_service.embed_text(query)

                results = supabase_client.search_memory(
                    query_embedding=query_embedding,
                    query_text=query,
                    limit=limit,
                )

                # Filter by source types if specified
                if source_types and "embeddings" in results:
                    results["embeddings"] = [
                        r for r in results["embeddings"]
                        if r.get("source_type") in source_types
                    ]

                # Sanitize — never return raw transcript text
                if "embeddings" in results:
                    results["embeddings"] = _sanitize_records(results["embeddings"])

                mcp_auth.log_call("search_memory", {"query": query, "limit": limit})
                return _success(results, source="hybrid_rag")

            except Exception as e:
                logger.error(f"search_memory error: {e}")
                mcp_auth.log_call("search_memory", success=False, error=str(e))
                return _error(str(e))

        # ============================================================
        # 5. get_tasks
        # ============================================================
        @mcp.tool(
            name="get_tasks",
            description="Query tasks with optional filters by assignee, status, or category.",
        )
        async def get_tasks(
            assignee: str | None = None,
            status: str | None = None,
            category: str | None = None,
            limit: int = 50,
        ) -> dict:
            try:
                from services.supabase_client import supabase_client

                tasks = supabase_client.get_tasks(
                    assignee=assignee,
                    status=status,
                    category=category,
                    limit=limit,
                )
                mcp_auth.log_call("get_tasks", {"assignee": assignee, "status": status})
                return _success(tasks)

            except Exception as e:
                logger.error(f"get_tasks error: {e}")
                mcp_auth.log_call("get_tasks", success=False, error=str(e))
                return _error(str(e))

        # ============================================================
        # 6. get_decisions
        # ============================================================
        @mcp.tool(
            name="get_decisions",
            description="Query decision history with optional filters by topic or meeting.",
        )
        async def get_decisions(
            topic: str | None = None,
            meeting_id: str | None = None,
            limit: int = 50,
        ) -> dict:
            try:
                from services.supabase_client import supabase_client

                decisions = supabase_client.list_decisions(
                    meeting_id=meeting_id,
                    topic=topic,
                    limit=limit,
                )
                mcp_auth.log_call("get_decisions", {"topic": topic})
                return _success(decisions)

            except Exception as e:
                logger.error(f"get_decisions error: {e}")
                mcp_auth.log_call("get_decisions", success=False, error=str(e))
                return _error(str(e))

        # ============================================================
        # 7. get_open_questions
        # ============================================================
        @mcp.tool(
            name="get_open_questions",
            description="Get unresolved questions from meetings, optionally filtered by status.",
        )
        async def get_open_questions(
            status: str = "open",
        ) -> dict:
            try:
                from services.supabase_client import supabase_client

                questions = supabase_client.get_open_questions(status=status)
                mcp_auth.log_call("get_open_questions", {"status": status})
                return _success(questions)

            except Exception as e:
                logger.error(f"get_open_questions error: {e}")
                mcp_auth.log_call("get_open_questions", success=False, error=str(e))
                return _error(str(e))

        # ============================================================
        # 8. get_commitments
        # ============================================================
        @mcp.tool(
            name="get_commitments",
            description="Query team commitments with optional filters by assignee or status.",
        )
        async def get_commitments(
            assignee: str | None = None,
            status: str | None = None,
        ) -> dict:
            try:
                from services.supabase_client import supabase_client

                commitments = supabase_client.get_commitments(
                    speaker=assignee,
                    status=status,
                )
                mcp_auth.log_call("get_commitments", {"assignee": assignee, "status": status})
                return _success(commitments)

            except Exception as e:
                logger.error(f"get_commitments error: {e}")
                mcp_auth.log_call("get_commitments", success=False, error=str(e))
                return _error(str(e))

        # ============================================================
        # 9. get_stakeholder_info
        # ============================================================
        @mcp.tool(
            name="get_stakeholder_info",
            description="Search the stakeholder tracker for contacts or organizations.",
        )
        async def get_stakeholder_info(
            name: str | None = None,
            organization: str | None = None,
        ) -> dict:
            try:
                from services.google_sheets import sheets_service

                stakeholders = await sheets_service.get_stakeholder_info(
                    name=name,
                    organization=organization,
                )
                mcp_auth.log_call("get_stakeholder_info", {"name": name, "org": organization})
                return _success(stakeholders, source="google_sheets")

            except Exception as e:
                logger.error(f"get_stakeholder_info error: {e}")
                mcp_auth.log_call("get_stakeholder_info", success=False, error=str(e))
                return _error(str(e))

        # ============================================================
        # 10. get_meeting_history
        # ============================================================
        @mcp.tool(
            name="get_meeting_history",
            description="List recent meetings with optional topic search.",
        )
        async def get_meeting_history(
            limit: int = 20,
            topic: str | None = None,
        ) -> dict:
            try:
                from services.supabase_client import supabase_client

                meetings = supabase_client.list_meetings(limit=limit)

                # Filter by topic if specified (search title + summary)
                if topic:
                    topic_lower = topic.lower()
                    meetings = [
                        m for m in meetings
                        if topic_lower in (m.get("title", "") or "").lower()
                        or topic_lower in (m.get("summary", "") or "").lower()
                    ]

                # Sanitize — don't return full transcripts
                meetings = _sanitize_records(meetings)
                mcp_auth.log_call("get_meeting_history", {"limit": limit, "topic": topic})
                return _success(meetings)

            except Exception as e:
                logger.error(f"get_meeting_history error: {e}")
                mcp_auth.log_call("get_meeting_history", success=False, error=str(e))
                return _error(str(e))

        # ============================================================
        # 11. get_pending_approvals
        # ============================================================
        @mcp.tool(
            name="get_pending_approvals",
            description="Get the current approval queue — items waiting for Eyal's review.",
        )
        async def get_pending_approvals() -> dict:
            try:
                from services.supabase_client import supabase_client

                approvals = supabase_client.get_pending_approval_summary()
                mcp_auth.log_call("get_pending_approvals")
                return _success(approvals)

            except Exception as e:
                logger.error(f"get_pending_approvals error: {e}")
                mcp_auth.log_call("get_pending_approvals", success=False, error=str(e))
                return _error(str(e))

        # ============================================================
        # 12. get_gantt_status
        # ============================================================
        @mcp.tool(
            name="get_gantt_status",
            description="Get current Gantt chart status for a specific week (defaults to current week).",
        )
        async def get_gantt_status(
            week: int | None = None,
        ) -> dict:
            try:
                from services.gantt_manager import gantt_manager

                status = await gantt_manager.get_gantt_status(week=week)
                mcp_auth.log_call("get_gantt_status", {"week": week})
                return _success(status, source="google_sheets")

            except Exception as e:
                logger.error(f"get_gantt_status error: {e}")
                mcp_auth.log_call("get_gantt_status", success=False, error=str(e))
                return _error(str(e))

        # ============================================================
        # 13. get_gantt_horizon
        # ============================================================
        @mcp.tool(
            name="get_gantt_horizon",
            description="Get upcoming milestones and transitions from the Gantt chart.",
        )
        async def get_gantt_horizon(
            weeks_ahead: int = 8,
        ) -> dict:
            try:
                from services.gantt_manager import gantt_manager

                horizon = await gantt_manager.get_gantt_horizon(weeks_ahead=weeks_ahead)
                mcp_auth.log_call("get_gantt_horizon", {"weeks_ahead": weeks_ahead})
                return _success(horizon, source="google_sheets")

            except Exception as e:
                logger.error(f"get_gantt_horizon error: {e}")
                mcp_auth.log_call("get_gantt_horizon", success=False, error=str(e))
                return _error(str(e))

        # ============================================================
        # 14. get_upcoming_meetings
        # ============================================================
        @mcp.tool(
            name="get_upcoming_meetings",
            description="Get upcoming calendar meetings with prep status.",
        )
        async def get_upcoming_meetings(
            days: int = 7,
        ) -> dict:
            try:
                from services.google_calendar import calendar_service
                from services.supabase_client import supabase_client

                events = await calendar_service.get_upcoming_events(days=days)

                # Enrich with prep status from meeting_prep_history
                prep_history = supabase_client.get_meeting_prep_history(limit=20)
                prep_by_title = {}
                for p in prep_history:
                    title = (p.get("meeting_title") or "").lower()
                    if title:
                        prep_by_title[title] = p.get("status", "unknown")

                enriched = []
                for event in events:
                    entry = {
                        "title": event.get("title", ""),
                        "start": event.get("start", ""),
                        "end": event.get("end", ""),
                        "location": event.get("location", ""),
                        "attendees": event.get("attendees", []),
                    }
                    title_lower = (event.get("title") or "").lower()
                    entry["prep_status"] = prep_by_title.get(title_lower, "none")
                    enriched.append(entry)

                mcp_auth.log_call("get_upcoming_meetings", {"days": days})
                return _success(enriched, source="google_calendar")

            except Exception as e:
                logger.error(f"get_upcoming_meetings error: {e}")
                mcp_auth.log_call("get_upcoming_meetings", success=False, error=str(e))
                return _error(str(e))

        # ============================================================
        # 15. get_weekly_summary
        # ============================================================
        @mcp.tool(
            name="get_weekly_summary",
            description=(
                "Compile weekly review data — meetings, decisions, tasks, Gantt proposals, "
                "and attention items. This is the foundation for the weekly CEO review."
            ),
        )
        async def get_weekly_summary() -> dict:
            try:
                from processors.weekly_review import compile_weekly_review_data

                now = datetime.now(timezone.utc)
                iso_cal = now.isocalendar()

                data = await compile_weekly_review_data(
                    week_number=iso_cal.week,
                    year=iso_cal.year,
                )
                mcp_auth.log_call("get_weekly_summary", response_size=len(str(data)))
                return _success(data, source="composite")

            except Exception as e:
                logger.error(f"get_weekly_summary error: {e}")
                mcp_auth.log_call("get_weekly_summary", success=False, error=str(e))
                return _error(str(e))

    # ------------------------------------------------------------------
    # Server lifecycle
    # ------------------------------------------------------------------

    def set_ready(self, ready: bool) -> None:
        """Set the readiness state (called after services initialize)."""
        self._ready = ready

    @property
    def is_ready(self) -> bool:
        """Check if the server reports ready."""
        return self._ready

    async def start(self) -> None:
        """Start the MCP server with SSE transport on the configured port."""
        self._mcp = self._build_mcp()

        # Get the Starlette SSE app and add auth middleware
        app = self._mcp.sse_app()
        app.add_middleware(MCPAuthMiddleware)

        port = settings.PORT
        config = uvicorn.Config(
            app,
            host="0.0.0.0",
            port=port,
            log_level="warning",
            access_log=False,
        )
        self._server = uvicorn.Server(config)

        logger.info(f"MCP server (SSE) listening on port {port}")
        await self._server.serve()

    async def stop(self) -> None:
        """Stop the MCP server."""
        if self._server:
            self._server.should_exit = True
            logger.info("MCP server stopped")


def _expired_report_html() -> str:
    """Return a friendly HTML page for expired reports."""
    return (
        '<!DOCTYPE html><html><head><title>Report Expired</title>'
        "<style>body{font-family:sans-serif;display:flex;justify-content:center;"
        "align-items:center;height:100vh;margin:0;background:#f5f5f5}"
        ".box{text-align:center;padding:40px;background:#fff;border-radius:8px;"
        "box-shadow:0 2px 8px rgba(0,0,0,.1)}"
        "h1{color:#666;margin:0 0 12px}p{color:#999}</style></head>"
        '<body><div class="box"><h1>Report Expired</h1>'
        "<p>This weekly report link has expired.<br>"
        "Please request a new report from Gianluigi.</p></div></body></html>"
    )


# Singleton
mcp_server = MCPServer()
