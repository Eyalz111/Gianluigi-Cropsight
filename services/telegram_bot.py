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

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
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
from services.conversation_memory import conversation_memory

logger = logging.getLogger(__name__)


def _escape_html(text: str) -> str:
    """Escape HTML special characters for Telegram HTML parse mode."""
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


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
        self.group_chat_id = settings.TELEGRAM_GROUP_CHAT_ID
        self.eyal_chat_id = settings.TELEGRAM_EYAL_CHAT_ID

        # Map Telegram user IDs to team member IDs
        # This will be populated when users interact with the bot
        self._telegram_user_map: dict[int, str] = {}

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
        self.app.add_handler(CommandHandler("myid", self._handle_myid))

        # Add callback handler for inline buttons (approval flow)
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

        # Initialize and start polling
        await self.app.initialize()
        await self.app.start()
        await self.app.updater.start_polling(drop_pending_updates=True)

        # Warn if Eyal's chat ID looks like a group (should be positive for DM)
        if self.eyal_chat_id and int(self.eyal_chat_id) < 0:
            logger.warning(
                f"TELEGRAM_EYAL_CHAT_ID ({self.eyal_chat_id}) is negative — "
                f"this looks like a group chat, not Eyal's personal DM. "
                f"Have Eyal send /myid in a private chat with the bot to get his real ID."
            )

        logger.info("Telegram bot started and polling for messages")

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

    # =========================================================================
    # Sending Messages
    # =========================================================================

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
            # Truncate if too long (Telegram limit is 4096 chars)
            if len(text) > 4000:
                text = text[:4000] + "\n\n... _(message truncated)_"

            await self.app.bot.send_message(
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

        Returns:
            True if request was sent successfully.
        """
        decisions = decisions or []
        tasks = tasks or []
        follow_ups = follow_ups or []
        open_questions = open_questions or []

        # Build a clean HTML message
        lines = [f"<b>Approval Request: {_escape_html(meeting_title)}</b>", ""]

        # Decisions
        if decisions:
            lines.append(f"<b>Decisions ({len(decisions)})</b>")
            for i, d in enumerate(decisions, 1):
                desc = _escape_html(d.get("description", ""))
                lines.append(f"  {i}. {desc}")
            lines.append("")

        # Tasks
        if tasks:
            lines.append(f"<b>Action Items ({len(tasks)})</b>")
            for i, t in enumerate(tasks, 1):
                title = _escape_html(t.get("title", ""))
                assignee = t.get("assignee", "TBD")
                priority = t.get("priority", "M")
                lines.append(f"  {i}. [{priority}] {title} -> {assignee}")
            lines.append("")

        # Follow-ups
        if follow_ups:
            lines.append(f"<b>Follow-up Meetings ({len(follow_ups)})</b>")
            for f in follow_ups:
                title = _escape_html(f.get("title", ""))
                led_by = f.get("led_by", "TBD")
                lines.append(f"  - {title} (led by {led_by})")
            lines.append("")

        # Open questions
        if open_questions:
            lines.append(f"<b>Open Questions ({len(open_questions)})</b>")
            for q in open_questions:
                question = _escape_html(q.get("question", ""))
                raised_by = q.get("raised_by", "")
                lines.append(f"  - {question}")
                if raised_by:
                    lines.append(f"    (raised by {raised_by})")
            lines.append("")

        # Discussion summary (brief excerpt)
        if summary_preview:
            # Take just the discussion summary portion, not the full markdown
            excerpt = summary_preview[:600]
            if len(summary_preview) > 600:
                excerpt += "..."
            lines.append(f"<b>Discussion Summary</b>")
            lines.append(_escape_html(excerpt))
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

        # Create inline keyboard
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
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)

        return await self.send_to_eyal(
            message, reply_markup=reply_markup, parse_mode="HTML"
        )

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
/search [topic] - Search meeting history for a topic
/decisions - List recent key decisions
/questions - List open questions

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

        Searches meeting history for a topic.
        """
        # Get search query from command arguments
        if context.args:
            query = " ".join(context.args)
        else:
            await self.send_message(
                update.effective_chat.id,
                "Usage: /search [topic]\n\nExample: /search cloud providers"
            )
            return

        # Perform search
        await self.send_message(
            update.effective_chat.id,
            f"Searching for: _{query}_..."
        )

        # Import here to avoid circular imports
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
        has_pending_edit = bool(context.user_data.get("pending_edit_meeting_id"))
        if is_eyal and (update.message.reply_to_message or has_pending_edit):
            await self._handle_review_mode_message(update, context)
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

        data = query.data
        action, meeting_id = data.split(":", 1)

        # Import here to avoid circular imports
        from services.supabase_client import supabase_client
        from guardrails.approval_flow import (
            distribute_approved_content,
            process_response,
        )

        if action == "approve":
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
            supabase_client.update_meeting(
                meeting_id,
                approval_status="rejected"
            )
            supabase_client.log_action(
                action="approval_rejected",
                details={"meeting_id": meeting_id},
                triggered_by="eyal",
            )
            conversation_memory.clear(str(self.eyal_chat_id))

            await query.edit_message_text(
                "Rejected. The summary will not be distributed."
            )
            logger.info(f"Meeting {meeting_id} rejected by Eyal")

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
        from anthropic import Anthropic

        try:
            client = Anthropic(api_key=settings.ANTHROPIC_API_KEY)
            response = client.messages.create(
                model=settings.model_simple,
                max_tokens=10,
                messages=[{
                    "role": "user",
                    "content": (
                        "Classify this message as either 'question' or 'edit'. "
                        "A question asks about the content (e.g., 'what decisions were made?'). "
                        "An edit requests changes (e.g., 'make it shorter', 'change the deadline'). "
                        "Reply with ONLY the word 'question' or 'edit'.\n\n"
                        f"Message: {message}"
                    ),
                }],
            )
            result = response.content[0].text.strip().lower()
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
            )

            if result.get("action") == "edit_requested":
                edits = result.get("edits", [])
                await self.send_message(
                    chat_id,
                    f"Applied {len(edits)} edit(s). "
                    f"A new approval request has been sent."
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
