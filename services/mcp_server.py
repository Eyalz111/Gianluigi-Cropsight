"""
MCP server for Gianluigi — Claude.ai as CEO dashboard.

Provides 45 tools (read + write + composite) as thin wrappers around existing brain functions.
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


def _coerce_urgency(value) -> str:
    """Normalize a task urgency to H/M/L (default M). Shared by create/update_task."""
    u = str(value or "M").strip().upper()
    return u if u in ("H", "M", "L") else "M"


def _resolve_category_field(category, resolver) -> tuple[dict, str | None]:
    """Canonicalize a Category (= Gantt area, 2026-06 realignment) for update_task.

    `category is None` means "leave it untouched" → ({}, None). Otherwise
    `resolver` (supabase_client.resolve_category) canonicalizes it against the
    live areas. Returns (fields_to_merge_into_updates, resolved_label_or_None).
    Extracted from the tool closures so the resolution logic is unit-testable.
    """
    if category is None:
        return {}, None
    canonical = resolver(category)
    return {"category": canonical}, canonical


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
    MCP server with SSE transport, auth middleware, and 45 tools (read + write + composite).

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
                "Always confirm changes with Eyal before executing.\n"
                "8. Pending proposals (knowledge topic merges/assignments, task-field "
                "updates, Gantt row->topic tags) are listed by get_proposals(type?) and "
                "acted on with decide_proposal(proposal_id, decision). Gantt write "
                "operations (tag/refresh/restructure/links/update) go through the "
                "gantt_ops(action, ...) composite; read views are get_gantt_status(view)."
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
    # MCP Tools (45 tools: read + write + composite)
    # ------------------------------------------------------------------

    def _register_tools(self, mcp: FastMCP) -> None:
        """Register all 45 MCP tools on the FastMCP instance."""

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
            description="[SESSION] Get the most recent MCP session summary for continuity across conversations.",
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
                "[SESSION] Save a session summary for continuity. Call at the end of each "
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
                "[MEMORY] Search Gianluigi's memory using hybrid RAG (semantic + keyword). "
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
            description="[TASKS] Query tasks with optional filters by assignee, status, or category.",
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
            description="[DECISIONS] Query decision history with optional filters by topic or meeting.",
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

                # Phase 12 A4: Touch queried decisions (freshness tracking)
                for d in decisions[:10]:
                    did = d.get("id")
                    if did:
                        try:
                            supabase_client.touch_decision(did)
                        except Exception:
                            pass

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
            description="[MEMORY] Get unresolved questions from meetings, optionally filtered by status.",
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
        # 7b. get_shadow_diff_summary (v2.5 knowledge cutover support)
        # ============================================================
        @mcp.tool(
            name="get_shadow_diff_summary",
            description=(
                "[KNOWLEDGE] Aggregate recent knowledge-foundation shadow runs to "
                "support the read-back cutover decision. Returns per-pass run counts, "
                "total task additions vs regressions, average added cost/latency, and "
                "how many recent meetings had a regression (a task the live path had "
                "but the shadow dropped). Use to decide when >=10 clean meetings are met."
            ),
        )
        async def get_shadow_diff_summary(limit: int = 50) -> dict:
            try:
                from services.supabase_client import supabase_client
                from config.settings import settings

                rows = (
                    supabase_client.client.table("audit_log")
                    .select("*")
                    .like("action", "shadow_%")
                    .order("created_at", desc=True)
                    .limit(limit)
                    .execute()
                    .data
                    or []
                )

                per_pass: dict[str, int] = {}
                tasks_added = 0
                tasks_removed = 0
                regression_meetings = 0
                costs: list[float] = []
                latencies: list[float] = []

                for r in rows:
                    pass_name = (r.get("action") or "shadow_?").replace("shadow_", "", 1)
                    per_pass[pass_name] = per_pass.get(pass_name, 0) + 1
                    details = r.get("details") or {}
                    diff = details.get("diff") or {}
                    tasks_added += len(diff.get("tasks_added") or [])
                    removed = diff.get("tasks_removed") or []
                    tasks_removed += len(removed)
                    if removed:
                        regression_meetings += 1
                    if details.get("cost_usd") is not None:
                        costs.append(details["cost_usd"])
                    if details.get("latency_s") is not None:
                        latencies.append(details["latency_s"])

                def _avg(xs: list[float]) -> float | None:
                    return round(sum(xs) / len(xs), 4) if xs else None

                summary = {
                    "runs": len(rows),
                    "per_pass": per_pass,
                    "tasks_added_total": tasks_added,
                    "tasks_removed_total": tasks_removed,
                    "regression_meetings": regression_meetings,
                    "avg_cost_usd": _avg(costs),
                    "avg_latency_s": _avg(latencies),
                    "cost_ceiling_usd": settings.KNOWLEDGE_READBACK_COST_CEILING_USD,
                    "latency_budget_s": settings.KNOWLEDGE_READBACK_LATENCY_BUDGET_S,
                }
                mcp_auth.log_call("get_shadow_diff_summary", {"limit": limit})
                return _success({"summary": summary, "recent": rows[:10]})

            except Exception as e:
                logger.error(f"get_shadow_diff_summary error: {e}")
                mcp_auth.log_call("get_shadow_diff_summary", success=False, error=str(e))
                return _error(str(e))

        # ============================================================
        # 7c. get_proposals + decide_proposal (composite — replaces
        #     get_knowledge_proposals / get_task_proposals /
        #     list_gantt_tag_proposals + their approve_* siblings)
        # ============================================================
        @mcp.tool(
            name="get_proposals",
            description=(
                "[PROPOSALS] List pending proposals awaiting your decision — knowledge "
                "topic merges/assignments, task-field updates, Gantt row->topic tags. "
                "Optional type filter: knowledge|task|gantt_tag. Act on one with "
                "decide_proposal."
            ),
        )
        async def get_proposals(type: str | None = None) -> dict:
            try:
                from services.supabase_client import supabase_client

                rows = supabase_client.get_pending_approvals_by_status("pending") or []

                if type == "knowledge":
                    types = ("topic_merge", "topic_assign")
                elif type == "task":
                    types = ("task_update_proposal",)
                elif type == "gantt_tag":
                    types = ("gantt_tag_mapping",)
                else:
                    types = None  # all pending proposals

                proposals = [
                    {"proposal_id": r.get("approval_id"), "type": r.get("content_type"),
                     **(r.get("content") or {})}
                    for r in rows
                    if types is None or r.get("content_type") in types
                ]
                mcp_auth.log_call("get_proposals", {"type": type, "count": len(proposals)})
                return _success(proposals)
            except Exception as e:
                logger.error(f"get_proposals error: {e}")
                mcp_auth.log_call("get_proposals", {"type": type}, success=False, error=str(e))
                return _error(str(e))

        @mcp.tool(
            name="decide_proposal",
            description=(
                "[PROPOSALS] Decide a pending proposal (knowledge/task/gantt_tag) by its "
                "proposal_id from get_proposals. decision='approve' applies it, 'reject' "
                "discards it. Optional edits overrides the proposed payload (gantt_tag "
                "mappings)."
            ),
        )
        async def decide_proposal(
            proposal_id: str,
            decision: str = "approve",
            edits: list | None = None,
        ) -> dict:
            try:
                from services.supabase_client import supabase_client

                pending = supabase_client.get_pending_approval(proposal_id)
                if not pending:
                    return _error(f"Proposal {proposal_id} not found")
                content_type = pending.get("content_type")

                # --- knowledge: topic merge / assignment ---
                if content_type in ("topic_merge", "topic_assign"):
                    from processors.topic_clustering import apply_topic_proposal

                    content = pending.get("content") or {}

                    if decision == "approve":
                        result = apply_topic_proposal(content)
                        supabase_client.delete_pending_approval(proposal_id)
                        supabase_client.log_action(
                            "knowledge_proposal_approved",
                            details={"proposal_id": proposal_id, **content, "result": result},
                            triggered_by="eyal",
                        )
                        mcp_auth.log_call("decide_proposal", {"proposal_id": proposal_id, "type": content_type, "decision": "approve"})
                        return _success({"decision": "approved", "result": result})

                    supabase_client.delete_pending_approval(proposal_id)
                    supabase_client.log_action(
                        "knowledge_proposal_rejected",
                        details={"proposal_id": proposal_id, **content},
                        triggered_by="eyal",
                    )
                    mcp_auth.log_call("decide_proposal", {"proposal_id": proposal_id, "type": content_type, "decision": "reject"})
                    return _success({"decision": "rejected"})

                # --- task-field update ---
                if content_type == "task_update_proposal":
                    c = pending.get("content") or {}
                    tid, field, proposed = c.get("task_id"), c.get("field"), c.get("proposed")
                    if decision == "approve" and tid and field:
                        upd = {field: proposed}
                        if field == "deadline":
                            upd["deadline_confidence"] = "EXPLICIT"
                        supabase_client.update_task(tid, **upd)
                        supabase_client.mark_task_field_manual(tid, field, "eyal_mcp")
                        supabase_client.delete_pending_approval(proposal_id)
                        supabase_client.log_action("task_proposal_approved",
                                                   details={"proposal_id": proposal_id, **c}, triggered_by="eyal")
                        mcp_auth.log_call("decide_proposal", {"proposal_id": proposal_id, "type": content_type, "decision": "approve"})
                        return _success({"decision": "approved", "task_id": tid, "field": field, "value": proposed})
                    supabase_client.delete_pending_approval(proposal_id)
                    supabase_client.log_action("task_proposal_rejected",
                                               details={"proposal_id": proposal_id, **c}, triggered_by="eyal")
                    mcp_auth.log_call("decide_proposal", {"proposal_id": proposal_id, "type": content_type, "decision": "reject"})
                    return _success({"decision": "rejected"})

                # --- gantt row->topic tag mapping ---
                if content_type == "gantt_tag_mapping":
                    content = pending.get("content") or {}
                    sheet_name = content.get("sheet_name")

                    # Reject must NOT perform the write. [audit P3-05]
                    if decision != "approve":
                        supabase_client.delete_pending_approval(proposal_id)
                        supabase_client.log_action(
                            "gantt_tag_proposal_rejected",
                            details={"proposal_id": proposal_id, "sheet_name": sheet_name},
                            triggered_by="eyal",
                        )
                        mcp_auth.log_call("decide_proposal", {"proposal_id": proposal_id, "type": content_type, "decision": "reject"})
                        return _success({"decision": "rejected"})

                    from processors.gantt_tagging import apply_row_tags

                    mapping = edits if edits is not None else content.get("candidates", [])
                    result = await apply_row_tags(sheet_name, mapping)
                    supabase_client.delete_pending_approval(proposal_id)
                    mcp_auth.log_call("decide_proposal", {"proposal_id": proposal_id, "type": content_type, **result})
                    return _success({"sheet": sheet_name, **result})

                return _error(
                    f"decide_proposal does not handle content_type {content_type!r}; "
                    "use the dedicated tool"
                )
            except Exception as e:
                logger.error(f"decide_proposal error: {e}")
                mcp_auth.log_call("decide_proposal", {"proposal_id": proposal_id}, success=False, error=str(e))
                return _error(str(e))

        # ============================================================
        # 7d. Task manual-flag control (v3 reconcile)
        # ============================================================
        @mcp.tool(
            name="clear_manual_flag",
            description=(
                "[TASKS] Clear the sticky 'manually set' flag on a task field "
                "(status/deadline/priority/assignee) so Gianluigi's inference can update it "
                "again. Use when a manual override is no longer needed."
            ),
        )
        async def clear_manual_flag(task_id: str, field: str) -> dict:
            try:
                from services.supabase_client import supabase_client
                ok = supabase_client.clear_manual_flag(task_id, field)
                mcp_auth.log_call("clear_manual_flag", {"task_id": task_id, "field": field})
                return _success({"cleared": ok, "task_id": task_id, "field": field})
            except Exception as e:
                logger.error(f"clear_manual_flag error: {e}")
                mcp_auth.log_call("clear_manual_flag", success=False, error=str(e))
                return _error(str(e))

        # ============================================================
        # 7e. gantt_ops (composite — Gantt write operations; replaces
        #     tag_gantt_row / clear_gantt_override / refresh_gantt /
        #     propose_gantt_restructure / apply_gantt_restructure_to_live /
        #     propose_gantt_links / approve_gantt_link_mapping /
        #     propose_gantt_update / approve_gantt_proposal)
        # ============================================================
        @mcp.tool(
            name="gantt_ops",
            description=(
                "[GANTT] Gantt write operations (composite). action: tag_row | "
                "clear_override | refresh | propose_restructure | apply_restructure "
                "(needs confirm=True) | propose_links | approve_links | propose_update | "
                "approve_proposal. See each action's params."
            ),
        )
        async def gantt_ops(
            action: str,
            sheet_name: str = "",
            row: int = 0,
            topic_id: str = "",
            area_id: str = "",
            owner: str = "",
            field: str = "",
            apply: bool = False,
            working_copy_id: str = "",
            confirm: bool = False,
            changes: list | None = None,
            reason: str = "",
            proposal_id: str = "",
        ) -> dict:
            try:
                # --- tag_row → tag_gantt_row(sheet_name, row, topic_id, area_id, owner) ---
                if action == "tag_row":
                    from services.gantt_rows import write_row_tag
                    from services.supabase_client import supabase_client
                    ok = await write_row_tag(sheet_name, row, topic_id)
                    if ok:
                        supabase_client.upsert_gantt_row({
                            "sheet_name": sheet_name, "topic_id": topic_id,
                            "area_id": area_id or None, "owner": owner or None, "display_order": row,
                        })
                    mcp_auth.log_call("gantt_ops", {"action": "tag_row", "sheet": sheet_name, "row": row, "topic_id": topic_id})
                    return _success({"tagged": ok, "sheet": sheet_name, "row": row, "topic_id": topic_id})

                # --- clear_override → clear_gantt_override(sheet_name, topic_id, field) ---
                if action == "clear_override":
                    from services.supabase_client import supabase_client
                    rows = supabase_client.get_gantt_rows(sheet_name)
                    gid = next((r["id"] for r in rows if r.get("topic_id") == topic_id), None)
                    if not gid:
                        return _error("Gantt row not found for that sheet + topic")
                    ok = supabase_client.clear_gantt_manual_flag(gid, field)
                    mcp_auth.log_call("gantt_ops", {"action": "clear_override", "sheet": sheet_name, "topic_id": topic_id, "field": field})
                    return _success({"cleared": ok, "field": field})

                # --- refresh → refresh_gantt(apply) ---
                if action == "refresh":
                    from processors.gantt_readback import reconcile_gantt_lanes
                    from processors.gantt_nudge import compute_gantt_nudges
                    shadow = not apply
                    recon = await reconcile_gantt_lanes(shadow=shadow)
                    nudges = compute_gantt_nudges(shadow=shadow)
                    mcp_auth.log_call("gantt_ops", {"action": "refresh", "apply": apply})
                    return _success({"status": "applied" if apply else "preview",
                                     "read_back": recon, "nudges": nudges})

                # --- propose_restructure → propose_gantt_restructure() ---
                if action == "propose_restructure":
                    from processors.gantt_restructure import propose_restructure
                    res = await propose_restructure()
                    mcp_auth.log_call("gantt_ops", {"action": "propose_restructure"})
                    return _success(res)

                # --- apply_restructure → apply_gantt_restructure_to_live(working_copy_id, confirm) ---
                if action == "apply_restructure":
                    from processors.gantt_restructure import apply_restructure_to_live
                    res = await apply_restructure_to_live(working_copy_id, confirm=confirm)
                    mcp_auth.log_call("gantt_ops", {"action": "apply_restructure", "confirm": confirm})
                    return _success(res)

                # --- propose_links → propose_gantt_links() ---
                if action == "propose_links":
                    from processors.gantt_linkage import propose_lane_links
                    res = propose_lane_links(persist_preview=True)
                    mcp_auth.log_call("gantt_ops", {"action": "propose_links"})
                    return _success(res)

                # --- approve_links → approve_gantt_link_mapping() ---
                if action == "approve_links":
                    from processors.gantt_linkage import build_link_proposals, apply_lane_links
                    res = apply_lane_links(build_link_proposals()["proposals"])
                    mcp_auth.log_call("gantt_ops", {"action": "approve_links"})
                    return _success(res)

                # --- propose_update → propose_gantt_update(changes, reason) ---
                if action == "propose_update":
                    from services.gantt_manager import gantt_manager
                    from services.supabase_client import supabase_client as _sc

                    changes_list = changes or []
                    # Add reason to each change if provided at top level
                    if reason:
                        for c in changes_list:
                            if not c.get("reason"):
                                c["reason"] = reason

                    result = await gantt_manager.propose_gantt_update(
                        changes=changes_list,
                        source="mcp",
                    )

                    status = result.get("status", "")

                    if status == "rejected":
                        mcp_auth.log_call("gantt_ops", {"action": "propose_update", "status": "rejected"})
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
                        mcp_auth.log_call("gantt_ops", {"action": "propose_update", "status": "needs_confirmation"})
                        return _success({
                            "status": "needs_confirmation",
                            "conflicts": conflict_details,
                            "message": (
                                "Some cells already have content. Present each conflict "
                                "to Eyal and ask whether to replace or append."
                            ),
                        })

                    # Success — proposal created
                    new_proposal_id = result.get("proposal_id", "")

                    _sc.log_action(
                        action="gantt_proposal_created",
                        details={
                            "proposal_id": new_proposal_id,
                            "changes_count": len(changes_list),
                            "source": "mcp",
                        },
                        triggered_by="eyal",
                    )

                    mcp_auth.log_call("gantt_ops", {"action": "propose_update", "proposal_id": new_proposal_id})
                    return _success({
                        "status": "pending",
                        "proposal_id": new_proposal_id,
                        "changes_count": len(result.get("changes", [])),
                        "message": (
                            "Proposal created. Present the changes to Eyal and "
                            "call gantt_ops(action='approve_proposal', proposal_id=...) when approved."
                        ),
                    })

                # --- approve_proposal → approve_gantt_proposal(proposal_id) ---
                if action == "approve_proposal":
                    from services.gantt_manager import gantt_manager
                    from services.supabase_client import supabase_client as _sc

                    try:
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

                        mcp_auth.log_call("gantt_ops", {"action": "approve_proposal", "proposal_id": proposal_id})
                        return _success({
                            "executed": True,
                            "proposal_id": proposal_id,
                            "changes_applied": changes_applied,
                            "snapshot_id": result.get("snapshot_id", ""),
                            "message": f"Applied {len(changes_applied)} Gantt changes. Snapshot saved for rollback.",
                        })
                    except Exception as e:
                        logger.error(f"gantt_ops approve_proposal error: {e}")
                        mcp_auth.log_call("gantt_ops", {"action": "approve_proposal"}, success=False, error=str(e))
                        try:
                            from services.alerting import send_system_alert, AlertSeverity
                            await send_system_alert(
                                AlertSeverity.CRITICAL, "gantt_execution",
                                f"Gantt proposal execution failed: {e}", error=e,
                            )
                        except Exception:
                            pass
                        return _error(str(e))

                return _error(
                    f"Unknown action '{action}'. Valid: tag_row, clear_override, refresh, "
                    "propose_restructure, apply_restructure, propose_links, approve_links, "
                    "propose_update, approve_proposal"
                )

            except Exception as e:
                logger.error(f"gantt_ops error: {e}")
                mcp_auth.log_call("gantt_ops", {"action": action}, success=False, error=str(e))
                return _error(str(e))

        # ============================================================
        # 8. deal_ops (composite — replaces deprecated get_commitments)
        # ============================================================
        @mcp.tool(
            name="deal_ops",
            description=(
                "[DEALS] Composite tool for deal & relationship intelligence. "
                "Actions: 'list' (all deals), 'get' (single deal + timeline), "
                "'create' (new deal), 'update' (change deal fields), "
                "'timeline' (deal interaction history), "
                "'commitment_list' (external commitments), "
                "'commitment_create' (new external commitment), "
                "'commitment_update' (update commitment status), "
                "'pulse' (deal pulse + overdue commitments for brief)."
            ),
        )
        async def deal_ops(
            action: str,
            deal_id: str | None = None,
            name: str | None = None,
            organization: str | None = None,
            stage: str | None = None,
            contact_person: str | None = None,
            value_estimate: str | None = None,
            next_action: str | None = None,
            next_action_date: str | None = None,
            source: str | None = None,
            notes: str | None = None,
            commitment: str | None = None,
            promised_to: str | None = None,
            deadline: str | None = None,
            status: str | None = None,
            commitment_id: str | None = None,
        ) -> dict:
            try:
                from services.supabase_client import supabase_client
                from processors.deal_intelligence import generate_deal_pulse, generate_commitments_due

                if action == "list":
                    deals = supabase_client.get_deals(stage=stage)
                    mcp_auth.log_call("deal_ops", {"action": "list", "stage": stage})
                    return _success(deals)

                elif action == "get":
                    if not deal_id:
                        return _error("deal_id required for 'get' action")
                    deal = supabase_client.get_deal(deal_id)
                    if not deal:
                        return _error(f"Deal {deal_id} not found")
                    timeline = supabase_client.get_deal_timeline(deal_id, limit=10)
                    mcp_auth.log_call("deal_ops", {"action": "get", "deal_id": deal_id})
                    return _success({"deal": deal, "timeline": timeline})

                elif action == "create":
                    if not name or not organization:
                        return _error("name and organization required for 'create' action")
                    deal = supabase_client.create_deal(
                        name=name,
                        organization=organization,
                        contact_person=contact_person,
                        stage=stage or "lead",
                        value_estimate=value_estimate,
                        next_action=next_action,
                        next_action_date=next_action_date,
                        source=source,
                        notes=notes,
                    )
                    mcp_auth.log_call("deal_ops", {"action": "create", "name": name})
                    return _success(deal)

                elif action == "update":
                    if not deal_id:
                        return _error("deal_id required for 'update' action")
                    updates = {}
                    if stage:
                        updates["stage"] = stage
                    if contact_person:
                        updates["contact_person"] = contact_person
                    if value_estimate:
                        updates["value_estimate"] = value_estimate
                    if next_action:
                        updates["next_action"] = next_action
                    if next_action_date:
                        updates["next_action_date"] = next_action_date
                    if notes:
                        updates["notes"] = notes
                    if name:
                        updates["name"] = name
                    if not updates:
                        return _error("No fields to update")
                    deal = supabase_client.update_deal(deal_id, **updates)
                    mcp_auth.log_call("deal_ops", {"action": "update", "deal_id": deal_id})
                    return _success(deal)

                elif action == "timeline":
                    if not deal_id:
                        return _error("deal_id required for 'timeline' action")
                    timeline = supabase_client.get_deal_timeline(deal_id)
                    mcp_auth.log_call("deal_ops", {"action": "timeline", "deal_id": deal_id})
                    return _success(timeline)

                elif action == "commitment_list":
                    commitments = supabase_client.get_external_commitments(
                        status=status,
                        organization=organization,
                    )
                    mcp_auth.log_call("deal_ops", {"action": "commitment_list"})
                    return _success(commitments)

                elif action == "commitment_create":
                    if not organization or not commitment:
                        return _error("organization and commitment required")
                    result = supabase_client.create_external_commitment(
                        organization=organization,
                        commitment=commitment,
                        deal_id=deal_id,
                        contact_person=contact_person,
                        promised_to=promised_to,
                        deadline=deadline,
                        notes=notes,
                    )
                    mcp_auth.log_call("deal_ops", {"action": "commitment_create"})
                    return _success(result)

                elif action == "commitment_update":
                    if not commitment_id:
                        return _error("commitment_id required")
                    updates = {}
                    if status:
                        updates["status"] = status
                    if notes:
                        updates["notes"] = notes
                    if deadline:
                        updates["deadline"] = deadline
                    if not updates:
                        return _error("No fields to update")
                    result = supabase_client.update_external_commitment(commitment_id, **updates)
                    mcp_auth.log_call("deal_ops", {"action": "commitment_update"})
                    return _success(result)

                elif action == "pulse":
                    pulse = generate_deal_pulse()
                    commitments_due = generate_commitments_due()
                    mcp_auth.log_call("deal_ops", {"action": "pulse"})
                    return _success({
                        "deal_pulse": pulse,
                        "commitments_due": commitments_due,
                    })

                else:
                    return _error(
                        f"Unknown action '{action}'. Valid: list, get, create, update, "
                        "timeline, commitment_list, commitment_create, commitment_update, pulse"
                    )

            except Exception as e:
                logger.error(f"deal_ops error: {e}")
                mcp_auth.log_call("deal_ops", {"action": action}, success=False, error=str(e))
                return _error(str(e))

        # ============================================================
        # 9. get_stakeholder_info
        # ============================================================
        @mcp.tool(
            name="get_stakeholder_info",
            description="[MEMORY] Search the stakeholder tracker for contacts or organizations.",
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
            description="[MEMORY] List recent meetings with optional topic search.",
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
            description="[SYSTEM] Get the current approval queue — items waiting for Eyal's review.",
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
        # 12. get_gantt_status (composite read — replaces get_gantt_horizon /
        #     get_gantt_metrics / get_gantt_nudges)
        # ============================================================
        @mcp.tool(
            name="get_gantt_status",
            description=(
                "[GANTT] Read the Gantt chart. view='status' (default): current week data "
                "(optional week=N). view='now_next_later': prioritized Now/Next/Later view. "
                "view='horizon': upcoming milestones/transitions (weeks_ahead). "
                "view='metrics': velocity, slippage, milestone risk. "
                "view='nudges': brief<->board divergence nudges (shadow; no writes)."
            ),
        )
        async def get_gantt_status(
            view: str = "status",
            week: int | None = None,
            weeks_ahead: int = 8,
        ) -> dict:
            try:
                if view == "now_next_later":
                    from processors.gantt_intelligence import generate_now_next_later
                    nnl = await generate_now_next_later()
                    mcp_auth.log_call("get_gantt_status", {"view": "now_next_later"})
                    return _success(nnl, source="gantt_intelligence")

                if view == "horizon":
                    from services.gantt_manager import gantt_manager
                    horizon = await gantt_manager.get_gantt_horizon(weeks_ahead=weeks_ahead)
                    mcp_auth.log_call("get_gantt_status", {"view": "horizon", "weeks_ahead": weeks_ahead})
                    return _success(horizon, source="google_sheets")

                if view == "metrics":
                    from processors.gantt_intelligence import compute_gantt_metrics
                    metrics = await compute_gantt_metrics()
                    mcp_auth.log_call("get_gantt_status", {"view": "metrics"})
                    return _success(metrics, source="gantt_intelligence")

                if view == "nudges":
                    from processors.gantt_nudge import compute_gantt_nudges
                    res = compute_gantt_nudges(shadow=True)
                    mcp_auth.log_call("get_gantt_status", {"view": "nudges"})
                    return _success(res)

                from services.gantt_manager import gantt_manager

                status = await gantt_manager.get_gantt_status(week=week)
                mcp_auth.log_call("get_gantt_status", {"week": week})
                return _success(status, source="google_sheets")

            except Exception as e:
                logger.error(f"get_gantt_status error: {e}")
                mcp_auth.log_call("get_gantt_status", success=False, error=str(e))
                return _error(str(e))

        # ============================================================
        # 14. get_upcoming_meetings
        # ============================================================
        @mcp.tool(
            name="get_upcoming_meetings",
            description="[SYSTEM] Get upcoming calendar meetings with prep status.",
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
                "[REVIEW] Compile weekly review data — meetings, decisions, tasks, Gantt proposals, "
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
                "[SYSTEM] Get a complete operational status in one call — tasks, Gantt status, "
                "pending approvals, upcoming meetings, and attention items. "
                "Use view='ceo_today' for a focused CEO dashboard: overdue tasks, "
                "this week's tasks, Gantt milestones, deal pulse, drift alerts. "
                "Default view='standard' returns the full status."
            ),
        )
        async def get_full_status(view: str = "standard") -> dict:
            # NOTE: MCP is CEO-only interface — no sensitivity filtering applied.
            # All data returned unfiltered (max_sensitivity_level=4).
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

            # CEO Today view — add focused dashboard sections
            if view == "ceo_today":
                # Overdue tasks (with titles)
                try:
                    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
                    overdue = [
                        {"title": t.get("title", ""), "assignee": t.get("assignee", ""), "deadline": t.get("deadline", "")}
                        for t in result.get("tasks", [])
                        if t.get("deadline") and t["deadline"] < today and t.get("status") not in ("done", "cancelled")
                    ][:5]
                    result["overdue_tasks"] = overdue
                except Exception as e:
                    warnings.append(f"Overdue tasks filter failed: {e}")
                    result["overdue_tasks"] = []

                # This week's tasks
                try:
                    from datetime import timedelta as _td
                    week_end = (datetime.now(timezone.utc) + _td(days=7)).strftime("%Y-%m-%d")
                    this_week = [
                        {"title": t.get("title", ""), "assignee": t.get("assignee", ""), "deadline": t.get("deadline", ""), "status": t.get("status", "")}
                        for t in result.get("tasks", [])
                        if t.get("deadline") and t["deadline"] <= week_end and t.get("status") not in ("done", "cancelled")
                    ][:10]
                    result["this_week_tasks"] = this_week
                except Exception as e:
                    warnings.append(f"This week tasks filter failed: {e}")
                    result["this_week_tasks"] = []

                # Gantt milestones this week
                try:
                    from processors.gantt_intelligence import compute_gantt_metrics
                    metrics = await compute_gantt_metrics()
                    result["gantt_milestones"] = metrics.get("milestone_risks", [])[:3]
                except Exception as e:
                    warnings.append(f"Gantt milestones unavailable: {e}")
                    result["gantt_milestones"] = []

                # Deal pulse
                try:
                    from processors.deal_intelligence import generate_deal_pulse, generate_commitments_due
                    result["deal_pulse"] = generate_deal_pulse(max_items=3)
                    result["commitments_due"] = generate_commitments_due(max_items=3)
                except Exception as e:
                    warnings.append(f"Deal pulse unavailable: {e}")
                    result["deal_pulse"] = []
                    result["commitments_due"] = []

                # Drift alerts
                try:
                    from processors.gantt_intelligence import detect_gantt_drift
                    result["drift_alerts"] = await detect_gantt_drift()
                except Exception as e:
                    warnings.append(f"Drift detection unavailable: {e}")
                    result["drift_alerts"] = []

            mcp_auth.log_call("get_full_status", {"view": view}, response_size=len(str(result)))
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
                "[REVIEW] Start or resume the weekly CEO review session. Returns all compiled "
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
                "[REVIEW] Approve, execute, and distribute the weekly review. "
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
        # 20b. get_decision_chain (read) — Phase 12 A6
        # ============================================================
        @mcp.tool(
            name="get_decision_chain",
            description=(
                "[DECISIONS] Trace the evolution of a decision over time. "
                "Given a decision ID, returns the full chain of related decisions "
                "(predecessors and successors) showing how the decision evolved."
            ),
        )
        async def get_decision_chain(decision_id: str) -> dict:
            try:
                from services.supabase_client import supabase_client as _sc

                chain = _sc.get_decision_chain(decision_id)
                mcp_auth.log_call("get_decision_chain", {"decision_id": decision_id})

                if not chain:
                    return _success(
                        {"chain": [], "message": "No chain found — this decision has no linked predecessors or successors."},
                        warnings=["Decision may not have parent/child links yet."],
                    )

                # Format for readability
                formatted = []
                for d in chain:
                    meeting = d.get("meetings") or {}
                    formatted.append({
                        "id": d.get("id"),
                        "description": d.get("description"),
                        "status": d.get("decision_status"),
                        "meeting_title": meeting.get("title", ""),
                        "meeting_date": str(meeting.get("date", ""))[:10],
                        "parent_id": d.get("parent_decision_id"),
                        "superseded_by": d.get("superseded_by"),
                        "last_referenced_at": d.get("last_referenced_at"),
                    })

                return _success({"chain": formatted, "count": len(formatted)})

            except Exception as e:
                logger.error(f"get_decision_chain error: {e}")
                mcp_auth.log_call("get_decision_chain", success=False, error=str(e))
                return _error(str(e))

        # ============================================================
        # 21. get_topic_thread (read) — Phase 9B
        # ============================================================
        @mcp.tool(
            name="get_topic_thread",
            description=(
                "[TOPICS] Get the evolution of a topic/project across meetings. "
                "Returns structured state (current status, stakeholders, open "
                "items, last decision, key facts) alongside the prose narrative. "
                "Set include_state=False for the pre-v2.3 narrative-only payload."
            ),
        )
        async def get_topic_thread(topic_name: str, include_state: bool = True) -> dict:
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

                # v2.3 PR 4: include state_json by default. state_json may be
                # null for threads that have not been backfilled and have not
                # received a new mention since the feature shipped — surface
                # the null gracefully rather than hiding the field.
                if not include_state:
                    full.pop("state_json", None)
                    full.pop("state_updated_at", None)

                mcp_auth.log_call("get_topic_thread", {"topic": topic_name, "include_state": include_state})
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
                "Shows which projects/topics are being actively discussed. "
                "Set include_state=True to include the structured state_json "
                "per thread (heavier payload)."
            ),
        )
        async def list_topic_threads(
            status: str | None = None,
            include_state: bool = False,
        ) -> dict:
            try:
                from processors.topic_threading import list_active_threads

                threads = list_active_threads(status=status)
                # v2.3 PR 4: the base list_active_threads already returns
                # state_json (SELECT *). Strip it by default to keep the
                # list payload light; caller opts in explicitly.
                if not include_state:
                    for t in threads:
                        t.pop("state_json", None)
                        t.pop("state_updated_at", None)

                mcp_auth.log_call("list_topic_threads", {"status": status, "include_state": include_state})
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
        # get_gantt_metrics folded into get_gantt_status(view="metrics")
        # ============================================================

        # ============================================================
        # 26. update_task (write) — Phase 8a
        # ============================================================
        @mcp.tool(
            name="update_task",
            description=(
                "[TASKS] Update an existing task's assignee, deadline, status, priority, "
                "urgency, or category. Use get_tasks() first to find the task_id. "
                "Deadline accepts: 'March 30', 'next Friday', '2026-04-15'. "
                "status='archived' removes the task from the working view (sheet + briefs) "
                "while keeping it in the DB for history. category is a Gantt board area "
                "name (e.g. 'PRODUCT & TECHNOLOGY') or 'General'. "
                "Always confirm the change with Eyal before calling this tool."
            ),
        )
        async def update_task(
            task_id: str,
            assignee: str | None = None,
            deadline: str | None = None,
            status: str | None = None,
            priority: str | None = None,
            urgency: str | None = None,
            category: str | None = None,
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
                # urgency (H/M/L time-pressure) + category (the Gantt-area
                # taxonomy, canonicalized against the live areas table).
                if urgency is not None:
                    updates["urgency"] = _coerce_urgency(urgency)
                _cat_fields, _cat_label = _resolve_category_field(category, _sc.resolve_category)
                updates.update(_cat_fields)

                # Parse deadline: day-first numeric dates FIRST (20.6.26 must
                # mean 20 June everywhere — same convention as the sheet),
                # then dateutil fuzzy for natural language ("next Friday").
                parsed_deadline = None
                if deadline is not None:
                    from core.dates import parse_human_date
                    _iso = parse_human_date(deadline)
                    if _iso:
                        from datetime import date as _date
                        parsed_deadline = _date.fromisoformat(_iso)
                        updates["deadline"] = _iso
                    else:
                        try:
                            from dateutil.parser import parse as parse_date
                            parsed_deadline = parse_date(deadline, fuzzy=True).date()
                            updates["deadline"] = parsed_deadline.isoformat()
                        except (ValueError, ImportError):
                            updates["deadline"] = deadline  # update_task drops it if unparseable

                if not updates:
                    return _error("No fields to update. Provide at least one of: assignee, deadline, status, priority, urgency, category.")

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
                            # Keep the Sheet's Urgency/Category cells in lockstep
                            # so the reconcile pull doesn't revert this edit
                            # (urgency no-ops when the K column isn't enabled).
                            if "urgency" in updates:
                                sheet_fields["urgency"] = updates["urgency"]
                            if _cat_label is not None:
                                sheet_fields["category"] = _cat_label
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
                "[TASKS] Create a new task. Assignee should be a team member name "
                "(Eyal, Roye, Paolo, Yoram) or empty string if unassigned. "
                "Deadline accepts: 'March 30', 'next Friday', '2026-04-15'. "
                "priority=H/M/L is importance; urgency=H/M/L is time-pressure "
                "(use urgency=H for 'ASAP' WITHOUT inventing a deadline). category is a "
                "Gantt board area name (e.g. 'PRODUCT & TECHNOLOGY') or 'General'. "
                "Always confirm with Eyal before creating."
            ),
        )
        async def create_task(
            title: str,
            assignee: str = "",
            deadline: str | None = None,
            priority: str = "M",
            category: str | None = None,
            label: str = "",
            urgency: str = "M",
        ) -> dict:
            try:
                from services.supabase_client import supabase_client as _sc
                from services.google_sheets import sheets_service

                # Parse deadline: day-first numeric first (sheet convention),
                # dateutil fuzzy as the natural-language fallback.
                parsed_deadline = None
                if deadline:
                    from core.dates import parse_human_date
                    _iso = parse_human_date(deadline)
                    if _iso:
                        from datetime import date as _date
                        parsed_deadline = _date.fromisoformat(_iso)
                    else:
                        try:
                            from dateutil.parser import parse as parse_date
                            parsed_deadline = parse_date(deadline, fuzzy=True).date()
                        except (ValueError, ImportError):
                            pass  # Will pass as None

                # urgency (time-pressure) + category canonicalized against the
                # live Gantt areas (blank/None -> 'General').
                _u = _coerce_urgency(urgency)
                _category = _sc.resolve_category(category)

                # Create in Supabase
                task = _sc.create_task(
                    title=title,
                    assignee=assignee,
                    priority=priority,
                    deadline=parsed_deadline,
                    category=_category,
                    urgency=_u,
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
                        category=_category,
                        label=label,
                        # PR10: write the UUID (col J) + urgency so the row is
                        # reconcile-complete — otherwise it'd be re-created as a dup.
                        task_id=task.get("id", "") if isinstance(task, dict) else "",
                        urgency=_u,
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
                "[QUICK] Inject information into Gianluigi's memory. Send natural language "
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
                "[QUICK] Save previously extracted quick injection items after Eyal approves. "
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
                "[SYSTEM] Get system health: scheduler status (last run, stale detection), "
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
        # 23b. run_qa_check (read) — Cross-cutting X1
        # ============================================================
        @mcp.tool(
            name="run_qa_check",
            description=(
                "[SYSTEM] Run on-demand QA quality check. Checks extraction quality, "
                "distribution completeness, scheduler health, data integrity, and prompt health. "
                "Returns issues found and overall health score. "
                "Set reload_prompts=true to hot-reload YAML prompt files from disk."
            ),
        )
        async def run_qa_check_tool(reload_prompts: bool = False) -> dict:
            try:
                from schedulers.qa_scheduler import run_qa_check, format_qa_report

                # Optional: reload prompts from YAML files
                reload_result = None
                if reload_prompts:
                    from config.prompt_registry import prompt_registry
                    reload_result = prompt_registry.reload()
                    logger.info(f"Prompts reloaded: {reload_result}")

                report = run_qa_check()
                formatted = format_qa_report(report)
                mcp_auth.log_call("run_qa_check", {"reload_prompts": reload_prompts})

                result = _success({
                    "report": report,
                    "formatted": formatted,
                })
                if reload_result:
                    result["data"]["prompt_reload"] = reload_result
                return result

            except Exception as e:
                logger.error(f"run_qa_check error: {e}")
                mcp_auth.log_call("run_qa_check", success=False, error=str(e))
                return _error(str(e))

        # ============================================================
        # 24. get_cost_summary (read)
        # ============================================================
        @mcp.tool(
            name="get_cost_summary",
            description=(
                "[SYSTEM] Get LLM token usage and estimated costs for the past N days. "
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
        # propose_gantt_update + approve_gantt_proposal folded into
        # gantt_ops(action="propose_update" | "approve_proposal")
        # ============================================================

        # ============================================================
        # 34. list_canonical_projects (read)
        # ============================================================
        @mcp.tool(
            name="list_canonical_projects",
            description=(
                "[PROJECTS] List all canonical project names with their aliases. "
                "Use to see which projects Gianluigi recognizes for label normalization."
            ),
        )
        async def list_canonical_projects(status: str = "active") -> dict:
            try:
                from services.supabase_client import supabase_client as _sc

                projects = _sc.get_canonical_projects(status=status)
                formatted = []
                for p in projects:
                    formatted.append({
                        "name": p["name"],
                        "description": p.get("description", ""),
                        "aliases": p.get("aliases", []),
                        "status": p.get("status", "active"),
                    })

                mcp_auth.log_call("list_canonical_projects", {"status": status})
                return _success(formatted, record_count=len(formatted))

            except Exception as e:
                logger.error(f"list_canonical_projects error: {e}")
                mcp_auth.log_call("list_canonical_projects", success=False, error=str(e))
                return _error(str(e))

        # ============================================================
        # 35. add_canonical_project (write)
        # ============================================================
        @mcp.tool(
            name="add_canonical_project",
            description=(
                "[PROJECTS] Add a new canonical project name for label normalization. "
                "Include aliases (common variations). Retroactively resolves any "
                "unmatched labels that match the new name or aliases. "
                "Use when the weekly review surfaces recurring labels not yet canonical."
            ),
        )
        async def add_canonical_project(
            name: str,
            description: str = "",
            aliases: list[str] | None = None,
        ) -> dict:
            try:
                from services.supabase_client import supabase_client as _sc

                project = _sc.add_canonical_project(
                    name=name,
                    description=description,
                    aliases=aliases or [],
                )

                if not project:
                    return _error(f"Failed to create project '{name}' — may already exist.")

                _sc.log_action(
                    action="canonical_project_added",
                    details={
                        "project_name": name,
                        "aliases": aliases or [],
                        "source": "mcp",
                    },
                    triggered_by="eyal",
                )

                mcp_auth.log_call("add_canonical_project", {"name": name})
                return _success({
                    "project": project,
                    "message": f"Added '{name}' as a canonical project.",
                })

            except Exception as e:
                logger.error(f"add_canonical_project error: {e}")
                mcp_auth.log_call("add_canonical_project", success=False, error=str(e))
                return _error(str(e))

        # ============================================================
        # 36. sync_from_sheets (read + write)
        # ============================================================
        @mcp.tool(
            name="sync_from_sheets",
            description=(
                "[SHEETS] Reconcile the Tasks sheet against the DB (v3 column-ownership "
                "engine). apply=False previews (computes + logs, no writes). apply=True "
                "reconciles: pulls your action-field edits (status/deadline/priority/owner) "
                "to the DB and marks them sticky, refreshes the Sheet from the DB, matches "
                "by task UUID. If RECONCILE_SHADOW_MODE is on, nothing is written even on "
                "apply=True (flip it off to go live)."
            ),
        )
        async def sync_from_sheets(apply: bool = False) -> dict:
            try:
                from processors.sheets_sync import reconcile_tasks

                summary = await reconcile_tasks(dry_run=not apply)
                mcp_auth.log_call("sync_from_sheets", {"apply": apply})
                if isinstance(summary, dict) and summary.get("error"):
                    return _error(summary["error"])
                applied = bool(apply) and not summary.get("shadow") and not summary.get("dry_run")
                return _success({
                    "status": "applied" if applied else "preview",
                    "summary": summary,
                    "note": (
                        "Reconcile is in SHADOW mode — nothing was written. "
                        "Set RECONCILE_SHADOW_MODE=false to apply."
                    ) if summary.get("shadow") else None,
                })
            except Exception as e:
                logger.error(f"sync_from_sheets error: {e}")
                mcp_auth.log_call("sync_from_sheets", success=False, error=str(e))
                return _error(str(e))

    # ------------------------------------------------------------------
    # Intelligence Signal Tools (39-43)
    # ------------------------------------------------------------------

        # 39. get_intelligence_signal_status (read)
        @mcp.tool(
            name="get_intelligence_signal_status",
            description=(
                "[INTELLIGENCE] Get the latest intelligence signal status, flags, "
                "Drive links, and next scheduled generation time."
            ),
        )
        async def get_intelligence_signal_status(
            signal_id: str | None = None,
        ) -> dict:
            try:
                from services.supabase_client import supabase_client as _sc

                if signal_id:
                    signal = _sc.get_intelligence_signal(signal_id)
                else:
                    signal = _sc.get_latest_intelligence_signal()

                if not signal:
                    return _success({"message": "No intelligence signals found."})

                # Build response
                result = {
                    "signal_id": signal.get("signal_id"),
                    "week_number": signal.get("week_number"),
                    "year": signal.get("year"),
                    "status": signal.get("status"),
                    "flags": signal.get("flags") or [],
                    "research_source": signal.get("research_source"),
                    "drive_doc_url": signal.get("drive_doc_url"),
                    "drive_video_url": signal.get("drive_video_url"),
                    "created_at": signal.get("created_at"),
                    "distributed_at": signal.get("distributed_at"),
                    "recipients": signal.get("recipients"),
                }

                # Recent signals summary
                recent = _sc.get_intelligence_signals(limit=4)
                result["recent_signals"] = [
                    {
                        "signal_id": s.get("signal_id"),
                        "status": s.get("status"),
                        "week_number": s.get("week_number"),
                    }
                    for s in recent
                ]

                mcp_auth.log_call(
                    "get_intelligence_signal_status",
                    {"signal_id": signal_id},
                )
                return _success(result)

            except Exception as e:
                logger.error(f"get_intelligence_signal_status error: {e}")
                mcp_auth.log_call(
                    "get_intelligence_signal_status",
                    success=False,
                    error=str(e),
                )
                return _error(str(e))

        # 40. approve_intelligence_signal (write)
        @mcp.tool(
            name="approve_intelligence_signal",
            description=(
                "[INTELLIGENCE] Approve and distribute the intelligence signal to "
                "the team, or cancel it. Use get_intelligence_signal_status() first "
                "to review the signal. Pass cancel=True to reject."
            ),
        )
        async def approve_intelligence_signal(
            signal_id: str,
            cancel: bool = False,
        ) -> dict:
            try:
                from services.supabase_client import supabase_client as _sc

                signal = _sc.get_intelligence_signal(signal_id)
                if not signal:
                    return _error(f"Signal {signal_id} not found")

                if cancel:
                    _sc.update_intelligence_signal(signal_id, {"status": "cancelled"})
                    _sc.update_pending_approval(signal_id, status="rejected")
                    _sc.log_action(
                        action="intelligence_signal_cancelled",
                        details={"signal_id": signal_id},
                        triggered_by="eyal",
                    )
                    # v2.3 PR 3: observation log
                    try:
                        _sc.log_approval_observation(
                            content_type="intelligence_signal",
                            action="rejected",
                            original_content={
                                "title": signal.get("title"),
                                "week_number": signal.get("week_number"),
                            },
                            context={"signal_id": signal_id},
                        )
                    except Exception as e:
                        logger.warning(f"[observation] signal reject log failed (non-fatal): {e}")
                    mcp_auth.log_call(
                        "approve_intelligence_signal",
                        {"signal_id": signal_id, "cancel": True},
                    )
                    return _success({"signal_id": signal_id, "status": "cancelled"})

                # Approve and distribute
                _sc.update_pending_approval(signal_id, status="approved")
                # v2.3 PR 3: observation log
                try:
                    _sc.log_approval_observation(
                        content_type="intelligence_signal",
                        action="approved",
                        final_content={
                            "title": signal.get("title"),
                            "week_number": signal.get("week_number"),
                        },
                        context={"signal_id": signal_id},
                    )
                except Exception as e:
                    logger.warning(f"[observation] signal approve log failed (non-fatal): {e}")

                from config.settings import settings as _settings

                if _settings.INTELLIGENCE_SIGNAL_SAFE_DISTRIBUTE:
                    # Restart-safe path: mark 'approved_finalizing' and hand off to a
                    # reconstructable background worker — no 30-min blocking await in
                    # the MCP request path. The team gets it when the worker finishes.
                    import asyncio as _asyncio
                    from datetime import datetime as _dt, timezone as _tz

                    _sc.update_intelligence_signal(signal_id, {
                        "status": "approved_finalizing",
                        "finalize_started_at": _dt.now(_tz.utc).isoformat(),
                    })
                    from processors.intelligence_signal_agent import (
                        _attach_finalize_done_callback,
                        finalize_and_distribute_intelligence_signal,
                    )

                    _task = _asyncio.create_task(
                        finalize_and_distribute_intelligence_signal(signal_id)
                    )
                    _attach_finalize_done_callback(_task, signal_id)
                    result = {
                        "signal_id": signal_id,
                        "status": "approved_finalizing",
                        "note": "Distributing in the background; the team will receive it shortly.",
                    }
                else:
                    _sc.update_intelligence_signal(signal_id, {"status": "approved"})
                    from processors.intelligence_signal_agent import (
                        distribute_intelligence_signal,
                    )

                    result = await distribute_intelligence_signal(signal_id)

                mcp_auth.log_call(
                    "approve_intelligence_signal",
                    {"signal_id": signal_id},
                )
                return _success(result)

            except Exception as e:
                logger.error(f"approve_intelligence_signal error: {e}")
                mcp_auth.log_call(
                    "approve_intelligence_signal",
                    success=False,
                    error=str(e),
                )
                return _error(str(e))

        # 41. trigger_intelligence_signal (write)
        @mcp.tool(
            name="trigger_intelligence_signal",
            description=(
                "[INTELLIGENCE] Manually trigger an ad-hoc intelligence signal "
                "generation. Use this to generate a signal outside the regular "
                "Thursday schedule. The signal will go through the normal "
                "approval flow."
            ),
        )
        async def trigger_intelligence_signal() -> dict:
            try:
                from processors.intelligence_signal_agent import (
                    generate_intelligence_signal,
                )

                result = await generate_intelligence_signal()

                from services.supabase_client import supabase_client as _sc

                _sc.log_action(
                    action="intelligence_signal_triggered",
                    details={"signal_id": result.get("signal_id"), "source": "mcp"},
                    triggered_by="eyal",
                )

                mcp_auth.log_call("trigger_intelligence_signal", {})
                return _success(result)

            except Exception as e:
                logger.error(f"trigger_intelligence_signal error: {e}")
                mcp_auth.log_call(
                    "trigger_intelligence_signal",
                    success=False,
                    error=str(e),
                )
                return _error(str(e))

        # 42. get_competitor_watchlist (read)
        @mcp.tool(
            name="get_competitor_watchlist",
            description=(
                "[INTELLIGENCE] Get the auto-curated competitor watchlist with "
                "categories (known, discovered, watching), appearance counts, "
                "and last seen dates."
            ),
        )
        async def get_competitor_watchlist(
            include_deactivated: bool = False,
        ) -> dict:
            try:
                from services.supabase_client import supabase_client as _sc

                watchlist = _sc.get_competitor_watchlist(
                    include_deactivated=include_deactivated
                )

                mcp_auth.log_call(
                    "get_competitor_watchlist",
                    {"include_deactivated": include_deactivated},
                )
                return _success(watchlist)

            except Exception as e:
                logger.error(f"get_competitor_watchlist error: {e}")
                mcp_auth.log_call(
                    "get_competitor_watchlist",
                    success=False,
                    error=str(e),
                )
                return _error(str(e))

        # 43. add_competitor (write)
        @mcp.tool(
            name="add_competitor",
            description=(
                "[INTELLIGENCE] Manually add a competitor to the watchlist. "
                "Provide at minimum a name. Optional: category (known/watching), "
                "funding, target_customer, key_limitation, notes."
            ),
        )
        async def add_competitor(
            name: str,
            category: str = "known",
            funding: str | None = None,
            target_customer: str | None = None,
            key_limitation: str | None = None,
            notes: str | None = None,
        ) -> dict:
            try:
                from services.supabase_client import supabase_client as _sc

                data: dict = {
                    "name": name,
                    "category": category,
                    "added_by": "eyal",
                    "is_active": True,
                }
                if funding:
                    data["funding"] = funding
                if target_customer:
                    data["target_customer"] = target_customer
                if key_limitation:
                    data["key_limitation"] = key_limitation
                if notes:
                    data["notes"] = notes

                result = _sc.upsert_competitor(data)

                _sc.log_action(
                    action="competitor_added",
                    details={"name": name, "category": category},
                    triggered_by="eyal",
                )

                mcp_auth.log_call("add_competitor", {"name": name})
                return _success(result)

            except Exception as e:
                logger.error(f"add_competitor error: {e}")
                mcp_auth.log_call(
                    "add_competitor",
                    success=False,
                    error=str(e),
                )
                return _error(str(e))

        # 44. get_approval_stats (read) — v2.3 PR 3
        @mcp.tool(
            name="get_approval_stats",
            description=(
                "[SYSTEM] Approval pattern stats from the observation log. "
                "Shows approval / edit / rejection counts and rates grouped by "
                "content_type for the last N days. Useful for understanding "
                "Gianluigi's accuracy over time and where Eyal tends to edit "
                "vs accept unchanged."
            ),
        )
        async def get_approval_stats(days: int = 30) -> dict:
            try:
                from services.supabase_client import supabase_client as _sc
                from datetime import datetime, timedelta, timezone

                cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()

                rows = (
                    _sc.client.table("approval_observations")
                    .select("content_type, action, edit_distance_pct")
                    .gte("created_at", cutoff)
                    .limit(10000)
                    .execute()
                    .data
                    or []
                )

                # Group by (content_type, action)
                by_type: dict[str, dict] = {}
                for r in rows:
                    ct = r.get("content_type", "unknown")
                    act = r.get("action", "unknown")
                    bucket = by_type.setdefault(ct, {
                        "approved": 0,
                        "edited": 0,
                        "rejected": 0,
                        "avg_edit_distance": None,
                        "_edit_distances": [],
                    })
                    if act in bucket:
                        bucket[act] += 1
                    if act == "edited" and r.get("edit_distance_pct") is not None:
                        bucket["_edit_distances"].append(r["edit_distance_pct"])

                # Compute rates + avg edit distance
                summary = []
                for ct, b in sorted(by_type.items()):
                    total = b["approved"] + b["edited"] + b["rejected"]
                    eds = b.pop("_edit_distances")
                    avg_ed = round(sum(eds) / len(eds), 3) if eds else None
                    b["avg_edit_distance"] = avg_ed
                    b["total"] = total
                    b["approve_rate"] = round(b["approved"] / total, 3) if total else 0.0
                    b["edit_rate"] = round(b["edited"] / total, 3) if total else 0.0
                    b["reject_rate"] = round(b["rejected"] / total, 3) if total else 0.0
                    summary.append({"content_type": ct, **b})

                mcp_auth.log_call("get_approval_stats", {"days": days})
                return _success({
                    "days": days,
                    "total_observations": len(rows),
                    "by_content_type": summary,
                })

            except Exception as e:
                logger.error(f"get_approval_stats error: {e}")
                mcp_auth.log_call(
                    "get_approval_stats",
                    success=False,
                    error=str(e),
                )
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
        app = self._mcp.streamable_http_app()
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

        logger.info(f"MCP server (Streamable HTTP) listening on port {port}")
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
