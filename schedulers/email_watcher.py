"""
Email inbox watcher.

Polls Gianluigi's Gmail inbox for new messages from team members.
Routes:
- Questions -> Claude agent -> reply via email
- Attachments -> document ingestion pipeline
- Approval replies -> approval flow

Usage:
    from schedulers.email_watcher import email_watcher
    await email_watcher.start()
"""

import asyncio
import logging
import re
from typing import Any

from config.settings import settings
from config.team import is_team_email, get_team_member_by_email
from services.gmail import gmail_service
from services.supabase_client import supabase_client
from services.conversation_memory import conversation_memory

logger = logging.getLogger(__name__)

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
            except Exception as e:
                logger.error(f"Error in email watcher: {e}")
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
                    await self._handle_approval_reply(msg_id, body, member_name)
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

    async def _handle_approval_reply(
        self, msg_id: str, body: str, member_name: str
    ) -> None:
        """Handle an approval reply email."""
        from guardrails.approval_flow import parse_approval_response

        logger.info(f"Processing approval reply from {member_name}")

        # Extract just the reply content (before quoted text)
        reply_text = self._extract_reply_text(body)
        parsed = parse_approval_response(reply_text)

        # Log it -- actual approval handling happens through meeting_id
        # which we'd need to extract from the subject or thread
        # supabase_client methods are SYNC -- never await
        supabase_client.log_action(
            action="approval_reply_received",
            details={
                "from": member_name,
                "action": parsed["action"],
                "source": "email",
            },
            triggered_by=member_name.lower().split()[0],  # first name
        )

        logger.info(f"Approval reply from {member_name}: {parsed['action']}")

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

            logger.info(f"Downloading attachment: {filename}")

            try:
                content_bytes = await gmail_service.download_attachment(
                    message_id=msg_id,
                    attachment_id=attachment_id,
                )

                if not content_bytes:
                    continue

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
