"""
Telegram bot for user interaction.

This module handles all Telegram bot operations:
- Receiving messages from team members
- Sending notifications and summaries
- Managing conversations for Q&A
- Handling approval requests/responses from Eyal

Bot capabilities:
- Group chat: Team-wide notifications
- DM to Eyal: Approval requests, sensitive content
- DM from anyone: Queries, task management

Usage:
    from services.telegram_bot import telegram_bot

    # Start the bot
    await telegram_bot.start()

    # Send a message
    await telegram_bot.send_message(chat_id, "Hello!")

    # Send approval request
    await telegram_bot.send_approval_request(meeting_summary)
"""

import asyncio
import logging
from typing import Any

from telegram import BotCommand, Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

from config.settings import settings
from config.team import TEAM_MEMBERS, get_team_member
from core.retry import retry
from services.conversation_memory import conversation_memory

logger = logging.getLogger(__name__)


def _escape_html(text: str) -> str:
    """Escape HTML special characters for Telegram HTML parse mode."""
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def _escape_markdown(text: str) -> str:
    """Escape Markdown special characters for Telegram Markdown parse mode."""
    for ch in ("*", "_", "`", "["):
        text = text.replace(ch, f"\\{ch}")
    return text


def _split_message(text: str, max_len: int = 4000) -> list[str]:
    """
    Split a long message into parts that fit within Telegram's limit.

    Splits on double-newline (section boundaries) when possible,
    falls back to single newline, then hard cut.
    """
    if len(text) <= max_len:
        return [text]

    parts = []
    remaining = text
    while len(remaining) > max_len:
        # Try to split at a section boundary (double newline)
        cut = remaining[:max_len].rfind("\n\n")
        if cut > max_len // 2:
            parts.append(remaining[:cut])
            remaining = remaining[cut + 2:]
            continue
        # Fall back to single newline
        cut = remaining[:max_len].rfind("\n")
        if cut > max_len // 2:
            parts.append(remaining[:cut])
            remaining = remaining[cut + 1:]
            continue
        # Hard cut
        parts.append(remaining[:max_len])
        remaining = remaining[max_len:]

    if remaining:
        parts.append(remaining)
    return parts


def format_summary_teaser(
    title: str,
    date: str,
    participants: list[str],
    content: dict,
    drive_link: str,
) -> str:
    """
    Format a short teaser for team distribution after approval.

    Shows counts, top decisions, top action items, and a Drive link.
    Designed to be under ~800 chars naturally (no truncation needed).
    """
    decisions = content.get("decisions", [])
    tasks = content.get("tasks", [])
    follow_ups = content.get("follow_ups", [])

    parts = [f"*Meeting Summary: {title}* ({date})"]
    if participants:
        parts.append(f"Participants: {', '.join(participants)}")

    # Counts line
    counts = []
    if decisions:
        counts.append(f"{len(decisions)} decisions")
    if tasks:
        counts.append(f"{len(tasks)} action items")
    if follow_ups:
        counts.append(f"{len(follow_ups)} follow-ups")
    if counts:
        parts.append("")
        parts.append(" · ".join(counts))

    # Top decisions (max 3)
    if decisions:
        parts.append("")
        parts.append("*Key decisions:*")
        for d in decisions[:3]:
            desc = d.get("description", "")
            if len(desc) > 80:
                desc = desc[:77] + "..."
            parts.append(f"• {desc}")
        if len(decisions) > 3:
            parts.append(f"  _...and {len(decisions) - 3} more in full summary_")

    # Top action items (max 5, H priority first)
    if tasks:
        sorted_tasks = sorted(tasks, key=lambda t: {"H": 0, "M": 1, "L": 2}.get(t.get("priority", "M"), 1))
        parts.append("")
        parts.append("*Top action items:*")
        for t in sorted_tasks[:5]:
            assignee = t.get("assignee", "TBD")
            title_text = t.get("title", "")
            if len(title_text) > 60:
                title_text = title_text[:57] + "..."
            deadline = t.get("deadline") or "no deadline"
            parts.append(f"• {assignee}: {title_text} — {deadline}")

    # Drive link
    if drive_link:
        parts.append("")
        parts.append(f"[Full summary]({drive_link})")

    return "\n".join(parts)


def _format_cross_reference_section(cross_ref: dict) -> list[str]:
    """
    Format cross-reference results as HTML lines for the Telegram approval message.

    Shows task status changes, deduplicated tasks, and resolved questions
    in a clean, readable format for Eyal to review.

    Args:
        cross_ref: Cross-reference results dict from run_cross_reference().

    Returns:
        List of HTML-formatted lines, or empty list if nothing to show.
    """
    lines = []
    has_content = False

    # Status changes
    status_changes = cross_ref.get("status_changes", [])
    if status_changes:
        has_content = True
        lines.append(f"<b>Cross-Meeting Intelligence</b>")
        lines.append("")
        lines.append(f"<b>Task Status Changes ({len(status_changes)})</b>")
        for sc in status_changes:
            conf = sc.get("confidence", "medium").upper()
            title = _escape_html(sc.get("task_title", "Unknown"))
            assignee = sc.get("assignee", "")
            new_status = sc.get("new_status", "").upper()
            evidence = _escape_html(sc.get("evidence", "")[:80])
            lines.append(f"  [{conf}] \"{title}\" ({assignee}) -> {new_status}")
            if evidence:
                lines.append(f"    \"{evidence}\"")
        lines.append("")

    # Dedup results
    dedup = cross_ref.get("dedup", {})
    duplicates = dedup.get("duplicates", [])
    updates = dedup.get("updates", [])

    if duplicates:
        has_content = True
        if not status_changes:
            lines.append(f"<b>Cross-Meeting Intelligence</b>")
            lines.append("")
        lines.append(f"<b>Deduplicated Tasks ({len(duplicates)})</b>")
        for dup in duplicates:
            task_title = _escape_html(dup.get("task", {}).get("title", ""))
            reason = _escape_html(dup.get("reason", "")[:60])
            lines.append(f"  \"{task_title}\" -> matched existing task")
            if reason:
                lines.append(f"    ({reason})")
        lines.append("")

    # Task Updates removed — Task Status Changes already covers this info.
    # The UPDATE: prefix from extraction is consumed by cross_reference dedup,
    # not displayed separately.

    # Resolved questions
    resolved_qs = cross_ref.get("resolved_questions", [])
    if resolved_qs:
        has_content = True
        if not status_changes and not duplicates:
            lines.append(f"<b>Cross-Meeting Intelligence</b>")
            lines.append("")
        lines.append(f"<b>Questions Resolved ({len(resolved_qs)})</b>")
        for rq in resolved_qs:
            q_text = rq.get("question", "")
            a_text = rq.get("answer", "")
            # Truncate at sentence boundary, not mid-word
            if len(q_text) > 100:
                cut = q_text[:100].rfind("?")
                if cut < 0:
                    cut = q_text[:100].rfind(" ")
                q_text = q_text[:cut + 1] if cut > 30 else q_text[:100] + "..."
            if len(a_text) > 120:
                cut = a_text[:120].rfind(".")
                if cut < 0:
                    cut = a_text[:120].rfind(" ")
                a_text = a_text[:cut + 1] if cut > 30 else a_text[:120] + "..."
            lines.append(f"  Q: \"{_escape_html(q_text)}\"")
            lines.append(f"  A: {_escape_html(a_text)}")
        lines.append("")

    return lines if has_content else []


