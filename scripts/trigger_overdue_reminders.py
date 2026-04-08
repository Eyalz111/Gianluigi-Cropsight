#!/usr/bin/env python3
"""
One-shot trigger for overdue task reminders.

Why this exists: the task_reminder_scheduler runs every 8 hours and the
in-memory task_action_map is wiped on every Cloud Run restart. After a
deploy, the user has to wait up to 8 hours before they can click any
reminder buttons (old buttons point to stale short_ids; the scheduler
hasn't run yet to populate fresh ones).

This script sends fresh overdue reminders directly via the Telegram Bot
API using the new task_id-based callback_data format. Once the user
clicks the new buttons, the handler does a fresh DB lookup by UUID and
the action goes through.

Usage:
    python scripts/trigger_overdue_reminders.py
"""

import asyncio
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config.settings import settings  # noqa: F401  (loads env)

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger(__name__)


async def main() -> int:
    from datetime import datetime, date
    from services.supabase_client import supabase_client
    from services.google_sheets import sheets_service
    from services.telegram_bot import telegram_bot
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup

    # Initialize the Telegram bot just enough to send messages
    if not telegram_bot.app:
        telegram_bot._init_app()

    # Find overdue tasks from Sheets (same source the real scheduler uses)
    sheets_tasks = await sheets_service.get_all_tasks()
    today = date.today()

    overdue = []
    for task in sheets_tasks:
        if task.get("status", "").lower() in ("done", "cancelled"):
            continue
        deadline_str = task.get("deadline", "")
        if not deadline_str:
            continue
        try:
            deadline = datetime.strptime(deadline_str, "%Y-%m-%d").date()
        except ValueError:
            continue
        if deadline < today:
            overdue.append((task, (today - deadline).days))

    logger.info(f"Found {len(overdue)} overdue tasks")
    if not overdue:
        logger.info("Nothing to send.")
        return 0

    # Resolve task_ids in bulk for any unique assignees
    sent_count = 0
    for task, days_overdue in overdue:
        assignee = task.get("assignee", "Unknown")
        task_text = task.get("task", "Unknown task")
        priority = task.get("priority", "M")
        source = task.get("source_meeting", "")

        # Resolve DB task_id by title+assignee match (status=None covers
        # pending + in_progress + overdue — done/cancelled filtered client-side)
        db_task_id = None
        try:
            db_tasks = supabase_client.get_tasks(assignee=assignee, status=None, limit=500)
            target = task_text.strip().lower()
            for dt in db_tasks:
                if dt.get("status") in ("done", "cancelled"):
                    continue
                if dt.get("title", "").strip().lower() == target:
                    db_task_id = dt["id"]
                    break
        except Exception as e:
            logger.warning(f"Could not resolve task_id for '{task_text[:50]}': {e}")

        if not db_task_id:
            logger.warning(f"SKIPPED (no DB id): '{task_text[:60]}' ({assignee})")
            continue

        priority_emoji = {"H": "!!!", "M": "!!", "L": "!"}.get(priority, "!!")
        message = (
            f"*OVERDUE TASK* {priority_emoji}\n\n"
            f"*Task:* {task_text}\n"
            f"*Assignee:* {assignee}\n"
            f"*Days Overdue:* {days_overdue}\n"
        )
        if source:
            message += f"*From Meeting:* {source}\n"
        message += f"\nTap a button or reply with an update:"

        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("Done", callback_data=f"taskdone:{db_task_id}"),
            InlineKeyboardButton("+1 Week", callback_data=f"taskdelay:{db_task_id}"),
            InlineKeyboardButton("Discuss", callback_data=f"taskdiscuss:{db_task_id}"),
        ]])

        try:
            await telegram_bot.app.bot.send_message(
                chat_id=telegram_bot.eyal_chat_id,
                text=message,
                parse_mode="Markdown",
                reply_markup=keyboard,
            )
            sent_count += 1
            logger.info(f"Sent reminder for: {task_text[:60]}")
        except Exception as e:
            logger.error(f"Failed to send reminder for '{task_text[:60]}': {e}")

    logger.info(f"Done. Sent {sent_count} fresh reminders.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
