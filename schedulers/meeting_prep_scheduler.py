"""
Meeting preparation scheduler — Phase 5 redesign.

New flow: propose → discuss → generate → approve → distribute.

1. Check calendar for upcoming CropSight meetings
2. Classify meeting type (template matching)
3. Generate structured outline with template-driven data queries
4. Send outline to Eyal via Telegram for review
5. Eyal can: generate as-is, add focus, reclassify, or skip
6. On generate: full prep doc created, submitted for standard approval

Timeline modes handle different lead times:
- normal (>24h): full outline → discuss → generate with reminders
- compressed (12-24h): outline with shortened reminders
- urgent (6-12h): outline + single reminder + auto-generate at 4h
- emergency (2-6h): outline + background generation simultaneously
- skip (<2h): too late, log and skip

Usage:
    from schedulers.meeting_prep_scheduler import MeetingPrepScheduler

    scheduler = MeetingPrepScheduler()
    await scheduler.start()
"""

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from config.settings import settings
from services.google_calendar import calendar_service
from services.supabase_client import supabase_client
from services.telegram_bot import telegram_bot
from guardrails.calendar_filter import is_cropsight_meeting
from guardrails.approval_flow import submit_for_approval
from processors.meeting_type_matcher import classify_meeting_type
from processors.meeting_prep import (
    generate_prep_outline,
    format_outline_for_telegram,
    format_prep_document_v2,
    calculate_timeline_mode,
    generate_meeting_prep_from_outline,
    find_related_meetings,
    find_participant_tasks,
)

logger = logging.getLogger(__name__)


