"""
Email inbox watcher.

Polls Gianluigi's Gmail inbox for new messages from team members.
Routes:
- Questions -> Claude agent -> reply via email
- Attachments -> document ingestion pipeline
- Approval replies -> approval flow

Phase 4 addition: After routing, classify + extract intelligence from
team emails and queue for the morning brief (approved=False).

Usage:
    from schedulers.email_watcher import email_watcher
    await email_watcher.start()
"""

import asyncio
import logging
import os
import re
from datetime import date
from typing import Any

from config.settings import settings
from config.team import is_team_email, get_team_member_by_email
from services.gmail import gmail_service
from services.supabase_client import supabase_client
from services.conversation_memory import conversation_memory

logger = logging.getLogger(__name__)


# Attachment filtering
ALLOWED_ATTACHMENT_TYPES = {".pdf", ".docx", ".xlsx", ".pptx", ".txt", ".csv"}
BLOCKED_ATTACHMENT_TYPES = {".exe", ".zip", ".dmg", ".bat", ".png", ".jpg", ".gif"}
MAX_ATTACHMENT_SIZE = 25 * 1024 * 1024  # 25MB


class EmailWatcher:
    """Watches Gianluigi's inbox for team emails."""

    def __init__(self, check_interval: int | None = None):
        self.check_interval = check_interval or settings.EMAIL_CHECK_INTERVAL
        self._running = False
        self._processed_ids: set[str] = set()  # Track processed message IDs

    async def start(self) -> None:
        """Start the email watcher loop."""
        if self._running:
            logger.warning("Email watcher already running")
            return
        self._running = True
        logger.info(f"Starting email watcher (interval: {self.check_interval}s)")

        while self._running:
            try:
                await self._check_inbox()
                try:
                    supabase_client.upsert_scheduler_heartbeat("email_watcher")
                except Exception:
                    pass  # Never let monitoring kill the thing being monitored
            except Exception as e:
                logger.error(f"Error in email watcher: {e}")
                from core.health_monitor import check_and_alert
                await check_and_alert("email_watcher", e)
                try:
                    supabase_client.upsert_scheduler_heartbeat("email_watcher", status="error", details={"error": str(e)})
                except Exception:
                    pass
            await asyncio.sleep(self.check_interval)

    def stop(self) -> None:
        """Stop the email watcher loop."""
        self._running = False
        logger.info("Email watcher stopped")

    async def _check_inbox(self) -> None:
        """Check for new unread messages and route them."""
        messages = await gmail_service.get_unread_messages(max_results=10)

        if not messages:
            return

        for msg in messages:
            msg_id = msg.get("id", "")

            # Skip if already processed
            if msg_id in self._processed_ids:
                continue

            sender = msg.get("from", "")
            subject = msg.get("subject", "")
            body = msg.get("body", "")
            attachments = msg.get("attachments", [])

            logger.info(f"Processing email from {sender}: {subject}")

            try:
                # Extract email address from "Name <email>" format
                email_match = re.search(r'[\w.+-]+@[\w-]+\.[\w.]+', sender)
                sender_email = email_match.group(0) if email_match else sender

                # Only process emails from team members
                if not is_team_email(sender_email):
                    logger.debug(f"Skipping non-team email from {sender_email}")
                    await gmail_service.mark_as_read(msg_id)
                    self._processed_ids.add(msg_id)
                    continue

                # Route the email
                team_member = get_team_member_by_email(sender_email)
                member_name = team_member["name"] if team_member else sender_email

                # Check if this is an approval reply
                if self._is_approval_reply(subject):
                    await self._handle_approval_reply(msg_id, body, member_name, subject)
                # Check if it has attachments
                elif attachments:
                    await self._handle_attachments(msg_id, msg, member_name)
                # Otherwise treat as a question
                else:
                    await self._handle_question(
                        msg_id, subject, body, sender_email, member_name,
                        thread_id=msg.get("threadId"),
                        message_id=msg.get("message_id"),
                    )

                # Phase 4: After existing routing, extract and log for morning brief
                await self._extract_and_log(
                    msg=msg,
                    sender_email=sender_email,
                    member_name=member_name,
                )

                # Mark as read
                await gmail_service.mark_as_read(msg_id)
                self._processed_ids.add(msg_id)

            except Exception as e:
                logger.error(f"Error processing email {msg_id}: {e}")

    def _is_approval_reply(self, subject: str) -> bool:
        """Check if the email is a reply to an approval request."""
        subject_lower = subject.lower()
        return (
            "[approval needed]" in subject_lower
            or "re: [approval needed]" in subject_lower
        )

    async def _extract_and_log(
        self,
        msg: dict,
        sender_email: str,
        member_name: str,
    ) -> None:
        """
        Classify, extract, and queue to email_scans (approved=False for morning brief).

        Runs after existing routing. Does NOT send a separate notification.
        Queued items are included in the next morning brief.
        """
        msg_id = msg.get("id", "")
        subject = msg.get("subject", "")
        body = msg.get("body", "")
        thread_id = msg.get("threadId")

        # Skip if already scanned (dedup)
        if supabase_client.is_email_already_scanned(msg_id):
            return

        # Skip approval replies — they're routing artifacts, not intelligence
        if self._is_approval_reply(subject):
            return

        try:
            from processors.email_classifier import classify_email, extract_email_intelligence

            classification = await classify_email(
                sender=sender_email,
                subject=subject,
                body_preview=body[:500],
            )

            extracted_items = []
            if classification in ("relevant", "borderline"):
                extracted_items = await extract_email_intelligence(
                    sender=sender_email,
                    subject=subject,
                    body=body,
                )

            # Log to email_scans — queued for morning brief, NOT auto-approved
            supabase_client.create_email_scan(
                scan_type="constant",
                email_id=msg_id,
                date=date.today().isoformat(),
                sender=sender_email,
                subject=subject,
                classification=classification,
                extracted_items=extracted_items if extracted_items else None,
                thread_id=thread_id,
                approved=False,
                direction="inbound",
                body_text=body if classification in ("relevant", "borderline") else None,
            )

            if extracted_items:
                logger.info(
                    f"Email intelligence: {len(extracted_items)} items from {member_name} "
                    f"(classification: {classification})"
                )

        except Exception as e:
            logger.error(f"Email extraction failed for {msg_id}: {e}")

    async def _handle_approval_reply(
        self, msg_id: str, body: str, member_name: str, subject: str
    ) -> None:
        """
        Handle an approval reply email.

        Extracts the meeting_id reference from the subject line,
        looks up the pending approval, and calls process_response().
        """
        from guardrails.approval_flow import process_response

        logger.info(f"Processing approval reply from {member_name}")

        # Extract just the reply content (before quoted text)
        reply_text = self._extract_reply_text(body)

        # Extract meeting_id short prefix from subject: [ref:abcd1234]
        ref_match = re.search(r'\[ref:([a-f0-9]{8})\]', subject)
        if not ref_match:
            logger.warning(
                f"Approval reply missing [ref:...] tag in subject: {subject}"
            )
            # Log and bail — can't route without meeting_id
            supabase_client.log_action(
                action="approval_reply_unroutable",
                details={"from": member_name, "subject": subject, "source": "email"},
                triggered_by=member_name.lower().split()[0],
            )
            return

        ref_prefix = ref_match.group(1)

        # Look up the full meeting_id from pending_approvals table
        pending = supabase_client.get_pending_approvals(status="pending")
        full_meeting_id = None
        for row in pending:
            aid = row.get("approval_id", "")
            if aid.startswith(ref_prefix):
                full_meeting_id = aid
                break

        if not full_meeting_id:
            logger.warning(f"No pending approval found for ref:{ref_prefix}")
            supabase_client.log_action(
                action="approval_reply_no_match",
                details={"from": member_name, "ref": ref_prefix, "source": "email"},
                triggered_by=member_name.lower().split()[0],
            )
            return

        # Process the approval response
        try:
            result = await process_response(
                meeting_id=full_meeting_id,
                response=reply_text,
                response_source="email",
            )

            action = result.get("action", "unknown")
            logger.info(f"Email approval processed: {action} for {full_meeting_id[:8]}")

            # Send confirmation to Eyal's Telegram DM
            try:
                from services.telegram_bot import telegram_bot
                if settings.TELEGRAM_EYAL_CHAT_ID:
                    await telegram_bot.send_message(
                        chat_id=settings.TELEGRAM_EYAL_CHAT_ID,
                        text=f"Email approval received: {action} (ref:{ref_prefix})",
                    )
            except Exception as e:
                logger.warning(f"Failed to send Telegram confirmation: {e}")

        except Exception as e:
            logger.error(f"Error processing email approval for {ref_prefix}: {e}")

    async def _handle_attachments(
        self, msg_id: str, msg: dict, member_name: str
    ) -> None:
        """Handle email with attachments -- ingest documents."""
        attachments = msg.get("attachments", [])

        for att in attachments:
            filename = att.get("filename", "")
            attachment_id = att.get("attachmentId", "")

            if not attachment_id:
                continue

            # Phase 4: Attachment type/size filtering
            ext = os.path.splitext(filename)[1].lower() if filename else ""
            if ext in BLOCKED_ATTACHMENT_TYPES:
                logger.info(f"Skipping blocked attachment type: {filename}")
                continue
            if ext and ext not in ALLOWED_ATTACHMENT_TYPES:
                logger.info(f"Skipping unrecognized attachment type: {filename}")
                continue
            att_size = att.get("size", 0)
            if att_size and att_size > MAX_ATTACHMENT_SIZE:
                logger.info(f"Skipping oversized attachment: {filename} ({att_size} bytes)")
                continue

            logger.info(f"Downloading attachment: {filename}")

            try:
                content_bytes = await gmail_service.download_attachment(
                    message_id=msg_id,
                    attachment_id=attachment_id,
                )

                if not content_bytes:
                    continue

                # Phase 13 B3: Persist attachment to Drive before processing
                drive_file_id = None
                try:
                    from config.settings import settings as _settings
                    folder_id = _settings.EMAIL_ATTACHMENTS_FOLDER_ID
                    if folder_id:
                        from services.google_drive import drive_service
                        import mimetypes
                        mime = mimetypes.guess_type(filename)[0] or "application/octet-stream"
                        drive_result = await drive_service._upload_bytes_file(
                            data=content_bytes,
                            filename=f"email_{msg_id}_{filename}",
                            folder_id=folder_id,
                            mime_type=mime,
                        )
                        drive_file_id = drive_result.get("id")
                        logger.info(f"Persisted attachment to Drive: {filename} ({drive_file_id})")
                except Exception as e:
                    logger.warning(f"Drive persistence failed for {filename} (non-fatal): {e}")

                # Try to decode as text for ingestion
                try:
                    content_text = content_bytes.decode("utf-8", errors="ignore")
                except Exception:
                    logger.warning(
                        f"Could not decode attachment {filename} as text"
                    )
                    continue

                # Create document record (supabase_client is SYNC)
                document = supabase_client.create_document(
                    title=filename,
                    source="email",
                    file_type=(
                        filename.rsplit(".", 1)[-1] if "." in filename else None
                    ),
                    summary=f"Uploaded via email by {member_name}",
                )

                # Generate embeddings
                from services.embeddings import embedding_service

                embedded_chunks = await embedding_service.chunk_and_embed_document(
                    document=content_text,
                    document_id=document["id"],
                )

                if embedded_chunks:
                    embedding_records = [
                        {
                            "source_type": "document",
                            "source_id": document["id"],
                            "chunk_text": chunk["text"],
                            "chunk_index": chunk["chunk_index"],
                            "embedding": chunk["embedding"],
                            "metadata": chunk.get("metadata", {}),
                        }
                        for chunk in embedded_chunks
                    ]
                    # supabase_client is SYNC
                    supabase_client.store_embeddings_batch(embedding_records)

                logger.info(
                    f"Ingested document: {filename} "
                    f"({len(embedded_chunks)} chunks)"
                )

            except Exception as e:
                logger.error(f"Error ingesting attachment {filename}: {e}")

    async def _handle_question(
        self,
        msg_id: str,
        subject: str,
        body: str,
        sender_email: str,
        member_name: str,
        thread_id: str | None = None,
        message_id: str | None = None,
    ) -> None:
        """Handle a question email -- process with Claude and reply in-thread."""
        from core.agent import gianluigi_agent

        # Build the question from subject and body
        question = body.strip() or subject

        logger.info(f"Processing question from {member_name}: {question[:100]}...")

        # Get team member ID
        member_id = member_name.lower().split()[0]  # "Eyal Zror" -> "eyal"

        # Get conversation history keyed by sender email
        history = conversation_memory.get_history(sender_email)

        try:
            result = await gianluigi_agent.process_message(
                user_message=question,
                user_id=member_id,
                conversation_history=history,
            )

            response_text = result.get("response", "I couldn't process your request.")

            # Outbound sanitization
            try:
                from guardrails.inbound_filter import sanitize_outbound_message
                response_text = sanitize_outbound_message(
                    response_text,
                    {"channel": "email", "recipient": sender_email},
                )
            except ImportError:
                pass
            except Exception as e:
                logger.warning(f"Outbound sanitization error (continuing): {e}")

            # Store conversation turn in memory
            conversation_memory.add_message(sender_email, "user", question)
            conversation_memory.add_message(sender_email, "assistant", response_text)

            # Reply via email (in the same thread as the original)
            await gmail_service.send_email(
                to=[sender_email],
                subject=f"Re: {subject}",
                body=(
                    f"Hi {member_name.split()[0]},\n\n"
                    f"{response_text}\n\n"
                    f"---\nGianluigi, CropSight AI Assistant"
                ),
                thread_id=thread_id,
                in_reply_to=message_id,
            )

            # Log (supabase_client is SYNC)
            supabase_client.log_action(
                action="email_question_answered",
                details={
                    "from": member_name,
                    "question_preview": question[:200],
                },
                triggered_by=member_id,
            )

        except Exception as e:
            logger.error(f"Error answering question from {member_name}: {e}")

    def _extract_reply_text(self, body: str) -> str:
        """Extract just the reply portion of an email (before quoted text)."""
        # Common reply markers
        markers = [
            "\nOn ",            # "On Mon, Feb 24..."
            "\n>",              # Quoted text
            "\n--- Original",   # Outlook-style
            "\nFrom:",          # Forwarded marker
            "---\nGianluigi",   # Our own signature
        ]

        text = body
        for marker in markers:
            idx = text.find(marker)
            if idx > 0:
                text = text[:idx]

        return text.strip()


# Singleton instance
email_watcher = EmailWatcher()
