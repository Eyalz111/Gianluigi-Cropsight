"""
Gianluigi - CropSight's AI Operations Assistant

Main entry point for the application.

This module:
1. Initializes all services (Telegram, Gmail, Drive, etc.)
2. Starts the Google Drive watcher for new transcripts
3. Runs the Telegram bot for user interaction
4. Starts scheduled tasks (meeting prep, task reminders)
5. Handles graceful shutdown

Usage:
    python main.py

For development:
    python main.py --debug
"""

import asyncio
import logging
import signal
import sys
from typing import NoReturn

from config.settings import settings
from core.logging_config import setup_logging


# Configure logging (JSON in production, plain text in dev)
setup_logging(level=settings.LOG_LEVEL, environment=settings.ENVIRONMENT)
logger = logging.getLogger("gianluigi")


# Global shutdown flag
_shutdown_event: asyncio.Event | None = None


async def initialize_services() -> dict:
    """
    Initialize all service connections.

    Returns:
        Dict with initialization status for each service.
    """
    from services.supabase_client import supabase_client
    from services.telegram_bot import telegram_bot
    from services.google_drive import drive_service
    from services.google_calendar import calendar_service
    from services.google_sheets import sheets_service
    from services.gmail import gmail_service
    from services.embeddings import embedding_service

    status = {}

    # Initialize Supabase
    logger.info("Initializing Supabase...")
    try:
        # Verify connection by listing tables
        supabase_client.client.table("meetings").select("id").limit(1).execute()
        status["supabase"] = True
        logger.info("  Supabase: OK")
    except Exception as e:
        status["supabase"] = False
        logger.error(f"  Supabase: Error - {e}")

    # Initialize Google Drive
    logger.info("Initializing Google Drive...")
    try:
        drive_ok = await drive_service.authenticate()
        status["google_drive"] = drive_ok
        if drive_ok:
            logger.info("  Google Drive: OK")
        else:
            logger.warning("  Google Drive: Authentication failed")
    except Exception as e:
        status["google_drive"] = False
        logger.error(f"  Google Drive: Error - {e}")

    # Initialize Google Calendar
    logger.info("Initializing Google Calendar...")
    try:
        calendar_ok = await calendar_service.authenticate()
        status["google_calendar"] = calendar_ok
        if calendar_ok:
            logger.info("  Google Calendar: OK")
        else:
            logger.warning("  Google Calendar: Authentication failed")
    except Exception as e:
        status["google_calendar"] = False
        logger.error(f"  Google Calendar: Error - {e}")

    # Initialize Google Sheets
    logger.info("Initializing Google Sheets...")
    try:
        sheets_ok = await sheets_service.authenticate()
        status["google_sheets"] = sheets_ok
        if sheets_ok:
            logger.info("  Google Sheets: OK")
        else:
            logger.warning("  Google Sheets: Authentication failed")
    except Exception as e:
        status["google_sheets"] = False
        logger.error(f"  Google Sheets: Error - {e}")

    # Initialize Gmail
    logger.info("Initializing Gmail...")
    try:
        gmail_ok = await gmail_service.authenticate()
        status["gmail"] = gmail_ok
        if gmail_ok:
            logger.info("  Gmail: OK")
        else:
            logger.warning("  Gmail: Authentication failed")
    except Exception as e:
        status["gmail"] = False
        logger.error(f"  Gmail: Error - {e}")

    # Initialize Embeddings
    logger.info("Initializing Embeddings service...")
    try:
        embeddings_ok = await embedding_service.health_check()
        status["embeddings"] = embeddings_ok
        if embeddings_ok:
            logger.info("  Embeddings: OK")
        else:
            logger.warning("  Embeddings: Not available")
    except Exception as e:
        status["embeddings"] = False
        logger.error(f"  Embeddings: Error - {e}")

    return status


