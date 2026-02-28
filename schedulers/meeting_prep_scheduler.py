"""
Meeting preparation document scheduler.

This module runs on a schedule to generate prep documents for upcoming
meetings. It identifies CropSight meetings happening in the next 24 hours
and generates preparation documents.

Workflow:
1. Check calendar for meetings in the next 24 hours
2. Filter to CropSight meetings only
3. For each meeting without prep:
   a. Use shared functions from processors.meeting_prep
   b. Format and save to Google Drive
   c. Check sensitivity — sensitive = Eyal-only, normal = full team
   d. Notify via Telegram

Usage:
    from schedulers.meeting_prep_scheduler import MeetingPrepScheduler

    scheduler = MeetingPrepScheduler()
    await scheduler.start()  # Runs on schedule
"""

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from config.settings import settings
from config.team import CROPSIGHT_TEAM_EMAILS
from services.google_calendar import calendar_service
from services.google_drive import drive_service
from services.google_sheets import sheets_service
from services.supabase_client import supabase_client
from services.embeddings import embedding_service
from services.telegram_bot import telegram_bot
from guardrails.calendar_filter import is_cropsight_meeting
from guardrails.sensitivity_classifier import classify_sensitivity
from guardrails.approval_flow import submit_for_approval
from processors.meeting_prep import (
    find_related_meetings,
    find_relevant_decisions,
    find_participant_tasks,
    get_stakeholder_context,
    format_prep_document,
    _find_open_questions,
)

logger = logging.getLogger(__name__)



