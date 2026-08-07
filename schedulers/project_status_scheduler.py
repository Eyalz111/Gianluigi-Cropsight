"""
Project Status scheduler — refreshes the weekly per-area status workbook ahead
of the Paolo + Nechama project review.

Follows the weekly_pulse_scheduler pattern: a day/hour WINDOW loop rather than
sleep-until-next-Monday, so a mid-week Cloud Run restart cannot lose the run,
plus a restart-safe fire-once key so a restart INSIDE the window cannot refresh
twice. The fire-once set is rebuilt from audit_log on boot.

Unlike the pulse this sends nothing to the team — it rewrites a Google Sheet and
DMs Eyal the link. Refreshing twice would be harmless (each tab is cleared and
rewritten), but the fire-once keeps the DM from repeating.

Gated by PROJECT_STATUS_ENABLED.
"""

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from config.settings import settings
from services.supabase_client import supabase_client
from services.orchestrator.spine import comms_spine

_ISRAEL_TZ = ZoneInfo("Asia/Jerusalem")

logger = logging.getLogger(__name__)


class ProjectStatusScheduler:
    """Refreshes the Projects Status workbook once per ISO week."""

    def __init__(self, check_interval: int | None = None):
        self.check_interval = check_interval or getattr(
            settings, "PROJECT_STATUS_CHECK_INTERVAL", 1800
        )
        self._running = False
        self._sent_weeks: set[str] = set()

    @staticmethod
    def _week_key(now: datetime) -> str:
        iso = now.isocalendar()
        return f"project_status:{iso[0]}-W{iso[1]:02d}"

    async def start(self) -> None:
        if self._running:
            logger.warning("Project status scheduler already running")
            return
        self._running = True
        logger.info(
            f"Starting project status scheduler (interval {self.check_interval}s, "
            f"day {settings.PROJECT_STATUS_DAY}, hour {settings.PROJECT_STATUS_HOUR}, "
            f"window {settings.PROJECT_STATUS_WINDOW_HOURS}h)"
        )
        try:
            n = await self.reconstruct_sent_weeks()
            logger.info(f"Project status: reconstructed {n} sent-week(s) from audit_log")
        except Exception as e:
            logger.error(f"Project status reconstruct failed: {e}")

        while self._running:
            try:
                await self._check_and_refresh()
                try:
                    supabase_client.upsert_scheduler_heartbeat("project_status")
                except Exception:
                    pass
            except Exception as e:
                logger.error(f"Error in project status scheduler: {e}")
                try:
                    from core.health_monitor import check_and_alert
                    await check_and_alert("project_status_scheduler", e)
                    supabase_client.upsert_scheduler_heartbeat(
                        "project_status", status="error", details={"error": str(e)}
                    )
                except Exception:
                    pass
            await asyncio.sleep(self.check_interval)

    def stop(self) -> None:
        self._running = False
        logger.info("Project status scheduler stopped")

    async def reconstruct_sent_weeks(self) -> int:
        """Rebuild the fire-once set from recent audit rows (bounded ~5 weeks)."""
        cutoff = datetime.now(timezone.utc) - timedelta(weeks=5)
        try:
            rows = supabase_client.get_audit_log(action="project_status_refreshed", limit=20) or []
        except Exception as e:
            logger.warning(f"project status reconstruct query failed: {e}")
            rows = []
        for r in rows:
            try:
                dt = datetime.fromisoformat(str(r.get("created_at")).replace("Z", "+00:00"))
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                if dt < cutoff:
                    continue
            except (ValueError, TypeError):
                pass
            key = (r.get("details") or {}).get("week_key")
            if key:
                self._sent_weeks.add(key)
        return len(self._sent_weeks)

    async def _check_and_refresh(self) -> bool:
        now = datetime.now(_ISRAEL_TZ)
        if now.weekday() != settings.PROJECT_STATUS_DAY:
            return False
        hour, window = settings.PROJECT_STATUS_HOUR, settings.PROJECT_STATUS_WINDOW_HOURS
        if not (hour <= now.hour < hour + window):
            return False
        key = self._week_key(now)
        if key in self._sent_weeks:
            return False

        result = await self.refresh(notify=True)
        if result.get("error"):
            # Do NOT mark the week done — a failed refresh should retry on the
            # next tick inside the window rather than silently skip the review.
            return False

        self._sent_weeks.add(key)
        try:
            supabase_client.log_action(
                action="project_status_refreshed",
                details={"week_key": key, "rows": result.get("rows", 0),
                         "tabs": len(result.get("tabs", []))},
                triggered_by="auto",
            )
        except Exception as e:
            logger.warning(f"project status audit write failed: {e}")
        return True

    async def refresh(self, notify: bool = False) -> dict:
        """Rebuild the workbook (v1). Also the entry point for a manual/MCP trigger.

        Under PROJECT_STATUS_V2_LAYOUT this does NOT touch the sheet. v1's
        refresh CLEARS every tab and rewrites it, which is precisely what v2
        exists to stop: from cutover the file is Nechama's working document, and
        a Monday 07:00 rebuild would delete a week of her edits. Until the
        reconcile engine lands (P3) the weekly slot is review-prep only — the
        link and a count, no writes.
        """
        if getattr(settings, "PROJECT_STATUS_V2_LAYOUT", False):
            return await self._review_prep(notify)

        from processors.project_status import build_status_pack, title_block
        from services.project_status_sheet import write_project_status

        pack = build_status_pack()
        blocks = {area: title_block(area) for area in pack}
        result = await write_project_status(pack, blocks)

        if result.get("error"):
            logger.error(f"[project-status] refresh failed: {result['error']}")
            return result

        logger.info(
            f"[project-status] refreshed {result['rows']} item(s) across "
            f"{len(result['tabs'])} tab(s)"
        )
        if notify and getattr(settings, 'PROJECT_STATUS_NOTIFY_ENABLED', True):
            counts = " · ".join(f"{a}: {len(r)}" for a, r in pack.items() if r)
            try:
                await comms_spine.send_to_eyal(
                    f"<b>Projects Status refreshed</b> — {result['rows']} open item(s)\n"
                    f"{counts}\n\n{result.get('url', '')}",
                    parse_mode="HTML",
                )
            except Exception as e:
                logger.warning(f"project status notify failed: {e}")
        return result

    async def _review_prep(self, notify: bool) -> dict:
        """v2 weekly slot: summarise the file, never write to it."""
        from services.supabase_client import supabase_client

        sid = settings.PROJECT_STATUS_SHEET_ID
        url = f"https://docs.google.com/spreadsheets/d/{sid}/edit" if sid else ""
        grouped = supabase_client.get_open_tasks_by_project()
        projects = [p for p in supabase_client.get_canonical_projects(status=None)
                    if (p.get("status") or "active") != "retired"]
        actions = sum(len(v) for v in grouped.values())
        idle = [p["name"] for p in projects if not grouped.get(p["id"])]

        result = {"spreadsheet_id": sid, "url": url, "rows": actions,
                  "tabs": [], "projects": len(projects), "actions": actions,
                  "rebuilt": False, "error": None}
        logger.info(
            f"[project-status] v2 review prep — {len(projects)} project(s), "
            f"{actions} open action(s), {len(idle)} with nothing open "
            "(sheet not modified)")

        if notify and getattr(settings, "PROJECT_STATUS_NOTIFY_ENABLED", True):
            # Projects with nothing open are the useful signal in a review:
            # each is either finished or stalled, and both need a decision.
            tail = (f"\n\nNothing open on {len(idle)}: "
                    + ", ".join(sorted(idle)[:8])) if idle else ""
            try:
                await comms_spine.send_to_eyal(
                    f"<b>Projects Status — ready for review</b>\n"
                    f"{len(projects)} projects · {actions} open actions{tail}\n\n{url}",
                    parse_mode="HTML",
                )
            except Exception as e:
                logger.warning(f"project status notify failed: {e}")
        return result


project_status_scheduler = ProjectStatusScheduler()