async def start_services() -> None:
    """
    Initialize and start all Gianluigi services.

    Services started:
    - Telegram bot (for user interaction)
    - Google Drive watcher (for new transcripts)
    - Meeting prep scheduler
    - Task reminder scheduler
    """
    global _shutdown_event
    _shutdown_event = asyncio.Event()

    logger.info("Starting Gianluigi services...")

    # Start HTTP server early (Cloud Run needs HTTP on PORT for liveness probe).
    # When MCP_AUTH_TOKEN is set, use the MCP server (includes health routes).
    # Otherwise, fall back to the lightweight aiohttp health server.
    _http_server = None
    if settings.MCP_AUTH_TOKEN:
        from services.mcp_server import mcp_server
        _http_server = mcp_server
        try:
            mcp_task = asyncio.create_task(
                mcp_server.start(), name="mcp_server"
            )
            # Give uvicorn a moment to bind the port
            await asyncio.sleep(0.5)
            logger.info("MCP server started (includes health routes)")
        except Exception as e:
            logger.warning(f"MCP server failed to start (non-fatal): {e}")
    else:
        from services.health_server import health_server
        _http_server = health_server
        try:
            await health_server.start()
        except Exception as e:
            logger.warning(f"Health server failed to start (non-fatal): {e}")

    # Validate configuration
    errors = settings.validate_required()
    if errors:
        for error in errors:
            logger.error(error)
        raise RuntimeError("Configuration validation failed")

    # Log optional configuration warnings
    warnings = settings.validate_optional()
    for warning in warnings:
        logger.warning(warning)

    # Initialize all services
    init_status = await initialize_services()

    # Check critical services
    critical_services = ["supabase"]
    for service in critical_services:
        if not init_status.get(service):
            raise RuntimeError(f"Critical service '{service}' failed to initialize")

    # Import and start background tasks
    from services.telegram_bot import telegram_bot
    from schedulers.transcript_watcher import transcript_watcher
    from schedulers.document_watcher import document_watcher
    from schedulers.meeting_prep_scheduler import meeting_prep_scheduler
    from schedulers.task_reminder_scheduler import task_reminder_scheduler

    logger.info("Starting background services...")

    # Create tasks for all background services
    tasks = []

    # Include MCP server task if it was started
    if settings.MCP_AUTH_TOKEN:
        tasks.append(mcp_task)

    # Start Telegram bot
    logger.info("  Starting Telegram bot...")
    telegram_task = asyncio.create_task(
        telegram_bot.start(),
        name="telegram_bot"
    )
    tasks.append(telegram_task)

    # Start transcript watcher (only if enabled and Google Drive is available)
    if settings.TRANSCRIPT_WATCHER_ENABLED and init_status.get("google_drive"):
        logger.info("  Starting transcript watcher...")
        watcher_task = asyncio.create_task(
            transcript_watcher.start(),
            name="transcript_watcher"
        )
        tasks.append(watcher_task)
    elif not settings.TRANSCRIPT_WATCHER_ENABLED:
        logger.info("  Transcript watcher disabled (TRANSCRIPT_WATCHER_ENABLED=false)")

        # Start document watcher (polls Documents folder for team uploads)
        if settings.DOCUMENTS_FOLDER_ID:
            logger.info("  Starting document watcher...")
            doc_watcher_task = asyncio.create_task(
                document_watcher.start(),
                name="document_watcher"
            )
            tasks.append(doc_watcher_task)
        else:
            logger.warning("  Document watcher disabled (DOCUMENTS_FOLDER_ID not set)")
    else:
        logger.warning("  Transcript watcher disabled (Google Drive not available)")
        logger.warning("  Document watcher disabled (Google Drive not available)")

    # Start meeting prep scheduler (only if Calendar is available)
    if init_status.get("google_calendar"):
        logger.info("  Starting meeting prep scheduler...")
        prep_task = asyncio.create_task(
            meeting_prep_scheduler.start(),
            name="meeting_prep_scheduler"
        )
        tasks.append(prep_task)
    else:
        logger.warning("  Meeting prep scheduler disabled (Google Calendar not available)")

    # Start weekly review scheduler (if enabled and calendar available)
    if init_status.get("google_calendar") and settings.WEEKLY_REVIEW_ENABLED:
        from schedulers.weekly_review_scheduler import weekly_review_scheduler
        logger.info("  Starting weekly review scheduler...")
        review_task = asyncio.create_task(
            weekly_review_scheduler.start(),
            name="weekly_review_scheduler"
        )
        tasks.append(review_task)

    # Digest always starts — it self-skips when a review session exists
    if init_status.get("google_calendar"):
        from schedulers.weekly_digest_scheduler import weekly_digest_scheduler
        logger.info("  Starting weekly digest scheduler...")
        digest_task = asyncio.create_task(
            weekly_digest_scheduler.start(),
            name="weekly_digest_scheduler"
        )
        tasks.append(digest_task)
    else:
        logger.warning("  Weekly digest/review schedulers disabled (Google Calendar not available)")

    # Task reminder scheduler — time-window filters added (C2), safe to enable
    if init_status.get("google_sheets"):
        logger.info("  Starting task reminder scheduler...")
        reminder_task = asyncio.create_task(
            task_reminder_scheduler.start(),
            name="task_reminder_scheduler"
        )
        tasks.append(reminder_task)
    else:
        logger.warning("  Task reminder scheduler disabled (Google Sheets not available)")

    # Alert scheduler — time-window filters added (C2), safe to enable
    from schedulers.alert_scheduler import alert_scheduler
    logger.info("  Starting alert scheduler...")
    alert_task = asyncio.create_task(
        alert_scheduler.start(),
        name="alert_scheduler"
    )
    tasks.append(alert_task)

    # Start orphan cleanup scheduler (always — only needs Supabase + Telegram)
    from schedulers.orphan_cleanup_scheduler import orphan_cleanup_scheduler
    logger.info("  Starting orphan cleanup scheduler...")
    cleanup_task = asyncio.create_task(
        orphan_cleanup_scheduler.start(),
        name="orphan_cleanup_scheduler"
    )
    tasks.append(cleanup_task)

    # Start email watcher (only if Gmail is available)
    if init_status.get("gmail"):
        from schedulers.email_watcher import email_watcher
        logger.info("  Starting email watcher...")
        email_watcher_task = asyncio.create_task(
            email_watcher.start(),
            name="email_watcher"
        )
        tasks.append(email_watcher_task)
    else:
        logger.warning("  Email watcher disabled (Gmail not available)")

    # Start morning brief scheduler (Phase 4)
    if settings.MORNING_BRIEF_ENABLED:
        from schedulers.morning_brief_scheduler import morning_brief_scheduler
        logger.info("  Starting morning brief scheduler...")
        brief_task = asyncio.create_task(
            morning_brief_scheduler.start(),
            name="morning_brief_scheduler"
        )
        tasks.append(brief_task)
    else:
        logger.info("  Morning brief scheduler disabled (MORNING_BRIEF_ENABLED=false)")

    # Start evening debrief prompt scheduler (Phase 11 C4)
    if settings.DEBRIEF_EVENING_PROMPT_ENABLED:
        from schedulers.debrief_prompt_scheduler import debrief_prompt_scheduler
        logger.info("  Starting evening debrief prompt scheduler...")
        debrief_prompt_task = asyncio.create_task(
            debrief_prompt_scheduler.start(),
            name="debrief_prompt_scheduler"
        )
        tasks.append(debrief_prompt_task)
    else:
        logger.info("  Evening debrief prompt disabled (DEBRIEF_EVENING_PROMPT_ENABLED=false)")

    logger.info("=" * 50)
    logger.info("  Gianluigi is ready!")
    logger.info("=" * 50)

    # Signal readiness to Cloud Run health check
    _http_server.set_ready(True)

    # Reconstruct auto-publish timers from persistent state (v0.4)
    from guardrails.approval_flow import reconstruct_auto_publish_timers
    try:
        reconstructed = await reconstruct_auto_publish_timers()
        if reconstructed:
            logger.info(f"  Reconstructed {reconstructed} auto-publish timer(s)")
    except Exception as e:
        logger.warning(f"  Timer reconstruction failed (non-fatal): {e}")

    # NOTE: reconstruct_prep_timers() now runs inside start() before the main loop.
    # This external call is kept as a safety net but is effectively a no-op.
    try:
        prep_reconstructed = await meeting_prep_scheduler.reconstruct_prep_timers()
        if prep_reconstructed:
            logger.info(f"  Reconstructed {prep_reconstructed} additional prep timer(s)")
    except Exception as e:
        logger.warning(f"  Prep timer reconstruction failed (non-fatal): {e}")

    # Reconstruct interactive session stack from persistent state (Phase 6)
    try:
        reconstructed = await telegram_bot._reconstruct_session_stack()
        if reconstructed:
            logger.info(f"  Reconstructed {reconstructed} interactive session(s)")
    except Exception as e:
        logger.warning(f"  Session stack reconstruction failed (non-fatal): {e}")

    # Log to Supabase
    from services.supabase_client import supabase_client
    supabase_client.log_action(
        action="startup",
        details={
            "services_initialized": init_status,
            "tasks_started": len(tasks),
        },
        triggered_by="auto",
    )

    # Wait for shutdown signal or any task to fail
    try:
        # Wait for either shutdown or a task failure
        done, pending = await asyncio.wait(
            tasks + [asyncio.create_task(_shutdown_event.wait())],
            return_when=asyncio.FIRST_COMPLETED
        )

        # Check if any task failed
        for task in done:
            if task.get_name() != "_shutdown_event.wait":
                if task.exception():
                    logger.error(
                        f"Task {task.get_name()} failed: {task.exception()}"
                    )

    except asyncio.CancelledError:
        logger.info("Tasks cancelled")

    # Cancel remaining tasks
    for task in pending:
        task.cancel()

    # Wait for cancellation to complete
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)


