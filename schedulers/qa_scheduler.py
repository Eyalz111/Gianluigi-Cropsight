"""
QA Agent scheduler — Cross-cutting infrastructure X1.

Runs automated quality checks on the system:
1. Extraction quality: recent meetings have reasonable item counts
2. Distribution completeness: approved items were actually distributed
3. Scheduler health: heartbeats are recent, no stale schedulers
4. Data integrity: orphan records, missing FK references, embedding coverage

Runs weekly (Friday morning) and on-demand via MCP tool.

Usage:
    from schedulers.qa_scheduler import qa_scheduler, run_qa_check
    await qa_scheduler.start()        # Weekly schedule
    report = run_qa_check()            # On-demand
"""

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from config.settings import settings
from services.supabase_client import supabase_client

logger = logging.getLogger(__name__)

# Thresholds for quality checks
_MIN_ITEMS_PER_MEETING = 1  # At least 1 decision or task expected
_MAX_HEARTBEAT_AGE_HOURS = 48  # Schedulers should heartbeat within 48h
_DISTRIBUTION_WINDOW_DAYS = 7  # Check last 7 days of approvals


def run_qa_check() -> dict:
    """
    Run all quality checks and return a structured report.

    Returns:
        Dict with check results:
        {
            "timestamp": str,
            "checks": {
                "extraction_quality": {...},
                "distribution_completeness": {...},
                "scheduler_health": {...},
                "data_integrity": {...},
            },
            "issues": [str],   # Human-readable issue descriptions
            "score": str,      # "healthy" | "warning" | "critical"
        }
    """
    report = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "checks": {},
        "issues": [],
    }

    # Run each check independently
    report["checks"]["extraction_quality"] = _check_extraction_quality(report["issues"])
    report["checks"]["distribution_completeness"] = _check_distribution_completeness(report["issues"])
    report["checks"]["scheduler_health"] = _check_scheduler_health(report["issues"])
    report["checks"]["data_integrity"] = _check_data_integrity(report["issues"])
    report["checks"]["prompt_health"] = _check_prompt_health(report["issues"])
    report["checks"]["rejected_meetings"] = _check_rejected_meetings(report["issues"])
    report["checks"]["approved_with_pending_children"] = _check_approved_meetings_with_pending_children(report["issues"])
    report["checks"]["rls_coverage"] = _check_rls_coverage(report["issues"])
    report["checks"]["duplicate_tasks"] = _check_duplicate_tasks(report["issues"])
    report["checks"]["topic_state_staleness"] = _check_topic_state_staleness(report["issues"])

    # Overall score
    issue_count = len(report["issues"])
    if issue_count == 0:
        report["score"] = "healthy"
    elif issue_count <= 3:
        report["score"] = "warning"
    else:
        report["score"] = "critical"

    logger.info(
        f"QA check complete: {report['score']} "
        f"({issue_count} issues found)"
    )

    return report


def _check_extraction_quality(issues: list[str]) -> dict:
    """Check that recent meetings have reasonable extraction results."""
    result = {"meetings_checked": 0, "empty_extractions": 0, "low_extractions": 0}

    try:
        # Get meetings from last 14 days
        cutoff = datetime.now(timezone.utc) - timedelta(days=14)
        meetings = supabase_client.list_meetings(date_from=cutoff, limit=20)
        result["meetings_checked"] = len(meetings)

        for meeting in meetings:
            mid = meeting.get("id")
            if not mid:
                continue

            # Count decisions + tasks for this meeting.
            # Tier 3.1: orphan detection must see all rows regardless of
            # approval_status (a pending child whose parent is rejected is
            # an orphan; so is an approved child whose parent is missing).
            try:
                decisions = supabase_client.list_decisions(
                    meeting_id=mid, include_pending=True
                )
                all_tasks = supabase_client.get_tasks(
                    status=None, include_pending=True
                )
                tasks = [t for t in all_tasks if t.get("meeting_id") == mid]
                total_items = len(decisions) + len(tasks)

                if total_items == 0:
                    result["empty_extractions"] += 1
                    issues.append(
                        f"Empty extraction: '{meeting.get('title', '?')}' "
                        f"({str(meeting.get('date', ''))[:10]}) has 0 decisions and 0 tasks"
                    )
                elif total_items < _MIN_ITEMS_PER_MEETING:
                    result["low_extractions"] += 1
            except Exception as e:
                logger.debug(f"Could not check extraction for {mid}: {e}")

    except Exception as e:
        logger.warning(f"Extraction quality check failed: {e}")
        issues.append(f"Extraction quality check failed: {e}")

    return result


