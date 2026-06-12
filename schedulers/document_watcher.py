"""
Document watcher for detecting new team uploads in Google Drive.

This module polls the Documents folder in Google Drive for new files
and triggers the document ingestion pipeline when found.

Workflow:
1. Poll Google Drive Documents folder every N minutes
2. For each new file:
   a. Download file content (handles PDF, DOCX, TXT, MD, Google Docs)
   b. Extract text using the appropriate method
   c. Run through the document processor (summarize, embed, store)
3. Track processed files to avoid reprocessing

Usage:
    from schedulers.document_watcher import document_watcher

    await document_watcher.start()  # Runs forever with polling interval
"""

import asyncio
import logging
from datetime import datetime, timezone

from config.settings import settings
from services.google_drive import drive_service
from services.supabase_client import supabase_client
from processors.document_processor import (
    process_document,
    extract_text_by_mime_type,
)

logger = logging.getLogger(__name__)



class DocumentWatcher:
    """
    Watches the Google Drive Documents folder for new uploads and processes them.
    """

    def __init__(
        self,
        poll_interval: int | None = None,
    ):
        """
        Initialize the document watcher.

        Args:
            poll_interval: Seconds between polls (default 5 minutes).
        """
        self.poll_interval = poll_interval or settings.DOCUMENT_POLL_INTERVAL
        self._running = False
        # Per-file consecutive-failure counts. A document that throws is NOT
        # marked processed, so without this it re-fails every poll and floods
        # alert_critical_error forever. After _POISON_THRESHOLD failures we
        # quarantine it (mark processed) and alert once. [audit P4-04]
        self._failure_counts: dict[str, int] = {}

    _POISON_THRESHOLD = 3

    async def start(self) -> None:
        """
        Start the document watcher polling loop.

        Runs indefinitely until stop() is called.
        """
        if self._running:
            logger.warning("Document watcher already running")
            return

        self._running = True
        logger.info(
            f"Starting document watcher (poll interval: {self.poll_interval}s)"
        )

        while self._running:
            try:
                await self._poll_once()
                try:
                    # Reflect a sustained Drive list-poll outage in the heartbeat
                    # so /status shows it (the list call swallows the error to []
                    # to avoid a per-poll alert flood). [audit P4-06]
                    if getattr(drive_service, "last_document_poll_failed", False) is True:
                        supabase_client.upsert_scheduler_heartbeat(
                            "document_watcher", status="error",
                            details={"error": "Drive document poll failing (API unavailable)"},
                        )
                    else:
                        supabase_client.upsert_scheduler_heartbeat("document_watcher")
                except Exception:
                    pass  # Never let monitoring kill the thing being monitored
            except Exception as e:
                logger.error(f"Error in document watcher poll: {e}")
                supabase_client.log_action(
                    action="document_watcher_error",
                    details={"error": str(e)},
                    triggered_by="auto",
                )
                from core.health_monitor import check_and_alert
                await check_and_alert("document_watcher", e)
                try:
                    supabase_client.upsert_scheduler_heartbeat("document_watcher", status="error", details={"error": str(e)})
                except Exception:
                    pass

            await asyncio.sleep(self.poll_interval)

    def stop(self) -> None:
        """Stop the document watcher."""
        self._running = False
        logger.info("Document watcher stopped")

    async def _poll_once(self) -> list[dict]:
        """
        Perform a single poll for new documents.

        Returns:
            List of processing results for new files found.
        """
        logger.debug("Polling for new documents...")

        new_files = await drive_service.get_new_documents()

        if not new_files:
            logger.debug("No new documents found")
            return []

        logger.info(f"Found {len(new_files)} new document(s)")

        results = []
        for file in new_files:
            file_id = file.get("id")
            file_name = file.get("name")
            try:
                result = await self._process_new_document(file)
                # Success — clear any prior failure streak for this file.
                self._failure_counts.pop(file_id, None)
                results.append(result)
            except Exception as e:
                logger.error(f"Error processing document {file_name}: {e}")
                results.append({
                    "file_id": file_id,
                    "file_name": file_name,
                    "status": "error",
                    "error": str(e),
                })
                count = self._failure_counts.get(file_id, 0) + 1
                self._failure_counts[file_id] = count

                from core.error_alerting import alert_critical_error
                if count >= self._POISON_THRESHOLD:
                    # Poison file — quarantine so it stops re-failing every poll,
                    # and alert ONCE that we're giving up. [audit P4-04]
                    try:
                        drive_service.mark_document_processed(file_id)
                    except Exception:
                        pass
                    self._failure_counts.pop(file_id, None)
                    await alert_critical_error(
                        component="document_pipeline",
                        error_message=(
                            f"Giving up on '{file_name}' after {count} failed attempts "
                            f"(quarantined, will not retry this session): {e}"
                        ),
                    )
                elif count == 1:
                    # Alert on the FIRST failure only; suppress the noisy middle
                    # retries so a transient blip doesn't flood, but a real
                    # problem is still surfaced immediately.
                    await alert_critical_error(
                        component="document_pipeline",
                        error_message=f"Failed to process '{file_name}': {e}",
                    )

        return results

    async def _process_new_document(self, file: dict) -> dict:
        """
        Process a newly detected document file.

        Handles text extraction based on file type, then runs the
        full document processing pipeline (summarize, embed, store).

        Args:
            file: File metadata from Google Drive.

        Returns:
            Dict with processing result.
        """
        file_id = file.get("id")
        file_name = file.get("name", "")
        mime_type = file.get("mimeType", "")

        logger.info(f"Processing new document: {file_name} (mime: {mime_type})")

        # Google Docs are exported as plain text via the Drive API
        if mime_type == "application/vnd.google-apps.document":
            content = await drive_service.download_file(file_id)
        else:
            # Binary files (PDF, DOCX) or text files
            file_bytes = await drive_service.download_file_bytes(file_id)
            if not file_bytes:
                drive_service.mark_document_processed(file_id)
                return {
                    "file_id": file_id,
                    "file_name": file_name,
                    "status": "error",
                    "error": "Failed to download file",
                }
            content = extract_text_by_mime_type(file_bytes, mime_type, file_name)

        if not content or not content.strip():
            drive_service.mark_document_processed(file_id)
            return {
                "file_id": file_id,
                "file_name": file_name,
                "status": "error",
                "error": "No text content extracted",
            }

        # Derive file_type from filename extension
        file_type = None
        if "." in file_name:
            file_type = file_name.rsplit(".", 1)[-1].lower()

        # Build a clean title from the filename (strip extension)
        title = file_name
        if file_type:
            title = file_name[: -(len(file_type) + 1)]

        # Run through the document processing pipeline
        result = await process_document(
            content=content,
            title=title,
            source="drive",
            file_type=file_type,
            drive_path=file_name,
        )

        # Mark as processed
        drive_service.mark_document_processed(file_id)

        # Log to audit trail
        supabase_client.log_action(
            action="document_ingested",
            details={
                "file_id": file_id,
                "file_name": file_name,
                "document_id": result.get("document_id"),
                "document_type": result.get("document_type", "other"),
                "chunk_count": result.get("chunk_count", 0),
            },
            triggered_by="auto",
        )

        # Notify Eyal via Telegram
        await self._notify_document_ingested(title, result)

        return {
            "file_id": file_id,
            "file_name": file_name,
            "status": "processed",
            "document_id": result.get("document_id"),
            "document_type": result.get("document_type", "other"),
            "summary_length": len(result.get("summary", "")),
            "chunk_count": result.get("chunk_count", 0),
        }

    async def _notify_document_ingested(self, title: str, result: dict) -> None:
        """
        Send Telegram notification to Eyal about a newly ingested document.

        Args:
            title: Document title.
            result: Processing result dict.
        """
        try:
            from services.orchestrator.spine import comms_spine

            doc_type = result.get("document_type", "other")
            chunks = result.get("chunk_count", 0)
            summary = result.get("summary", "")
            # Truncate summary for notification
            summary_preview = summary[:200] + "..." if len(summary) > 200 else summary

            message = (
                f"New document ingested: <b>{title}</b>\n"
                f"Type: {doc_type} | Chunks: {chunks}\n"
            )
            if summary_preview:
                message += f"\n{summary_preview}"

            await comms_spine.send_to_eyal(message, parse_mode="HTML")
        except Exception as e:
            logger.warning(f"Failed to notify about document ingestion: {e}")

    async def process_file_manually(self, file_id: str) -> dict:
        """
        Manually trigger processing of a specific document file.

        Useful for reprocessing or testing.

        Args:
            file_id: Google Drive file ID.

        Returns:
            Processing result dict.
        """
        file = await drive_service.get_file_metadata(file_id)
        if not file:
            return {"status": "error", "error": "File not found"}

        return await self._process_new_document(file)


# Singleton instance
document_watcher = DocumentWatcher()