class MeetingPrepScheduler:
    """
    Schedules and generates meeting preparation documents.
    """

    def __init__(
        self,
        check_interval: int | None = None,
        prep_hours_before: int | None = None
    ):
        """
        Initialize the meeting prep scheduler.

        Args:
            check_interval: Seconds between checks (default 4 hours).
            prep_hours_before: Hours before meeting to generate prep.
        """
        self.check_interval = check_interval or settings.MEETING_PREP_CHECK_INTERVAL
        self.prep_hours_before = prep_hours_before or settings.MEETING_PREP_HOURS_BEFORE
        self._running = False
        # Track meetings we've already generated prep for
        self._prep_generated: set[str] = set()
        # Track meetings we've already sent reminders for
        self._reminders_sent: set[str] = set()

    async def start(self) -> None:
        """
        Start the meeting prep scheduler loop.

        This runs indefinitely until stop() is called.
        """
        if self._running:
            logger.warning("Meeting prep scheduler already running")
            return

        self._running = True
        logger.info(
            f"Starting meeting prep scheduler "
            f"(interval: {self.check_interval}s, prep window: {self.prep_hours_before}h)"
        )

        while self._running:
            try:
                await self._check_and_generate_preps()
            except Exception as e:
                logger.error(f"Error in meeting prep scheduler: {e}")
                supabase_client.log_action(
                    action="prep_scheduler_error",
                    details={"error": str(e)},
                    triggered_by="auto",
                )

            # Wait for next check
            await asyncio.sleep(self.check_interval)

    def stop(self) -> None:
        """Stop the scheduler."""
        self._running = False
        logger.info("Meeting prep scheduler stopped")

    async def _check_and_generate_preps(self) -> list[dict]:
        """
        Check for meetings needing prep and generate documents.

        Returns:
            List of prep generation results.
        """
        logger.debug("Checking for meetings needing prep...")

        # Get upcoming events
        events = await calendar_service.get_events_needing_prep(
            hours_ahead=self.prep_hours_before
        )

        if not events:
            logger.debug("No upcoming meetings")
            return []

        results = []
        for event in events:
            event_id = event.get("id", "")

            # Skip if we already generated prep for this meeting
            if event_id in self._prep_generated:
                continue

            # Check if CropSight meeting
            if not is_cropsight_meeting(event):
                continue

            try:
                result = await self._generate_prep_for_meeting(event)
                results.append(result)

                if result.get("status") == "success":
                    self._prep_generated.add(event_id)

            except Exception as e:
                logger.error(f"Error generating prep for {event.get('title')}: {e}")
                results.append({
                    "event_id": event_id,
                    "title": event.get("title"),
                    "status": "error",
                    "error": str(e),
                })

        # After prep generation, check for meetings needing a reminder (2-3h away)
        # We re-fetch with a broader window to catch meetings in the reminder range
        try:
            reminder_events = await calendar_service.get_events_needing_prep(
                hours_ahead=self.prep_hours_before
            )
            for event in (reminder_events or []):
                if not is_cropsight_meeting(event):
                    continue
                try:
                    await self._send_pre_meeting_reminder(event)
                except Exception as e:
                    logger.error(
                        f"Error sending reminder for {event.get('title')}: {e}"
                    )
        except Exception as e:
            logger.error(f"Error checking for reminders: {e}")

        return results

    async def _send_pre_meeting_reminder(self, event: dict) -> bool:
        """
        Send a brief pre-meeting reminder 2-3 hours before.

        Includes context from last related meeting + open task count.
        Tracks sent reminders to avoid duplicates.

        Args:
            event: Calendar event dict.

        Returns:
            True if reminder was sent, False if skipped.
        """
        event_id = event.get("id", "")
        reminder_key = f"reminder:{event_id}"

        if reminder_key in self._reminders_sent:
            return False

        title = event.get("title", "Untitled Meeting")
        start_time = event.get("start", "")

        # Parse start time and check if 2-3 hours away
        try:
            # Handle ISO format
            if isinstance(start_time, str):
                start_dt = datetime.fromisoformat(
                    start_time.replace("Z", "+00:00")
                )
            else:
                return False

            now = datetime.now(timezone.utc)
            hours_until = (start_dt - now).total_seconds() / 3600

            if not (2 <= hours_until <= 3):
                return False
        except Exception:
            return False

        # Get participant names
        attendees = event.get("attendees", [])
        participant_names = [
            a.get("displayName") or a.get("email", "").split("@")[0]
            for a in attendees
        ]

        # Build reminder message
        context_lines = [f"*Upcoming in ~{int(hours_until)}h: {title}*\n"]

        # Find related past meeting (use shared function)
        try:
            related = await find_related_meetings(title, participant_names, limit=1)
            if related:
                last_meeting = related[0]
                context_lines.append(
                    f"Last related meeting: {last_meeting.get('title', 'N/A')} "
                    f"({last_meeting.get('date', 'N/A')})"
                )
        except Exception:
            pass

        # Count open tasks for participants
        try:
            tasks_by_person = await find_participant_tasks(participant_names)
            total_tasks = sum(len(t) for t in tasks_by_person.values())
            if total_tasks > 0:
                context_lines.append(f"Open tasks for attendees: {total_tasks}")
        except Exception:
            pass

        context_lines.append(f"\nParticipants: {', '.join(participant_names)}")

        message = "\n".join(context_lines)

        # Send to Eyal
        await telegram_bot.send_to_eyal(message)

        self._reminders_sent.add(reminder_key)

        supabase_client.log_action(
            action="pre_meeting_reminder_sent",
            details={"event_id": event_id, "title": title},
            triggered_by="auto",
        )

        return True

    async def _generate_prep_for_meeting(self, event: dict) -> dict:
        """
        Generate a prep document for a single meeting.

        Uses shared functions from processors.meeting_prep for all data
        gathering and formatting. Handles Drive save and Telegram notification.

        Sensitivity-aware distribution:
        - Sensitive meetings: notify Eyal only
        - Normal meetings: notify the full team

        Args:
            event: Calendar event dict.

        Returns:
            Result dict with status and details.
        """
        title = event.get("title", "Untitled Meeting")
        event_id = event.get("id", "")
        start_time = event.get("start", "")

        logger.info(f"Generating prep for: {title}")

        # Get participants
        attendees = event.get("attendees", [])
        participant_names = [
            a.get("displayName") or a.get("email", "").split("@")[0]
            for a in attendees
        ]

        # Step 1: Search for related past meetings (shared function)
        related_meetings = await find_related_meetings(title, participant_names)

        # Step 2: Find relevant decisions (shared function — hybrid search)
        relevant_decisions = await find_relevant_decisions(title)

        # Step 3: Find open questions that might be addressed
        open_questions = _find_open_questions(title)

        # Step 4: Get stakeholder context (shared function)
        stakeholder_info = await get_stakeholder_context(
            participant_names,
            title
        )

        # Step 5: Get participant tasks (shared function)
        participant_tasks = await find_participant_tasks(participant_names)

        # Step 6: Format prep document (shared function)
        prep_content = format_prep_document(
            event=event,
            related_meetings=related_meetings,
            relevant_decisions=relevant_decisions,
            open_questions=open_questions,
            participant_tasks=participant_tasks,
            stakeholder_info=stakeholder_info,
        )

        # Step 7: Save to Google Drive
        date_str = start_time[:10] if start_time else datetime.now().strftime("%Y-%m-%d")
        filename = f"{date_str} - Prep - {title}.md"

        drive_result = await drive_service.save_meeting_prep(
            content=prep_content,
            filename=filename
        )

        # Step 8: Submit for Eyal's approval (instead of direct distribution)
        sensitivity = classify_sensitivity({"title": title})
        drive_link = drive_result.get("webViewLink", "") if drive_result else ""

        prep_approval_id = f"prep-{event_id}"
        await submit_for_approval(
            content_type="meeting_prep",
            content={
                "title": title,
                "summary": prep_content,
                "start_time": start_time,
                "sensitivity": sensitivity,
                "drive_link": drive_link,
            },
            meeting_id=prep_approval_id,
        )

        # Log the action
        supabase_client.log_action(
            action="meeting_prep_generated",
            details={
                "event_id": event_id,
                "title": title,
                "sensitivity": sensitivity,
                "related_meetings": len(related_meetings),
                "decisions": len(relevant_decisions),
            },
            triggered_by="auto",
        )

        return {
            "event_id": event_id,
            "title": title,
            "status": "success",
            "sensitivity": sensitivity,
            "drive_link": drive_result.get("webViewLink", "") if drive_result else "",
            "related_meetings_count": len(related_meetings),
            "decisions_count": len(relevant_decisions),
            "open_questions_count": len(open_questions),
        }

    async def generate_prep_now(self, event_id: str) -> dict:
        """
        Manually trigger prep generation for a specific event.

        Args:
            event_id: Google Calendar event ID.

        Returns:
            Generation result dict.
        """
        event = await calendar_service.get_event(event_id)
        if not event:
            return {"status": "error", "error": "Event not found"}

        return await self._generate_prep_for_meeting(event)


# Singleton instance
meeting_prep_scheduler = MeetingPrepScheduler()