def _check_distribution_completeness(issues: list[str]) -> dict:
    """Check that approved items were actually distributed."""
    result = {"approvals_checked": 0, "undistributed": 0}

    try:
        # Get recent approved items
        approvals = supabase_client.get_pending_approvals_by_status("approved")
        cutoff = (datetime.now(timezone.utc) - timedelta(days=_DISTRIBUTION_WINDOW_DAYS)).isoformat()

        recent_approvals = [
            a for a in approvals
            if str(a.get("created_at", "")) >= cutoff
        ]
        result["approvals_checked"] = len(recent_approvals)

        # Check if they have distribution records in action_log
        for approval in recent_approvals:
            approval_id = approval.get("id")
            meeting_id = approval.get("meeting_id")
            content_type = approval.get("content_type", "meeting_summary")

            if not meeting_id:
                continue

            # Look for a distribution log entry
            try:
                logs = supabase_client.client.table("audit_log").select("id").eq(
                    "action", "distribution_sent"
                ).execute()
                # Simple check: if there are any distribution logs at all for this meeting
                # A more thorough check would match meeting_id in details JSON
                if not logs.data:
                    result["undistributed"] += 1
                    issues.append(
                        f"Approved content may not have been distributed "
                        f"(approval {approval_id}, type={content_type})"
                    )
            except Exception:
                pass

    except Exception as e:
        logger.warning(f"Distribution completeness check failed: {e}")

    return result


def _check_scheduler_health(issues: list[str]) -> dict:
    """Check that all schedulers have recent heartbeats."""
    result = {"schedulers_checked": 0, "stale": [], "missing": []}

    try:
        heartbeats = supabase_client.get_scheduler_heartbeats()
        result["schedulers_checked"] = len(heartbeats)

        now = datetime.now(timezone.utc)

        for hb in heartbeats:
            name = hb.get("scheduler_name", "?")
            last_beat = hb.get("last_heartbeat")

            if not last_beat:
                result["missing"].append(name)
                issues.append(f"Scheduler '{name}' has never sent a heartbeat")
                continue

            try:
                beat_time = datetime.fromisoformat(
                    str(last_beat).replace("Z", "+00:00")
                )
                age_hours = (now - beat_time).total_seconds() / 3600

                if age_hours > _MAX_HEARTBEAT_AGE_HOURS:
                    result["stale"].append(name)
                    issues.append(
                        f"Scheduler '{name}' heartbeat is {int(age_hours)}h old "
                        f"(threshold: {_MAX_HEARTBEAT_AGE_HOURS}h)"
                    )
            except (ValueError, TypeError):
                result["missing"].append(name)

    except Exception as e:
        logger.warning(f"Scheduler health check failed: {e}")

    return result


def _check_data_integrity(issues: list[str]) -> dict:
    """Check for orphan records and data quality issues."""
    result = {
        "tasks_without_meeting": 0,
        "decisions_without_meeting": 0,
        "meetings_without_embeddings": 0,
    }

    try:
        # Tasks pointing to nonexistent meetings
        try:
            tasks = supabase_client.get_tasks(status=None, limit=200)
            meetings_cache = set()
            for t in tasks:
                mid = t.get("meeting_id")
                if mid and mid not in meetings_cache:
                    meeting = supabase_client.get_meeting(mid)
                    if meeting:
                        meetings_cache.add(mid)
                    else:
                        result["tasks_without_meeting"] += 1

            if result["tasks_without_meeting"] > 0:
                issues.append(
                    f"{result['tasks_without_meeting']} tasks reference "
                    f"nonexistent meetings (orphan records)"
                )
        except Exception as e:
            logger.debug(f"Task orphan check failed: {e}")

        # Recent meetings without embeddings
        try:
            cutoff = datetime.now(timezone.utc) - timedelta(days=14)
            meetings = supabase_client.list_meetings(date_from=cutoff, limit=20)

            for m in meetings:
                mid = m.get("id")
                if not mid:
                    continue
                try:
                    embeddings = supabase_client.client.table("embeddings").select(
                        "id", count="exact"
                    ).eq("source_id", mid).limit(1).execute()
                    if not embeddings.data:
                        result["meetings_without_embeddings"] += 1
                except Exception:
                    pass

            if result["meetings_without_embeddings"] > 0:
                issues.append(
                    f"{result['meetings_without_embeddings']} recent meetings "
                    f"have no embeddings (search won't find them)"
                )
        except Exception as e:
            logger.debug(f"Embedding coverage check failed: {e}")

    except Exception as e:
        logger.warning(f"Data integrity check failed: {e}")

    return result


