"""
Gmail API integration for Gianluigi's email operations.

This module handles sending and receiving emails via Gianluigi's dedicated
Gmail account (gianluigi.cropsight@gmail.com).

Capabilities:
- Send meeting summaries and prep documents to team members
- Send approval requests to Eyal
- Receive document uploads via email (v0.2)
- Receive queries from team members (v0.2)

Usage:
    from services.gmail import gmail_service

    # Send a meeting summary
    await gmail_service.send_meeting_summary(
        recipients=["eyal@...", "roye@..."],
        subject="Meeting Summary: MVP Focus",
        content="...",
        drive_link="https://drive.google.com/..."
    )

    # Send approval request to Eyal
    await gmail_service.send_approval_request(
        content="...",
        meeting_title="MVP Focus"
    )
"""

import base64
import logging
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Any

from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build

from config.settings import settings
from config.team import TEAM_MEMBERS

logger = logging.getLogger(__name__)


class GmailService:
    """
    Service for Gmail API operations.

    Uses Gianluigi's dedicated Gmail account for all email operations.
    """

    def __init__(self):
        """
        Initialize the Gmail service with credentials.
        """
        self._service = None
        self._credentials: Credentials | None = None
        self.sender_email = settings.GIANLUIGI_EMAIL

    @property
    def service(self):
        """
        Lazy initialization of Gmail API service.

        Uses OAuth2 credentials from settings.
        """
        if self._service is None:
            self._service = self._build_service()
        return self._service

    def _build_service(self):
        """Build the Gmail API service with OAuth2 credentials."""
        if not settings.GOOGLE_CLIENT_ID or not settings.GOOGLE_CLIENT_SECRET:
            raise RuntimeError("Google OAuth credentials not configured")

        if not settings.GOOGLE_REFRESH_TOKEN:
            raise RuntimeError(
                "Google refresh token not configured. "
                "Run the OAuth flow to obtain a refresh token."
            )

        # Create credentials from refresh token
        self._credentials = Credentials(
            token=None,
            refresh_token=settings.GOOGLE_REFRESH_TOKEN,
            token_uri="https://oauth2.googleapis.com/token",
            client_id=settings.GOOGLE_CLIENT_ID,
            client_secret=settings.GOOGLE_CLIENT_SECRET,
            scopes=[
                "https://www.googleapis.com/auth/gmail.send",
                "https://www.googleapis.com/auth/gmail.readonly",
                "https://www.googleapis.com/auth/gmail.modify",
            ],
        )

        # Refresh the token if needed
        if self._credentials.expired or not self._credentials.token:
            self._credentials.refresh(Request())

        return build("gmail", "v1", credentials=self._credentials)

    async def authenticate(self) -> bool:
        """
        Authenticate with Gmail API using OAuth2.

        Returns:
            True if authentication successful, False otherwise.
        """
        try:
            # Force service initialization to verify auth
            _ = self.service
            logger.info("Gmail API authentication successful")
            return True
        except Exception as e:
            logger.error(f"Gmail API authentication failed: {e}")
            return False

    # =========================================================================
    # Sending Emails
    # =========================================================================

    async def send_email(
        self,
        to: list[str],
        subject: str,
        body: str,
        cc: list[str] | None = None,
        html_body: str | None = None,
        thread_id: str | None = None,
        in_reply_to: str | None = None,
    ) -> bool:
        """
        Send an email from Gianluigi's account.

        Args:
            to: List of recipient email addresses.
            subject: Email subject line.
            body: Plain text email body.
            cc: Optional list of CC recipients.
            html_body: Optional HTML version of the body.
            thread_id: Gmail thread ID to reply in the same thread.
            in_reply_to: Message-ID of the email being replied to.

        Returns:
            True if email was sent successfully.
        """
        try:
            # Create message
            if html_body:
                message = MIMEMultipart("alternative")
                message.attach(MIMEText(body, "plain"))
                message.attach(MIMEText(html_body, "html"))
            else:
                message = MIMEText(body, "plain")

            message["From"] = self.sender_email
            message["To"] = ", ".join(to)
            message["Subject"] = subject

            if cc:
                message["Cc"] = ", ".join(cc)

            # Threading headers — makes Gmail group the reply in the same thread
            if in_reply_to:
                message["In-Reply-To"] = in_reply_to
                message["References"] = in_reply_to

            # Encode and send
            raw_message = base64.urlsafe_b64encode(
                message.as_bytes()
            ).decode("utf-8")

            send_body: dict[str, Any] = {"raw": raw_message}
            if thread_id:
                send_body["threadId"] = thread_id

            self.service.users().messages().send(
                userId="me",
                body=send_body,
            ).execute()

            logger.info(f"Email sent: {subject} to {to}")
            return True

        except Exception as e:
            logger.error(f"Error sending email: {e}")
            return False

    async def send_meeting_summary(
        self,
        recipients: list[str],
        meeting_title: str,
        summary_content: str,
        drive_link: str,
        meeting_date: str
    ) -> bool:
        """
        Send a meeting summary email to team members.

        Formats the email with:
        - Subject: "Meeting Summary: {title} ({date})"
        - Body: Summary content with link to Google Drive

        Args:
            recipients: List of recipient emails.
            meeting_title: Title of the meeting.
            summary_content: The summary text (abbreviated for email).
            drive_link: Link to full summary in Google Drive.
            meeting_date: Date of the meeting.

        Returns:
            True if email was sent successfully.
        """
        subject = f"Meeting Summary: {meeting_title} ({meeting_date})"

        # Truncate content for email (keep it brief)
        preview = summary_content[:1500]
        if len(summary_content) > 1500:
            preview += "\n\n... (see full summary in Drive)"

        body = f"""Meeting Summary: {meeting_title}
Date: {meeting_date}

{preview}

---
View the full summary in Google Drive:
{drive_link}

---
This summary was generated by Gianluigi, CropSight's AI Operations Assistant.
"""

        html_body = f"""
<h2>Meeting Summary: {meeting_title}</h2>
<p><strong>Date:</strong> {meeting_date}</p>

<pre style="white-space: pre-wrap; font-family: Arial, sans-serif;">{preview}</pre>

<hr>
<p><a href="{drive_link}">View Full Summary in Google Drive</a></p>

<hr>
<p style="color: gray; font-size: 12px;">
This summary was generated by Gianluigi, CropSight's AI Operations Assistant.
</p>
"""

        return await self.send_email(
            to=recipients,
            subject=subject,
            body=body,
            html_body=html_body
        )

    async def send_approval_request(
        self,
        meeting_title: str,
        summary_preview: str,
        draft_link: str | None = None
    ) -> bool:
        """
        Send an approval request to Eyal for a meeting summary.

        Args:
            meeting_title: Title of the meeting.
            summary_preview: Preview of the summary for quick review.
            draft_link: Optional link to draft in Google Drive.

        Returns:
            True if email was sent successfully.
        """
        if not settings.EYAL_EMAIL:
            logger.warning("Eyal's email not configured")
            return False

        subject = f"[APPROVAL NEEDED] Meeting Summary: {meeting_title}"

        body = f"""Hi Eyal,

A new meeting summary is ready for your review.

Meeting: {meeting_title}

--- PREVIEW ---

{summary_preview[:2000]}
{"... (truncated)" if len(summary_preview) > 2000 else ""}

--- END PREVIEW ---

"""

        if draft_link:
            body += f"""
View the full draft in Google Drive:
{draft_link}

"""

        body += """
Please review and respond:
- Reply "APPROVE" to distribute to the team
- Reply with your edit instructions for changes
- Reply "REJECT" to discard

---
Gianluigi
"""

        return await self.send_email(
            to=[settings.EYAL_EMAIL],
            subject=subject,
            body=body
        )

    async def send_meeting_prep(
        self,
        recipients: list[str],
        meeting_title: str,
        prep_content: str,
        drive_link: str,
        meeting_date: str
    ) -> bool:
        """
        Send a meeting prep document to team members.

        Args:
            recipients: List of recipient emails.
            meeting_title: Title of the upcoming meeting.
            prep_content: The prep document content (abbreviated).
            drive_link: Link to full prep doc in Google Drive.
            meeting_date: Date of the upcoming meeting.

        Returns:
            True if email was sent successfully.
        """
        subject = f"Meeting Prep: {meeting_title} ({meeting_date})"

        preview = prep_content[:1500]
        if len(prep_content) > 1500:
            preview += "\n\n... (see full prep doc in Drive)"

        body = f"""Meeting Prep: {meeting_title}
Date: {meeting_date}

{preview}

---
View the full prep document in Google Drive:
{drive_link}

---
This prep document was generated by Gianluigi, CropSight's AI Operations Assistant.
"""

        return await self.send_email(
            to=recipients,
            subject=subject,
            body=body
        )

    async def send_weekly_digest(
        self,
        recipients: list[str],
        week_of: str,
        digest_content: str,
        drive_link: str
    ) -> bool:
        """
        Send a weekly digest to team members.

        Args:
            recipients: List of recipient emails.
            week_of: Week identifier (e.g., "2026-02-17").
            digest_content: The digest content (abbreviated).
            drive_link: Link to full digest in Google Drive.

        Returns:
            True if email was sent successfully.
        """
        subject = f"CropSight Weekly Digest - Week of {week_of}"

        preview = digest_content[:2000]
        if len(digest_content) > 2000:
            preview += "\n\n... (see full digest in Drive)"

        body = f"""CropSight Weekly Digest
Week of: {week_of}

{preview}

---
View the full digest in Google Drive:
{drive_link}

---
This digest was generated by Gianluigi, CropSight's AI Operations Assistant.
"""

        return await self.send_email(
            to=recipients,
            subject=subject,
            body=body
        )

    # =========================================================================
    # Reading Emails (v0.2)
    # =========================================================================

    async def get_unread_messages(self, max_results: int = 10) -> list[dict]:
        """
        Get unread messages from Gianluigi's inbox.

        For v0.2: Process document uploads and queries from team.

        Args:
            max_results: Maximum number of messages to return.

        Returns:
            List of message dicts with id, from, subject, body, attachments.
        """
        try:
            results = self.service.users().messages().list(
                userId="me",
                q="is:unread",
                maxResults=max_results
            ).execute()

            messages = results.get("messages", [])
            detailed_messages = []

            for msg in messages:
                full_msg = await self.get_message(msg["id"])
                if full_msg:
                    detailed_messages.append(full_msg)

            return detailed_messages

        except Exception as e:
            logger.error(f"Error fetching unread messages: {e}")
            return []

    async def get_message(self, message_id: str) -> dict | None:
        """
        Get a specific email message by ID.

        Args:
            message_id: Gmail message ID.

        Returns:
            Message dict with full details, or None if not found.
        """
        try:
            message = self.service.users().messages().get(
                userId="me",
                id=message_id,
                format="full"
            ).execute()

            headers = message.get("payload", {}).get("headers", [])
            header_dict = {h["name"]: h["value"] for h in headers}

            # Extract body
            body = self._extract_body(message.get("payload", {}))

            # Check for attachments
            attachments = self._extract_attachments(message.get("payload", {}))

            return {
                "id": message_id,
                "threadId": message.get("threadId", ""),
                "from": header_dict.get("From", ""),
                "to": header_dict.get("To", ""),
                "subject": header_dict.get("Subject", ""),
                "date": header_dict.get("Date", ""),
                "message_id": header_dict.get("Message-ID", ""),
                "body": body,
                "attachments": attachments,
                "snippet": message.get("snippet", ""),
            }

        except Exception as e:
            logger.error(f"Error fetching message {message_id}: {e}")
            return None

    async def mark_as_read(self, message_id: str) -> bool:
        """
        Mark a message as read.

        Args:
            message_id: Gmail message ID.

        Returns:
            True if successful.
        """
        try:
            self.service.users().messages().modify(
                userId="me",
                id=message_id,
                body={"removeLabelIds": ["UNREAD"]}
            ).execute()
            return True
        except Exception as e:
            logger.error(f"Error marking message as read: {e}")
            return False

    async def download_attachment(
        self,
        message_id: str,
        attachment_id: str
    ) -> bytes:
        """
        Download an email attachment.

        Args:
            message_id: Gmail message ID.
            attachment_id: Attachment ID within the message.

        Returns:
            Attachment content as bytes.
        """
        try:
            attachment = self.service.users().messages().attachments().get(
                userId="me",
                messageId=message_id,
                id=attachment_id
            ).execute()

            data = attachment.get("data", "")
            return base64.urlsafe_b64decode(data)

        except Exception as e:
            logger.error(f"Error downloading attachment: {e}")
            return b""

    # =========================================================================
    # Helper Methods
    # =========================================================================

    def _extract_body(self, payload: dict) -> str:
        """Extract plain text body from email payload."""
        if "body" in payload and payload["body"].get("data"):
            return base64.urlsafe_b64decode(
                payload["body"]["data"]
            ).decode("utf-8", errors="ignore")

        # Check parts for multipart messages
        parts = payload.get("parts", [])
        for part in parts:
            if part.get("mimeType") == "text/plain":
                if part.get("body", {}).get("data"):
                    return base64.urlsafe_b64decode(
                        part["body"]["data"]
                    ).decode("utf-8", errors="ignore")

            # Recursively check nested parts
            if "parts" in part:
                nested = self._extract_body(part)
                if nested:
                    return nested

        return ""

    def _extract_attachments(self, payload: dict) -> list[dict]:
        """Extract attachment metadata from email payload."""
        attachments = []

        parts = payload.get("parts", [])
        for part in parts:
            if part.get("filename"):
                attachments.append({
                    "filename": part["filename"],
                    "mimeType": part.get("mimeType", ""),
                    "attachmentId": part.get("body", {}).get("attachmentId", ""),
                    "size": part.get("body", {}).get("size", 0),
                })

        return attachments


# Singleton instance
gmail_service = GmailService()
