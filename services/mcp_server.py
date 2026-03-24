"""
MCP server for Gianluigi — Claude.ai as CEO dashboard.

Provides 32 tools (22 read + 10 write) tools as thin wrappers around existing brain functions.
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
from mcp.server.transport_security import TransportSecuritySettings
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from config.settings import settings
from guardrails.mcp_auth import MCPAuthMiddleware, mcp_auth

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helper: standard response wrapper
# ---------------------------------------------------------------------------

def _success(
    data,
    source: str = "supabase",
    record_count: int | None = None,
    warnings: list[str] | None = None,
) -> dict:
    """Wrap data in standard MCP tool response format."""
    meta = {
        "source": source,
        "freshness": datetime.now(timezone.utc).isoformat(),
    }
    if record_count is not None:
        meta["record_count"] = record_count
    elif isinstance(data, list):
        meta["record_count"] = len(data)
    if warnings:
        meta["warnings"] = warnings
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
    MCP server with SSE transport, auth middleware, and 32 tools (22 read + 10 write) tools.

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
                "You are connected to Gianluigi, CropSight's AI operations assistant. "
                "IMPORTANT RULES:\n"
                "1. Call get_system_context() FIRST in every session to load company context.\n"
                "2. For status updates, call get_full_status() for a complete snapshot in one call, "
                "or use individual tools (get_gantt_status, get_tasks, get_pending_approvals, "
                "get_upcoming_meetings) for focused queries. "
                "Do not skip the Gantt — it is the primary operational source.\n"
                "3. ONLY report information that comes from Gianluigi's tools. "
                "Do NOT mix in information from your own memory, prior conversations, "
                "or any source outside these tools. If Gianluigi's data is empty, "
                "say so — do not fill gaps with outside knowledge.\n"
                "4. Gianluigi tracks CropSight business operations ONLY. "
                "Personal matters (reserve duty, travel, family) are out of scope. "
                "If a query touches personal topics, clarify that Gianluigi only "
                "covers CropSight operations.\n"
                "5. Gianluigi proposes, Eyal approves. Never suggest direct team actions.\n"
                "6. For weekly reviews, call start_weekly_review() to begin, then "
                "confirm_weekly_review(session_id) when Eyal approves.\n"
                "7. Use create_task() and update_task() to manage tasks. "
                "Always confirm changes with Eyal before executing."
            ),
            transport_security=TransportSecuritySettings(
                enable_dns_rebinding_protection=False,
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
                    except (ValueError, TypeError) as e:
                        logger.warning(f"Failed to parse expires_at '{expires_at}': {e}")

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
    # MCP Tools (32 tools (22 read + 10 write) tools)
    # ------------------------------------------------------------------

    def _register_tools(self, mcp: FastMCP) -> None:
        """Register all 15 MCP tools on the FastMCP instance."""

        # ============================================================
        # 1. get_system_context — Onboarding tool
        # ============================================================
        @mcp.tool(
            name="get_system_context",
            description=(
                "[SYSTEM] Load CropSight company context, current operational state, "
                "pending items, and attention flags. Call this FIRST in every session. "
                "Set refresh=True to force-regenerate the operational snapshot."
            ),
        )
        async def get_system_context(refresh: bool = False) -> dict:
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

                # Operational snapshot (Phase 9B) — compressed CEO brief
                try:
                    from processors.operational_snapshot import (
                        generate_operational_snapshot,
                        get_latest_snapshot,
                    )
                    if refresh:
                        snapshot_result = await generate_operational_snapshot()
                        context["operational_context"] = snapshot_result.get("content", "")
                    else:
                        snapshot = get_latest_snapshot()
                        if snapshot:
                            context["operational_context"] = snapshot.get("content", "")
                except Exception as snap_err:
                    logger.debug(f"Operational snapshot unavailable: {snap_err}")

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
            description=(
                "DEPRECATED: Commitments have been merged into tasks. "
                "Use get_tasks() for all action items. "
                "This tool still returns legacy commitment records for backward compatibility."
            ),
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
                result = _success(commitments)
                result["note"] = (
                    "Commitments have been merged into tasks (action items). "
                    "Use get_tasks() for all action items going forward."
                )
                return result

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

                # Attach report URL if a report exists for this week
                try:
                    from services.supabase_client import supabase_client as _sc

                    report = _sc.get_weekly_report(iso_cal.week, iso_cal.year)
                    if report and report.get("access_token"):
                        base_url = settings.REPORTS_BASE_URL.rstrip("/") if settings.REPORTS_BASE_URL else ""
                        if base_url:
                            data["report_url"] = f"{base_url}/reports/weekly/{report['access_token']}"
                except Exception as report_err:
                    logger.debug(f"Could not attach report URL: {report_err}")

                mcp_auth.log_call("get_weekly_summary", response_size=len(str(data)))
                return _success(data, source="composite")

            except Exception as e:
                logger.error(f"get_weekly_summary error: {e}")
                mcp_auth.log_call("get_weekly_summary", success=False, error=str(e))
                return _error(str(e))

        # ============================================================
        # 16. get_full_status (composite)
        # ============================================================
        @mcp.tool(
            name="get_full_status",
            description=(
                "Get a complete operational status in one call — tasks, Gantt status, "
                "pending approvals, upcoming meetings, and attention items. "
                "Use this instead of calling 5+ individual tools for status updates."
            ),
        )
        async def get_full_status() -> dict:
            import asyncio

            warnings: list[str] = []
            result: dict = {}

            # Sync calls (supabase)
            try:
                from services.supabase_client import supabase_client as _sc

                result["tasks"] = _sc.get_tasks(limit=50)
            except Exception as e:
                warnings.append(f"Tasks unavailable: {e}")
                result["tasks"] = []

            try:
                from services.supabase_client import supabase_client as _sc

                result["pending_approvals"] = _sc.get_pending_approval_summary()
            except Exception as e:
                warnings.append(f"Approvals unavailable: {e}")
                result["pending_approvals"] = []

            # Async calls with timeout
            try:
                from services.gantt_manager import gantt_manager

                result["gantt_status"] = await asyncio.wait_for(
                    gantt_manager.get_gantt_status(), timeout=10,
                )
            except asyncio.TimeoutError:
                warnings.append("Gantt status timed out (10s)")
                result["gantt_status"] = None
            except Exception as e:
                warnings.append(f"Gantt unavailable: {e}")
                result["gantt_status"] = None

            try:
                from services.google_calendar import calendar_service

                result["upcoming_meetings"] = await asyncio.wait_for(
                    calendar_service.get_upcoming_events(days=7), timeout=10,
                )
            except asyncio.TimeoutError:
                warnings.append("Calendar timed out (10s)")
                result["upcoming_meetings"] = []
            except Exception as e:
                warnings.append(f"Meetings unavailable: {e}")
                result["upcoming_meetings"] = []

            try:
                from processors.proactive_alerts import generate_alerts

                alerts = generate_alerts()
                result["attention_items"] = [
                    f"[{a.get('severity', 'info').upper()}] {a.get('title', '')}"
                    for a in (alerts or [])[:5]
                ]
            except Exception as e:
                warnings.append(f"Alerts unavailable: {e}")
                result["attention_items"] = []

            mcp_auth.log_call("get_full_status", response_size=len(str(result)))
            return _success(
                result,
                source="composite",
                warnings=warnings if warnings else None,
            )

        # ============================================================
        # 17. start_weekly_review (write)
        # ============================================================
        @mcp.tool(
            name="start_weekly_review",
            description=(
                "Start or resume the weekly CEO review session. Returns all compiled "
                "data (week stats, Gantt proposals, attention items, next week preview, "
                "horizon check) in one payload for conversational presentation. "
                "Call this when Eyal wants to do the weekly review. "
                "Works any time — not restricted to the Friday calendar window."
            ),
        )
        async def start_weekly_review(force_fresh: bool = False) -> dict:
            try:
                from processors.weekly_review_session import (
                    start_weekly_review as _start_review,
                )
                from services.supabase_client import supabase_client as _sc

                # Create or resume session
                result = await _start_review(
                    user_id="eyal",
                    trigger="mcp",
                    force_fresh=force_fresh,
                )

                session_id = result.get("session_id")
                if not session_id:
                    return _error(result.get("response", "Failed to start review"))

                # Fetch full session data (raw agenda, not Telegram-formatted)
                session = _sc.get_weekly_review_session(session_id)
                if not session:
                    return _error("Session created but not found in DB")

                agenda_data = session.get("agenda_data", {}) or {}

                # Staleness warning (>4 hours)
                stale_warning = None
                compiled_at = agenda_data.get("meta", {}).get("compiled_at", "")
                if compiled_at:
                    try:
                        compiled_dt = datetime.fromisoformat(
                            compiled_at.replace("Z", "+00:00")
                        )
                        if compiled_dt.tzinfo is None:
                            compiled_dt = compiled_dt.replace(tzinfo=timezone.utc)
                        age_hours = (
                            datetime.now(timezone.utc) - compiled_dt
                        ).total_seconds() / 3600
                        if age_hours > 4:
                            stale_warning = (
                                f"Data compiled {age_hours:.0f}h ago. "
                                f"Use force_fresh=True to recompile."
                            )
                    except (ValueError, TypeError):
                        pass

                # Attach report URL if available
                report_url = None
                week_number = session.get("week_number", 0)
                year = session.get("year", 0)
                try:
                    report = _sc.get_weekly_report(week_number, year)
                    if report and report.get("access_token"):
                        base_url = (
                            settings.REPORTS_BASE_URL.rstrip("/")
                            if settings.REPORTS_BASE_URL
                            else ""
                        )
                        if base_url:
                            report_url = (
                                f"{base_url}/reports/weekly/{report['access_token']}"
                            )
                except Exception:
                    pass

                # Audit log
                _sc.log_action(
                    action="weekly_review_started",
                    details={
                        "session_id": session_id,
                        "week_number": week_number,
                        "year": year,
                        "force_fresh": force_fresh,
                        "source": "mcp",
                    },
                    triggered_by="eyal",
                )

                response = {
                    "session_id": session_id,
                    "action": result.get("action", "review_started"),
                    "week_number": week_number,
                    "year": year,
                    "compiled_at": compiled_at,
                    "stale_warning": stale_warning,
                    **agenda_data,
                    "report_url": report_url,
                }

                mcp_auth.log_call(
                    "start_weekly_review",
                    {"force_fresh": force_fresh, "session_id": session_id},
                )
                return _success(response, source="composite")

            except Exception as e:
                logger.error(f"start_weekly_review error: {e}")
                mcp_auth.log_call(
                    "start_weekly_review", success=False, error=str(e)
                )
                try:
                    from services.alerting import send_system_alert, AlertSeverity

                    await send_system_alert(
                        AlertSeverity.CRITICAL,
                        "mcp_weekly_review",
                        f"start_weekly_review failed: {e}",
                        error=e,
                    )
                except Exception:
                    pass
                return _error(str(e))

        # ============================================================
        # 18. confirm_weekly_review (write)
        # ============================================================
        @mcp.tool(
            name="confirm_weekly_review",
            description=(
                "Approve, execute, and distribute the weekly review. "
                "Generates outputs (HTML report, PPTX), executes approved Gantt "
                "proposals, uploads to Drive, emails team, and notifies Telegram. "
                "Call when Eyal says 'approve', 'looks good', 'distribute', etc. "
                "Set cancel=True to cancel the review instead. "
                "Set approve_gantt=False to distribute without executing Gantt changes."
            ),
        )
        async def confirm_weekly_review(
            session_id: str,
            approve_gantt: bool = True,
            cancel: bool = False,
        ) -> dict:
            try:
                from processors.weekly_review_session import (
                    finalize_review,
                    confirm_review,
                )
                from services.supabase_client import supabase_client as _sc

                # Validate session
                session = _sc.get_weekly_review_session(session_id)
                if not session:
                    return _error(f"Session {session_id} not found")

                status = session.get("status", "")

                # Cancel flow
                if cancel:
                    _sc.update_weekly_review_session(
                        session_id, status="cancelled"
                    )
                    _sc.log_action(
                        action="weekly_review_cancelled",
                        details={"session_id": session_id, "source": "mcp"},
                        triggered_by="eyal",
                    )
                    mcp_auth.log_call(
                        "confirm_weekly_review", {"action": "cancelled"}
                    )
                    return _success(
                        {"session_id": session_id, "action": "review_cancelled"}
                    )

                # Guard: already done
                if status in ("approved", "cancelled"):
                    return _error(
                        f"This review has already been {status}."
                    )

                # Finalize if not yet done (generates HTML + PPTX)
                # Fast no-op if already in "confirming" state (T-3h prep did it)
                warnings: list[str] = []
                if status in ("in_progress", "ready"):
                    try:
                        fin_result = await finalize_review(session_id)
                        if fin_result.get("action") == "error":
                            return _error(
                                fin_result.get("response", "Finalization failed")
                            )
                    except Exception as e:
                        warnings.append(f"Output generation issue: {e}")

                # Execute approval (Gantt + distribution)
                result = await confirm_review(
                    session_id,
                    approved=True,
                    execute_gantt=approve_gantt,
                )

                action = result.get("action", "")
                if action == "gantt_failed":
                    return _success(
                        {
                            "session_id": session_id,
                            "action": "gantt_failed",
                            "gantt_executed": False,
                            "backup_available": True,
                            "message": (
                                "Gantt execution failed. You can retry, "
                                "or call with approve_gantt=False to distribute "
                                "without Gantt changes."
                            ),
                        },
                        warnings=["Gantt execution failed"],
                    )

                # Audit log
                _sc.log_action(
                    action="weekly_review_confirmed",
                    details={
                        "session_id": session_id,
                        "approve_gantt": approve_gantt,
                        "action": action,
                        "source": "mcp",
                    },
                    triggered_by="eyal",
                )

                mcp_auth.log_call(
                    "confirm_weekly_review",
                    {"session_id": session_id, "action": action},
                )

                response = {
                    "session_id": session_id,
                    "action": action,
                    "gantt_executed": approve_gantt and action != "gantt_failed",
                    "distribution": result.get("distribution", {}),
                    "response": result.get("response", ""),
                }
                return _success(
                    response,
                    source="composite",
                    warnings=warnings if warnings else None,
                )

            except Exception as e:
                logger.error(f"confirm_weekly_review error: {e}")
                mcp_auth.log_call(
                    "confirm_weekly_review", success=False, error=str(e)
                )
                try:
                    from services.alerting import send_system_alert, AlertSeverity

                    await send_system_alert(
                        AlertSeverity.CRITICAL,
                        "mcp_weekly_review",
                        f"confirm_weekly_review failed: {e}",
                        error=e,
                    )
                except Exception:
                    pass
                return _error(str(e))

        # ============================================================
        # ============================================================
        # 19. update_decision (write) — Phase 9A
        # ============================================================
        @mcp.tool(
            name="update_decision",
            description=(
                "[DECISIONS] Update a decision's lifecycle status, review date, or rationale. "
                "Use get_decisions() to find the decision_id first. "
                "Status values: active, superseded, reversed."
            ),
        )
        async def update_decision(
            decision_id: str,
            decision_status: str | None = None,
            review_date: str | None = None,
            rationale: str | None = None,
        ) -> dict:
            try:
                from services.supabase_client import supabase_client as _sc

                updates = {}
                if decision_status:
                    updates["decision_status"] = decision_status
                if review_date:
                    updates["review_date"] = review_date
                if rationale:
                    updates["rationale"] = rationale

                if not updates:
                    return _error("No fields to update.")

                updated = _sc.update_decision(decision_id, **updates)
                _sc.log_action(
                    action="decision_updated",
                    details={"decision_id": decision_id, "updates": updates, "source": "mcp"},
                    triggered_by="eyal",
                )
                mcp_auth.log_call("update_decision", {"decision_id": decision_id})
                return _success({"decision": updated, "action": "decision_updated"})

            except Exception as e:
                logger.error(f"update_decision error: {e}")
                mcp_auth.log_call("update_decision", success=False, error=str(e))
                return _error(str(e))

        # ============================================================
        # 20. get_decisions_for_review (read) — Phase 9A
        # ============================================================
        @mcp.tool(
            name="get_decisions_for_review",
            description=(
                "[DECISIONS] Get active decisions with upcoming review dates. "
                "Returns decisions due for review within the next N days."
            ),
        )
        async def get_decisions_for_review(days_ahead: int = 30) -> dict:
            try:
                from services.supabase_client import supabase_client as _sc

                decisions = _sc.get_decisions_for_review(days_ahead=days_ahead)
                mcp_auth.log_call("get_decisions_for_review", {"days_ahead": days_ahead})
                return _success(decisions)

            except Exception as e:
                logger.error(f"get_decisions_for_review error: {e}")
                mcp_auth.log_call("get_decisions_for_review", success=False, error=str(e))
                return _error(str(e))

        # ============================================================
        # ============================================================
        # 21. get_topic_thread (read) — Phase 9B
        # ============================================================
        @mcp.tool(
            name="get_topic_thread",
            description=(
                "[TOPICS] Get the evolution of a topic/project across meetings. "
                "Returns chronological narrative of how the topic was discussed "
                "and what decisions were made over time."
            ),
        )
        async def get_topic_thread(topic_name: str) -> dict:
            try:
                from processors.topic_threading import (
                    _find_thread_by_name,
                    _match_canonical_name,
                    generate_topic_evolution,
                    _get_thread_with_mentions,
                )

                # Try exact match, then canonical
                thread = _find_thread_by_name(topic_name)
                if not thread:
                    canonical = _match_canonical_name(topic_name)
                    if canonical:
                        thread = _find_thread_by_name(canonical)

                if not thread:
                    return _error(f"No topic thread found for '{topic_name}'")

                # Get full details with mentions
                full = _get_thread_with_mentions(thread["id"])

                # Generate evolution narrative if not cached
                if not full.get("evolution_summary"):
                    narrative = await generate_topic_evolution(thread["id"])
                    full["evolution_summary"] = narrative

                mcp_auth.log_call("get_topic_thread", {"topic": topic_name})
                return _success(full)

            except Exception as e:
                logger.error(f"get_topic_thread error: {e}")
                mcp_auth.log_call("get_topic_thread", success=False, error=str(e))
                return _error(str(e))

        # ============================================================
        # 22. list_topic_threads (read) — Phase 9B
        # ============================================================
        @mcp.tool(
            name="list_topic_threads",
            description=(
                "[TOPICS] List all topic threads with meeting counts. "
                "Shows which projects/topics are being actively discussed."
            ),
        )
        async def list_topic_threads(status: str | None = None) -> dict:
            try:
                from processors.topic_threading import list_active_threads

                threads = list_active_threads(status=status)
                mcp_auth.log_call("list_topic_threads", {"status": status})
                return _success(threads)

            except Exception as e:
                logger.error(f"list_topic_threads error: {e}")
                mcp_auth.log_call("list_topic_threads", success=False, error=str(e))
                return _error(str(e))

        # ============================================================
        # 23. merge_topic_threads (write) — Phase 9B
        # ============================================================
        @mcp.tool(
            name="merge_topic_threads",
            description=(
                "[TOPICS] Merge two duplicate topic threads into one. "
                "Re-links all mentions from source to target, deletes source. "
                "Use when auto-threading created duplicates."
            ),
        )
        async def merge_topic_threads(source_id: str, target_id: str) -> dict:
            try:
                from processors.topic_threading import merge_threads
                from services.supabase_client import supabase_client as _sc

                result = merge_threads(source_id, target_id)

                _sc.log_action(
                    action="topic_threads_merged",
                    details={"source": source_id, "target": target_id, "source": "mcp"},
                    triggered_by="eyal",
                )
                mcp_auth.log_call("merge_topic_threads", {"source": source_id, "target": target_id})
                return _success({"merged_into": result, "action": "threads_merged"})

            except Exception as e:
                logger.error(f"merge_topic_threads error: {e}")
                mcp_auth.log_call("merge_topic_threads", success=False, error=str(e))
                return _error(str(e))

        # ============================================================
        # 24. rename_topic_thread (write) — Phase 9B
        # ============================================================
        @mcp.tool(
            name="rename_topic_thread",
            description=(
                "[TOPICS] Rename a topic thread. Use when the auto-generated "
                "name is wrong or needs normalization."
            ),
        )
        async def rename_topic_thread(topic_id: str, new_name: str) -> dict:
            try:
                from processors.topic_threading import rename_thread
                from services.supabase_client import supabase_client as _sc

                result = rename_thread(topic_id, new_name)

                _sc.log_action(
                    action="topic_thread_renamed",
                    details={"topic_id": topic_id, "new_name": new_name, "source": "mcp"},
                    triggered_by="eyal",
                )
                mcp_auth.log_call("rename_topic_thread", {"topic_id": topic_id})
                return _success({"thread": result, "action": "thread_renamed"})

            except Exception as e:
                logger.error(f"rename_topic_thread error: {e}")
                mcp_auth.log_call("rename_topic_thread", success=False, error=str(e))
                return _error(str(e))

        # ============================================================
        # 25. update_task (write) — Phase 8a
        # ============================================================
        @mcp.tool(
            name="update_task",
            description=(
                "Update an existing task's assignee, deadline, status, or priority. "
                "Use get_tasks() first to find the task_id. "
                "Deadline accepts: 'March 30', 'next Friday', '2026-04-15'. "
                "Always confirm the change with Eyal before calling this tool."
            ),
        )
        async def update_task(
            task_id: str,
            assignee: str | None = None,
            deadline: str | None = None,
            status: str | None = None,
            priority: str | None = None,
        ) -> dict:
            try:
                from services.supabase_client import supabase_client as _sc
                from services.google_sheets import sheets_service

                # Build update dict from non-None params
                updates = {}
                if assignee is not None:
                    updates["assignee"] = assignee
                if status is not None:
                    updates["status"] = status
                if priority is not None:
                    updates["priority"] = priority

                # Parse deadline (natural language support)
                parsed_deadline = None
                if deadline is not None:
                    try:
                        from dateutil.parser import parse as parse_date
                        parsed_deadline = parse_date(deadline, fuzzy=True).date()
                        updates["deadline"] = parsed_deadline.isoformat()
                    except (ValueError, ImportError):
                        updates["deadline"] = deadline  # Pass as-is, let DB handle

                if not updates:
                    return _error("No fields to update. Provide at least one of: assignee, deadline, status, priority.")

                # Get current task for response context
                current = _sc.get_task(task_id) if hasattr(_sc, "get_task") else None

                # Update in Supabase
                updated = _sc.update_task(task_id, **updates)

                # Sheets sync: find row and update
                warnings: list[str] = []
                try:
                    title = updated.get("title", "") or (current or {}).get("title", "")
                    if title:
                        row = await sheets_service.find_task_row(title)
                        if row:
                            sheet_fields = {}
                            if assignee is not None:
                                sheet_fields["assignee"] = assignee
                            if status is not None:
                                sheet_fields["status"] = status
                            if priority is not None:
                                sheet_fields["priority"] = priority
                            if parsed_deadline is not None:
                                sheet_fields["deadline"] = parsed_deadline.isoformat()
                            await sheets_service.update_task_row(row, **sheet_fields)
                        else:
                            warnings.append("Task not found in Google Sheets — Sheets not synced")
                except Exception as sheets_err:
                    warnings.append(f"Sheets sync failed: {sheets_err}")

                # Note unusual transitions
                notes = []
                if status and current:
                    old_status = current.get("status", "")
                    if old_status == "done" and status == "pending":
                        notes.append("Reopening completed task.")

                # Audit log
                _sc.log_action(
                    action="task_updated",
                    details={
                        "task_id": task_id,
                        "updates": updates,
                        "source": "mcp",
                    },
                    triggered_by="eyal",
                )

                response = {
                    "task_id": task_id,
                    "updated_fields": list(updates.keys()),
                    "task": updated,
                }
                if notes:
                    response["notes"] = notes

                mcp_auth.log_call("update_task", {"task_id": task_id, "fields": list(updates.keys())})
                return _success(response, warnings=warnings if warnings else None)

            except Exception as e:
                logger.error(f"update_task error: {e}")
                mcp_auth.log_call("update_task", success=False, error=str(e))
                return _error(str(e))

        # ============================================================
        # 20. create_task (write)
        # ============================================================
        @mcp.tool(
            name="create_task",
            description=(
                "Create a new task. Assignee should be a team member name "
                "(Eyal, Roye, Paolo, Yoram) or empty string if unassigned. "
                "Deadline accepts: 'March 30', 'next Friday', '2026-04-15'. "
                "Always confirm with Eyal before creating."
            ),
        )
        async def create_task(
            title: str,
            assignee: str = "",
            deadline: str | None = None,
            priority: str = "M",
            category: str | None = None,
        ) -> dict:
            try:
                from services.supabase_client import supabase_client as _sc
                from services.google_sheets import sheets_service

                # Parse deadline
                parsed_deadline = None
                if deadline:
                    try:
                        from dateutil.parser import parse as parse_date
                        parsed_deadline = parse_date(deadline, fuzzy=True).date()
                    except (ValueError, ImportError):
                        pass  # Will pass as None

                # Create in Supabase
                task = _sc.create_task(
                    title=title,
                    assignee=assignee,
                    priority=priority,
                    deadline=parsed_deadline,
                    category=category,
                )

                # Sheets sync
                warnings: list[str] = []
                try:
                    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
                    await sheets_service.add_task(
                        task=title,
                        assignee=assignee,
                        source_meeting="Via Claude.ai",
                        deadline=parsed_deadline.isoformat() if parsed_deadline else None,
                        status="pending",
                        priority=priority,
                        created_date=today,
                        category=category or "",
                    )
                except Exception as sheets_err:
                    warnings.append(f"Sheets sync failed: {sheets_err}")

                # Audit log
                _sc.log_action(
                    action="task_created",
                    details={
                        "task_id": task.get("id"),
                        "title": title,
                        "assignee": assignee,
                        "source": "mcp",
                    },
                    triggered_by="eyal",
                )

                mcp_auth.log_call("create_task", {"title": title[:50]})
                return _success(
                    {"task": task, "action": "task_created"},
                    warnings=warnings if warnings else None,
                )

            except Exception as e:
                logger.error(f"create_task error: {e}")
                mcp_auth.log_call("create_task", success=False, error=str(e))
                return _error(str(e))

        # ============================================================
        # 21. quick_inject (write)
        # ============================================================
        @mcp.tool(
            name="quick_inject",
            description=(
                "Inject information into Gianluigi's memory. Send natural language "
                "and Gianluigi extracts tasks, decisions, and information. "
                "Returns extracted items for Eyal's review before saving. "
                "NEVER auto-confirm — always present items to Eyal first, then "
                "call confirm_quick_inject() after approval."
            ),
        )
        async def quick_inject(text: str) -> dict:
            try:
                from processors.debrief import process_quick_injection

                result = await process_quick_injection(
                    user_message=text,
                    user_id="eyal",
                )

                mcp_auth.log_call("quick_inject", {"text_length": len(text)})
                return _success({
                    "response": result.get("response", ""),
                    "extracted_items": result.get("extracted_items", []),
                    "action": result.get("action", "none"),
                    "instructions": (
                        "Present these items to Eyal for review. "
                        "If he wants changes, modify the items dict. "
                        "Then call confirm_quick_inject(items) to save."
                    ),
                })

            except Exception as e:
                logger.error(f"quick_inject error: {e}")
                mcp_auth.log_call("quick_inject", success=False, error=str(e))
                return _error(str(e))

        # ============================================================
        # 22. confirm_quick_inject (write)
        # ============================================================
        @mcp.tool(
            name="confirm_quick_inject",
            description=(
                "Save previously extracted quick injection items after Eyal approves. "
                "Items schema: [{type: 'task'|'decision'|'info'|'gantt_update', "
                "text: str, assignee?: str, priority?: str, deadline?: str}]. "
                "Call only after Eyal reviews and approves the items from quick_inject()."
            ),
        )
        async def confirm_quick_inject(items: list[dict]) -> dict:
            try:
                from processors.debrief import _inject_debrief_items
                from services.supabase_client import supabase_client as _sc

                today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
                result = await _inject_debrief_items(
                    session_id=None,
                    items=items,
                    source_date=today,
                )

                # Audit log
                _sc.log_action(
                    action="quick_inject_confirmed",
                    details={
                        "items_count": len(items),
                        "result": result.get("summary", ""),
                        "source": "mcp",
                    },
                    triggered_by="eyal",
                )

                mcp_auth.log_call("confirm_quick_inject", {"items_count": len(items)})
                return _success({
                    "action": "items_injected",
                    "summary": result.get("summary", ""),
                    "counts": result.get("counts", {}),
                    "meeting_id": result.get("meeting_id"),
                })

            except Exception as e:
                logger.error(f"confirm_quick_inject error: {e}")
                mcp_auth.log_call("confirm_quick_inject", success=False, error=str(e))
                return _error(str(e))

        # ============================================================
        # 23. get_system_health (read)
        # ============================================================
        @mcp.tool(
            name="get_system_health",
            description=(
                "Get system health: scheduler status (last run, stale detection), "
                "component health (Supabase, Google, Telegram), error counts, "
                "and data freshness. Use when Eyal asks 'is everything working?'"
            ),
        )
        async def get_system_health() -> dict:
            try:
                from core.health_monitor import collect_health_data

                data = collect_health_data()
                mcp_auth.log_call("get_system_health")
                return _success(data, source="health_monitor")

            except Exception as e:
                logger.error(f"get_system_health error: {e}")
                mcp_auth.log_call("get_system_health", success=False, error=str(e))
                return _error(str(e))

        # ============================================================
        # 24. get_cost_summary (read)
        # ============================================================
        @mcp.tool(
            name="get_cost_summary",
            description=(
                "Get LLM token usage and estimated costs for the past N days. "
                "Shows total cost, breakdown by model and by feature, and daily trend."
            ),
        )
        async def get_cost_summary(days: int = 7) -> dict:
            try:
                from services.supabase_client import supabase_client as _sc
                from core.cost_calculator import compute_cost_summary

                records = _sc.get_token_usage_summary(days=days)
                summary = compute_cost_summary(records)
                summary["period_days"] = days

                mcp_auth.log_call("get_cost_summary", {"days": days})
                return _success(summary, source="token_usage")

            except Exception as e:
                logger.error(f"get_cost_summary error: {e}")
                mcp_auth.log_call("get_cost_summary", success=False, error=str(e))
                return _error(str(e))

        # ============================================================
        # 25. propose_gantt_update (write)
        # ============================================================
        @mcp.tool(
            name="propose_gantt_update",
            description=(
                "Propose changes to the operational Gantt chart. Creates a proposal "
                "for Eyal's approval — changes are NOT applied until approved via "
                "approve_gantt_proposal(). "
                "Changes schema: [{section, subsection, week (int), value, reason?}]. "
                "OWNER PREFIX RULE: Every cell value MUST include an owner prefix "
                "like [R], [E], [P], [Y], [E/R], [ALL], [TBD]. "
                "Example: [{\"section\": \"Product & Technology\", "
                "\"subsection\": \"Execution\", \"week\": 14, "
                "\"value\": \"[R] Completed\", \"reason\": \"Per founders review\"}]"
            ),
        )
        async def propose_gantt_update(
            changes: list[dict],
            reason: str = "",
        ) -> dict:
            try:
                from services.gantt_manager import gantt_manager
                from services.supabase_client import supabase_client as _sc

                # Add reason to each change if provided at top level
                if reason:
                    for c in changes:
                        if not c.get("reason"):
                            c["reason"] = reason

                result = await gantt_manager.propose_gantt_update(
                    changes=changes,
                    source="mcp",
                )

                status = result.get("status", "")

                if status == "rejected":
                    mcp_auth.log_call("propose_gantt_update", {"status": "rejected"})
                    return _error(
                        f"Proposal rejected: {result.get('errors', [])}"
                    )

                if status == "needs_confirmation":
                    # Conflicts — return details for Claude to explain
                    conflicts = result.get("conflicts", [])
                    conflict_details = []
                    for c in conflicts:
                        conflict_details.append(
                            f"{c.get('section')} → {c.get('subsection')} (W{c.get('week')}): "
                            f"current='{c.get('existing_content')}', "
                            f"proposed='{c.get('proposed_content')}'"
                        )
                    mcp_auth.log_call("propose_gantt_update", {"status": "needs_confirmation"})
                    return _success({
                        "status": "needs_confirmation",
                        "conflicts": conflict_details,
                        "message": (
                            "Some cells already have content. Present each conflict "
                            "to Eyal and ask whether to replace or append."
                        ),
                    })

                # Success — proposal created
                proposal_id = result.get("proposal_id", "")

                _sc.log_action(
                    action="gantt_proposal_created",
                    details={
                        "proposal_id": proposal_id,
                        "changes_count": len(changes),
                        "source": "mcp",
                    },
                    triggered_by="eyal",
                )

                mcp_auth.log_call("propose_gantt_update", {"proposal_id": proposal_id})
                return _success({
                    "status": "pending",
                    "proposal_id": proposal_id,
                    "changes_count": len(result.get("changes", [])),
                    "message": (
                        "Proposal created. Present the changes to Eyal and "
                        "call approve_gantt_proposal(proposal_id) when approved."
                    ),
                })

            except Exception as e:
                logger.error(f"propose_gantt_update error: {e}")
                mcp_auth.log_call("propose_gantt_update", success=False, error=str(e))
                return _error(str(e))

        # ============================================================
        # 26. approve_gantt_proposal (write)
        # ============================================================
        @mcp.tool(
            name="approve_gantt_proposal",
            description=(
                "Execute an approved Gantt proposal. Creates a backup snapshot first, "
                "then applies changes to the Gantt chart. Call only after Eyal "
                "explicitly approves the proposal from propose_gantt_update()."
            ),
        )
        async def approve_gantt_proposal(proposal_id: str) -> dict:
            try:
                from services.gantt_manager import gantt_manager
                from services.supabase_client import supabase_client as _sc

                result = await gantt_manager.execute_approved_proposal(proposal_id)

                status = result.get("status", "")
                if status == "error":
                    return _error(result.get("error", "Execution failed"))

                # Build human-readable change descriptions
                changes_applied = []
                for c in result.get("changes_applied", result.get("changes", [])):
                    desc = (
                        f"{c.get('section', '')} → {c.get('subsection', '')}: "
                        f"'{c.get('old_value', '')}' → '{c.get('new_value', '')}' "
                        f"(W{c.get('week', '?')})"
                    )
                    changes_applied.append(desc)

                _sc.log_action(
                    action="gantt_proposal_executed",
                    details={
                        "proposal_id": proposal_id,
                        "changes_count": len(changes_applied),
                        "source": "mcp",
                    },
                    triggered_by="eyal",
                )

                mcp_auth.log_call("approve_gantt_proposal", {"proposal_id": proposal_id})
                return _success({
                    "executed": True,
                    "proposal_id": proposal_id,
                    "changes_applied": changes_applied,
                    "snapshot_id": result.get("snapshot_id", ""),
                    "message": f"Applied {len(changes_applied)} Gantt changes. Snapshot saved for rollback.",
                })

            except Exception as e:
                logger.error(f"approve_gantt_proposal error: {e}")
                mcp_auth.log_call("approve_gantt_proposal", success=False, error=str(e))
                try:
                    from services.alerting import send_system_alert, AlertSeverity
                    await send_system_alert(
                        AlertSeverity.CRITICAL, "gantt_execution",
                        f"Gantt proposal execution failed: {e}", error=e,
                    )
                except Exception:
                    pass
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