def _check_rejected_meetings(issues: list[str]) -> dict:
    """
    Defense in depth (T2.4): surface any rejected meetings that still have
    orphan child data.

    POST-T1.9 SEMANTICS: Rejected meetings are tombstones — the `meetings`
    row is kept with approval_status='rejected' while children are
    cascade-deleted at reject time. A tombstone with ZERO children is the
    EXPECTED state and must not trigger an alert — this check only flags
    tombstones that still have child rows.

    A non-zero count here means one of:
    - Pre-T1.9 data that pre-dates the cascade fix
    - A bug in delete_meeting_cascade(keep_tombstone=True)
    - Manual DB edit that re-created children

    All three warrant running scripts/cleanup_rejected_meetings.py --apply,
    which now preserves tombstones (see T3.3 fix) so the source-file
    re-processing guard stays intact.
    """
    result = {
        "rejected_meetings": 0,
        "rejected_with_orphans": 0,
        "orphan_tasks": 0,
        "orphan_decisions": 0,
        "orphan_embeddings": 0,
    }

    try:
        rejected = supabase_client.list_meetings(
            approval_status="rejected", limit=100
        )
        result["rejected_meetings"] = len(rejected)

        for m in rejected:
            mid = m.get("id")
            if not mid:
                continue

            has_orphans = False

            try:
                t = (
                    supabase_client.client.table("tasks")
                    .select("id", count="exact")
                    .eq("meeting_id", mid)
                    .execute()
                )
                if t.count:
                    result["orphan_tasks"] += t.count
                    has_orphans = True
            except Exception:
                pass

            try:
                d = (
                    supabase_client.client.table("decisions")
                    .select("id", count="exact")
                    .eq("meeting_id", mid)
                    .execute()
                )
                if d.count:
                    result["orphan_decisions"] += d.count
                    has_orphans = True
            except Exception:
                pass

            try:
                e = (
                    supabase_client.client.table("embeddings")
                    .select("id", count="exact")
                    .eq("source_id", mid)
                    .execute()
                )
                if e.count:
                    result["orphan_embeddings"] += e.count
                    has_orphans = True
            except Exception:
                pass

            if has_orphans:
                result["rejected_with_orphans"] += 1

        if result["rejected_with_orphans"] > 0:
            issues.append(
                f"{result['rejected_with_orphans']} rejected meetings have orphan data "
                f"({result['orphan_tasks']} tasks, {result['orphan_decisions']} decisions, "
                f"{result['orphan_embeddings']} embeddings) "
                f"— run scripts/cleanup_rejected_meetings.py --apply"
            )
    except Exception as e:
        logger.warning(f"Rejected meetings check failed: {e}")

    return result


def _check_approved_meetings_with_pending_children(issues: list[str]) -> dict:
    """
    Tier 3.1 safety net for the _promote_children_to_approved failure mode.

    When a meeting is approved, guardrails/approval_flow._promote_children_to_approved
    flips all child rows (tasks/decisions/open_questions/follow_up_meetings)
    from approval_status='pending' to 'approved'. If that call fails
    (transient DB error, partial success, manual SQL edit, future bug),
    the meeting row shows 'approved' but the children stay 'pending' — so
    default reads (MCP tools, morning brief, weekly review, team digests)
    see nothing for that meeting.

    This check runs daily and flags any such inconsistency so Eyal sees it
    in the morning brief and can run the one-line SQL fix:
        UPDATE <table> SET approval_status='approved' WHERE meeting_id='<id>';

    Only looks at meetings approved in the last 30 days — older data
    pre-dates T3.1 and backfilled to 'approved', so it can't be inconsistent.
    """
    result: dict = {
        "meetings_checked": 0,
        "inconsistent_meetings": 0,
        "details": [],
    }
    try:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
        approved = supabase_client.list_meetings(
            approval_status="approved",
            date_from=cutoff,
            limit=500,
        )
        result["meetings_checked"] = len(approved)

        for m in approved:
            mid = m.get("id")
            if not mid:
                continue

            pending_counts: dict[str, int] = {}
            for table, fk in [
                ("tasks", "meeting_id"),
                ("decisions", "meeting_id"),
                ("open_questions", "meeting_id"),
                ("follow_up_meetings", "source_meeting_id"),
            ]:
                try:
                    r = (
                        supabase_client.client.table(table)
                        .select("id", count="exact")
                        .eq(fk, mid)
                        .eq("approval_status", "pending")
                        .execute()
                    )
                    if r.count and r.count > 0:
                        pending_counts[table] = r.count
                except Exception as e:
                    logger.debug(f"Safety-net scan for {table} skipped: {e}")

            if pending_counts:
                result["inconsistent_meetings"] += 1
                title = (m.get("title") or "untitled")[:50]
                result["details"].append({
                    "meeting_id": mid,
                    "title": title,
                    "pending_counts": pending_counts,
                })
                parts = ", ".join(f"{v} {k}" for k, v in pending_counts.items())
                issues.append(
                    f"Approved meeting '{title}' ({mid[:8]}) has pending children: "
                    f"{parts} — run: UPDATE <table> SET approval_status='approved' "
                    f"WHERE meeting_id='{mid}';"
                )
    except Exception as e:
        logger.error(f"_check_approved_meetings_with_pending_children failed: {e}")
        issues.append(f"QA safety-net scan (approved_with_pending_children) failed: {e}")

    return result


