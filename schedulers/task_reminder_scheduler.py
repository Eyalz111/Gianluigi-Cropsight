"""
Task reminder scheduler.

This module sends reminders for tasks with approaching or past deadlines.
It runs on a schedule to notify team members via Telegram about:
- Tasks due today
- Tasks due tomorrow
- Overdue tasks

Reminder rules:
- Daily check at configured time (default: 9 AM)
- Due today: Notify assignee directly
- Overdue: Notify assignee + Eyal
- Weekly summary: Sent to Eyal every Monday

Usage:
    from schedulers.task_reminder_scheduler import TaskReminderScheduler

    scheduler = TaskReminderScheduler()
    await scheduler.start()  # Runs on daily schedule
"""

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from config.settings import settings

_ISRAEL_TZ = ZoneInfo("Asia/Jerusalem")
from config.team import CROPSIGHT_TEAM_EMAILS, TEAM_TELEGRAM_IDS
from services.google_sheets import sheets_service
from services.supabase_client import supabase_client
from services.telegram_bot import telegram_bot

logger = logging.getLogger(__name__)


# How many days before deadline to start warning
DAYS_BEFORE_WARNING = 2


class TaskReminderScheduler:
    """
    Schedules and sends task deadline reminders.

    Overdue task reminders include inline action buttons:
    [Done] [+1 Week] [Discuss]
    """

    def __init__(
        self,
        check_interval: int | None = None,
        days_before_warning: int = DAYS_BEFORE_WARNING
    ):
        """
        Initialize the task reminder scheduler.

        Args:
            check_interval: Seconds between checks.
            days_before_warning: Days before deadline to start warning.
        """
        self.check_interval = check_interval or settings.TASK_REMINDER_CHECK_INTERVAL
        self.days_before_warning = days_before_warning
        self._running = False
        # Track reminders sent today to avoid duplicates
        self._reminders_sent_today: set[str] = set()
        self._last_reminder_date: str = ""
        # Phase 3: inline task reply support
        self._task_action_counter = 0
        # short_id → task info dict for button callbacks
        self.task_action_map: dict[str, dict] = {}
        # message_id → short_id for free-text reply detection
        self.message_task_map: dict[int, str] = {}

    async def start(self) -> None:
        """
        Start the task reminder scheduler loop.

        This runs indefinitely until stop() is called.
        """
        if self._running:
            logger.warning("Task reminder scheduler already running")
            return

        self._running = True
        logger.info(
            f"Starting task reminder scheduler (interval: {self.check_interval}s)"
        )

        # Wait 5 minutes before first check to avoid spamming on every restart
        await asyncio.sleep(300)

        while self._running:
            try:
                # Reset daily tracker if it's a new day
                today = datetime.now(_ISRAEL_TZ).strftime("%Y-%m-%d")
                if today != self._last_reminder_date:
                    self._reminders_sent_today.clear()
                    self._last_reminder_date = today

                await self._check_and_send_reminders()
                try:
                    supabase_client.upsert_scheduler_heartbeat("task_reminder")
                except Exception:
                    pass  # Never let monitoring kill the thing being monitored
            except Exception as e:
                logger.error(f"Error in task reminder scheduler: {e}")
                supabase_client.log_action(
                    action="reminder_scheduler_error",
                    details={"error": str(e)},
                    triggered_by="auto",
                )
                from core.health_monitor import check_and_alert
                await check_and_alert("task_reminder_scheduler", e)
                try:
                    supabase_client.upsert_scheduler_heartbeat("task_reminder", status="error", details={"error": str(e)})
                except Exception:
                    pass

            # Wait for next check
            await asyncio.sleep(self.check_interval)

    def stop(self) -> None:
        """Stop the scheduler."""
        self._running = False
        logger.info("Task reminder scheduler stopped")

    async def _check_and_send_reminders(self) -> dict:
        """
        Check all tasks and send appropriate reminders.

        Returns:
            Summary of reminders sent.
        """
        logger.debug("Checking for task reminders...")

        # Get all tasks
        tasks = await sheets_service.get_all_tasks()

        if not tasks:
            logger.debug("No tasks found")
            return {"reminders_sent": 0}

        today = datetime.now(_ISRAEL_TZ).date()
        lookback_cutoff = today - timedelta(days=settings.ALERT_LOOKBACK_DAYS)
        summary = {
            "overdue": [],
            "due_today": [],
            "due_soon": [],
            "reminders_sent": 0,
        }

        for task in tasks:
            # Skip completed tasks
            if task.get("status") == "done":
                continue

            # Skip tasks created before the lookback window
            created_str = task.get("created_date", "")
            if created_str:
                try:
                    created_date = datetime.strptime(created_str, "%Y-%m-%d").date()
                    if created_date < lookback_cutoff:
                        continue
                except ValueError:
                    pass

            deadline_str = task.get("deadline", "")
            if not deadline_str:
                continue

            try:
                deadline = datetime.strptime(deadline_str, "%Y-%m-%d").date()
            except ValueError:
                continue

            days_until = (deadline - today).days
            task_id = f"{task.get('task', '')}:{task.get('assignee', '')}"

            # Skip if we already sent a reminder for this task today
            if task_id in self._reminders_sent_today:
                continue

            if days_until < 0:
                # Overdue
                summary["overdue"].append(task)
                await self._send_overdue_reminder(task, abs(days_until))
                self._reminders_sent_today.add(task_id)
                summary["reminders_sent"] += 1

                # Update status to overdue in sheet
                row = task.get("row_number")
                if row:
                    await sheets_service.update_task_status(
                        row_number=row,
                        status="overdue",
                        updated_date=today.strftime("%Y-%m-%d")
                    )

            elif days_until == 0:
                # Due today
                summary["due_today"].append(task)
                await self._send_due_today_reminder(task)
                self._reminders_sent_today.add(task_id)
                summary["reminders_sent"] += 1

            elif days_until <= self.days_before_warning:
                # Due soon
                summary["due_soon"].append(task)
                await self._send_due_soon_reminder(task, days_until)
                self._reminders_sent_today.add(task_id)
                summary["reminders_sent"] += 1

        logger.info(
            f"Task reminder check complete: "
            f"{len(summary['overdue'])} overdue, "
            f"{len(summary['due_today'])} due today, "
            f"{len(summary['due_soon'])} due soon"
        )

        # Send daily summary to Eyal if there are any items
        if summary["overdue"] or summary["due_today"]:
            await self._send_daily_summary_to_eyal(summary)

        return summary

    def _next_short_id(self) -> str:
        """Generate a short incrementing ID for callback_data (Telegram 64-byte limit)."""
        self._task_action_counter += 1
        return f"t{self._task_action_counter}"

    def _resolve_db_task_id(self, task_text: str, assignee: str) -> str | None:
        """
        Resolve the Supabase task_id for a task by matching title + assignee.

        Called once per reminder send, so the button callback handler can later
        do a direct id-based lookup instead of brittle title matching.

        Returns None if no match is found — caller should fall back to title match.
        """
        try:
            # Query all active statuses — pending, in_progress, overdue.
            # Reminder targets ANY active task, not just pending ones.
            db_tasks = supabase_client.get_tasks(assignee=assignee, status=None, limit=500)
            target = (task_text or "").strip().lower()
            for dt in db_tasks:
                if dt.get("status") in ("done", "cancelled"):
                    continue
                if dt.get("title", "").strip().lower() == target:
                    return dt["id"]
            logger.warning(
                f"Could not resolve DB task_id for reminder: '{task_text[:60]}' ({assignee})"
            )
        except Exception as e:
            logger.warning(f"DB task_id resolution failed: {e}")
        return None

    async def _send_overdue_reminder(
        self,
        task: dict,
        days_overdue: int
    ) -> bool:
        """
        Send reminder for an overdue task with inline action buttons.

        Notifies Eyal with [Done] [+1 Week] [Discuss] buttons.

        Args:
            task: Task dict from sheet.
            days_overdue: Number of days past deadline.

        Returns:
            True if sent successfully.
        """
        from telegram import InlineKeyboardButton, InlineKeyboardMarkup

        assignee = task.get("assignee", "Unknown")
        task_desc = task.get("task", "Unknown task")
        priority = task.get("priority", "M")
        source = task.get("source_meeting", "")

        # Priority emoji
        priority_emoji = {"H": "!!!", "M": "!!", "L": "!"}.get(priority, "!!")

        message = (
            f"*OVERDUE TASK* {priority_emoji}\n\n"
            f"*Task:* {task_desc}\n"
            f"*Assignee:* {assignee}\n"
            f"*Days Overdue:* {days_overdue}\n"
        )

        if source:
            message += f"*From Meeting:* {source}\n"

        message += f"\nTap a button or reply with an update:"

        # Resolve DB task_id so button handlers can update by id directly,
        # surviving instance restarts (no in-memory map dependency).
        db_task_id = self._resolve_db_task_id(task_desc, assignee)

        # Generate short ID and store mapping (legacy fallback when task_id is None,
        # plus extra metadata cache for the active instance)
        short_id = self._next_short_id()
        self.task_action_map[short_id] = {
            "task_id": db_task_id,
            "task_text": task_desc,
            "assignee": assignee,
            "row_number": task.get("row_number"),
            "deadline": task.get("deadline", ""),
        }
        # Also cache by task_id so the handler can find metadata after restart
        # (callback_data carries the UUID; metadata cache is best-effort)
        if db_task_id:
            self.task_action_map[db_task_id] = self.task_action_map[short_id]

        # Encode task_id in callback_data when available (44 bytes — UUID is
        # 36 chars + 8-char prefix). Falls back to short_id only when DB
        # resolution failed at send time.
        callback_key = db_task_id if db_task_id else short_id

        # Build inline keyboard
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("Done", callback_data=f"taskdone:{callback_key}"),
            InlineKeyboardButton("+1 Week", callback_data=f"taskdelay:{callback_key}"),
            InlineKeyboardButton("Discuss", callback_data=f"taskdiscuss:{callback_key}"),
        ]])

        # Send to assignee (no buttons — buttons only for Eyal)
        assignee_telegram = self._get_telegram_id(assignee)
        if assignee_telegram:
            await telegram_bot.send_message(
                chat_id=assignee_telegram,
                text=message
            )

        # Send to Eyal WITH inline buttons
        try:
            msg = await telegram_bot.app.bot.send_message(
                chat_id=telegram_bot.eyal_chat_id,
                text=message,
                parse_mode="Markdown",
                reply_markup=keyboard,
            )
            # Store message_id → short_id for free-text reply detection
            if msg:
                self.message_task_map[msg.message_id] = short_id
        except Exception as e:
            logger.error(f"Failed to send overdue reminder with buttons: {e}")
            # Fallback: send without buttons
            await telegram_bot.send_to_eyal(message)

        logger.info(f"Sent overdue reminder for: {task_desc}")
        return True

    async def _send_due_today_reminder(self, task: dict) -> bool:
        """
        Send reminder for a task due today.

        Args:
            task: Task dict from sheet.

        Returns:
            True if sent successfully.
        """
        assignee = task.get("assignee", "Unknown")
        task_desc = task.get("task", "Unknown task")
        priority = task.get("priority", "M")

        priority_emoji = {"H": "!!!", "M": "!!", "L": "!"}.get(priority, "!!")

        message = (
            f"*Task Due Today* {priority_emoji}\n\n"
            f"*Task:* {task_desc}\n"
            f"*Assignee:* {assignee}\n"
            f"\nPlease complete this task today!"
        )

        # Send to assignee
        assignee_telegram = self._get_telegram_id(assignee)
        if assignee_telegram:
            await telegram_bot.send_message(
                chat_id=assignee_telegram,
                text=message
            )

        logger.info(f"Sent due-today reminder for: {task_desc}")
        return True

    async def _send_due_soon_reminder(
        self,
        task: dict,
        days_until: int
    ) -> bool:
        """
        Send reminder for a task due soon.

        Args:
            task: Task dict from sheet.
            days_until: Days until deadline.

        Returns:
            True if sent successfully.
        """
        assignee = task.get("assignee", "Unknown")
        task_desc = task.get("task", "Unknown task")
        deadline = task.get("deadline", "")

        day_word = "day" if days_until == 1 else "days"

        message = (
            f"*Upcoming Deadline*\n\n"
            f"*Task:* {task_desc}\n"
            f"*Assignee:* {assignee}\n"
            f"*Due:* {deadline} ({days_until} {day_word})\n"
        )

        # Send to assignee
        assignee_telegram = self._get_telegram_id(assignee)
        if assignee_telegram:
            await telegram_bot.send_message(
                chat_id=assignee_telegram,
                text=message
            )

        logger.info(f"Sent due-soon reminder for: {task_desc}")
        return True

    async def _send_daily_summary_to_eyal(self, summary: dict) -> bool:
        """
        Send a daily task summary to Eyal.

        Args:
            summary: Summary dict with overdue, due_today, due_soon lists.

        Returns:
            True if sent successfully.
        """
        lines = ["*Daily Task Summary*\n"]

        # Overdue tasks
        if summary["overdue"]:
            lines.append(f"\n*Overdue ({len(summary['overdue'])})* !!!")
            for task in summary["overdue"][:5]:  # Limit to 5
                lines.append(
                    f"  - {task.get('task', 'Unknown')[:40]} "
                    f"({task.get('assignee', 'Unknown')})"
                )
            if len(summary["overdue"]) > 5:
                lines.append(f"  ... and {len(summary['overdue']) - 5} more")

        # Due today
        if summary["due_today"]:
            lines.append(f"\n*Due Today ({len(summary['due_today'])})*")
            for task in summary["due_today"][:5]:
                lines.append(
                    f"  - {task.get('task', 'Unknown')[:40]} "
                    f"({task.get('assignee', 'Unknown')})"
                )
            if len(summary["due_today"]) > 5:
                lines.append(f"  ... and {len(summary['due_today']) - 5} more")

        message = "\n".join(lines)
        await telegram_bot.send_to_eyal(message)

        logger.info("Sent daily task summary to Eyal")
        return True

    def _get_telegram_id(self, name: str) -> int | None:
        """
        Get Telegram chat ID for a team member by name.

        Args:
            name: Team member name.

        Returns:
            Telegram chat ID or None if not found.
        """
        name_lower = name.lower().strip()

        for team_name, telegram_id in TEAM_TELEGRAM_IDS.items():
            if name_lower in team_name.lower():
                return telegram_id

        return None

    async def get_task_summary(self) -> dict:
        """
        Get a summary of all task statuses.

        Returns:
            Dict with counts by status and priority.
        """
        tasks = await sheets_service.get_all_tasks()

        summary = {
            "total": len(tasks),
            "by_status": {
                "pending": 0,
                "in_progress": 0,
                "done": 0,
                "overdue": 0,
            },
            "by_priority": {
                "H": 0,
                "M": 0,
                "L": 0,
            },
            "by_assignee": {},
        }

        today = datetime.now(_ISRAEL_TZ).date()

        for task in tasks:
            status = task.get("status", "pending")
            priority = task.get("priority", "M")
            assignee = task.get("assignee", "Unassigned")
            deadline_str = task.get("deadline", "")

            # Check if overdue
            if deadline_str and status not in ["done", "overdue"]:
                try:
                    deadline = datetime.strptime(deadline_str, "%Y-%m-%d").date()
                    if deadline < today:
                        status = "overdue"
                except ValueError:
                    pass

            # Count by status
            if status in summary["by_status"]:
                summary["by_status"][status] += 1
            else:
                summary["by_status"]["pending"] += 1

            # Count by priority
            if priority in summary["by_priority"]:
                summary["by_priority"][priority] += 1

            # Count by assignee
            if assignee not in summary["by_assignee"]:
                summary["by_assignee"][assignee] = 0
            summary["by_assignee"][assignee] += 1

        return summary

    async def send_weekly_summary(self) -> bool:
        """
        Send weekly task summary to Eyal.

        Called manually or scheduled for Monday mornings.

        Returns:
            True if sent successfully.
        """
        summary = await self.get_task_summary()

        lines = [
            "*Weekly Task Summary*\n",
            f"*Total Tasks:* {summary['total']}",
            "",
            "*By Status:*",
            f"  - Pending: {summary['by_status']['pending']}",
            f"  - In Progress: {summary['by_status']['in_progress']}",
            f"  - Overdue: {summary['by_status']['overdue']}",
            f"  - Completed: {summary['by_status']['done']}",
            "",
            "*By Priority:*",
            f"  - High: {summary['by_priority']['H']}",
            f"  - Medium: {summary['by_priority']['M']}",
            f"  - Low: {summary['by_priority']['L']}",
            "",
            "*By Assignee:*",
        ]

        for assignee, count in sorted(
            summary["by_assignee"].items(),
            key=lambda x: x[1],
            reverse=True
        ):
            lines.append(f"  - {assignee}: {count}")

        message = "\n".join(lines)
        await telegram_bot.send_to_eyal(message)

        logger.info("Sent weekly task summary to Eyal")
        return True


# Singleton instance
task_reminder_scheduler = TaskReminderScheduler()
