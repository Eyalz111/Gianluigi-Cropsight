"""
Transcript watcher for detecting new Tactiq exports.

This module polls Google Drive for new transcript files and triggers
processing when found. It's the primary entry point for meeting
transcript processing.

Workflow:
1. Poll Google Drive Raw Transcripts folder every N minutes
2. For each new file:
   a. Download transcript content
   b. Extract meeting metadata from filename
   c. Check if CropSight meeting (calendar filter)
   d. If uncertain, ask Eyal
   e. If CropSight, process through pipeline
3. Track processed files to avoid reprocessing

Usage:
    from schedulers.transcript_watcher import TranscriptWatcher

    watcher = TranscriptWatcher()
    await watcher.start()  # Runs forever with polling interval
"""

import asyncio
import logging
import re
from datetime import datetime, timezone
from typing import Any

from config.settings import settings
from services.google_drive import drive_service
from services.telegram_bot import telegram_bot
from services.supabase_client import supabase_client
from processors.transcript_processor import process_transcript
from config.team import TEAM_MEMBERS
from guardrails.calendar_filter import (
    is_cropsight_meeting,
    ask_eyal_about_meeting,
    remember_meeting_classification,
    check_remembered_classification,
)
from guardrails.approval_flow import submit_for_approval

logger = logging.getLogger(__name__)

# Default polling interval in seconds (5 minutes)
DEFAULT_POLL_INTERVAL = 300