def _check_duplicate_tasks(issues: list[str]) -> dict:
    """
    Daily scan for potential duplicate tasks.

    Wraps the fuzzy detector in processors.sheets_sync so the morning brief
    always carries an up-to-date list even when no /sync has been run.
    Added 2026-04-11 after duplicates were only surfaced through the sync
    path, which had been silently broken for days.

    Each pair surfaced as an issue includes both task IDs so Eyal can act
    on them directly (e.g. via an MCP update_task call) without hunting
    through the sheet.
    """
    result = {"pairs_found": 0, "sample": []}

    try:
        from processors.sheets_sync import _detect_duplicate_tasks

        tasks = supabase_client.get_tasks(limit=10000)
        pairs = _detect_duplicate_tasks(tasks)
        result["pairs_found"] = len(pairs)
        # Small snapshot in the report so the JSON stays self-contained
        for dup in pairs[:5]:
            result["sample"].append({
                "a_id": dup["task_a"]["id"],
                "a_title": dup["task_a"]["title"][:60],
                "b_id": dup["task_b"]["id"],
                "b_title": dup["task_b"]["title"][:60],
            })

        if pairs:
            # Keep the issue line short — full detail is in result["sample"].
            # ASCII-only to avoid issues on any log backend that defaults to
            # cp1252 or similar (the '<->' was unicode in an earlier draft).
            issues.append(
                f"Potential duplicate tasks: {len(pairs)} open-task pair(s) flagged. "
                f"Example: '{pairs[0]['task_a']['title'][:50]}' <-> "
                f"'{pairs[0]['task_b']['title'][:50]}'. "
                f"Run /sync for the full list."
            )

    except Exception as e:
        logger.warning(f"Duplicate task check failed: {e}")
        issues.append(f"Duplicate task check failed: {e}")

    return result


def _check_rls_coverage(issues: list[str]) -> dict:
    """
    Defense-in-depth security check: verify Row Level Security is enabled
    on every table in the public schema.

    Calls the Postgres function public.get_table_rls_status() which returns
    (table_name, rls_enabled) for each table. If any table has
    rls_enabled=false, that table is publicly accessible to anyone with the
    project URL + anon key. Surface as a CRITICAL issue with the table list.

    If the helper function doesn't exist yet (migrate_rls_security_v2.sql
    hasn't been run), the check is skipped with a warning in the result —
    not raised as an issue, since that would cause noise on fresh deploys.
    """
    result = {
        "function_available": False,
        "tables_total": 0,
        "tables_without_rls": [],
    }

    try:
        rpc_result = supabase_client.client.rpc("get_table_rls_status").execute()
    except Exception as e:
        err = str(e).lower()
        if "could not find" in err or "function" in err or "does not exist" in err:
            result["note"] = (
                "Helper function public.get_table_rls_status() not found. "
                "Run scripts/migrate_rls_security_v2.sql on Supabase to enable "
                "automated RLS coverage checks."
            )
            logger.info("RLS coverage check skipped: helper function not installed")
            return result
        logger.warning(f"RLS coverage check failed: {e}")
        result["error"] = str(e)
        return result

    result["function_available"] = True
    rows = rpc_result.data or []
    result["tables_total"] = len(rows)

    missing = [r.get("table_name", "?") for r in rows if not r.get("rls_enabled")]
    result["tables_without_rls"] = missing

    if missing:
        issues.append(
            f"SECURITY: {len(missing)} public table(s) missing Row Level Security "
            f"(publicly accessible): {', '.join(missing)}. "
            f"Fix: ALTER TABLE <name> ENABLE ROW LEVEL SECURITY for each, "
            f"then update scripts/migrate_rls_security_v2.sql."
        )

    return result


