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

            # Count decisions + tasks for this meeting
            try:
                decisions = supabase_client.list_decisions(meeting_id=mid)
                all_tasks = supabase_client.get_tasks(status=None)
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
    orphan child data. After Tier 1's cascading reject this should always
    return 0. If it ever returns >0, the cleanup script needs to run.
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