class MeetingPrepScheduler:
    """Schedules meeting prep outlines using the propose-discuss-generate flow."""

    def __init__(
        self,
        check_interval: int | None = None,
        prep_hours_before: int | None = None,
    ):
        self.check_interval = check_interval or settings.MEETING_PREP_CHECK_INTERVAL
        self.prep_hours_before = prep_hours_before or settings.MEETING_PREP_OUTLINE_LEAD_HOURS
        self._running = False
        # Track events we've already created outlines for (in-memory cache)
        self._prep_generated: set[str] = set()
        # Track events currently being processed (cleared on failure via finally)
        self._prep_in_progress: set[str] = set()
        # Track reminders sent (in-memory, rebuilt on startup)
        self._reminders_sent: set[str] = set()
        # Pending prep timers: approval_id -> list of asyncio.Task
        self._pending_prep_timers: dict[str, list[asyncio.Task]] = {}

    async def start(self) -> None:
        """Start the scheduler loop."""
        if self._running:
            logger.warning("Meeting prep scheduler already running")
            return

        self._running = True
        logger.info(
            f"Starting meeting prep scheduler "
            f"(interval: {self.check_interval}s, outline window: {self.prep_hours_before}h)"
        )

        # Wait for Telegram bot to be ready before first check
        try:
            await telegram_bot.wait_until_ready(timeout=30)
        except Exception:
            pass  # If bot not available, proceed anyway

        # Populate _prep_generated cache from DB BEFORE entering the loop.
        # Without this, the first check treats all existing outlines as "new"
        # and re-sends Telegram notifications on every restart.
        try:
            count = await self.reconstruct_prep_timers()
            logger.info(f"Reconstructed {count} prep timers from DB on startup")
        except Exception as e:
            logger.error(f"Failed to reconstruct prep timers on startup: {e}")

        while self._running:
            try:
                await self._check_and_generate_preps()
                try:
                    supabase_client.upsert_scheduler_heartbeat("meeting_prep")
                except Exception:
                    pass  # Never let monitoring kill the thing being monitored
            except Exception as e:
                logger.error(f"Error in meeting prep scheduler: {e}")
                supabase_client.log_action(
                    action="prep_scheduler_error",
                    details={"error": str(e)},
                    triggered_by="auto",
                )
                from core.health_monitor import check_and_alert
                await check_and_alert("meeting_prep_scheduler", e)
                try:
                    supabase_client.upsert_scheduler_heartbeat("meeting_prep", status="error", details={"error": str(e)})
                except Exception:
                    pass

            await asyncio.sleep(self.check_interval)

    def stop(self) -> None:
        """Stop the scheduler and cancel all pending timers."""
        self._running = False
        for approval_id, tasks in self._pending_prep_timers.items():
            for task in tasks:
                task.cancel()
        self._pending_prep_timers.clear()
        logger.info("Meeting prep scheduler stopped")

    async def _check_and_generate_preps(self) -> list[dict]:
        """Check for meetings needing prep and create outline proposals."""
        logger.info("Checking for meetings needing prep...")

        # Step 1: Re-verify existing pending outlines
        await self._reverify_pending_outlines()

        # Step 2: Get upcoming events
        events = await calendar_service.get_events_needing_prep(
            hours_ahead=self.prep_hours_before
        )

        if not events:
            logger.info("No upcoming meetings in next %dh", self.prep_hours_before)
            return []

        # Step 3: Check existing outlines to avoid duplicates
        existing_outlines = supabase_client.get_pending_prep_outlines()
        existing_event_ids = set()
        for outline in existing_outlines:
            content = outline.get("content", {})
            oid = content.get("outline", {}).get("event", {}).get("id", "")
            if not oid:
                oid = content.get("event", {}).get("id", "")
            if oid:
                existing_event_ids.add(oid)

        logger.info("Found %d upcoming events in next %dh", len(events), self.prep_hours_before)

        results = []
        for event in events:
            event_id = event.get("id", "")
            title = event.get("title", "untitled")

            # Skip if already processed, in progress, or has existing outline
            if event_id in self._prep_generated or event_id in self._prep_in_progress or event_id in existing_event_ids:
                logger.info("  Skipping '%s' — already has outline or in progress", title)
                continue

            # Check if CropSight meeting
            if not is_cropsight_meeting(event):
                logger.info("  Skipping event — not CropSight meeting (id=%s)", event_id[:12])
                continue

            # Skip solo events (no other attendees — just Eyal)
            attendees = event.get("attendees", [])
            if len(attendees) <= 1:
                logger.info("  Skipping event — solo (%d attendees, id=%s)", len(attendees), event_id[:12])
                continue

            logger.info("  Processing event — %d attendees, CropSight=True (id=%s)", len(attendees), event_id[:12])

            self._prep_in_progress.add(event_id)
            try:
                result = await self._create_outline_for_meeting(event)
                results.append(result)

                if result.get("status") == "success":
                    self._prep_generated.add(event_id)

            except Exception as e:
                logger.error(f"Error creating outline for {event.get('title')}: {e}")
                results.append({
                    "event_id": event_id,
                    "title": event.get("title"),
                    "status": "error",
                    "error": str(e),
                })
            finally:
                self._prep_in_progress.discard(event_id)

        return results

    async def _reverify_pending_outlines(self) -> None:
        """Re-verify calendar events for pending outlines."""
        outlines = supabase_client.get_pending_prep_outlines()
        for outline in outlines:
            approval_id = outline.get("approval_id", "")
            content = outline.get("content", {})
            event_data = content.get("outline", {}).get("event", content.get("event", {}))
            event_id = event_data.get("id", "")

            if not event_id:
                continue

            try:
                live_event = await calendar_service.get_event(event_id)

                if not live_event:
                    # Event deleted — expire outline
                    title = event_data.get("title", "Unknown")
                    supabase_client.update_pending_approval(approval_id, status="expired")
                    self._cancel_prep_timers(approval_id)
                    await telegram_bot.send_to_eyal(
                        f"📅 {title} was removed from calendar — prep cancelled."
                    )
                    supabase_client.log_action(
                        action="prep_outline_expired_event_deleted",
                        details={"approval_id": approval_id, "title": title},
                        triggered_by="auto",
                    )
                    continue

                # Check if rescheduled (time shifted >2h)
                stored_start = content.get("event_start_time", "")
                live_start = live_event.get("start", "")
                if stored_start and live_start:
                    try:
                        stored_dt = datetime.fromisoformat(stored_start.replace("Z", "+00:00"))
                        live_dt = datetime.fromisoformat(live_start.replace("Z", "+00:00"))
                        shift_hours = abs((live_dt - stored_dt).total_seconds()) / 3600

                        if shift_hours > 2:
                            now = datetime.now(timezone.utc)
                            new_hours = (live_dt - now).total_seconds() / 3600
                            new_mode = calculate_timeline_mode(new_hours)
                            title = event_data.get("title", "Unknown")

                            # Update stored time and mode
                            content["event_start_time"] = live_start
                            content["timeline_mode"] = new_mode
                            supabase_client.update_pending_approval(
                                approval_id, content=content
                            )

                            # Reschedule timers
                            self._cancel_prep_timers(approval_id)
                            self._schedule_prep_timers(approval_id, new_mode, new_hours)

                            await telegram_bot.send_to_eyal(
                                f"📅 {title} rescheduled — timeline updated to {new_mode} mode."
                            )
                            logger.info(f"Outline {approval_id} rescheduled: {new_mode}")
                    except (ValueError, TypeError):
                        pass

            except Exception as e:
                logger.warning(f"Error re-verifying event for {approval_id}: {e}")

    async def _create_outline_for_meeting(self, event: dict) -> dict:
        """Create a prep outline proposal or quick brief for a meeting."""
        title = event.get("title", "Untitled Meeting")
        event_id = event.get("id", "")
        start_time = event.get("start", "")

        # Calculate hours until meeting
        hours_until = self._hours_until(start_time)

        # Determine timeline mode
        mode = calculate_timeline_mode(hours_until)

        if mode == "skip":
            logger.info(f"Prep skipped for '{title}' — detected {hours_until:.1f}h before meeting")
            supabase_client.log_action(
                action="prep_skipped_too_late",
                details={"event_id": event_id, "title": title, "hours_until": hours_until},
                triggered_by="auto",
            )
            return {"event_id": event_id, "title": title, "status": "skipped", "reason": "too_late"}

        # Classify meeting type
        meeting_type, confidence, signals = classify_meeting_type(event)

        logger.info(
            f"Prep for '{title}': type={meeting_type}, confidence={confidence}, "
            f"mode={mode}, hours={hours_until:.1f}"
        )

        # Emergency mode (<6h): skip outline, generate quick brief directly
        if mode == "emergency":
            return await self._create_quick_brief(
                event, meeting_type, confidence, signals, hours_until
            )

        # Normal/compressed/urgent: full outline → discuss → generate flow
        outline = await generate_prep_outline(event, meeting_type)

        # Create approval record
        approval_id = f"outline-{event_id}"
        outline_content = {
            "outline": outline,
            "event": event,
            "event_start_time": start_time,
            "meeting_type": meeting_type,
            "confidence": confidence,
            "signals": signals,
            "focus_instructions": [],
            "focus_active": False,
            "timeline_mode": mode,
            "next_reminder_at": None,
        }

        await submit_for_approval(
            content_type="prep_outline",
            content=outline_content,
            meeting_id=approval_id,
        )

        # Send outline to Eyal via Telegram with interactive buttons
        await telegram_bot.send_prep_outline(outline, approval_id, confidence=confidence)

        # Schedule timers based on mode
        self._schedule_prep_timers(approval_id, mode, hours_until)

        supabase_client.log_action(
            action="prep_outline_created",
            details={
                "event_id": event_id,
                "title": title,
                "meeting_type": meeting_type,
                "confidence": confidence,
                "timeline_mode": mode,
            },
            triggered_by="auto",
        )

        return {
            "event_id": event_id,
            "title": title,
            "status": "success",
            "meeting_type": meeting_type,
            "confidence": confidence,
            "timeline_mode": mode,
        }

    async def _create_quick_brief(
        self, event: dict, meeting_type: str,
        confidence: str, signals: list[str], hours_until: float,
    ) -> dict:
        """Emergency mode: skip outline, generate prep doc directly, send for approval.

        One message, one action. No outline proposal, no race condition.
        """
        event_id = event.get("id", "")
        title = event.get("title", "Untitled Meeting")
        start_time = event.get("start", "")

        logger.info(f"Quick brief for '{title}' — emergency mode ({hours_until:.1f}h)")

        # Generate outline data (still need context), then immediately generate doc
        outline = await generate_prep_outline(event, meeting_type)
        gathered_data = outline.get("sections", [])

        from config.meeting_prep_templates import get_template
        template = get_template(meeting_type)

        # Generate full document
        prep_document = format_prep_document_v2(
            event=event, template=template,
            gathered_data=gathered_data, focus_instructions=None,
        )

        # Submit as meeting_prep (standard approve/reject — one message).
        # Drive upload happens AFTER Eyal approves (in distribute_approved_prep).
        from guardrails.sensitivity_classifier import classify_sensitivity
        sensitivity = classify_sensitivity({"title": title})
        prep_approval_id = f"prep-{event_id}"

        await submit_for_approval(
            content_type="meeting_prep",
            content={
                "title": title,
                "summary": prep_document,
                "start_time": start_time,
                "sensitivity": sensitivity,
                "meeting_type": meeting_type,
                "focus_instructions": [],
                "sections": gathered_data,
                "attendees": event.get("attendees", []),
            },
            meeting_id=prep_approval_id,
        )

        supabase_client.log_action(
            action="quick_brief_created",
            details={
                "event_id": event_id, "title": title,
                "meeting_type": meeting_type, "hours_until": hours_until,
            },
            triggered_by="auto",
        )

        return {
            "event_id": event_id, "title": title, "status": "success",
            "meeting_type": meeting_type, "timeline_mode": "emergency",
            "prep_approval_id": prep_approval_id,
        }

    def _schedule_prep_timers(self, approval_id: str, mode: str, hours_until: float) -> None:
        """Schedule reminder and auto-generate timers based on timeline mode."""
        tasks = []

        if mode == "normal":
            # Reminders at configured intervals, auto-generate at generation_lead_hours
            reminder_hours = settings.meeting_prep_reminder_hours_list
            for rh in reminder_hours:
                if hours_until > rh:
                    delay = (hours_until - rh) * 3600
                    task = asyncio.create_task(
                        self._send_prep_reminder(approval_id, delay),
                        name=f"reminder_{approval_id}_{rh}h",
                    )
                    tasks.append(task)

            # Auto-generate at generation_lead_hours before meeting
            gen_hours = settings.MEETING_PREP_GENERATION_LEAD_HOURS
            if hours_until > gen_hours:
                delay = (hours_until - gen_hours) * 3600
                task = asyncio.create_task(
                    self._auto_generate_after_delay(approval_id, delay),
                    name=f"autogen_{approval_id}",
                )
                tasks.append(task)

        elif mode == "compressed":
            # Shortened reminders: 2h, 4h
            for rh in [2, 4]:
                if hours_until > rh:
                    delay = (hours_until - rh) * 3600
                    task = asyncio.create_task(
                        self._send_prep_reminder(approval_id, delay),
                        name=f"reminder_{approval_id}_{rh}h",
                    )
                    tasks.append(task)

            # Auto-generate at 6h before
            gen_at = 6
            if hours_until > gen_at:
                delay = (hours_until - gen_at) * 3600
                task = asyncio.create_task(
                    self._auto_generate_after_delay(approval_id, delay),
                    name=f"autogen_{approval_id}",
                )
                tasks.append(task)

        elif mode == "urgent":
            # Single 2h reminder + auto-generate at 4h
            if hours_until > 2:
                delay = (hours_until - 2) * 3600
                task = asyncio.create_task(
                    self._send_prep_reminder(approval_id, delay),
                    name=f"reminder_{approval_id}_2h",
                )
                tasks.append(task)

            if hours_until > 4:
                delay = (hours_until - 4) * 3600
                task = asyncio.create_task(
                    self._auto_generate_after_delay(approval_id, delay),
                    name=f"autogen_{approval_id}",
                )
                tasks.append(task)

        if tasks:
            self._pending_prep_timers[approval_id] = tasks

            # Store next_reminder_at in content for restart recovery
            try:
                row = supabase_client.get_pending_approval(approval_id)
                if row:
                    content = row.get("content", {})
                    # Calculate next reminder time
                    if mode in ("normal", "compressed", "urgent"):
                        now = datetime.now(timezone.utc)
                        if tasks:
                            # Rough estimate for persistence
                            content["next_reminder_at"] = (
                                now + timedelta(hours=min(2, hours_until))
                            ).isoformat()
                        supabase_client.update_pending_approval(
                            approval_id, content=content
                        )
            except Exception as e:
                logger.warning(f"Failed to persist timer state for {approval_id}: {e}")

    async def _send_prep_reminder(self, approval_id: str, delay_seconds: float) -> None:
        """Send a prep outline reminder after a delay."""
        try:
            await asyncio.sleep(delay_seconds)

            row = supabase_client.get_pending_approval(approval_id)
            if not row or row.get("status") != "pending":
                return

            content = row.get("content", {})
            event = content.get("outline", {}).get("event", content.get("event", {}))
            title = event.get("title", "Unknown")

            await telegram_bot.send_to_eyal(
                f"🔔 Reminder: prep outline for '{title}' is still pending. "
                f"Tap Generate, add focus, or skip."
            )

            supabase_client.log_action(
                action="prep_reminder_sent",
                details={"approval_id": approval_id, "title": title},
                triggered_by="auto",
            )

        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(f"Error sending prep reminder for {approval_id}: {e}")

    async def _auto_generate_after_delay(self, approval_id: str, delay_seconds: float) -> None:
        """Auto-generate prep after delay if still pending."""
        try:
            await asyncio.sleep(delay_seconds)

            row = supabase_client.get_pending_approval(approval_id)
            if not row or row.get("status") != "pending":
                return

            content = row.get("content", {})
            event = content.get("outline", {}).get("event", content.get("event", {}))
            title = event.get("title", "Unknown")

            logger.info(f"Auto-generating prep for '{title}' (timeout)")

            result = await generate_meeting_prep_from_outline(approval_id)

            if result.get("status") == "success":
                await telegram_bot.send_to_eyal(
                    f"⏰ Auto-generated prep for '{title}' (no response received). "
                    f"Check your pending approvals."
                )

            supabase_client.log_action(
                action="prep_auto_generated",
                details={"approval_id": approval_id, "title": title},
                triggered_by="auto",
            )

        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(f"Error auto-generating prep for {approval_id}: {e}")

    def cancel_prep_timers(self, approval_id: str) -> None:
        """Cancel all pending timers for a prep outline. Public API for callbacks."""
        self._cancel_prep_timers(approval_id)

    def _cancel_prep_timers(self, approval_id: str) -> None:
        """Cancel all pending timers for a prep outline."""
        tasks = self._pending_prep_timers.pop(approval_id, [])
        for task in tasks:
            task.cancel()
        if tasks:
            logger.info(f"Cancelled {len(tasks)} timer(s) for {approval_id}")

    async def reconstruct_prep_timers(self) -> int:
        """
        Reconstruct prep timers from persistent state on startup.

        Queries pending prep_outline approvals and rebuilds timers
        based on stored event_start_time and timeline_mode.

        Returns:
            Number of timers reconstructed.
        """
        outlines = supabase_client.get_pending_prep_outlines()
        if not outlines:
            logger.info("No prep timers to reconstruct")
            return 0

        count = 0
        now = datetime.now(timezone.utc)

        for outline in outlines:
            approval_id = outline.get("approval_id", "")
            content = outline.get("content", {})
            start_time = content.get("event_start_time", "")
            mode = content.get("timeline_mode", "normal")
            event_id = content.get("outline", {}).get("event", {}).get("id", "")

            if not start_time:
                continue

            try:
                start_dt = datetime.fromisoformat(start_time.replace("Z", "+00:00"))
                hours_until = (start_dt - now).total_seconds() / 3600

                if hours_until <= 0:
                    # Meeting has passed — skip
                    continue

                # Add to prep_generated cache
                if event_id:
                    self._prep_generated.add(event_id)

                # Recalculate timeline mode based on current time
                current_mode = calculate_timeline_mode(hours_until)

                # Schedule timers
                self._schedule_prep_timers(approval_id, current_mode, hours_until)
                count += 1

                logger.info(
                    f"Reconstructed timers for {approval_id}: "
                    f"mode={current_mode}, hours={hours_until:.1f}"
                )

            except (ValueError, TypeError) as e:
                logger.warning(f"Error reconstructing timer for {approval_id}: {e}")

        return count

    async def generate_prep_now(self, event_id: str) -> dict:
        """Manually trigger outline creation for a specific event."""
        event = await calendar_service.get_event(event_id)
        if not event:
            return {"status": "error", "error": "Event not found"}

        return await self._create_outline_for_meeting(event)

    def _hours_until(self, start_time: str) -> float:
        """Calculate hours until a meeting starts."""
        if not start_time:
            return 24.0  # Default to normal mode
        try:
            start_dt = datetime.fromisoformat(start_time.replace("Z", "+00:00"))
            now = datetime.now(timezone.utc)
            return max(0, (start_dt - now).total_seconds() / 3600)
        except (ValueError, TypeError):
            return 24.0


# Singleton instance
meeting_prep_scheduler = MeetingPrepScheduler()