class TranscriptWatcher:
    """
    Watches Google Drive for new transcript files and processes them.
    """

    def __init__(
        self,
        poll_interval: int = DEFAULT_POLL_INTERVAL,
        auto_start: bool = False
    ):
        """
        Initialize the transcript watcher.

        Args:
            poll_interval: Seconds between polls (default 5 minutes).
            auto_start: Whether to start polling immediately.
        """
        self.poll_interval = poll_interval
        self._running = False
        self._task: asyncio.Task | None = None
        # Track files pending Eyal's classification response
        self._pending_classifications: dict[str, dict] = {}

        if auto_start:
            asyncio.create_task(self.start())

    async def start(self) -> None:
        """
        Start the transcript watcher polling loop.

        This runs indefinitely until stop() is called.
        """
        if self._running:
            logger.warning("Transcript watcher already running")
            return

        self._running = True
        logger.info(
            f"Starting transcript watcher (poll interval: {self.poll_interval}s)"
        )

        while self._running:
            try:
                await self._poll_once()
            except Exception as e:
                logger.error(f"Error in transcript watcher poll: {e}")
                # Log to audit
                supabase_client.log_action(
                    action="watcher_error",
                    details={"error": str(e)},
                    triggered_by="auto",
                )

            # Wait for next poll
            await asyncio.sleep(self.poll_interval)

    def stop(self) -> None:
        """Stop the transcript watcher."""
        self._running = False
        logger.info("Transcript watcher stopped")

    async def _poll_once(self) -> list[dict]:
        """
        Perform a single poll for new transcripts.

        Returns:
            List of processing results for new files found.
        """
        logger.debug("Polling for new transcripts...")

        # Get new files from Google Drive
        new_files = await drive_service.get_new_transcripts()

        if not new_files:
            logger.debug("No new transcripts found")
            return []

        logger.info(f"Found {len(new_files)} new transcript(s)")

        results = []
        for file in new_files:
            try:
                result = await self._process_new_file(file)
                results.append(result)
            except Exception as e:
                logger.error(f"Error processing file {file.get('name')}: {e}")
                results.append({
                    "file_id": file.get("id"),
                    "file_name": file.get("name"),
                    "status": "error",
                    "error": str(e),
                })

        return results

    async def _process_new_file(self, file: dict) -> dict:
        """
        Process a newly detected transcript file.

        Args:
            file: File metadata from Google Drive.

        Returns:
            Dict with processing result.
        """
        file_id = file.get("id")
        file_name = file.get("name", "")

        logger.info(f"Processing new file: {file_name}")

        # Extract meeting metadata from filename
        # Expected format: "YYYY-MM-DD - Meeting Title.txt"
        # or "Meeting Title - YYYY-MM-DD.txt"
        metadata = self._parse_filename(file_name)

        # Download content
        content = await drive_service.download_file(file_id)
        if not content:
            return {
                "file_id": file_id,
                "file_name": file_name,
                "status": "error",
                "error": "Failed to download file content",
            }

        # Build event dict for calendar filter
        event = {
            "id": file_id,
            "title": metadata["title"],
            "start": metadata["date"],
            "attendees": self._extract_participants_from_transcript(content),
        }

        # Check if CropSight meeting
        is_cropsight = await self._check_cropsight_meeting(event)

        if is_cropsight is False:
            # Definitely not CropSight - skip processing
            logger.info(f"Skipping non-CropSight meeting: {file_name}")
            drive_service.mark_file_processed(file_id)
            return {
                "file_id": file_id,
                "file_name": file_name,
                "status": "skipped",
                "reason": "Not a CropSight meeting",
            }

        if is_cropsight is None:
            # Uncertain - ask Eyal
            logger.info(f"Uncertain meeting, asking Eyal: {file_name}")
            await ask_eyal_about_meeting(event, telegram_bot)
            # Store for later processing when Eyal responds
            self._pending_classifications[file_id] = {
                "file": file,
                "content": content,
                "metadata": metadata,
                "event": event,
            }
            return {
                "file_id": file_id,
                "file_name": file_name,
                "status": "pending_classification",
                "reason": "Waiting for Eyal's response",
            }

        # CropSight meeting - process through pipeline
        return await self._run_processing_pipeline(
            file_id=file_id,
            file_name=file_name,
            content=content,
            metadata=metadata,
        )

    async def _run_processing_pipeline(
        self,
        file_id: str,
        file_name: str,
        content: str,
        metadata: dict
    ) -> dict:
        """
        Run the full transcript processing pipeline.

        Args:
            file_id: Google Drive file ID.
            file_name: Original filename.
            content: Transcript content.
            metadata: Extracted metadata (title, date, participants).

        Returns:
            Processing result dict.
        """
        logger.info(f"Running processing pipeline for: {metadata['title']}")

        # Check if this meeting was already processed (avoid duplicates on restart)
        # Use ilike for partial match since path may include folder prefix
        existing = supabase_client.client.table("meetings").select("id").ilike(
            "source_file_path", f"%{file_name}"
        ).execute()
        if existing.data:
            logger.info(
                f"Meeting already processed (source: {file_name}), skipping"
            )
            drive_service.mark_file_processed(file_id)
            return {
                "file_id": file_id,
                "file_name": file_name,
                "status": "already_processed",
                "meeting_id": existing.data[0]["id"],
            }

        # Extract participants from transcript if not in metadata
        participants = metadata.get("participants", [])
        if not participants:
            participants = self._extract_participants_from_transcript(content)

        # Process transcript
        result = await process_transcript(
            file_content=content,
            meeting_title=metadata["title"],
            meeting_date=metadata["date"],
            participants=participants,
            source_file_path=file_name,
        )

        # Mark file as processed
        drive_service.mark_file_processed(file_id)

        # Submit for Eyal's approval
        if result.get("meeting_id"):
            await submit_for_approval(
                content_type="meeting_summary",
                content={
                    "meeting_id": result["meeting_id"],
                    "title": metadata["title"],
                    "summary": result.get("summary", ""),
                    "decisions": result.get("decisions", []),
                    "tasks": result.get("tasks", []),
                    "follow_ups": result.get("follow_ups", []),
                    "open_questions": result.get("open_questions", []),
                    "discussion_summary": result.get("discussion_summary", ""),
                },
                meeting_id=result["meeting_id"],
            )

        return {
            "file_id": file_id,
            "file_name": file_name,
            "status": "processed",
            "meeting_id": result.get("meeting_id"),
            "summary_length": len(result.get("summary", "")),
            "decisions_count": len(result.get("decisions", [])),
            "tasks_count": len(result.get("tasks", [])),
        }

    async def _check_cropsight_meeting(self, event: dict) -> bool | None:
        """
        Check if a meeting is CropSight-related.

        For transcripts, we check:
        1. Remembered classifications
        2. Calendar filter (title prefixes, blocklist)
        3. Team member names in transcript speakers (2+ = CropSight)

        Args:
            event: Event dict with title, attendees (speaker names).

        Returns:
            True if CropSight, False if not, None if uncertain.
        """
        title = event.get("title", "")

        # Check if we've seen a similar meeting before
        remembered = check_remembered_classification(title)
        if remembered is not None:
            return remembered

        # Use the calendar filter for title-based checks (blocklist + prefixes).
        # Pass a copy without attendees since transcript attendees are name strings,
        # not email dicts — the calendar filter's participant check would crash.
        title_only_event = {"title": event.get("title", ""), "attendees": []}
        filter_result = is_cropsight_meeting(title_only_event)
        if filter_result is not None:
            return filter_result

        # For transcripts: check if 2+ known team member names are speakers
        speaker_names = event.get("attendees", [])
        if isinstance(speaker_names, list) and speaker_names:
            known_first_names = {
                m["name"].split()[0].lower() for m in TEAM_MEMBERS.values()
            }
            team_speakers = [
                name for name in speaker_names
                if name.split()[0].lower() in known_first_names
            ]
            if len(team_speakers) >= 2:
                logger.info(
                    f"CropSight meeting detected: {len(team_speakers)} team members "
                    f"({', '.join(team_speakers)})"
                )
                return True

        # Uncertain
        return None

    def _parse_filename(self, filename: str) -> dict:
        """
        Extract meeting metadata from transcript filename.

        Handles formats:
        - "YYYY-MM-DD - Meeting Title.txt"
        - "Meeting Title - YYYY-MM-DD.txt"
        - "Meeting Title.txt" (uses today's date)

        Args:
            filename: Original filename.

        Returns:
            Dict with title, date, and empty participants list.
        """
        # Remove extension
        name = re.sub(r'\.(txt|md|docx?)$', '', filename, flags=re.IGNORECASE)

        # Pattern: Date at start
        date_start = re.match(
            r'^(\d{4}-\d{2}-\d{2})\s*[-–]\s*(.+)$',
            name
        )
        if date_start:
            return {
                "date": date_start.group(1),
                "title": date_start.group(2).strip(),
                "participants": [],
            }

        # Pattern: Date at end
        date_end = re.match(
            r'^(.+)\s*[-–]\s*(\d{4}-\d{2}-\d{2})$',
            name
        )
        if date_end:
            return {
                "date": date_end.group(2),
                "title": date_end.group(1).strip(),
                "participants": [],
            }

        # No date found - use today
        return {
            "date": datetime.now().strftime("%Y-%m-%d"),
            "title": name.strip(),
            "participants": [],
        }

    def _extract_participants_from_transcript(
        self,
        content: str
    ) -> list[str]:
        """
        Extract participant names from transcript content.

        Handles both Tactiq formats:
        - Bracketed:   [HH:MM:SS] Speaker: text
        - Unbracketed: MM:SS Speaker: text

        Args:
            content: Transcript text.

        Returns:
            List of unique participant names (Title Case).
        """
        # Try bracketed format first: [HH:MM:SS] Speaker:
        speaker_pattern_bracketed = r'\[\d{1,2}:\d{2}(?::\d{2})?\]\s*([^:\[\]]+):'
        matches = re.findall(speaker_pattern_bracketed, content)

        if not matches:
            # Fall back to unbracketed Tactiq format: MM:SS Speaker:
            speaker_pattern_unbracketed = r'^\d{1,2}:\d{2}(?::\d{2})?\s+([^:\d][^:]+):'
            matches = re.findall(speaker_pattern_unbracketed, content, re.MULTILINE)

        # Return unique names preserving order, normalized to Title Case
        seen = set()
        participants = []
        for name in matches:
            name_clean = name.strip().title()
            if name_clean and name_clean not in seen:
                seen.add(name_clean)
                participants.append(name_clean)

        return participants

    async def handle_classification_response(
        self,
        file_id: str,
        is_cropsight: bool
    ) -> dict:
        """
        Handle Eyal's response to a meeting classification question.

        Called by the Telegram callback handler.

        Args:
            file_id: Google Drive file ID of the pending file.
            is_cropsight: Eyal's classification decision.

        Returns:
            Processing result dict.
        """
        if file_id not in self._pending_classifications:
            logger.warning(f"No pending classification for file: {file_id}")
            return {"status": "error", "error": "No pending classification"}

        pending = self._pending_classifications.pop(file_id)

        # Remember this classification for future similar meetings
        remember_meeting_classification(
            pending["metadata"]["title"],
            is_cropsight
        )

        if not is_cropsight:
            # Mark as processed and skip
            drive_service.mark_file_processed(file_id)
            return {
                "file_id": file_id,
                "file_name": pending["file"].get("name"),
                "status": "skipped",
                "reason": "Eyal classified as not CropSight",
            }

        # Process the meeting
        return await self._run_processing_pipeline(
            file_id=file_id,
            file_name=pending["file"].get("name"),
            content=pending["content"],
            metadata=pending["metadata"],
        )

    async def process_file_manually(self, file_id: str) -> dict:
        """
        Manually trigger processing of a specific file.

        Useful for reprocessing or testing.

        Args:
            file_id: Google Drive file ID.

        Returns:
            Processing result dict.
        """
        # Get file metadata
        file = await drive_service.get_file_metadata(file_id)
        if not file:
            return {"status": "error", "error": "File not found"}

        # Download content
        content = await drive_service.download_file(file_id)
        if not content:
            return {"status": "error", "error": "Failed to download file"}

        # Parse metadata
        metadata = self._parse_filename(file.get("name", ""))

        # Process directly (skip CropSight check for manual processing)
        return await self._run_processing_pipeline(
            file_id=file_id,
            file_name=file.get("name", ""),
            content=content,
            metadata=metadata,
        )


# Singleton instance
transcript_watcher = TranscriptWatcher()