async def stop_services() -> None:
    """
    Gracefully stop all services.
    """
    global _shutdown_event

    logger.info("Stopping Gianluigi services...")

    # Signal shutdown
    if _shutdown_event:
        _shutdown_event.set()

    # Stop schedulers
    from schedulers.transcript_watcher import transcript_watcher
    from schedulers.document_watcher import document_watcher
    from schedulers.meeting_prep_scheduler import meeting_prep_scheduler
    from schedulers.task_reminder_scheduler import task_reminder_scheduler
    from schedulers.weekly_digest_scheduler import weekly_digest_scheduler
    from schedulers.email_watcher import email_watcher
    from schedulers.alert_scheduler import alert_scheduler
    from schedulers.orphan_cleanup_scheduler import orphan_cleanup_scheduler

    transcript_watcher.stop()
    document_watcher.stop()
    meeting_prep_scheduler.stop()
    task_reminder_scheduler.stop()
    weekly_digest_scheduler.stop()
    email_watcher.stop()
    alert_scheduler.stop()
    orphan_cleanup_scheduler.stop()

    # Stop weekly review scheduler if started
    if settings.WEEKLY_REVIEW_ENABLED:
        try:
            from schedulers.weekly_review_scheduler import weekly_review_scheduler
            weekly_review_scheduler.stop()
        except Exception:
            pass

    # Stop morning brief scheduler if started
    if settings.MORNING_BRIEF_ENABLED:
        try:
            from schedulers.morning_brief_scheduler import morning_brief_scheduler
            morning_brief_scheduler.stop()
        except Exception:
            pass

    # Stop debrief prompt scheduler if started
    if settings.DEBRIEF_EVENING_PROMPT_ENABLED:
        try:
            from schedulers.debrief_prompt_scheduler import debrief_prompt_scheduler
            debrief_prompt_scheduler.stop()
        except Exception:
            pass

    # Stop Telegram bot
    from services.telegram_bot import telegram_bot
    await telegram_bot.stop()

    # Stop HTTP server (MCP or health server)
    if settings.MCP_AUTH_TOKEN:
        from services.mcp_server import mcp_server
        await mcp_server.stop()
    else:
        from services.health_server import health_server
        await health_server.stop()

    # Log shutdown
    from services.supabase_client import supabase_client
    supabase_client.log_action(
        action="shutdown",
        details={},
        triggered_by="auto",
    )

    logger.info("Gianluigi stopped.")