def _check_topic_state_staleness(issues: list[str]) -> dict:
    """
    v2.3 PR 4 — metadata-only daily sweep.

    For every active topic_thread with last_updated more than 30 days old,
    flip its state_json.current_status to 'stale'. No LLM call — this is a
    cheap maintenance pass that keeps the morning brief's "Needs attention"
    surfacing accurate without blocking on Haiku.

    A topic becomes 'stale' when it hasn't been mentioned in a new meeting
    for 30 days. Re-activation happens naturally on the next update_topic_state
    call (which will overwrite the status based on the new meeting's content).

    Does NOT raise issues — staleness is expected churn, not a defect. Returns
    the count of threads flipped so the daily report shows the sweep ran.
    """
    from datetime import datetime, timedelta, timezone

    result = {"threads_scanned": 0, "threads_marked_stale": 0}
    try:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()

        rows = (
            supabase_client.client.table("topic_threads")
            .select("id, state_json, last_updated")
            .eq("status", "active")
            .not_.is_("state_json", "null")
            .lt("last_updated", cutoff)
            .limit(500)
            .execute()
        )
        result["threads_scanned"] = len(rows.data or [])

        for row in (rows.data or []):
            state = row.get("state_json") or {}
            if state.get("current_status") == "stale":
                continue  # already marked
            state["current_status"] = "stale"
            try:
                supabase_client.client.table("topic_threads").update({
                    "state_json": state,
                }).eq("id", row["id"]).execute()
                result["threads_marked_stale"] += 1
            except Exception as e:
                logger.warning(f"[topic_state] stale flip failed for {row.get('id')}: {e}")

        if result["threads_marked_stale"]:
            logger.info(
                f"[topic_state] staleness sweep: {result['threads_marked_stale']} "
                f"threads flipped to stale (of {result['threads_scanned']} scanned)"
            )

    except Exception as e:
        logger.warning(f"_check_topic_state_staleness failed: {e}")
        result["error"] = str(e)

    return result


def _check_prompt_health(issues: list[str]) -> dict:
    """Check that YAML prompt files are loadable and up-to-date."""
    result = {"prompts_loaded": 0, "load_errors": 0, "files_modified": 0}

    try:
        from config.prompt_registry import prompt_registry

        health = prompt_registry.health_check()
        result["prompts_loaded"] = health["prompts_loaded"]
        result["load_errors"] = health["load_errors"]
        result["files_modified"] = health["files_modified_since_startup"]

        if health["load_errors"] > 0:
            issues.append(
                f"Prompt health: {health['load_errors']} YAML load error(s) — "
                f"using Python fallbacks"
            )

        if health["files_modified_since_startup"] > 0:
            issues.append(
                f"Prompt health: {health['files_modified_since_startup']} prompt file(s) "
                f"modified since startup — consider reloading"
            )

    except Exception as e:
        logger.warning(f"Prompt health check failed: {e}")

    return result