class TelegramBot:
    """
    Telegram bot for Gianluigi's user interface.

    Handles both group and direct message interactions.
    """

    def __init__(self):
        """
        Initialize the Telegram bot with token from settings.
        """
        self._app: Application | None = None
        self._stop_event: asyncio.Event = asyncio.Event()
        self._ready: asyncio.Event = asyncio.Event()
        self.group_chat_id = settings.TELEGRAM_GROUP_CHAT_ID
        self.eyal_chat_id = settings.TELEGRAM_EYAL_CHAT_ID

        # Map Telegram user IDs to team member IDs
        # This will be populated when users interact with the bot
        self._telegram_user_map: dict[int, str] = {}

        # Session stack: allows debrief to interrupt weekly review
        self._session_stack: list[str] = []

        # Track approval message IDs for cleanup on edit/resubmit
        self._approval_message_ids: dict[str, list[int]] = {}

    @property
    def _active_interactive_session(self) -> str | None:
        """Backward compat: returns current session type from stack."""
        return self._session_stack[-1] if self._session_stack else None

    @_active_interactive_session.setter
    def _active_interactive_session(self, value: str | None) -> None:
        """Backward compat: set/clear current session via stack."""
        if value is None:
            if self._session_stack:
                self._session_stack.pop()
        else:
            if not self._session_stack or self._session_stack[-1] != value:
                self._session_stack.append(value)

    async def _reconstruct_session_stack(self) -> int:
        """Rebuild session stack from Supabase on startup.

        Design: The stack is derived from active session records rather than
        persisted separately. Each session type (debrief, weekly_review) has
        its own Supabase table with status tracking. We query those tables
        and reconstruct the stack order from created_at timestamps. This
        avoids a separate session_stack table and write-through overhead.
        """
        from services.supabase_client import supabase_client
        sessions = []

        review = supabase_client.get_active_weekly_review_session()
        if review:
            sessions.append(("weekly_review", review.get("created_at", "")))

        try:
            debrief = supabase_client.get_active_debrief_session()
            if debrief:
                sessions.append(("debrief", debrief.get("created_at", "")))
        except Exception:
            pass

        sessions.sort(key=lambda x: x[1])  # older first (bottom of stack)
        self._session_stack = [s[0] for s in sessions]
        return len(self._session_stack)

    @property
    def app(self) -> Application:
        """Lazy initialization of the Telegram Application."""
        if self._app is None:
            if not settings.TELEGRAM_BOT_TOKEN:
                raise RuntimeError("TELEGRAM_BOT_TOKEN not configured")
            self._app = (
                Application.builder()
                .token(settings.TELEGRAM_BOT_TOKEN)
                .build()
            )
        return self._app

    async def start(self) -> None:
        """
        Start the Telegram bot and begin polling for messages.
        """
        logger.info("Starting Telegram bot...")

        # Add command handlers
        self.app.add_handler(CommandHandler("start", self._handle_start))
        self.app.add_handler(CommandHandler("help", self._handle_help))
        self.app.add_handler(CommandHandler("tasks", self._handle_tasks))
        self.app.add_handler(CommandHandler("mytasks", self._handle_tasks))
        self.app.add_handler(CommandHandler("search", self._handle_search))
        self.app.add_handler(CommandHandler("decisions", self._handle_decisions))
        self.app.add_handler(CommandHandler("questions", self._handle_questions))
        self.app.add_handler(CommandHandler("retract", self._handle_retract))
        self.app.add_handler(CommandHandler("reprocess", self._handle_reprocess))
        self.app.add_handler(CommandHandler("cost", self._handle_cost))
        self.app.add_handler(CommandHandler("meetings", self._handle_meetings))
        self.app.add_handler(CommandHandler("status", self._handle_status))
        self.app.add_handler(CommandHandler("myid", self._handle_myid))
        self.app.add_handler(CommandHandler("debrief", self._handle_debrief))
        self.app.add_handler(CommandHandler("cancel", self._handle_cancel_debrief))
        self.app.add_handler(CommandHandler("emailscan", self._handle_email_scan))
        self.app.add_handler(CommandHandler("review", self._handle_review))
        self.app.add_handler(CommandHandler("sync", self._handle_sync))

        # Add callback handler for inline buttons (approval flow, debrief)
        self.app.add_handler(
            CallbackQueryHandler(self._handle_callback_query)
        )

        # Add message handler for general text (must be last)
        self.app.add_handler(
            MessageHandler(
                filters.TEXT & ~filters.COMMAND,
                self._handle_message
            )
        )

        # Global error handler — PTB swallows handler exceptions and logs
        # them to its own logger, which goes to stdout and vanishes. Route
        # them to our logger AND to supabase audit_log so we can diagnose
        # silently-failing commands like the /debrief incident 2026-04-11.
        self.app.add_error_handler(self._on_handler_error)

        # Initialize and start polling
        await self.app.initialize()
        await self.app.start()
        await self.app.updater.start_polling(drop_pending_updates=True)

        # Register the command list with Telegram (setMyCommands).
        # Without this, /commands embedded in message text render as blue
        # links but tapping them only inserts into the composer — the user
        # has to press Send. Registering makes them proper one-tap commands.
        try:
            await self.app.bot.set_my_commands([
                BotCommand("debrief", "Start end-of-day debrief session"),
                BotCommand("cancel", "Cancel active debrief"),
                BotCommand("status", "Show pending approvals and system state"),
                BotCommand("tasks", "List open tasks"),
                BotCommand("decisions", "List recent decisions"),
                BotCommand("questions", "List open questions"),
                BotCommand("meetings", "List recent meetings"),
                BotCommand("search", "Search memory"),
                BotCommand("retract", "Retract a distributed meeting"),
                BotCommand("reprocess", "Reprocess a meeting"),
                BotCommand("review", "Start weekly review"),
                BotCommand("sync", "Sync tasks from Sheets"),
                BotCommand("emailscan", "Scan personal email for action items"),
                BotCommand("cost", "Show API cost summary"),
                BotCommand("help", "Show help"),
            ])
            logger.info("Telegram bot commands registered via setMyCommands")
        except Exception as e:
            logger.warning(f"Could not register bot commands: {e}")

        # Warn if Eyal's chat ID looks like a group (should be positive for DM)
        if self.eyal_chat_id and int(self.eyal_chat_id) < 0:
            logger.warning(
                f"TELEGRAM_EYAL_CHAT_ID ({self.eyal_chat_id}) is negative — "
                f"this looks like a group chat, not Eyal's personal DM. "
                f"Have Eyal send /myid in a private chat with the bot to get his real ID."
            )

        logger.info("Telegram bot started and polling for messages")
        self._ready.set()

        # Block until stop() is called — keeps this task alive
        # so asyncio.wait(FIRST_COMPLETED) doesn't trigger shutdown
        await self._stop_event.wait()

    async def stop(self) -> None:
        """
        Gracefully stop the Telegram bot.
        """
        logger.info("Stopping Telegram bot...")

        # Signal the start() method to unblock
        self._stop_event.set()

        if self._app is not None:
            await self.app.updater.stop()
            await self.app.stop()
            await self.app.shutdown()

        logger.info("Telegram bot stopped")

    async def wait_until_ready(self, timeout: float = 60):
        """Wait until the Telegram bot is fully initialized."""
        try:
            await asyncio.wait_for(self._ready.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            logger.warning("Telegram bot ready timeout — proceeding anyway")

    # =========================================================================
    # Sending Messages
    # =========================================================================

    @retry(max_attempts=3, backoff=2.0, base_delay=1.0)
    async def _bot_send_message(self, **kwargs) -> None:
        """
        Network-call wrapper for Telegram's bot.send_message — extracted
        so the @retry decorator can catch transient BrokenPipeError /
        ConnectionError / TimeoutError / OSError from the underlying
        socket. BrokenPipeError is a subclass of OSError, which
        core.retry's default TRANSIENT_EXCEPTIONS tuple catches, so the
        existing decorator covers the observed pain directly. 3 attempts
        with exponential backoff (1s, 2s).

        Tier 3.4: added after a BrokenPipe was observed during test 4
        on 2026-04-09 that silently dropped an approval Telegram message.
        """
        await self.app.bot.send_message(**kwargs)

    async def send_message(
        self,
        chat_id: str | int,
        text: str,
        parse_mode: str = "Markdown",
        reply_markup: Any = None
    ) -> bool:
        """
        Send a text message to a chat.

        Args:
            chat_id: Telegram chat ID.
            text: Message text (supports Markdown).
            parse_mode: 'Markdown' or 'HTML'.
            reply_markup: Optional inline keyboard markup.

        Returns:
            True if message was sent successfully.
        """
        try:
            # Split long messages (Telegram limit is 4096 chars).
            # Send overflow parts first, buttons only on the last part.
            # Tier 3.4: _bot_send_message wraps the network call in @retry.
            if len(text) > 4000:
                parts = _split_message(text, max_len=4000)
                # Send all parts except the last without buttons
                for part in parts[:-1]:
                    await self._bot_send_message(
                        chat_id=chat_id,
                        text=part,
                        parse_mode=parse_mode,
                    )
                # Send the last part with buttons
                await self._bot_send_message(
                    chat_id=chat_id,
                    text=parts[-1],
                    parse_mode=parse_mode,
                    reply_markup=reply_markup,
                )
            else:
                await self._bot_send_message(
                    chat_id=chat_id,
                    text=text,
                    parse_mode=parse_mode,
                    reply_markup=reply_markup,
                )
            return True
        except Exception as e:
            logger.error(f"Error sending message to {chat_id}: {e}")
            return False

    async def send_to_group(self, text: str) -> bool:
        """
        Send a message to the CropSight team group chat.

        Args:
            text: Message text.

        Returns:
            True if message was sent successfully.
        """
        if not self.group_chat_id:
            logger.warning("Group chat ID not configured")
            return False
        return await self.send_message(self.group_chat_id, text)

    async def _cleanup_approval_parts(
        self, meeting_id: str, keep_message_id: int | None = None
    ) -> None:
        """Delete orphan multi-part approval messages for a meeting.

        When a long approval preview is split into multiple Telegram messages,
        only the last one (with buttons) gets updated on approve/reject.
        This deletes the earlier parts to avoid stale orphan messages.

        Args:
            meeting_id: The meeting/approval ID to clean up.
            keep_message_id: Message ID to NOT delete (the one being edited).
        """
        msg_ids = self._approval_message_ids.pop(meeting_id, [])
        for msg_id in msg_ids:
            if msg_id == keep_message_id:
                continue
            try:
                await self.app.bot.delete_message(
                    chat_id=self.eyal_chat_id, message_id=msg_id,
                )
            except Exception:
                pass  # Message may already be deleted or too old

    async def send_to_eyal(
        self,
        text: str,
        reply_markup: Any = None,
        parse_mode: str | None = None
    ) -> bool:
        """
        Send a direct message to Eyal.

        Used for approval requests and sensitive content.

        Args:
            text: Message text.
            reply_markup: Optional inline keyboard.
            parse_mode: Override parse mode ('HTML', 'Markdown', or None).

        Returns:
            True if message was sent successfully.
        """
        if not self.eyal_chat_id:
            logger.warning("Eyal's chat ID not configured")
            return False
        return await self.send_message(
            self.eyal_chat_id,
            text,
            parse_mode=parse_mode if parse_mode else "Markdown",
            reply_markup=reply_markup
        )

    async def send_meeting_summary(
        self,
        title: str,
        summary: str,
        drive_link: str,
        sensitive: bool = False
    ) -> bool:
        """
        Send a meeting summary notification.

        Args:
            title: Meeting title.
            summary: Brief summary (or full for sensitive).
            drive_link: Link to full summary in Google Drive.
            sensitive: If True, send only to Eyal.

        Returns:
            True if message was sent successfully.
        """
        message = f"""*New Meeting Summary: {title}*

{summary[:500]}{'...' if len(summary) > 500 else ''}

[View Full Summary]({drive_link})
"""

        if sensitive:
            message = f"*[SENSITIVE]*\n\n{message}"
            return await self.send_to_eyal(message)
        elif settings.ENVIRONMENT != "production":
            # Development mode: send to Eyal only, not group
            return await self.send_to_eyal(message)
        else:
            return await self.send_to_group(message)

    async def send_approval_request(
        self,
        meeting_title: str,
        summary_preview: str,
        meeting_id: str,
        decisions: list[dict] | None = None,
        tasks: list[dict] | None = None,
        follow_ups: list[dict] | None = None,
        open_questions: list[dict] | None = None,
        drive_link: str | None = None,
        cross_reference: dict | None = None,
        executive_summary: str | None = None,
        sensitivity: str = "founders",
    ) -> bool:
        """
        Send an approval request to Eyal with a clean, structured preview.

        Args:
            meeting_title: Title of the meeting.
            summary_preview: Discussion summary text (not the full markdown).
            meeting_id: UUID for tracking.
            decisions: List of decision dicts.
            tasks: List of task dicts.
            follow_ups: List of follow-up meeting dicts.
            open_questions: List of open question dicts.
            drive_link: Optional link to draft in Drive.
            cross_reference: v0.3 cross-reference results (dedup, status changes, etc.).
            executive_summary: One-line TLDR of the meeting's key outcome.

        Returns:
            True if request was sent successfully.
        """
        decisions = decisions or []
        tasks = tasks or []
        follow_ups = follow_ups or []
        open_questions = open_questions or []

        # Build a clean HTML message
        lines = [f"<b>Approval Request: {_escape_html(meeting_title)}</b>", ""]

        # Executive summary (TLDR) at the top
        if executive_summary:
            lines.append(f"<i>{_escape_html(executive_summary)}</i>")
            lines.append("")

        # Decisions
        if decisions:
            lines.append(f"<b>Decisions ({len(decisions)})</b>")
            for i, d in enumerate(decisions, 1):
                label = d.get("label", "")
                desc = _escape_html(d.get("description", ""))
                prefix = f"<b>{_escape_html(label)}</b> — " if label else ""
                lines.append(f"  {i}. {prefix}{desc}")
            lines.append("")

        # Tasks
        if tasks:
            lines.append(f"<b>Action Items ({len(tasks)})</b>")
            for i, t in enumerate(tasks, 1):
                label = t.get("label", "")
                title = _escape_html(t.get("title", ""))
                assignee = t.get("assignee", "") or "—"
                priority = t.get("priority", "M")
                label_prefix = f"<b>{_escape_html(label)}</b>: " if label else ""
                lines.append(f"  {i}. [{priority}] {label_prefix}{title} -> {assignee}")
            lines.append("")

        # Follow-ups
        if follow_ups:
            lines.append(f"<b>Follow-up Meetings ({len(follow_ups)})</b>")
            for f in follow_ups:
                label = f.get("label", "")
                title = _escape_html(f.get("title", ""))
                led_by = f.get("led_by", "TBD")
                label_prefix = f"<b>{_escape_html(label)}</b>: " if label else ""
                lines.append(f"  - {label_prefix}{title} (led by {led_by})")
            lines.append("")

        # Open questions
        if open_questions:
            lines.append(f"<b>Open Questions ({len(open_questions)})</b>")
            for q in open_questions:
                label = q.get("label", "")
                question = _escape_html(q.get("question", ""))
                raised_by = q.get("raised_by", "")
                label_prefix = f"<b>{_escape_html(label)}</b>: " if label else ""
                lines.append(f"  - {label_prefix}{question}")
                if raised_by:
                    lines.append(f"    (raised by {raised_by})")
            lines.append("")

        # Discussion summary (brief excerpt, truncated at sentence boundary)
        if summary_preview:
            if len(summary_preview) > 600:
                cut = summary_preview[:600].rfind(".")
                if cut > 300:
                    excerpt = summary_preview[:cut + 1]
                else:
                    cut = summary_preview[:600].rfind(" ")
                    excerpt = summary_preview[:cut] + "..." if cut > 0 else summary_preview[:600] + "..."
            else:
                excerpt = summary_preview
            lines.append(f"<b>Discussion Summary</b>")
            lines.append(_escape_html(excerpt))
            lines.append("")

        # v0.3: Cross-meeting intelligence section
        if cross_reference:
            cr_lines = _format_cross_reference_section(cross_reference)
            if cr_lines:
                lines.extend(cr_lines)
                lines.append("")

        if drive_link:
            lines.append(f'<a href="{drive_link}">View Full Draft</a>')
            lines.append("")

        # Show countdown indicator when in auto_review mode
        from config.settings import settings as _settings
        if _settings.APPROVAL_MODE == "auto_review":
            minutes = _settings.AUTO_REVIEW_WINDOW_MINUTES
            lines.append(
                f"Auto-publish in {minutes} minutes if no action taken."
            )
            lines.append("")

        lines.append("Use the buttons below to approve, request changes, or reject.")

        message = "\n".join(lines)

        # Create inline keyboard with sensitivity tier button
        tier_labels = {
            "public": "\U0001f30d PUBLIC \u2014 safe for anyone",
            "team": "\U0001f465 TEAM \u2014 all employees",
            "founders": "\U0001f465 FOUNDERS \u2014 founding team",
            "ceo": "\U0001f512 CEO \u2014 Eyal only",
        }
        # Normalize legacy values
        tier = sensitivity.lower()
        if tier in ("normal", "team"):
            tier = "founders"
        elif tier in ("sensitive", "ceo_only", "restricted", "legal"):
            tier = "ceo"
        sens_label = tier_labels.get(tier, tier_labels["founders"])
        keyboard = [
            [
                InlineKeyboardButton(
                    "Approve",
                    callback_data=f"approve:{meeting_id}"
                ),
                InlineKeyboardButton(
                    "Request Changes",
                    callback_data=f"edit:{meeting_id}"
                ),
            ],
            [
                InlineKeyboardButton(
                    "Reject",
                    callback_data=f"reject:{meeting_id}"
                ),
            ],
            [
                InlineKeyboardButton(
                    sens_label,
                    callback_data=f"sens_toggle:{meeting_id}"
                ),
            ],
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)

        # Delete old approval messages for this meeting (cleanup on resubmit after edit)
        old_msg_ids = self._approval_message_ids.pop(meeting_id, [])
        for msg_id in old_msg_ids:
            try:
                await self.app.bot.delete_message(
                    chat_id=self.eyal_chat_id, message_id=msg_id,
                )
            except Exception:
                pass  # Message may already be deleted or too old

        # Send and track message IDs for multi-part cleanup
        sent_message_ids = []
        try:
            chat_id = self.eyal_chat_id
            if len(message) > 4000:
                parts = _split_message(message, max_len=4000)
                for part in parts[:-1]:
                    msg = await self.app.bot.send_message(
                        chat_id=chat_id, text=part, parse_mode="HTML",
                    )
                    sent_message_ids.append(msg.message_id)
                msg = await self.app.bot.send_message(
                    chat_id=chat_id, text=parts[-1], parse_mode="HTML",
                    reply_markup=reply_markup,
                )
                sent_message_ids.append(msg.message_id)
            else:
                msg = await self.app.bot.send_message(
                    chat_id=chat_id, text=message, parse_mode="HTML",
                    reply_markup=reply_markup,
                )
                sent_message_ids.append(msg.message_id)

            self._approval_message_ids[meeting_id] = sent_message_ids
            return True
        except Exception as e:
            logger.error(f"Error sending approval request: {e}")
            return False

    async def send_prep_outline(
        self,
        outline: dict,
        approval_id: str,
        confidence: str = "auto",
    ) -> bool:
        """
        Send a meeting prep outline proposal to Eyal with inline buttons.

        Args:
            outline: Outline dict from generate_prep_outline().
            approval_id: Unique ID for this prep outline approval.
            confidence: 'auto' or 'ask' — determines button set.

        Returns:
            True if message was sent.
        """
        from processors.meeting_prep import format_outline_for_telegram

        text = format_outline_for_telegram(outline, confidence)

        # Build button rows
        buttons = [
            [
                InlineKeyboardButton("Generate as-is", callback_data=f"prep_generate:{approval_id}"),
                InlineKeyboardButton("Add focus", callback_data=f"prep_focus:{approval_id}"),
            ],
        ]
        if confidence == "ask":
            buttons.append([
                InlineKeyboardButton("Wrong meeting type", callback_data=f"prep_reclassify:{approval_id}"),
                InlineKeyboardButton("Skip this prep", callback_data=f"prep_skip:{approval_id}"),
            ])
        else:
            buttons.append([
                InlineKeyboardButton("Skip this prep", callback_data=f"prep_skip:{approval_id}"),
            ])

        reply_markup = InlineKeyboardMarkup(buttons)
        return await self.send_to_eyal(text, reply_markup=reply_markup, parse_mode="HTML")

    async def send_stakeholder_approval_request(
        self,
        stakeholder_name: str,
        organization: str,
        updates: dict,
        is_new: bool = True,
        source_meeting_id: str | None = None,
    ) -> bool:
        """Send stakeholder update request to Eyal with approve/reject buttons."""
        action = "New Stakeholder" if is_new else "Update Stakeholder"

        lines = [f"<b>{action} Suggestion</b>", ""]
        lines.append(f"<b>Organization:</b> {_escape_html(organization)}")
        lines.append(f"<b>Contact:</b> {_escape_html(stakeholder_name)}")
        lines.append("")

        if updates:
            lines.append("<b>Details:</b>")
            for key, value in updates.items():
                lines.append(f"  - {_escape_html(key)}: {_escape_html(str(value))}")
            lines.append("")

        if source_meeting_id:
            lines.append(f"<i>Source meeting: {source_meeting_id}</i>")

        message = "\n".join(lines)

        # Create callback data — encode org name (truncated for Telegram 64-byte limit)
        org_key = organization[:30].replace(":", "_")

        keyboard = [
            [
                InlineKeyboardButton(
                    "Approve",
                    callback_data=f"stakeholder_approve:{org_key}"
                ),
                InlineKeyboardButton(
                    "Reject",
                    callback_data=f"stakeholder_reject:{org_key}"
                ),
            ],
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)

        return await self.send_to_eyal(
            message, reply_markup=reply_markup, parse_mode="HTML"
        )

    async def send_task_reminder(
        self,
        assignee_chat_id: str | int,
        task_description: str,
        deadline: str,
        overdue: bool = False
    ) -> bool:
        """
        Send a task reminder to a team member.

        Args:
            assignee_chat_id: Chat ID of the assignee.
            task_description: What the task is.
            deadline: When it's due.
            overdue: Whether the task is past due.

        Returns:
            True if reminder was sent successfully.
        """
        status = "OVERDUE" if overdue else "Reminder"
        emoji = "!!" if overdue else ""

        message = f"""*Task {status}* {emoji}

*Task:* {task_description}
*Deadline:* {deadline}

Reply with "done" when completed, or "postpone [date]" to update the deadline.
"""
        return await self.send_message(assignee_chat_id, message)

    # =========================================================================
    # Command Handlers
    # =========================================================================

    async def _handle_start(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """
        Handle /start command.

        Introduces Gianluigi and explains capabilities.
        """
        user = update.effective_user
        welcome_message = f"""Hello {user.first_name}! I'm *Gianluigi*, CropSight's AI operations assistant.

I help the team by:
- Processing meeting transcripts
- Tracking tasks and decisions
- Answering questions about past discussions
- Preparing meeting briefs

*Available commands:*
/help - Show all commands
/tasks - Show your open tasks
/search [topic] - Search meeting history
/decisions - List recent decisions
/questions - List open questions

Or just send me a question and I'll search our meeting history to answer it!
"""
        await self.send_message(update.effective_chat.id, welcome_message)

        # Register user if we can identify them
        logger.info(f"User started chat: {user.id} - {user.username}")

    async def _handle_cost(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
    ) -> None:
        """
        Handle /cost command — show API token usage summary.

        Admin-only (Eyal). Shows last 7 days of LLM usage grouped by
        call_site and model, including cache hit rates.
        """
        user = update.effective_user
        if not self._is_admin(user.id):
            await self.send_message(
                update.effective_chat.id,
                "Only Eyal can view cost data.",
            )
            return

        from services.supabase_client import supabase_client

        try:
            # Query last 7 days of token usage
            from datetime import datetime, timedelta
            seven_days_ago = (datetime.now() - timedelta(days=7)).isoformat()

            rows = (
                supabase_client.client.table("token_usage")
                .select("call_site,model,input_tokens,output_tokens,cache_read_tokens,cache_creation_tokens")
                .gte("created_at", seven_days_ago)
                .execute()
            ).data

            if not rows:
                await self.send_message(
                    update.effective_chat.id,
                    "No API usage recorded in the last 7 days.",
                )
                return

            # Aggregate by call_site + model
            agg = {}
            for r in rows:
                key = (r["call_site"], r["model"])
                if key not in agg:
                    agg[key] = {
                        "calls": 0,
                        "input": 0,
                        "output": 0,
                        "cache_read": 0,
                        "cache_create": 0,
                    }
                agg[key]["calls"] += 1
                agg[key]["input"] += r.get("input_tokens", 0)
                agg[key]["output"] += r.get("output_tokens", 0)
                agg[key]["cache_read"] += r.get("cache_read_tokens", 0) or 0
                agg[key]["cache_create"] += r.get("cache_creation_tokens", 0) or 0

            # Format output
            total_input = sum(v["input"] for v in agg.values())
            total_output = sum(v["output"] for v in agg.values())
            total_calls = sum(v["calls"] for v in agg.values())
            total_cache_read = sum(v["cache_read"] for v in agg.values())

            lines = ["*API Usage (Last 7 Days)*\n"]
            for (site, model), v in sorted(agg.items()):
                # Shorten model name for display
                short_model = model.split("/")[-1] if "/" in model else model
                short_model = short_model.replace("claude-", "")
                cache_pct = ""
                if v["cache_read"] > 0:
                    pct = round(v["cache_read"] / max(v["input"], 1) * 100)
                    cache_pct = f" ({pct}% cached)"
                lines.append(
                    f"`{site}` ({short_model})\n"
                    f"  {v['calls']} calls | "
                    f"{v['input']:,} in / {v['output']:,} out"
                    f"{cache_pct}"
                )

            lines.append(
                f"\n*Totals:* {total_calls} calls | "
                f"{total_input:,} in / {total_output:,} out | "
                f"{total_cache_read:,} cached"
            )

            await self.send_message(update.effective_chat.id, "\n".join(lines))

        except Exception as e:
            logger.error(f"Error in /cost command: {e}")
            await self.send_message(
                update.effective_chat.id,
                f"Error fetching cost data: {e}",
            )

    async def _handle_meetings(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
    ) -> None:
        """
        Handle /meetings command — browse past meetings.

        /meetings          — List last 10 meetings
        /meetings <title>  — Search by title
        """
        from services.supabase_client import supabase_client

        try:
            if context.args:
                # Search by title
                search_term = " ".join(context.args)
                meetings = supabase_client.list_meetings(limit=50)
                # Filter by title (case-insensitive)
                term_lower = search_term.lower()
                meetings = [
                    m for m in meetings
                    if term_lower in m.get("title", "").lower()
                ][:10]
                header = f"*Meetings matching:* _{_escape_markdown(search_term)}_\n"
            else:
                meetings = supabase_client.list_meetings(limit=10)
                header = "*Recent Meetings (last 10):*\n"

            if not meetings:
                await self.send_message(
                    update.effective_chat.id,
                    "No meetings found.",
                )
                return

            lines = [header]
            for i, m in enumerate(meetings, 1):
                title = _escape_markdown(m.get("title", "Untitled"))
                date = m.get("date", "")[:10]
                participants = m.get("participants", [])
                p_count = len(participants) if participants else 0
                status = m.get("approval_status", "unknown")

                lines.append(
                    f"*{i}.* {title}\n"
                    f"   {date} | {p_count} participants | {status}"
                )

            await self.send_message(update.effective_chat.id, "\n".join(lines))

        except Exception as e:
            logger.error(f"Error in /meetings command: {e}")
            await self.send_message(
                update.effective_chat.id,
                f"Error fetching meetings: {e}",
            )

    async def _handle_status(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
    ) -> None:
        """
        Handle /status command — system dashboard.

        Admin-only. Shows key metrics: meetings, tasks, commitments,
        API cost, and environment.
        """
        user = update.effective_user
        if not self._is_admin(user.id):
            await self.send_message(
                update.effective_chat.id,
                "Only Eyal can view system status.",
            )
            return

        from services.supabase_client import supabase_client
        from datetime import datetime, timedelta

        try:
            # Gather metrics
            meetings = supabase_client.list_meetings(limit=1000)
            meetings_count = len(meetings)
            last_meeting = meetings[0] if meetings else None
            last_date = last_meeting.get("date", "")[:10] if last_meeting else "never"

            all_tasks = supabase_client.get_tasks(status=None)
            total_tasks = len(all_tasks)
            open_tasks = len([t for t in all_tasks if t.get("status") == "pending"])
            overdue_tasks = len([
                t for t in all_tasks
                if t.get("status") == "pending"
                and t.get("deadline")
                and t["deadline"] < datetime.now().strftime("%Y-%m-%d")
            ])

            open_commitments = supabase_client.get_commitments(status="open")
            open_c = len(open_commitments)
            two_weeks_ago = (datetime.now() - timedelta(weeks=2)).isoformat()
            stale_c = len([
                c for c in open_commitments
                if c.get("created_at", "") < two_weeks_ago
            ])

            documents = supabase_client.list_documents(limit=1000)
            docs_count = len(documents)

            # API cost (last 30 days)
            thirty_days_ago = (datetime.now() - timedelta(days=30)).isoformat()
            try:
                usage_rows = (
                    supabase_client.client.table("token_usage")
                    .select("input_tokens,output_tokens")
                    .gte("created_at", thirty_days_ago)
                    .execute()
                ).data
                total_tokens = sum(
                    (r.get("input_tokens", 0) + r.get("output_tokens", 0))
                    for r in usage_rows
                ) if usage_rows else 0
            except Exception:
                total_tokens = 0

            # Pending approvals queue
            pending_approvals = supabase_client.get_pending_approval_summary()

            from config.settings import settings as _settings
            env = _settings.ENVIRONMENT

            lines = [
                "*Gianluigi Status*\n",
                f"Meetings processed: {meetings_count}",
                f"Last processing: {last_date}",
                f"Documents ingested: {docs_count}",
                f"Tasks tracked: {total_tasks}",
                f"Open tasks: {open_tasks} | Overdue: {overdue_tasks}",
                f"Open commitments: {open_c} | Stale (2+ weeks): {stale_c}",
                f"Monthly tokens: {total_tokens:,}",
                f"Environment: {env}",
            ]

            if pending_approvals:
                lines.append(f"\n*Pending Approvals ({len(pending_approvals)}):*")
                for pa in pending_approvals:
                    ct = pa.get("content_type", "unknown").replace("_", " ")
                    created = pa.get("created_at", "")[:16].replace("T", " ")
                    expires = pa.get("expires_at")
                    exp_str = f" (expires {expires[:16].replace('T', ' ')})" if expires else ""
                    lines.append(f"  - {ct}: {pa.get('approval_id', '')[:8]}... ({created}){exp_str}")
            else:
                lines.append("\nNo pending approvals.")

            # Pending prep outlines
            try:
                prep_outlines = supabase_client.get_pending_prep_outlines()
                if prep_outlines:
                    lines.append(f"\n*Pending Prep Outlines ({len(prep_outlines)}):*")
                    for po in prep_outlines:
                        content = po.get("content", {})
                        event = content.get("outline", {}).get("event", content.get("event", {}))
                        ptitle = event.get("title", "Unknown")
                        pstart = event.get("start", "")
                        time_info = ""
                        if pstart:
                            try:
                                pdt = datetime.fromisoformat(pstart.replace("Z", "+00:00"))
                                hours_left = (pdt - datetime.now(pdt.tzinfo)).total_seconds() / 3600
                                time_info = f" ({hours_left:.0f}h until meeting)"
                            except (ValueError, TypeError):
                                pass
                        lines.append(f"  - {ptitle}{time_info}")
            except Exception:
                pass

            # Weekly review session state
            try:
                review_session = supabase_client.get_active_weekly_review_session()
                if review_session:
                    rw = review_session.get("week_number", 0)
                    rs = review_session.get("status", "unknown")
                    rpart = review_session.get("current_part", 0)
                    lines.append(f"\n*Weekly Review:*")
                    lines.append(f"  W{rw} — {rs} (part {rpart})")
            except Exception:
                pass

            # T2.1 — Transcript watcher heartbeat
            try:
                heartbeats = supabase_client.get_scheduler_heartbeats()
                watcher_hb = next(
                    (hb for hb in heartbeats if hb.get("scheduler_name") == "transcript_watcher"),
                    None,
                )
                if watcher_hb:
                    last_beat = str(watcher_hb.get("last_heartbeat", ""))[:19].replace("T", " ")
                    hb_status = watcher_hb.get("status", "unknown")
                    lines.append(f"\n*Transcript Watcher:* {hb_status} (last: {last_beat})")
                else:
                    lines.append("\n*Transcript Watcher:* no heartbeat yet")
            except Exception:
                pass

            # T2.1 — Rejected meetings (should always be 0 after Tier 1)
            try:
                rejected_meetings = supabase_client.list_meetings(
                    approval_status="rejected", limit=10
                )
                if rejected_meetings:
                    lines.append(
                        f"\n*Rejected meetings with orphan data:* {len(rejected_meetings)} "
                        f"(run scripts/cleanup_rejected_meetings.py)"
                    )
                else:
                    lines.append("\n*Rejected meetings:* 0 (clean)")
            except Exception:
                pass

            # T2.1 — Errors in last 24h from audit_log
            try:
                from datetime import datetime as _dt, timedelta as _td
                yesterday = (_dt.now() - _td(days=1)).isoformat()
                errors_result = (
                    supabase_client.client.table("audit_log")
                    .select("action, details, created_at")
                    .in_("action", [
                        "critical_error",
                        "watcher_error",
                        "reminder_scheduler_error",
                    ])
                    .gte("created_at", yesterday)
                    .order("created_at", desc=True)
                    .limit(5)
                    .execute()
                )
                errors = errors_result.data or []
                if errors:
                    lines.append(f"\n*Errors (24h):* {len(errors)}")
                    for err in errors[:3]:
                        action = err.get("action", "")
                        details = err.get("details", {})
                        if isinstance(details, dict):
                            msg = str(details.get("error", details.get("message", "")))[:60]
                        else:
                            msg = str(details)[:60]
                        lines.append(f"  - {action}: {msg}")
                else:
                    lines.append("\n*Errors (24h):* 0")
            except Exception:
                pass

            await self.send_message(update.effective_chat.id, "\n".join(lines))

        except Exception as e:
            logger.error(f"Error in /status command: {e}")
            await self.send_message(
                update.effective_chat.id,
                f"Error fetching status: {e}",
            )

    async def _handle_myid(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """
        Handle /myid command.

        Returns the user's Telegram chat ID so it can be used
        in the TELEGRAM_EYAL_CHAT_ID configuration.
        """
        chat_id = update.effective_chat.id
        user = update.effective_user
        await self.send_message(
            chat_id,
            f"Your chat ID: {chat_id}\n"
            f"User ID: {user.id}\n"
            f"Username: @{user.username or 'N/A'}\n\n"
            f"To use this for TELEGRAM_EYAL_CHAT_ID, "
            f"set it to: {chat_id}",
            parse_mode=None,
        )

    async def _handle_help(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """
        Handle /help command.

        Lists available commands and how to interact.
        """
        help_message = """*Gianluigi - Help*

*Commands:*
/start - Welcome message
/help - This help message
/tasks - Show your open tasks
/mytasks - Same as /tasks
/search [query] - Search meetings and documents
/search -m [query] - Search meetings only
/search -d [query] - Search documents only
/meetings - List recent meetings
/meetings [title] - Search meetings by title
/decisions - List recent key decisions
/questions - List open questions
/reprocess [title] - Reprocess a transcript (Eyal only)
/debrief - Start end-of-day debrief (Eyal only)
/cancel - Cancel active debrief (Eyal only)
/emailscan - Trigger email scan + morning brief (Eyal only)
/cost - API token usage summary (Eyal only)
/status - System dashboard (Eyal only)

*Ask Questions:*
Just type your question and I'll search our meeting history to answer it.

Examples:
- "What did we decide about cloud providers?"
- "What are Roye's pending tasks?"
- "Summarize last week's meetings"

*For Eyal:*
When you receive approval requests, use the buttons to approve, request changes, or reject. You can also reply with edit instructions.
"""
        await self.send_message(update.effective_chat.id, help_message)

    async def _handle_tasks(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """
        Handle /tasks command.

        Shows the user's open tasks.
        """
        user = update.effective_user
        user_id = self._get_user_id(user.id)

        # Import here to avoid circular imports
        from services.supabase_client import supabase_client

        if user_id:
            member = get_team_member(user_id)
            assignee_name = member["name"] if member else None
            tasks = supabase_client.get_tasks(
                assignee=assignee_name,
                status="pending"
            )
        else:
            # Show all tasks if user not identified
            tasks = supabase_client.get_tasks(status="pending")

        if not tasks:
            message = "No open tasks found."
        else:
            message = "*Open Tasks:*\n\n"
            for i, task in enumerate(tasks[:10], 1):
                priority = task.get("priority", "M")
                priority_indicator = {"H": "!!", "M": "", "L": "~"}.get(priority, "")
                deadline = task.get("deadline", "No deadline")
                assignee = task.get("assignee", "Unassigned")

                message += f"{i}. {priority_indicator}{task.get('title', 'Untitled')}\n"
                message += f"   Assignee: {assignee} | Due: {deadline}\n\n"

            if len(tasks) > 10:
                message += f"\n_...and {len(tasks) - 10} more tasks_"

        await self.send_message(update.effective_chat.id, message)

    async def _handle_search(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """
        Handle /search command.

        Searches meetings and/or documents for a topic.
        Supports flags: -m (meetings only), -d (documents only).
        """
        if not context.args:
            await self.send_message(
                update.effective_chat.id,
                "Usage: /search [query]\n"
                "  /search -m [query]  — meetings only\n"
                "  /search -d [query]  — documents only\n\n"
                "Example: /search cloud providers"
            )
            return

        # Parse flags
        args = list(context.args)
        source_filter = None  # None = both
        if args[0] == "-m":
            source_filter = "meeting"
            args = args[1:]
        elif args[0] == "-d":
            source_filter = "document"
            args = args[1:]

        if not args:
            await self.send_message(
                update.effective_chat.id,
                "Please provide a search query after the flag."
            )
            return

        query = " ".join(args)

        await self.send_message(
            update.effective_chat.id,
            f"Searching for: _{query}_..."
        )

        from services.supabase_client import supabase_client
        from services.embeddings import embedding_service

        try:
            # Embed the query for semantic search
            query_embedding = await embedding_service.embed_text(query)
            results = supabase_client.search_embeddings(
                query_embedding=query_embedding,
                limit=10,
                source_type=source_filter,
            )

            if not results:
                await self.send_message(
                    update.effective_chat.id,
                    f"No results found for: {query}"
                )
                return

            # Deduplicate by source_id and take top 3
            seen = set()
            top_results = []
            for r in results:
                sid = r.get("source_id")
                if sid not in seen:
                    seen.add(sid)
                    top_results.append(r)
                if len(top_results) >= 3:
                    break

            # Format results
            lines = [f"*Search Results for:* _{_escape_markdown(query)}_\n"]
            for i, r in enumerate(top_results, 1):
                source_type = r.get("source_type", "unknown")
                chunk = r.get("chunk_text", "")[:200]
                similarity = r.get("similarity", 0)

                # Try to get source title
                source_id = r.get("source_id", "")
                if source_type == "meeting":
                    meeting = supabase_client.get_meeting(source_id)
                    title = meeting.get("title", "Unknown") if meeting else "Unknown"
                    date = (meeting.get("date", "")[:10] if meeting else "")
                    lines.append(f"*{i}. {_escape_markdown(title)}* ({date})")
                else:
                    doc = supabase_client.get_document(source_id)
                    title = doc.get("title", "Unknown") if doc else "Unknown"
                    lines.append(f"*{i}. {_escape_markdown(title)}* (document)")

                lines.append(f"  _{_escape_markdown(chunk)}_...")
                lines.append("")

            await self.send_message(update.effective_chat.id, "\n".join(lines))

        except Exception as e:
            logger.error(f"Error in /search: {e}")
            # Fall back to agent-based search
            from core.agent import gianluigi_agent
            user_id = self._get_user_id(update.effective_user.id) or "unknown"
            result = await gianluigi_agent.process_message(
                user_message=f"Search our meeting history for: {query}",
                user_id=user_id,
            )
            await self.send_message(
                update.effective_chat.id,
                result.get("response", "No results found.")
            )

    async def _handle_decisions(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """
        Handle /decisions command.

        Lists recent decisions.
        """
        from services.supabase_client import supabase_client

        decisions = supabase_client.list_decisions()[:10]

        if not decisions:
            message = "No decisions recorded yet."
        else:
            message = "*Recent Decisions:*\n\n"
            for i, decision in enumerate(decisions, 1):
                desc = decision.get("description", "")[:100]
                timestamp = decision.get("transcript_timestamp", "")
                message += f"{i}. {desc}\n   _(ref: ~{timestamp})_\n\n"

        await self.send_message(update.effective_chat.id, message)

    async def _handle_questions(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """
        Handle /questions command.

        Lists open questions.
        """
        from services.supabase_client import supabase_client

        questions = supabase_client.list_open_questions(status="open")[:10]

        if not questions:
            message = "No open questions at the moment."
        else:
            message = "*Open Questions:*\n\n"
            for i, q in enumerate(questions, 1):
                question = q.get("question", "")[:100]
                raised_by = q.get("raised_by", "Unknown")
                message += f"{i}. {question}\n   _Raised by: {raised_by}_\n\n"

        await self.send_message(update.effective_chat.id, message)

    async def _handle_retract(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """
        Handle /retract command — undo the last auto-published summary.

        Only works for Eyal (admin). Finds the most recently approved
        meeting and reverts its status to 'retracted'.
        """
        user = update.effective_user
        if not self._is_admin(user.id):
            await self.send_message(
                update.effective_chat.id,
                "Only Eyal can retract published summaries."
            )
            return

        # Find the most recently approved meeting
        from services.supabase_client import supabase_client

        meetings = supabase_client.list_meetings(
            approval_status="approved", limit=1
        )

        if not meetings:
            await self.send_message(
                update.effective_chat.id,
                "No recently approved summaries to retract."
            )
            return

        meeting = meetings[0]
        meeting_id = meeting.get("id")
        title = meeting.get("title", "Unknown")

        # Revert to retracted
        supabase_client.update_meeting(
            meeting_id,
            approval_status="retracted",
        )

        supabase_client.log_action(
            action="summary_retracted",
            details={"meeting_id": meeting_id, "title": title},
            triggered_by="eyal",
        )

        await self.send_message(
            update.effective_chat.id,
            f"Retracted summary for: *{title}*\n\n"
            f"The summary has been unpublished. Team notifications cannot be unsent "
            f"but the Drive document status has been updated."
        )

    async def _handle_reprocess(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
    ) -> None:
        """
        Handle /reprocess command — re-run transcript processing.

        Admin-only (Eyal). Deletes old meeting data and reprocesses the file.
        Usage:
            /reprocess            — list 10 recent meetings
            /reprocess <title>    — search by partial title
        """
        user = update.effective_user
        if not self._is_admin(user.id):
            await self.send_message(
                update.effective_chat.id,
                "Only Eyal can reprocess transcripts.",
            )
            return

        from services.supabase_client import supabase_client
        from schedulers.transcript_watcher import transcript_watcher

        # Parse args — everything after /reprocess
        args = " ".join(context.args) if context.args else ""

        if not args:
            # No args — list 10 recent meetings
            meetings = supabase_client.list_meetings(limit=10)
            if not meetings:
                await self.send_message(
                    update.effective_chat.id,
                    "No meetings found in the database.",
                )
                return

            lines = ["*Recent Meetings:*\n"]
            for m in meetings:
                date = m.get("date", "")[:10]
                title = m.get("title", "Unknown")
                lines.append(f"• {date} — {title}")
            lines.append("\nUse `/reprocess <title>` to reprocess a specific meeting.")
            await self.send_message(update.effective_chat.id, "\n".join(lines))
            return

        # Search by title
        matches = supabase_client.search_meetings_by_title(args)

        if not matches:
            await self.send_message(
                update.effective_chat.id,
                f"No meetings found matching '{args}'.",
            )
            return

        if len(matches) > 1:
            lines = [f"Multiple matches for '{args}':\n"]
            for m in matches:
                date = m.get("date", "")[:10]
                title = m.get("title", "Unknown")
                lines.append(f"• {date} — {title}")
            lines.append("\nPlease be more specific.")
            await self.send_message(update.effective_chat.id, "\n".join(lines))
            return

        # Single match — reprocess
        meeting = matches[0]
        meeting_id = meeting["id"]
        title = meeting.get("title", "Unknown")
        source_path = meeting.get("source_file_path", "")

        # Escape underscores in title for Telegram markdown
        safe_title = title.replace("_", "\\_")

        await self.send_message(
            update.effective_chat.id,
            f"Reprocessing: *{safe_title}*\nLooking for source file...",
        )

        # Find the Drive file by source_file_path
        from services.google_drive import drive_service

        if source_path and settings.RAW_TRANSCRIPTS_FOLDER_ID:
            files = await drive_service.list_files_in_folder(
                settings.RAW_TRANSCRIPTS_FOLDER_ID,
            )
            # Match by filename
            source_name = source_path.split("/")[-1] if "/" in source_path else source_path
            drive_file = next(
                (f for f in files if source_name.lower() in f.get("name", "").lower()),
                None,
            )

            if drive_file:
                result = await transcript_watcher.reprocess_file(drive_file["id"])
                status = result.get("status", "unknown")
                deleted = result.get("deleted_old", {})
                if deleted:
                    msg = (
                        f"Reprocess complete: *{safe_title}*\n"
                        f"Status: {status}\n"
                        f"Deleted: {deleted.get('embeddings', 0)} embeddings, "
                        f"{deleted.get('tasks', 0)} tasks"
                    )
                else:
                    msg = f"Reprocess complete: *{safe_title}*\nStatus: {status}"
                await self.send_message(update.effective_chat.id, msg)
                return

        # File not found in Drive — cascade delete and ask to re-upload
        deleted = supabase_client.delete_meeting_cascade(meeting_id)
        await self.send_message(
            update.effective_chat.id,
            f"Source file not found in Drive.\n"
            f"Deleted old data for *{safe_title}*: "
            f"{deleted.get('embeddings', 0)} embeddings, "
            f"{deleted.get('tasks', 0)} tasks.\n\n"
            f"Please re-upload the transcript file to the raw transcripts folder.",
        )

    # =========================================================================
    # Debrief Handlers
    # =========================================================================

    async def _handle_debrief(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
    ) -> None:
        """Handle /debrief command — start or resume a debrief session."""
        # Immediate ack — if anything below this raises, Eyal at least sees
        # that the handler fired (debugging the "nothing happens" symptom
        # reported 2026-04-11).
        chat_id = update.effective_chat.id
        try:
            await self.send_message(chat_id, "Starting debrief...", parse_mode=None)
        except Exception as ack_err:
            logger.error(f"Debrief ack send failed: {ack_err}", exc_info=True)

        try:
            user = update.effective_user
            is_eyal = str(user.id) == str(self.eyal_chat_id)

            if not is_eyal:
                await self.send_message(
                    chat_id,
                    "Only Eyal can use the debrief feature.",
                )
                return

            # Session lock check — debrief can interrupt weekly review (push onto stack)
            active = self._active_interactive_session
            if active and active != "debrief" and active != "weekly_review":
                await self.send_message(
                    chat_id,
                    f"Another interactive session ({active}) is active. "
                    f"Finish or /cancel it first.",
                )
                return

            if active == "weekly_review":
                await self.send_message(
                    chat_id,
                    "Pausing weekly review to start debrief. "
                    "You can resume the review when the debrief is done.",
                    parse_mode=None,
                )

            # Surface pending approvals before debrief (wrapped — a stale DB
            # row or a schema drift here should NOT kill the whole handler).
            try:
                pending = supabase_client.get_pending_approval_summary()
                if pending:
                    pending_note = (
                        f"Heads up: {len(pending)} approval(s) pending. "
                        f"Use /status to review.\n\n"
                    )
                    await self.send_message(chat_id, pending_note, parse_mode=None)
            except Exception as pend_err:
                logger.warning(f"Debrief pending-approvals preview failed: {pend_err}")

            self._active_interactive_session = "debrief"
            from processors.debrief import start_debrief

            result = await start_debrief(user_id="eyal")
            response = result.get("response", "Starting debrief...")
            session_id = result.get("session_id")
        except Exception as fatal:
            logger.error(
                f"_handle_debrief fatal error: {fatal}", exc_info=True
            )
            # Surface the error to Eyal so we can actually see why /debrief dies.
            try:
                await self.send_message(
                    chat_id,
                    f"Debrief failed to start: `{type(fatal).__name__}: {fatal}`\n"
                    f"Check main.py logs for the traceback.",
                    parse_mode="Markdown",
                )
            except Exception:
                pass
            # Release the session lock so we don't wedge the bot
            self._active_interactive_session = None
            return

        # Store session ID in context as cache
        if session_id:
            context.user_data["debrief_session_id"] = session_id

        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton(
                "Finish debrief",
                callback_data=f"debrief_finish:{session_id}",
            )]
        ])

        await self.send_message(
            update.effective_chat.id,
            response,
            parse_mode=None,
            reply_markup=keyboard,
        )

    async def _handle_cancel_debrief(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
    ) -> None:
        """Handle /cancel command — cancel an active debrief session."""
        user = update.effective_user
        is_eyal = str(user.id) == str(self.eyal_chat_id)

        if not is_eyal:
            await self.send_message(
                update.effective_chat.id,
                "Only Eyal can cancel a debrief.",
            )
            return

        from services.supabase_client import supabase_client
        active_session = supabase_client.get_active_debrief_session()
        if not active_session:
            await self.send_message(
                update.effective_chat.id,
                "No active debrief session to cancel.",
            )
            return

        supabase_client.update_debrief_session(
            active_session["id"], status="cancelled"
        )
        context.user_data.pop("debrief_session_id", None)
        context.user_data.pop("debrief_editing", None)
        self._active_interactive_session = None
        await self.send_message(
            update.effective_chat.id,
            "Debrief cancelled. You can start a new one anytime with /debrief.",
        )

    # =========================================================================
    # Weekly Review Handlers
    # =========================================================================

    async def _handle_sync(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
    ) -> None:
        """Handle /sync command — compare Sheets edits against DB and apply."""
        user = update.effective_user
        is_eyal = str(user.id) == str(self.eyal_chat_id)

        if not is_eyal:
            await self.send_message(
                update.effective_chat.id,
                "Only Eyal can sync Sheets.",
            )
            return

        await self.send_message(
            update.effective_chat.id, "Computing Sheets diff..."
        )

        try:
            from processors.sheets_sync import (
                compute_sheets_diff,
                format_diff_preview,
                apply_sheets_to_db,
            )

            diff = await compute_sheets_diff()

            if not diff.get("has_changes"):
                await self.send_to_eyal("Sheets and DB are in sync. No changes needed.")
                return

            # Store diff for approval callback
            context.user_data["pending_sync_diff"] = diff

            preview = format_diff_preview(diff)
            keyboard = [
                [
                    InlineKeyboardButton("Apply changes", callback_data="sync_apply:confirm"),
                    InlineKeyboardButton("Cancel", callback_data="sync_apply:cancel"),
                ],
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)
            await self.send_to_eyal(preview, reply_markup=reply_markup, parse_mode="HTML")

        except Exception as e:
            logger.error(f"Sheets sync failed: {e}")
            await self.send_to_eyal(f"Sheets sync error: {e}")

    async def _handle_review(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
    ) -> None:
        """Handle /review command — start or resume a weekly review session."""
        user = update.effective_user
        is_eyal = str(user.id) == str(self.eyal_chat_id)

        if not is_eyal:
            await self.send_message(
                update.effective_chat.id,
                "Only Eyal can use the weekly review feature.",
            )
            return

        # Allow debrief to interrupt review (push onto stack)
        # But don't start review if debrief is active
        if self._active_interactive_session == "debrief":
            await self.send_message(
                update.effective_chat.id,
                "A debrief is currently active. Finish or /cancel it first, "
                "then start the weekly review.",
            )
            return

        # Parse --fresh flag to force recompilation
        args = (update.message.text or "").split()
        force_fresh = "--fresh" in args

        # Suggest Claude.ai as primary interface, offer Telegram as fallback
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton(
                "Continue here in Telegram",
                callback_data=f"review_start_telegram:{'fresh' if force_fresh else 'normal'}",
            )],
        ])
        await self.send_message(
            update.effective_chat.id,
            "The weekly review works best in Claude.ai (CropSight Ops project) "
            "where you can have a natural conversation.\n\n"
            "Want to continue here in Telegram instead?",
            parse_mode=None,
            reply_markup=keyboard,
        )
        return
        response = result.get("response", "Starting weekly review...")
        session_id = result.get("session_id")

        if not session_id:
            # Session creation failed — don't push onto stack
            await self.send_message(
                update.effective_chat.id,
                response,
                parse_mode=None,
            )
            return

        self._session_stack.append("weekly_review")
        context.user_data["review_session_id"] = session_id

        keyboard = self._get_review_navigation_keyboard(
            session_id or "", result.get("current_part", 1)
        )

        await self.send_message(
            update.effective_chat.id,
            response,
            parse_mode="HTML",
            reply_markup=keyboard,
        )

    async def _handle_review_message(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
    ) -> None:
        """Handle a message during an active weekly review session."""
        from processors.weekly_review_session import process_review_message
        from services.supabase_client import supabase_client

        message_text = update.message.text
        chat_id = update.effective_chat.id

        session_id = context.user_data.get("review_session_id")
        if not session_id:
            # Try DB lookup
            active = supabase_client.get_active_weekly_review_session()
            if active:
                session_id = active["id"]
                context.user_data["review_session_id"] = session_id
            else:
                self._active_interactive_session = None
                return

        result = await process_review_message(
            session_id=session_id,
            user_message=message_text,
            user_id="eyal",
        )

        response = result.get("response", "")
        action = result.get("action", "")

        if action in ("session_expired", "review_ended", "error"):
            context.user_data.pop("review_session_id", None)
            self._active_interactive_session = None
            await self.send_message(chat_id, response, parse_mode="HTML")
            return

        keyboard = self._get_review_navigation_keyboard(
            session_id, result.get("current_part", 1)
        )

        await self.send_message(
            chat_id, response, parse_mode="HTML", reply_markup=keyboard,
        )

    def _get_review_navigation_keyboard(
        self, session_id: str, current_part: int
    ) -> InlineKeyboardMarkup:
        """Build navigation keyboard for weekly review.

        Layout per part:
        Part 1: [Continue >>]           / [End review]
        Part 2: [<< Back] [Continue >>] / [End review]
        Part 3: [<< Back]              / [Generate Outputs] / [End review]
        """
        rows = []

        # Row 1: Navigation
        nav = []
        if current_part > 1:
            nav.append(InlineKeyboardButton(
                "<< Back",
                callback_data=f"review_back:{session_id}",
            ))
        if current_part < 3:
            nav.append(InlineKeyboardButton(
                "Continue >>",
                callback_data=f"review_next:{session_id}",
            ))
        if nav:
            rows.append(nav)

        # Row 2: Action (Part 3 only)
        if current_part == 3:
            rows.append([InlineKeyboardButton(
                "Generate Outputs",
                callback_data=f"review_finalize:{session_id}",
            )])

        # Row 3: End review (always available)
        rows.append([InlineKeyboardButton(
            "End review",
            callback_data=f"review_end:{session_id}",
        )])

        return InlineKeyboardMarkup(rows)

    async def _handle_review_callback(
        self,
        query,
        context: ContextTypes.DEFAULT_TYPE,
        action: str,
        session_id: str,
    ) -> None:
        """Handle weekly review inline button callbacks."""
        if str(query.from_user.id) != str(self.eyal_chat_id):
            await query.answer("Only Eyal can use review controls.", show_alert=True)
            return

        from processors.weekly_review_session import (
            advance_to_part,
            finalize_review,
            confirm_review,
        )
        from services.supabase_client import supabase_client

        chat_id = query.message.chat_id

        # Handle "Continue here in Telegram" redirect button
        if action == "review_start_telegram":
            await query.answer()
            force_fresh = session_id == "fresh"  # session_id carries the flag here

            from processors.weekly_review_session import start_weekly_review

            await self.send_message(chat_id, "Starting weekly review...", parse_mode=None)
            result = await start_weekly_review(user_id="eyal", force_fresh=force_fresh)
            response = result.get("response", "Starting weekly review...")
            sid = result.get("session_id")

            if not sid:
                await self.send_message(chat_id, response, parse_mode=None)
                return

            self._session_stack.append("weekly_review")
            context.user_data["review_session_id"] = sid

            keyboard = self._get_review_navigation_keyboard(
                sid, result.get("current_part", 1)
            )
            await self.send_message(chat_id, response, parse_mode="HTML", reply_markup=keyboard)
            return

        if action == "review_next":
            session = supabase_client.get_weekly_review_session(session_id)
            current = session.get("current_part", 1) if session else 1
            next_part = min(current + 1, 3)
            result = await advance_to_part(session_id, next_part)
            keyboard = self._get_review_navigation_keyboard(session_id, next_part)
            await query.edit_message_text(
                result.get("response", ""),
                parse_mode="HTML",
                reply_markup=keyboard,
            )

        elif action == "review_back":
            session = supabase_client.get_weekly_review_session(session_id)
            current = session.get("current_part", 1) if session else 1
            prev_part = max(current - 1, 1)
            result = await advance_to_part(session_id, prev_part)
            keyboard = self._get_review_navigation_keyboard(session_id, prev_part)
            await query.edit_message_text(
                result.get("response", ""),
                parse_mode="HTML",
                reply_markup=keyboard,
            )

        elif action == "review_finalize":
            await query.edit_message_text("Generating outputs...")
            result = await finalize_review(session_id)
            keyboard = InlineKeyboardMarkup([
                [
                    InlineKeyboardButton(
                        "Approve & Distribute",
                        callback_data=f"review_approve:{session_id}",
                    ),
                    InlineKeyboardButton(
                        "Edit",
                        callback_data=f"review_correct:{session_id}",
                    ),
                ],
                [
                    InlineKeyboardButton(
                        "Cancel",
                        callback_data=f"review_reject:{session_id}",
                    ),
                ],
            ])
            await self.send_message(
                chat_id,
                result.get("response", "Outputs ready."),
                parse_mode="HTML",
                reply_markup=keyboard,
            )

        elif action == "review_approve":
            await query.edit_message_text("Approved! Distributing...")
            result = await confirm_review(session_id, approved=True)

            if result.get("action") == "gantt_failed":
                keyboard = InlineKeyboardMarkup([
                    [
                        InlineKeyboardButton(
                            "Distribute anyway",
                            callback_data=f"review_force_distribute:{session_id}",
                        ),
                        InlineKeyboardButton(
                            "Hold",
                            callback_data=f"review_hold:{session_id}",
                        ),
                    ],
                ])
                await self.send_message(
                    chat_id,
                    result.get("response", "Gantt update failed."),
                    parse_mode="HTML",
                    reply_markup=keyboard,
                )
            else:
                context.user_data.pop("review_session_id", None)
                self._active_interactive_session = None

                # Offer debrief resume if review was interrupted
                await self.send_message(
                    chat_id,
                    result.get("response", "Review approved."),
                    parse_mode="HTML",
                )

        elif action == "review_reject":
            result = await confirm_review(session_id, approved=False)
            context.user_data.pop("review_session_id", None)
            self._active_interactive_session = None
            await query.edit_message_text("Weekly review cancelled.")

        elif action == "review_correct":
            context.user_data["review_correcting"] = session_id
            await query.edit_message_text(
                "Send your corrections (e.g., 'Change the title', "
                "'Remove the funding section', 'Add a note about the MVP deadline')."
            )

        elif action == "review_end":
            supabase_client.update_weekly_review_session(
                session_id, status="cancelled"
            )
            context.user_data.pop("review_session_id", None)
            self._active_interactive_session = None
            await query.edit_message_text("Weekly review ended.")

        elif action == "review_force_distribute":
            # Force distribute without Gantt
            result = await confirm_review(session_id, approved=True)
            context.user_data.pop("review_session_id", None)
            self._active_interactive_session = None
            await self.send_message(
                chat_id,
                result.get("response", "Distributed."),
                parse_mode="HTML",
            )

        elif action == "review_hold":
            await query.edit_message_text(
                "Distribution held. Fix the Gantt issue and try again."
            )

        elif action == "review_resume_after_debrief":
            from processors.weekly_review_session import resume_after_debrief
            await query.edit_message_text("Resuming weekly review with refreshed data...")
            result = await resume_after_debrief(session_id)
            keyboard = self._get_review_navigation_keyboard(
                session_id, result.get("current_part", 1)
            )
            await self.send_message(
                chat_id,
                result.get("response", "Resumed."),
                parse_mode="HTML",
                reply_markup=keyboard,
            )

    async def _handle_email_scan(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
    ) -> None:
        """Handle /emailscan — manually trigger email scan + morning brief."""
        user = update.effective_user
        is_eyal = str(user.id) == str(self.eyal_chat_id)

        if not is_eyal:
            await self.send_message(
                update.effective_chat.id,
                "Only Eyal can trigger email scans.",
            )
            return

        from datetime import date
        from services.supabase_client import supabase_client

        # Rate limiting: check if already scanned today
        last_scan = supabase_client.get_last_scan_date(scan_type="daily")
        today_str = date.today().isoformat()

        if last_scan and last_scan == today_str:
            force = context.user_data.get("emailscan_force", False)
            if not force:
                context.user_data["emailscan_force"] = True
                await self.send_message(
                    update.effective_chat.id,
                    "Already scanned today. Send /emailscan again to force re-scan.",
                )
                return
            # Reset force flag
            context.user_data.pop("emailscan_force", None)

        await self.send_message(
            update.effective_chat.id,
            "Running email scan + morning brief...",
        )

        try:
            from processors.morning_brief import trigger_morning_brief
            result = await trigger_morning_brief()
            if result:
                await self.send_message(
                    update.effective_chat.id,
                    "Email scan complete. Check above for the morning brief.",
                )
            else:
                await self.send_message(
                    update.effective_chat.id,
                    "Email scan complete. Nothing new to report.",
                )
        except Exception as e:
            logger.error(f"Email scan failed: {e}")
            await self.send_message(
                update.effective_chat.id,
                f"Email scan failed: {e}",
            )

    async def _handle_debrief_message(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
    ) -> None:
        """Handle a message during an active debrief session."""
        from processors.debrief import (
            process_debrief_message,
            finalize_debrief,
            is_done_signal,
        )
        from services.supabase_client import supabase_client

        message_text = update.message.text
        chat_id = update.effective_chat.id

        # Check if in edit mode for debrief
        editing_session = context.user_data.get("debrief_editing")
        if editing_session:
            from processors.debrief import edit_debrief_items

            context.user_data.pop("debrief_editing", None)
            result = await edit_debrief_items(
                session_id=editing_session,
                edit_instruction=message_text,
                user_id="eyal",
            )
            response = result.get("response", "Items updated.")
            session_id = result.get("session_id", editing_session)

            # Show Approve/Edit/Cancel buttons
            keyboard = InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("Approve", callback_data=f"debrief_approve:{session_id}"),
                    InlineKeyboardButton("Edit", callback_data=f"debrief_edit:{session_id}"),
                    InlineKeyboardButton("Cancel", callback_data=f"debrief_reject:{session_id}"),
                ]
            ])
            await self.send_message(chat_id, response, parse_mode=None, reply_markup=keyboard)
            return

        # Get active session
        active_session = supabase_client.get_active_debrief_session()
        if not active_session:
            # Session gone — fall through to normal agent processing
            context.user_data.pop("debrief_session_id", None)
            logger.warning("Debrief session disappeared mid-conversation, routing to normal agent")
            from core.agent import gianluigi_agent
            result = await gianluigi_agent.process_message(
                user_message=message_text,
                user_id="eyal",
            )
            await self.send_message(
                chat_id,
                result.get("response", "I couldn't process your request."),
            )
            return

        session_id = active_session["id"]

        # Check for done signal (text-secondary)
        if is_done_signal(message_text):
            await self.send_message(chat_id, "Finalizing your debrief...")
            result = await finalize_debrief(session_id)
            await self._send_debrief_confirmation(chat_id, result)
            return

        # Process the debrief message
        await self.send_message(chat_id, "Processing...")

        result = await process_debrief_message(
            session_id=session_id,
            user_message=message_text,
            user_id="eyal",
        )

        response = result.get("response", "Got it.")

        # Include "Finish debrief" button on every response
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton(
                "Finish debrief",
                callback_data=f"debrief_finish:{session_id}",
            )]
        ])

        await self.send_message(
            chat_id, response, parse_mode=None, reply_markup=keyboard,
        )

    async def _send_debrief_confirmation(
        self,
        chat_id: int | str,
        result: dict,
    ) -> None:
        """Send debrief summary with Approve/Edit/Cancel buttons."""
        response = result.get("response", "No items captured.")
        session_id = result.get("session_id", "")
        action = result.get("action", "")

        if action == "debrief_cancelled" or not session_id:
            await self.send_message(chat_id, response, parse_mode=None)
            return

        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("Approve", callback_data=f"debrief_approve:{session_id}"),
                InlineKeyboardButton("Edit", callback_data=f"debrief_edit:{session_id}"),
                InlineKeyboardButton("Cancel", callback_data=f"debrief_reject:{session_id}"),
            ]
        ])

        await self.send_message(
            chat_id, response, parse_mode=None, reply_markup=keyboard,
        )

    async def _send_quick_injection_confirmation(
        self,
        chat_id: int | str,
        result: dict,
    ) -> None:
        """Send quick injection summary with Inject/Dismiss buttons."""
        from datetime import date as date_cls
        from services.supabase_client import supabase_client

        response = result.get("response", "")
        items = result.get("extracted_items", [])

        # Build summary
        summary_lines = [response, ""]
        for i, item in enumerate(items, 1):
            item_type = item.get("type", "info")
            title = item.get("title", item.get("description", ""))[:80]
            summary_lines.append(f"  {i}. [{item_type}] {title}")

        summary = "\n".join(summary_lines)

        # Store items in a temporary debrief session for callback retrieval.
        # Status="confirming" so it won't interfere with active debrief checks
        # (get_active_debrief_session only returns status="in_progress").
        today_str = date_cls.today().isoformat()
        session = supabase_client.create_debrief_session(today_str)
        supabase_client.update_debrief_session(
            session["id"],
            items_captured=items,
            status="confirming",
        )

        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("Inject", callback_data=f"inject_approve:{session['id']}"),
                InlineKeyboardButton("Dismiss", callback_data=f"inject_dismiss:{session['id']}"),
            ]
        ])

        await self.send_message(
            chat_id, summary, parse_mode=None, reply_markup=keyboard,
        )

    # =========================================================================
    # Message Handlers
    # =========================================================================

    async def _handle_message(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """
        Handle general text messages (queries).

        Runs inbound guardrails, then routes to Claude for processing.
        """
        user = update.effective_user
        message_text = update.message.text

        # Check if Eyal is in review mode (pending edit or replying to approval)
        is_eyal = str(user.id) == str(self.eyal_chat_id)

        # Phase 5: Check for active prep focus input (before debrief/review routing)
        if is_eyal:
            handled = await self._handle_prep_focus_input(update, context)
            if handled:
                return

        # Phase 3 (v2.2): Check if this is a reply to a task reminder message
        if is_eyal and update.message.reply_to_message:
            handled = await self._handle_task_reply(update, context)
            if handled:
                return

        # Phase 6: Check for active weekly review correction mode
        if is_eyal and context.user_data.get("review_correcting"):
            from processors.weekly_review_session import process_correction
            session_id = context.user_data.pop("review_correcting")
            result = await process_correction(session_id, message_text, "eyal")
            # Re-show approve/edit/cancel buttons after correction
            keyboard = InlineKeyboardMarkup([
                [
                    InlineKeyboardButton(
                        "Approve & Distribute",
                        callback_data=f"review_approve:{session_id}",
                    ),
                    InlineKeyboardButton(
                        "Edit more",
                        callback_data=f"review_correct:{session_id}",
                    ),
                ],
                [
                    InlineKeyboardButton(
                        "Cancel",
                        callback_data=f"review_reject:{session_id}",
                    ),
                ],
            ])
            await self.send_message(
                update.effective_chat.id,
                result.get("response", "Correction applied."),
                parse_mode="HTML",
                reply_markup=keyboard,
            )
            return

        # Phase 6: Check for active weekly review session
        if is_eyal and self._active_interactive_session == "weekly_review":
            # Intercept debrief intent — redirect to /debrief handler
            if message_text.strip().lower() in ("debrief", "/debrief", "start debrief"):
                await self._handle_debrief(update, context)
                return
            await self._handle_review_message(update, context)
            return

        # Check for active debrief session (Supabase = source of truth, survives restarts)
        if is_eyal:
            # Debrief edit mode takes priority (local cache only, no DB call)
            if context.user_data.get("debrief_editing"):
                await self._handle_debrief_message(update, context)
                return

            # Fast path: skip DB call if we already know there's no active session.
            # "debrief_session_id" is truthy (session ID) when active, False when
            # we've checked and found nothing. Missing key = first check needed.
            cached_session = context.user_data.get("debrief_session_id")
            if cached_session is not False:
                from services.supabase_client import supabase_client
                active_session = supabase_client.get_active_debrief_session()
                if active_session and active_session.get("status") == "in_progress":
                    context.user_data["debrief_session_id"] = active_session["id"]
                    await self._handle_debrief_message(update, context)
                    return
                else:
                    # No active session — cache negative to skip DB on next message
                    context.user_data["debrief_session_id"] = False

        has_pending_edit = bool(context.user_data.get("pending_edit_meeting_id"))
        if is_eyal and (update.message.reply_to_message or has_pending_edit):
            await self._handle_review_mode_message(update, context)
            return

        # Check if Eyal typed an approval/reject as free text (not via buttons)
        if is_eyal:
            lower_msg = message_text.strip().lower()
            is_approval_text = any(
                lower_msg.startswith(w)
                for w in ["approve", "approved", "reject", "rejected"]
            )
            if is_approval_text:
                from services.supabase_client import supabase_client
                from guardrails.approval_flow import process_response

                # Find the most recent pending approval
                pending = supabase_client.get_pending_approvals_by_status("pending")
                if pending:
                    latest = pending[0]
                    mid = latest.get("approval_id") or latest.get("id")
                    action = "approve" if "approve" in lower_msg else "reject"
                    await self.send_message(
                        update.effective_chat.id,
                        "Processing your approval..." if action == "approve"
                        else "Rejecting...",
                    )
                    try:
                        result = await process_response(
                            meeting_id=mid,
                            response=action,
                            response_source="telegram",
                        )
                        dist = result.get("distribution", {})
                        if action == "approve" and dist:
                            status_lines = ["Distribution complete:"]
                            if dist.get("drive_saved"):
                                status_lines.append("  - Saved to Google Drive")
                            if dist.get("sheets_updated"):
                                status_lines.append(
                                    f"  - {dist.get('tasks_added', 0)} tasks added to tracker"
                                )
                            if dist.get("telegram_sent"):
                                status_lines.append("  - Notification sent")
                            if dist.get("email_sent"):
                                status_lines.append("  - Email sent")
                            await self.send_to_eyal(
                                "\n".join(status_lines), parse_mode=None
                            )
                        else:
                            await self.send_to_eyal(
                                result.get("next_step", "Done."), parse_mode=None
                            )
                    except Exception as e:
                        logger.error(f"Error processing text-based approval: {e}")
                        await self.send_to_eyal(
                            f"Error processing approval: {e}", parse_mode=None
                        )
                    return

        # --- Inbound guardrails ---
        try:
            from guardrails.inbound_filter import check_inbound_message, sanitize_outbound_message

            chat_id = update.effective_chat.id
            channel = "telegram_dm" if chat_id > 0 else "telegram_group"

            check_result = await check_inbound_message(
                message=message_text,
                sender_id=str(user.id),
                channel=channel,
                telegram_user_id=user.id,
            )

            if not check_result.get("allowed", True):
                deflection = check_result.get(
                    "deflection_message",
                    "I can only help with CropSight-related work topics."
                )
                await self.send_message(chat_id, deflection)
                return
        except ImportError:
            pass  # inbound_filter not yet available — skip
        except Exception as e:
            logger.warning(f"Inbound filter error (continuing): {e}")

        # Regular query - process with Gianluigi agent
        await self.send_message(
            update.effective_chat.id,
            "Thinking..."
        )

        # Import here to avoid circular imports
        from core.agent import gianluigi_agent

        user_id = self._get_user_id(user.id) or "unknown"
        chat_id_str = str(update.effective_chat.id)

        # Get conversation history for context
        history = conversation_memory.get_history(chat_id_str)

        try:
            result = await gianluigi_agent.process_message(
                user_message=message_text,
                user_id=user_id,
                conversation_history=history,
            )

            # Handle quick injection confirmation
            if result.get("action") == "quick_injection_confirm":
                await self._send_quick_injection_confirmation(
                    update.effective_chat.id, result
                )
                return

            # Handle debrief actions (started/resumed via agent routing)
            if result.get("action") in ("debrief_started", "debrief_resumed", "debrief_message"):
                response = result.get("response", "")
                session_id = result.get("session_id", "")
                if session_id:
                    context.user_data["debrief_session_id"] = session_id
                    keyboard = InlineKeyboardMarkup([
                        [InlineKeyboardButton(
                            "Finish debrief",
                            callback_data=f"debrief_finish:{session_id}",
                        )]
                    ])
                    await self.send_message(
                        update.effective_chat.id, response,
                        parse_mode=None, reply_markup=keyboard,
                    )
                else:
                    await self.send_message(
                        update.effective_chat.id, response, parse_mode=None,
                    )
                return

            response = result.get("response", "I couldn't process your request.")

            # --- Outbound sanitization ---
            try:
                from guardrails.inbound_filter import sanitize_outbound_message
                chat_id = update.effective_chat.id
                channel = "telegram_dm" if chat_id > 0 else "telegram_group"
                response = sanitize_outbound_message(
                    response,
                    {"channel": channel, "recipient": user_id},
                )
            except ImportError:
                pass
            except Exception as e:
                logger.warning(f"Outbound sanitization error (continuing): {e}")

            # Store conversation turn in memory
            conversation_memory.add_message(chat_id_str, "user", message_text)
            conversation_memory.add_message(chat_id_str, "assistant", response)

            await self.send_message(update.effective_chat.id, response)

        except Exception as e:
            logger.error(f"Error processing message: {e}")
            await self.send_message(
                update.effective_chat.id,
                "Sorry, I encountered an error processing your request."
            )

    async def _on_handler_error(
        self,
        update: object,
        context: ContextTypes.DEFAULT_TYPE,
    ) -> None:
        """
        Global PTB error handler.

        Without this, exceptions raised inside command handlers are logged
        only to PTB's internal logger and disappear into stdout — which is
        why the /debrief silent failure on 2026-04-11 was invisible.

        Captures the error to our logger, persists it to audit_log, and
        DMs Eyal a short summary so he actually sees that something broke.
        """
        import traceback

        err = context.error
        tb_text = ""
        if err is not None:
            tb_text = "".join(
                traceback.format_exception(type(err), err, err.__traceback__)
            )

        # Identify which handler/update blew up
        update_kind = "unknown"
        cmd_text = ""
        try:
            if isinstance(update, Update):
                if update.message and update.message.text:
                    cmd_text = update.message.text[:100]
                    update_kind = "message"
                elif update.callback_query:
                    cmd_text = update.callback_query.data or ""
                    update_kind = "callback"
        except Exception:
            pass

        logger.error(
            f"PTB handler error [{update_kind}] on {cmd_text!r}: "
            f"{type(err).__name__ if err else 'None'}: {err}\n{tb_text}"
        )

        # Persist to audit_log so it's queryable from any session
        try:
            supabase_client.log_action(
                action="telegram_handler_error",
                details={
                    "update_kind": update_kind,
                    "trigger": cmd_text,
                    "error_type": type(err).__name__ if err else "None",
                    "error_msg": str(err)[:500] if err else "",
                    "traceback": tb_text[:2000],
                },
                triggered_by="auto",
            )
        except Exception as log_err:
            logger.error(f"Could not persist handler error to audit_log: {log_err}")

        # DM Eyal a short summary so he knows something failed
        if self.eyal_chat_id:
            try:
                summary = (
                    f"Handler error on {update_kind} `{cmd_text[:60]}`:\n"
                    f"`{type(err).__name__ if err else 'None'}: "
                    f"{str(err)[:200] if err else ''}`"
                )
                await self.app.bot.send_message(
                    chat_id=self.eyal_chat_id,
                    text=summary,
                    parse_mode="Markdown",
                )
            except Exception as dm_err:
                logger.error(f"Could not DM Eyal about handler error: {dm_err}")

    async def _handle_callback_query(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """
        Handle callback queries from inline buttons.

        Processes approval responses: approve triggers distribution,
        reject discards, edit prompts for instructions.
        """
        query = update.callback_query
        await query.answer()  # Acknowledge the callback

        data = query.data or ""
        if ":" not in data:
            logger.warning(f"Malformed callback data (no colon): {data!r}")
            await query.edit_message_text("Invalid button data. Please try again.")
            return

        action, meeting_id = data.split(":", 1)

        # Import here to avoid circular imports
        from services.supabase_client import supabase_client

        # ---- Prep outline callbacks (Phase 5) ----
        if action in ("prep_generate", "prep_focus", "prep_reclassify", "prep_skip"):
            await self._handle_prep_outline_callback(query, context, action, meeting_id)
            return

        if action == "prep_settype":
            # Format: prep_settype:approval_id:meeting_type
            parts = meeting_id.split(":", 1)
            if len(parts) == 2:
                await self._handle_prep_reclassify_callback(query, context, parts[0], parts[1])
            return

        # ---- Weekly review callbacks ----
        review_actions = (
            "review_next", "review_back", "review_finalize",
            "review_approve", "review_reject", "review_correct",
            "review_end", "review_force_distribute", "review_hold",
            "review_resume_after_debrief", "review_start_telegram",
        )
        if action in review_actions:
            await self._handle_review_callback(query, context, action, meeting_id)
            return

        # ---- Sheets sync callbacks (Phase 11 C7) ----
        if action == "sync_apply":
            if meeting_id == "confirm":
                diff = context.user_data.pop("pending_sync_diff", None)
                if not diff:
                    await query.edit_message_text("Sync session expired. Run /sync again.")
                    return
                from processors.sheets_sync import apply_sheets_to_db
                result = apply_sheets_to_db(diff)
                total = sum(result.values())
                lines = [f"Sync applied — {total} changes:"]
                if result.get("tasks_updated"):
                    lines.append(f"  • {result['tasks_updated']} tasks updated")
                if result.get("tasks_created"):
                    lines.append(f"  • {result['tasks_created']} tasks added")
                if result.get("decisions_updated"):
                    lines.append(f"  • {result['decisions_updated']} decisions updated")
                if result.get("decisions_created"):
                    lines.append(f"  • {result['decisions_created']} decisions added")
                await query.edit_message_text("\n".join(lines))
            else:
                context.user_data.pop("pending_sync_diff", None)
                await query.edit_message_text("Sync cancelled.")
            return

        # ---- Debrief callbacks ----
        if action in ("debrief_finish", "debrief_approve", "debrief_edit", "debrief_reject"):
            await self._handle_debrief_callback(query, context, action, meeting_id)
            return

        if action in ("inject_approve", "inject_dismiss"):
            await self._handle_inject_callback(query, context, action, meeting_id)
            return

        from guardrails.approval_flow import (
            distribute_approved_content,
            process_response,
        )

        if action == "approve":
            # Delete orphan multi-part messages (all except the button message)
            await self._cleanup_approval_parts(meeting_id, keep_message_id=query.message.message_id)
            await query.edit_message_text("Approved! Distributing to team...")
            conversation_memory.clear(str(self.eyal_chat_id))

            try:
                result = await process_response(
                    meeting_id=meeting_id,
                    response="approve",
                    response_source="telegram",
                )
                logger.info(f"Meeting {meeting_id} approved and distributed: {result}")

                # Notify Eyal of distribution result
                dist = result.get("distribution", {})
                status_lines = ["Distribution complete:"]
                if dist.get("drive_saved"):
                    status_lines.append("  - Saved to Google Drive")
                if dist.get("sheets_updated"):
                    status_lines.append(f"  - {dist.get('tasks_added', 0)} tasks added to tracker")
                if dist.get("stakeholders_updated"):
                    status_lines.append(f"  - {dist.get('stakeholders_added', 0)} stakeholders updated")
                if dist.get("telegram_sent"):
                    status_lines.append("  - Team notified via Telegram")
                if dist.get("email_sent"):
                    status_lines.append("  - Email sent to team")

                await self.send_to_eyal("\n".join(status_lines), parse_mode=None)

            except Exception as e:
                logger.error(f"Error distributing meeting {meeting_id}: {e}")
                await self.send_to_eyal(
                    f"Error during distribution: {e}\n\n"
                    f"The meeting was marked as approved but distribution failed. "
                    f"Please check the logs.",
                    parse_mode=None,
                )

        elif action == "reject":
            # Delete orphan multi-part messages (all except the button message)
            await self._cleanup_approval_parts(meeting_id, keep_message_id=query.message.message_id)

            # Delegate to process_response so both paths (Telegram + email) go
            # through the same cascading-reject logic. This is critical because
            # the old inline path only flipped approval_status and left tasks/
            # decisions/embeddings as orphans in the DB.
            await query.edit_message_text("Rejecting...")
            try:
                from guardrails.approval_flow import process_response
                result = await process_response(
                    meeting_id=meeting_id,
                    response="reject",
                    response_source="telegram",
                    force_action="reject",
                )
                confirmation = result.get("next_step") or "Rejected."
                await query.edit_message_text(confirmation)
                logger.info(f"Meeting {meeting_id} rejected by Eyal: {confirmation}")
            except Exception as e:
                logger.error(f"Reject delegation failed for {meeting_id}: {e}")
                try:
                    from services.alerting import send_system_alert, AlertSeverity
                    await send_system_alert(
                        AlertSeverity.CRITICAL,
                        "telegram_bot.reject_callback",
                        f"Reject handler failed for {meeting_id}: {e}",
                        error=e,
                    )
                except Exception as alert_err:
                    logger.error(f"Alert on reject failure also failed: {alert_err}")
                await query.edit_message_text(f"Reject failed: {e}")

            conversation_memory.clear(str(self.eyal_chat_id))

        elif action == "edit":
            await query.edit_message_text(
                "Please reply to this message with your edit instructions.\n\n"
                "Examples:\n"
                "- 'Change task 3 deadline to March 5'\n"
                "- 'Remove the second open question'\n"
                "- 'Add a decision about the budget'"
            )
            # Store meeting_id in context for edit handling
            context.user_data["pending_edit_meeting_id"] = meeting_id

        elif action == "sens_toggle":
            # Cycle sensitivity tier: founders → ceo → team → public → founders
            # T2.5: wrap DB write in try/except + CRITICAL alert so silent
            # failures cannot hide behind the UI cycling.
            from services.supabase_client import supabase_client as _sc
            from guardrails.sensitivity_classifier import propagate_meeting_sensitivity
            meeting = _sc.get_meeting(meeting_id)
            current_sens = meeting.get("sensitivity", "founders")
            # Normalize legacy values
            if current_sens in ("normal", "team"):
                current_sens = "founders"
            elif current_sens in ("sensitive", "ceo_only", "restricted", "legal"):
                current_sens = "ceo"

            tier_cycle = ["founders", "ceo", "team", "public"]
            current_idx = tier_cycle.index(current_sens) if current_sens in tier_cycle else 0
            new_sens = tier_cycle[(current_idx + 1) % len(tier_cycle)]

            try:
                _sc.update_meeting(meeting_id, sensitivity=new_sens)
                propagate_meeting_sensitivity(meeting_id, new_sens)
            except Exception as e:
                logger.error(f"Sensitivity toggle DB update failed for {meeting_id}: {e}")
                try:
                    from services.alerting import send_system_alert, AlertSeverity
                    await send_system_alert(
                        AlertSeverity.CRITICAL,
                        "telegram_bot.sens_toggle",
                        f"Sensitivity toggle DB write failed for {meeting_id}: {e}. "
                        f"UI may show {new_sens} but DB is unchanged.",
                        error=e,
                    )
                except Exception as alert_err:
                    logger.error(f"Alert on sens_toggle failure also failed: {alert_err}")
                await query.answer(f"Failed to update sensitivity: {e}")
                return

            # Update button text in-place
            tier_labels = {
                "public": "\U0001f30d PUBLIC \u2014 safe for anyone",
                "team": "\U0001f465 TEAM \u2014 all employees",
                "founders": "\U0001f465 FOUNDERS \u2014 founding team",
                "ceo": "\U0001f512 CEO \u2014 Eyal only",
            }
            new_label = tier_labels.get(new_sens, tier_labels["founders"])
            new_keyboard = [
                [
                    InlineKeyboardButton("Approve", callback_data=f"approve:{meeting_id}"),
                    InlineKeyboardButton("Request Changes", callback_data=f"edit:{meeting_id}"),
                ],
                [InlineKeyboardButton("Reject", callback_data=f"reject:{meeting_id}")],
                [InlineKeyboardButton(new_label, callback_data=f"sens_toggle:{meeting_id}")],
            ]
            await query.edit_message_reply_markup(
                reply_markup=InlineKeyboardMarkup(new_keyboard)
            )
            await query.answer(f"Sensitivity set to: {new_sens.upper()}")
            logger.info(f"Sensitivity cycled to '{new_sens}' for meeting {meeting_id}")

        elif action == "stakeholder_approve":
            org_key = meeting_id  # The part after the colon
            await query.edit_message_text(
                f"Approved stakeholder update for: {org_key}"
            )

            # Look up pending update and apply it
            supabase_client.log_action(
                action="stakeholder_approved",
                details={"organization": org_key},
                triggered_by="eyal",
            )
            logger.info(f"Stakeholder update approved: {org_key}")

        elif action == "stakeholder_reject":
            org_key = meeting_id  # The part after the colon
            await query.edit_message_text(
                f"Rejected stakeholder update for: {org_key}"
            )

            supabase_client.log_action(
                action="stakeholder_rejected",
                details={"organization": org_key},
                triggered_by="eyal",
            )
            logger.info(f"Stakeholder update rejected: {org_key}")

        # ── Phase 3: Task reminder inline actions ──────────────────
        elif action in ("taskdone", "taskdelay", "taskdiscuss"):
            await self._handle_task_action(query, action, meeting_id)

    async def _handle_task_action(
        self,
        query,
        action: str,
        callback_key: str,
    ) -> None:
        """
        Handle inline button press on an overdue task reminder.

        Actions: taskdone, taskdelay, taskdiscuss.

        callback_key may be:
        - A task UUID (new format from rev 52+) — does direct DB lookup
        - A short_id like "t12" (legacy format) — uses in-memory map

        Both paths converge on the same task_info dict with task_id, task_text,
        assignee, row_number, deadline.
        """
        from schedulers.task_reminder_scheduler import task_reminder_scheduler
        # Import without alias so the existing 'discuss' branch (which references
        # supabase_client by its bare name) keeps working.
        from services.supabase_client import supabase_client
        _sc = supabase_client  # local alias used by the cold-lookup code below

        # Detect format: UUID (36 chars with dashes) vs legacy short_id (e.g. "t12")
        is_uuid = len(callback_key) == 36 and callback_key.count("-") == 4

        task_info: dict | None = None

        if is_uuid:
            # New format — try in-memory cache first (faster), then fresh DB lookup
            task_info = task_reminder_scheduler.task_action_map.get(callback_key)
            if not task_info:
                # Cold lookup — fetch task from DB and synthesize task_info.
                # Note: intentionally NOT filtering approval_status — button
                # callbacks must work on pending tasks too (e.g., a task under
                # active review). The PK match on `id` guarantees uniqueness.
                # (Tier 3.1 narrow: audited, not load-bearing.)
                try:
                    db_task = _sc.client.table("tasks").select("*").eq("id", callback_key).limit(1).execute()
                    if db_task.data:
                        t = db_task.data[0]
                        # Look up Sheets row_number on demand
                        row_number = None
                        try:
                            from services.google_sheets import sheets_service
                            row_number = await sheets_service.find_task_row(t.get("title", ""))
                        except Exception as e:
                            logger.warning(f"Could not find Sheets row for task {callback_key}: {e}")
                        task_info = {
                            "task_id": callback_key,
                            "task_text": t.get("title", ""),
                            "assignee": t.get("assignee", ""),
                            "row_number": row_number,
                            "deadline": t.get("deadline", "") or "",
                        }
                        logger.info(f"Cold-resolved task action for {callback_key} (instance restart recovery)")
                except Exception as e:
                    logger.error(f"Cold lookup failed for task {callback_key}: {e}")
        else:
            # Legacy short_id format — only works on the same instance that sent the reminder
            task_info = task_reminder_scheduler.task_action_map.get(callback_key)

        if not task_info:
            # Loud failure — edit the message so Eyal sees the issue (not just an ephemeral toast)
            try:
                await query.edit_message_text(
                    "Task reminder expired (instance restarted). "
                    "Reply with 'done' / 'delay 7 days' / 'discuss' "
                    "or wait for the next reminder cycle."
                )
            except Exception:
                pass
            await query.answer("Reminder expired — see message")
            logger.warning(
                f"Task action for {callback_key!r} not found in map and not resolvable from DB"
            )
            return

        task_text = task_info.get("task_text", "Unknown task")

        if action == "taskdone":
            await self._execute_task_update_from_reminder(task_info, status="done")
            await query.edit_message_text(
                f"Done: {task_text[:60]}\n\nTask marked as complete. DB + Sheets updated."
            )
            await query.answer("Marked as done")
            logger.info(f"Task marked done via Telegram button: {task_text[:50]}")

        elif action == "taskdelay":
            new_deadline = await self._execute_task_update_from_reminder(
                task_info, delay_days=7
            )
            await query.edit_message_text(
                f"Delayed: {task_text[:60]}\n\nNew deadline: {new_deadline}. DB + Sheets updated."
            )
            await query.answer("Delayed by 1 week")
            logger.info(f"Task delayed +7 days via Telegram button: {task_text[:50]}")

        elif action == "taskdiscuss":
            await query.edit_message_text(
                f"Discuss: {task_text[:60]}\n\nAdded to next meeting agenda."
            )
            await query.answer("Will discuss in next meeting")
            # Look up the task's source meeting so the open_question is anchored
            # to it (create_open_question requires meeting_id).
            try:
                task_db_id = task_info.get("task_id")
                source_meeting_id = None
                if task_db_id:
                    lookup = (
                        supabase_client.client.table("tasks")
                        .select("meeting_id")
                        .eq("id", task_db_id)
                        .limit(1)
                        .execute()
                    )
                    if lookup.data:
                        source_meeting_id = lookup.data[0].get("meeting_id")
                if not source_meeting_id:
                    raise ValueError(
                        f"No source meeting_id for task {task_db_id} — cannot anchor open_question"
                    )
                supabase_client.create_open_question(
                    meeting_id=source_meeting_id,
                    question=f"Discuss overdue task: {task_text}",
                    raised_by="Eyal",
                )
                logger.info(f"Created discussion open_question for task: {task_text[:60]}")
            except Exception as e:
                logger.error(f"Failed to create discussion item: {e}")
                # Loud failure — prevent silent no-ops like the Tier 1 task button bugs
                try:
                    from services.alerting import send_system_alert, AlertSeverity
                    await send_system_alert(
                        AlertSeverity.CRITICAL,
                        "telegram_bot.task_discuss",
                        f"Failed to create open_question for discuss action on "
                        f"'{task_text[:60]}': {e}",
                        error=e,
                    )
                except Exception as alert_err:
                    logger.error(f"Alert on discuss failure also failed: {alert_err}")

    async def _execute_task_update_from_reminder(
        self,
        task_info: dict,
        status: str | None = None,
        delay_days: int | None = None,
    ) -> str | None:
        """
        Execute a task update from a reminder button or free-text reply.

        DB-first, Sheets-second. Prefers task_id (set by reminder scheduler
        via _resolve_db_task_id); falls back to title+assignee match for
        legacy reminders where task_id is None.

        If either DB or Sheets update fails, fires a CRITICAL system alert
        so silent failures become impossible.

        Returns:
            New deadline string if delay was applied, None otherwise.
        """
        task_text = task_info.get("task_text", "")
        assignee = task_info.get("assignee", "")
        row_number = task_info.get("row_number")
        task_id = task_info.get("task_id")

        new_deadline = None

        # Calculate new deadline if delaying
        if delay_days:
            from datetime import datetime, timedelta
            old_deadline_str = task_info.get("deadline", "")
            try:
                old_date = datetime.strptime(old_deadline_str, "%Y-%m-%d").date()
            except (ValueError, TypeError):
                old_date = datetime.now().date()
            new_date = old_date + timedelta(days=delay_days)
            new_deadline = new_date.isoformat()

        # Build update payload
        updates: dict = {}
        if status:
            updates["status"] = status
        if new_deadline:
            updates["deadline"] = new_deadline
        if not updates:
            return new_deadline

        # Local import — telegram_bot does not import supabase_client at module
        # level (all its other Supabase calls use local imports too)
        from services.supabase_client import supabase_client

        # --- 1. DB update (prefer task_id, fall back to title match) ---
        db_ok = False
        db_error: str | None = None
        resolved_task_id = task_id
        try:
            if not resolved_task_id:
                # Query all active statuses — must include in_progress, not just pending/overdue.
                # The reminder may target a task that's already started.
                tasks = supabase_client.get_tasks(assignee=assignee, status=None, limit=500)
                target = (task_text or "").strip().lower()
                for t in tasks:
                    if t.get("status") in ("done", "cancelled"):
                        continue
                    if t.get("title", "").strip().lower() == target:
                        resolved_task_id = t["id"]
                        break

            if resolved_task_id:
                supabase_client.update_task(resolved_task_id, **updates)
                db_ok = True
                logger.info(f"Updated task in DB: {resolved_task_id} -> {updates}")
            else:
                db_error = (
                    f"No matching DB task for '{task_text[:60]}' ({assignee}) "
                    f"— title-match fallback failed"
                )
                logger.error(db_error)
        except Exception as e:
            db_error = str(e)
            logger.error(f"Failed to update task in Supabase: {e}")

        # --- 2. Sheets update (independent of DB outcome) ---
        sheets_ok = False
        sheets_error: str | None = None
        try:
            if row_number:
                from services.google_sheets import sheets_service
                await sheets_service.update_task_row(row_number, **updates)
                sheets_ok = True
                logger.info(f"Updated task in Sheets row {row_number} -> {updates}")
            else:
                # No row number — skip Sheets silently, not a failure
                sheets_ok = True
        except Exception as e:
            sheets_error = str(e)
            logger.error(f"Failed to update task in Sheets: {e}")

        # --- 3. Alert loudly if either side failed ---
        if not db_ok or not sheets_ok:
            try:
                from services.alerting import send_system_alert, AlertSeverity
                failures = []
                if not db_ok:
                    failures.append(f"DB: {db_error}")
                if not sheets_ok:
                    failures.append(f"Sheets: {sheets_error}")
                await send_system_alert(
                    AlertSeverity.CRITICAL,
                    "telegram_bot.task_update",
                    f"Task update partial failure for '{task_text[:60]}' "
                    f"({assignee}): " + "; ".join(failures),
                )
            except Exception as alert_err:
                logger.error(f"Alert on task update failure also failed: {alert_err}")

        return new_deadline

    async def _handle_task_reply(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
    ) -> bool:
        """
        Handle free-text reply to a task reminder message.

        Detects if the replied-to message was a task reminder,
        classifies the intent with Haiku, and executes the action.

        Returns:
            True if handled, False if not a task reply.
        """
        from schedulers.task_reminder_scheduler import task_reminder_scheduler

        reply_msg_id = update.message.reply_to_message.message_id
        short_id = task_reminder_scheduler.message_task_map.get(reply_msg_id)
        if not short_id:
            return False  # Not a reply to a task reminder

        task_info = task_reminder_scheduler.task_action_map.get(short_id)
        if not task_info:
            await self.send_message(
                update.effective_chat.id,
                "Task action expired. Please use MCP or update the Sheets directly."
            )
            return True

        user_text = update.message.text.strip()
        task_text = task_info.get("task_text", "Unknown task")

        # Classify intent with Haiku
        try:
            from core.llm import call_llm
            from config.settings import settings

            response, _ = call_llm(
                prompt=(
                    f"The user replied to an overdue task reminder for: \"{task_text}\"\n"
                    f"Their reply: \"{user_text}\"\n\n"
                    "Classify the intent as exactly one of: done, delay, discuss, other\n"
                    "- done: task is completed (\"done\", \"finished\", \"handled\", \"completed\")\n"
                    "- delay: push to later (\"next week\", \"delay\", \"push\", \"postpone\")\n"
                    "- discuss: needs discussion (\"discuss\", \"let's talk\", \"meeting\")\n"
                    "- other: anything else\n\n"
                    "Respond with exactly one word."
                ),
                model=settings.model_simple,
                max_tokens=10,
                call_site="task_reply_classify",
            )
            intent = response.strip().lower()
        except Exception as e:
            logger.error(f"Task reply classification failed: {e}")
            intent = "other"

        if intent == "done":
            await self._execute_task_update_from_reminder(task_info, status="done")
            await self.send_message(
                update.effective_chat.id,
                f"Got it \u2014 marked *{task_text[:50]}* as done. Sheets updated."
            )
        elif intent == "delay":
            new_deadline = await self._execute_task_update_from_reminder(
                task_info, delay_days=7
            )
            await self.send_message(
                update.effective_chat.id,
                f"Pushed *{task_text[:50]}* to {new_deadline}. Sheets updated."
            )
        elif intent == "discuss":
            try:
                supabase_client.create_open_question(
                    question=f"Discuss overdue task: {task_text}",
                    raised_by="Eyal",
                )
            except Exception:
                pass
            await self.send_message(
                update.effective_chat.id,
                f"Added *{task_text[:50]}* to next meeting agenda."
            )
        else:
            await self.send_message(
                update.effective_chat.id,
                f"Noted your reply about *{task_text[:50]}*. Use the buttons or MCP for specific actions."
            )

        return True

    async def _handle_review_mode_message(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """
        Handle a message from Eyal while in review mode.

        Classifies intent (question vs edit instruction), then routes:
        - Questions → agent with conversation history (stays in review mode)
        - Edit instructions → edit flow (clears review mode after processing)
        """
        meeting_id = context.user_data.get("pending_edit_meeting_id")
        message_text = update.message.text
        chat_id = update.effective_chat.id
        chat_id_str = str(chat_id)

        if not meeting_id:
            await self.send_message(
                chat_id,
                "I'm not sure which meeting you're referring to. "
                "Please use the buttons on the approval request."
            )
            return

        # Classify: is this a question about the summary, or an edit instruction?
        intent = await self._classify_review_intent(message_text)

        if intent == "question":
            # Answer the question using the agent + conversation history
            await self.send_message(chat_id, "Thinking...")

            from core.agent import gianluigi_agent

            history = conversation_memory.get_history(chat_id_str)

            try:
                result = await gianluigi_agent.process_message(
                    user_message=message_text,
                    user_id="eyal",
                    conversation_history=history,
                )

                response = result.get("response", "I couldn't process your request.")

                # Store conversation turn (stay in review mode)
                conversation_memory.add_message(chat_id_str, "user", message_text)
                conversation_memory.add_message(chat_id_str, "assistant", response)

                await self.send_message(chat_id, response)

            except Exception as e:
                logger.error(f"Error answering review question: {e}")
                await self.send_message(
                    chat_id,
                    "Sorry, I encountered an error processing your question."
                )
        else:
            # Edit instruction — route to edit flow
            await self._route_edit_instruction(update, context, meeting_id, message_text)

    async def _classify_review_intent(self, message: str) -> str:
        """
        Classify whether a review-mode message is a question or edit instruction.

        Uses a cheap Haiku call (~50 tokens) for fast classification.

        Args:
            message: The user's message text.

        Returns:
            "question" or "edit"
        """
        from core.llm import call_llm

        try:
            result, _ = call_llm(
                prompt=(
                    "Classify this message as either 'question' or 'edit'. "
                    "A question asks about the content (e.g., 'what decisions were made?'). "
                    "An edit requests changes (e.g., 'make it shorter', 'change the deadline'). "
                    "Reply with ONLY the word 'question' or 'edit'.\n\n"
                    f"Message: {message}"
                ),
                model=settings.model_simple,
                max_tokens=10,
                call_site="review_intent",
            )
            result = result.strip().lower()
            if "question" in result:
                return "question"
            return "edit"
        except Exception as e:
            logger.warning(f"Intent classification failed, defaulting to edit: {e}")
            return "edit"

    async def _route_edit_instruction(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
        meeting_id: str,
        edit_instructions: str,
    ) -> None:
        """
        Process edit instructions and resubmit the summary for approval.

        Extracted from the old _handle_edit_instructions. Clears
        pending_edit_meeting_id after processing.

        Args:
            update: Telegram update.
            context: Telegram context.
            meeting_id: UUID of the meeting being edited.
            edit_instructions: The edit instruction text from Eyal.
        """
        chat_id = update.effective_chat.id

        await self.send_message(chat_id, "Processing your edits...")

        try:
            from guardrails.approval_flow import process_response

            result = await process_response(
                meeting_id=meeting_id,
                response=edit_instructions,
                response_source="telegram",
                force_action="edit",
            )

            if result.get("action") == "edit_requested":
                await self.send_message(
                    chat_id,
                    "Edits applied successfully. "
                    "A new approval request has been sent."
                )
            else:
                await self.send_message(
                    chat_id,
                    f"Edit processing result: {result.get('next_step', 'Unknown')}"
                )

        except Exception as e:
            logger.error(f"Error processing edits for {meeting_id}: {e}")
            await self.send_message(chat_id, f"Error processing edits: {e}")

        # Clear the pending edit
        context.user_data.pop("pending_edit_meeting_id", None)
        logger.info(f"Edit instructions processed for meeting {meeting_id}")

    # =========================================================================
    # Debrief Callback Handlers
    # =========================================================================

    async def _handle_debrief_callback(
        self,
        query,
        context: ContextTypes.DEFAULT_TYPE,
        action: str,
        session_id: str,
    ) -> None:
        """Handle debrief-related inline button callbacks."""
        # Auth: only Eyal can interact with debrief buttons
        if str(query.from_user.id) != str(self.eyal_chat_id):
            await query.answer("Only Eyal can use debrief controls.", show_alert=True)
            return

        from processors.debrief import finalize_debrief, confirm_debrief

        chat_id = query.message.chat_id

        if action == "debrief_finish":
            await query.edit_message_text("Finalizing your debrief...")
            result = await finalize_debrief(session_id)
            await self._send_debrief_confirmation(chat_id, result)

        elif action == "debrief_approve":
            await query.edit_message_text("Approved! Injecting items...")
            result = await confirm_debrief(session_id, approved=True)
            context.user_data.pop("debrief_session_id", None)
            self._active_interactive_session = None
            await self.send_message(
                chat_id,
                result.get("response", "Debrief saved."),
                parse_mode=None,
            )

            # Phase 6: Offer to resume weekly review if one was paused
            if self._session_stack and self._session_stack[-1] == "weekly_review":
                review_session_id = context.user_data.get("review_session_id")
                if not review_session_id:
                    # DB fallback (context lost after restart)
                    active_review = supabase_client.get_active_weekly_review_session()
                    if active_review:
                        review_session_id = active_review["id"]
                        context.user_data["review_session_id"] = review_session_id
                if review_session_id:
                    keyboard = InlineKeyboardMarkup([
                        [
                            InlineKeyboardButton(
                                "Resume weekly review",
                                callback_data=f"review_resume_after_debrief:{review_session_id}",
                            ),
                            InlineKeyboardButton(
                                "End review",
                                callback_data=f"review_end:{review_session_id}",
                            ),
                        ],
                    ])
                    await self.send_message(
                        chat_id,
                        "Resume your weekly review?",
                        parse_mode=None,
                        reply_markup=keyboard,
                    )

        elif action == "debrief_edit":
            context.user_data["debrief_editing"] = session_id
            # Keep the summary visible so user can reference it while editing
            current_text = query.message.text or ""
            edit_prompt = (
                "\n\n--- EDIT MODE ---\n"
                "Send your corrections (e.g., 'Change task 2 assignee to Roye', "
                "'Remove the last decision', 'Add a task for Paolo')."
            )
            # Telegram has a 4096 char limit for messages
            combined = current_text + edit_prompt
            if len(combined) > 4096:
                combined = current_text[:3900] + "\n...\n" + edit_prompt
            await query.edit_message_text(combined)

        elif action == "debrief_reject":
            result = await confirm_debrief(session_id, approved=False)
            context.user_data.pop("debrief_session_id", None)
            context.user_data.pop("debrief_editing", None)
            self._active_interactive_session = None
            await query.edit_message_text("Debrief cancelled. Nothing was saved.")

    async def _handle_inject_callback(
        self,
        query,
        context: ContextTypes.DEFAULT_TYPE,
        action: str,
        session_id: str,
    ) -> None:
        """Handle quick injection Inject/Dismiss button callbacks."""
        # Auth: only Eyal can interact with injection buttons
        if str(query.from_user.id) != str(self.eyal_chat_id):
            await query.answer("Only Eyal can use injection controls.", show_alert=True)
            return

        from processors.debrief import confirm_debrief
        from services.supabase_client import supabase_client

        chat_id = query.message.chat_id

        if action == "inject_approve":
            await query.edit_message_text("Injecting...")
            result = await confirm_debrief(session_id, approved=True)
            await self.send_message(
                chat_id,
                result.get("response", "Information saved."),
                parse_mode=None,
            )

        elif action == "inject_dismiss":
            if session_id and session_id != "0":
                supabase_client.update_debrief_session(
                    session_id, status="cancelled"
                )
            await query.edit_message_text("Dismissed. Nothing was saved.")

    # =========================================================================
    # Phase 5 — Prep Outline Callbacks
    # =========================================================================

    async def _handle_prep_outline_callback(
        self,
        query,
        context: ContextTypes.DEFAULT_TYPE,
        action: str,
        approval_id: str,
    ) -> None:
        """Handle prep outline button callbacks (generate/focus/reclassify/skip)."""
        if str(query.from_user.id) != str(self.eyal_chat_id):
            await query.answer("Only Eyal can manage prep outlines.", show_alert=True)
            return

        from services.supabase_client import supabase_client

        if action == "prep_generate":
            await query.edit_message_text("Generating prep document...")
            try:
                from processors.meeting_prep import generate_meeting_prep_from_outline
                result = await generate_meeting_prep_from_outline(approval_id)
                if result.get("status") == "success":
                    await self.send_to_eyal(
                        "Prep document generated and submitted for your approval.",
                        parse_mode=None,
                    )
                else:
                    await self.send_to_eyal(
                        f"Prep generation issue: {result.get('error', 'unknown')}",
                        parse_mode=None,
                    )
            except Exception as e:
                logger.error(f"Error generating prep from outline: {e}")
                await self.send_to_eyal(f"Error generating prep: {e}", parse_mode=None)

        elif action == "prep_focus":
            # Store focus state in Supabase (survives restarts) + cache in context
            row = supabase_client.get_pending_approval(approval_id)
            if row:
                content = row.get("content", {})
                content["focus_active"] = True
                supabase_client.update_pending_approval(approval_id, content=content)
            context.user_data["prep_focus_approval_id"] = approval_id
            await query.edit_message_text(
                "What should I focus on? Examples:\n"
                "- 'Focus on MVP timeline'\n"
                "- 'Skip stakeholder section'\n"
                "- 'Check what Paolo said about Lavazza'"
            )

        elif action == "prep_reclassify":
            # Show template picker buttons
            from config.meeting_prep_templates import MEETING_PREP_TEMPLATES
            buttons = []
            row_buttons = []
            for key, tmpl in MEETING_PREP_TEMPLATES.items():
                row_buttons.append(InlineKeyboardButton(
                    tmpl["display_name"],
                    callback_data=f"prep_settype:{approval_id}:{key}",
                ))
                if len(row_buttons) == 2:
                    buttons.append(row_buttons)
                    row_buttons = []
            if row_buttons:
                buttons.append(row_buttons)

            reply_markup = InlineKeyboardMarkup(buttons)
            await query.edit_message_text(
                "Select the correct meeting type:",
                reply_markup=reply_markup,
            )

        elif action == "prep_skip":
            supabase_client.update_pending_approval(approval_id, status="skipped")
            supabase_client.log_action(
                action="prep_outline_skipped",
                details={"approval_id": approval_id},
                triggered_by="eyal",
            )
            await query.edit_message_text("Prep skipped.")

    async def _handle_prep_reclassify_callback(
        self,
        query,
        context: ContextTypes.DEFAULT_TYPE,
        approval_id: str,
        new_type: str,
    ) -> None:
        """Handle meeting type reclassification — regenerate outline with new template."""
        if str(query.from_user.id) != str(self.eyal_chat_id):
            await query.answer("Only Eyal can reclassify meetings.", show_alert=True)
            return

        from services.supabase_client import supabase_client
        from processors.meeting_type_matcher import remember_meeting_type
        from processors.meeting_prep import generate_prep_outline

        await query.edit_message_text(f"Reclassifying and regenerating outline...")

        try:
            # Load existing outline data
            row = supabase_client.get_pending_approval(approval_id)
            if not row:
                await self.send_to_eyal("Could not find outline to reclassify.", parse_mode=None)
                return

            content = row.get("content", {})
            event = content.get("outline", {}).get("event", content.get("event", {}))
            title = event.get("title", "")

            # Persist learning
            remember_meeting_type(title, new_type)

            # Regenerate outline
            outline = await generate_prep_outline(event, new_type)

            # Update approval content
            content["outline"] = outline
            content["meeting_type"] = new_type
            content["confidence"] = "auto"  # User selected, so now auto
            supabase_client.update_pending_approval(approval_id, content=content)

            # Send new outline with buttons
            await self.send_prep_outline(outline, approval_id, confidence="auto")

        except Exception as e:
            logger.error(f"Error reclassifying prep: {e}")
            await self.send_to_eyal(f"Error reclassifying: {e}", parse_mode=None)

    async def _handle_prep_focus_input(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
    ) -> bool:
        """
        Handle focus input text from Eyal for a prep outline.

        Checks context cache first, then falls back to Supabase for restart safety.

        Args:
            update: Telegram update.
            context: Bot context.

        Returns:
            True if this message was handled as focus input, False otherwise.
        """
        approval_id = context.user_data.get("prep_focus_approval_id")

        # Restart-safe fallback: check Supabase for active focus
        if not approval_id:
            from services.supabase_client import supabase_client
            pending = supabase_client.get_pending_prep_outlines()
            for row in pending:
                content = row.get("content", {})
                if content.get("focus_active"):
                    approval_id = row["approval_id"]
                    break

        if not approval_id:
            return False

        message_text = update.message.text
        from services.supabase_client import supabase_client

        try:
            row = supabase_client.get_pending_approval(approval_id)
            if not row:
                context.user_data.pop("prep_focus_approval_id", None)
                return False

            content = row.get("content", {})

            # Add focus instruction
            focus_list = content.get("focus_instructions", [])
            focus_list.append(message_text)
            content["focus_instructions"] = focus_list
            content["focus_active"] = False  # Clear active flag

            supabase_client.update_pending_approval(approval_id, content=content)

            # Clear cache
            context.user_data.pop("prep_focus_approval_id", None)

            # Send updated outline with buttons
            outline = content.get("outline", {})
            buttons = [
                [
                    InlineKeyboardButton("Generate", callback_data=f"prep_generate:{approval_id}"),
                    InlineKeyboardButton("Edit more", callback_data=f"prep_focus:{approval_id}"),
                ],
                [
                    InlineKeyboardButton("Skip", callback_data=f"prep_skip:{approval_id}"),
                ],
            ]
            reply_markup = InlineKeyboardMarkup(buttons)

            await self.send_message(
                update.effective_chat.id,
                f"Focus added: \"{message_text}\"\n\n"
                f"Total focus instructions: {len(focus_list)}\n"
                f"Ready to generate or add more.",
                reply_markup=reply_markup,
                parse_mode=None,
            )
            return True

        except Exception as e:
            logger.error(f"Error handling prep focus input: {e}")
            context.user_data.pop("prep_focus_approval_id", None)
            return False

    # =========================================================================
    # Helper Methods
    # =========================================================================

    def _get_user_id(self, telegram_user_id: int) -> str | None:
        """
        Map a Telegram user ID to a team member ID.

        Args:
            telegram_user_id: Telegram's user ID.

        Returns:
            Team member ID (eyal, roye, etc.) or None if not found.
        """
        # Check cached mapping
        if telegram_user_id in self._telegram_user_map:
            return self._telegram_user_map[telegram_user_id]

        # Check if this is Eyal (we know his chat ID)
        if str(telegram_user_id) == str(self.eyal_chat_id):
            self._telegram_user_map[telegram_user_id] = "eyal"
            return "eyal"

        # TODO: In v0.2, implement user registration/verification
        return None

    def _is_admin(self, telegram_user_id: int) -> bool:
        """
        Check if a Telegram user has admin privileges.

        Args:
            telegram_user_id: Telegram's user ID.

        Returns:
            True if user is Eyal (admin).
        """
        return str(telegram_user_id) == str(self.eyal_chat_id)

    def register_user(self, telegram_user_id: int, team_member_id: str) -> None:
        """
        Register a Telegram user to a team member.

        Args:
            telegram_user_id: Telegram's user ID.
            team_member_id: Team member ID (eyal, roye, paolo, yoram).
        """
        if team_member_id in TEAM_MEMBERS:
            self._telegram_user_map[telegram_user_id] = team_member_id
            logger.info(
                f"Registered Telegram user {telegram_user_id} as {team_member_id}"
            )


# Singleton instance
telegram_bot = TelegramBot()