def handle_signal(sig: int, frame) -> NoReturn:
    """
    Handle shutdown signals (SIGINT, SIGTERM).
    """
    logger.info(f"Received signal {sig}, initiating shutdown...")

    # Schedule the async stop
    loop = asyncio.get_event_loop()
    loop.create_task(stop_services())


async def main() -> None:
    """
    Main entry point.
    """
    # Register signal handlers for graceful shutdown (Unix only)
    if sys.platform != "win32":
        signal.signal(signal.SIGINT, handle_signal)
        signal.signal(signal.SIGTERM, handle_signal)

    try:
        await start_services()
    except KeyboardInterrupt:
        logger.info("Keyboard interrupt received")
        await stop_services()
    except Exception as e:
        logger.exception(f"Fatal error: {e}")
        await stop_services()
        sys.exit(1)


if __name__ == "__main__":
    # Check Python version
    if sys.version_info < (3, 11):
        print("Error: Python 3.11+ required")
        sys.exit(1)

    # Parse command line arguments
    debug_mode = "--debug" in sys.argv

    if debug_mode:
        logging.getLogger().setLevel(logging.DEBUG)
        logger.debug("Debug mode enabled")

    # Run the application
    logger.info("=" * 50)
    logger.info("  Gianluigi - CropSight AI Operations Assistant")
    logger.info("=" * 50)

    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Interrupted by user")