def format_qa_report(report: dict) -> str:
    """
    Format QA report as readable text for Telegram or MCP display.

    Args:
        report: Output of run_qa_check().

    Returns:
        Formatted text report.
    """
    score = report.get("score", "unknown")
    score_emoji = {"healthy": "OK", "warning": "WARN", "critical": "ALERT"}.get(score, "?")
    timestamp = str(report.get("timestamp", ""))[:19]

    lines = [
        f"QA Report [{score_emoji}] — {timestamp}",
        "",
    ]

    checks = report.get("checks", {})

    # Extraction quality
    eq = checks.get("extraction_quality", {})
    lines.append(f"Extraction: {eq.get('meetings_checked', 0)} meetings checked")
    if eq.get("empty_extractions"):
        lines.append(f"  {eq['empty_extractions']} empty extractions")
    if eq.get("low_extractions"):
        lines.append(f"  {eq['low_extractions']} low-quality extractions")

    # Distribution
    dc = checks.get("distribution_completeness", {})
    lines.append(f"Distribution: {dc.get('approvals_checked', 0)} approvals checked")
    if dc.get("undistributed"):
        lines.append(f"  {dc['undistributed']} possibly undistributed")

    # Scheduler health
    sh = checks.get("scheduler_health", {})
    stale = sh.get("stale", [])
    missing = sh.get("missing", [])
    lines.append(f"Schedulers: {sh.get('schedulers_checked', 0)} checked")
    if stale:
        lines.append(f"  Stale: {', '.join(stale)}")
    if missing:
        lines.append(f"  Missing heartbeat: {', '.join(missing)}")

    # Data integrity
    di = checks.get("data_integrity", {})
    lines.append(f"Data integrity:")
    if di.get("tasks_without_meeting"):
        lines.append(f"  {di['tasks_without_meeting']} orphan tasks")
    if di.get("meetings_without_embeddings"):
        lines.append(f"  {di['meetings_without_embeddings']} meetings without embeddings")
    if not di.get("tasks_without_meeting") and not di.get("meetings_without_embeddings"):
        lines.append("  All clean")

    # Tier 3.1 safety net: approved meetings with still-pending children
    apc = checks.get("approved_with_pending_children", {})
    checked = apc.get("meetings_checked", 0)
    inconsistent = apc.get("inconsistent_meetings", 0)
    if checked:
        if inconsistent:
            lines.append(
                f"Approval consistency: {inconsistent}/{checked} approved "
                f"meetings have pending children (promote failure)"
            )
        else:
            lines.append(f"Approval consistency: {checked} meetings checked, all clean")

    # Issues summary
    issues = report.get("issues", [])
    if issues:
        lines.append("")
        lines.append(f"Issues ({len(issues)}):")
        for issue in issues[:10]:
            lines.append(f"  - {issue}")

    return "\n".join(lines)


class QAScheduler:
    """Daily QA check scheduler — runs before morning brief.

    Starts daily, can be switched to weekly once the system is stable.
    """

    QA_HOUR = 6  # 6:00 IST (before morning brief at 7:00)

    def __init__(self):
        self._running = False
        self._last_report: dict | None = None

    @property
    def last_report(self) -> dict | None:
        """Most recent QA report — used by morning brief for inline summary."""
        return self._last_report

    async def start(self) -> None:
        """Start the daily QA scheduler."""
        self._running = True
        logger.info("QA scheduler started (daily 06:00 IST)")

        while self._running:
            try:
                await self._sleep_until_next_run()
                if not self._running:
                    break

                report = run_qa_check()
                self._last_report = report
                formatted = format_qa_report(report)

                # Only send standalone Telegram message if there are issues
                if report.get("issues"):
                    try:
                        from services.telegram_bot import telegram_bot
                        await telegram_bot.send_to_eyal(formatted)
                        logger.info("QA report sent to Eyal (issues found)")
                    except Exception as e:
                        logger.error(f"Failed to send QA report: {e}")

                # Log the report
                supabase_client.log_action(
                    action="qa_check_completed",
                    details={
                        "score": report.get("score"),
                        "issue_count": len(report.get("issues", [])),
                    },
                    triggered_by="auto",
                )

            except Exception as e:
                logger.error(f"QA scheduler cycle failed: {e}")
                await asyncio.sleep(3600)  # Wait 1h on error

    async def _sleep_until_next_run(self) -> None:
        """Sleep until next QA_HOUR IST. Skips Saturday."""
        import pytz

        ist = pytz.timezone("Asia/Jerusalem")
        now = datetime.now(ist)

        # Next run: tomorrow at QA_HOUR (or today if before QA_HOUR)
        if now.hour < self.QA_HOUR:
            next_run = now.replace(hour=self.QA_HOUR, minute=0, second=0, microsecond=0)
        else:
            next_run = (now + timedelta(days=1)).replace(
                hour=self.QA_HOUR, minute=0, second=0, microsecond=0
            )

        # Skip Saturday (weekday 5)
        if next_run.weekday() == 5:
            next_run += timedelta(days=1)

        sleep_seconds = (next_run - now).total_seconds()
        if sleep_seconds > 0:
            logger.debug(f"QA scheduler sleeping {sleep_seconds/3600:.1f}h until {next_run}")
            await asyncio.sleep(sleep_seconds)

    def stop(self) -> None:
        """Stop the scheduler."""
        self._running = False
        logger.info("QA scheduler stopped")


qa_scheduler = QAScheduler()
