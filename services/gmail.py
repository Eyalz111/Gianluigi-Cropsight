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
import html as html_lib
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


def _html_escape(text: str) -> str:
    """Escape HTML special characters for safe embedding in email HTML."""
    return html_lib.escape(str(text))


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

    async def send_email_with_attachments(
        self,
        to: list[str],
        subject: str,
        body: str,
        html_body: str | None = None,
        attachments: list[dict] | None = None,
    ) -> bool:
        """
        Send an email with file attachments.

        Args:
            to: List of recipient email addresses.
            subject: Email subject line.
            body: Plain text email body.
            html_body: Optional HTML version of the body.
            attachments: List of dicts with filename, data (bytes), mimetype.

        Returns:
            True if email was sent successfully.
        """
        try:
            from email.mime.base import MIMEBase
            from email import encoders

            msg = MIMEMultipart("mixed")
            msg["From"] = self.sender_email
            msg["To"] = ", ".join(to)
            msg["Subject"] = subject

            # Body — use HTML directly to avoid Gmail showing
            # the plain text alternative as a separate attachment
            if html_body:
                msg.attach(MIMEText(html_body, "html"))
            else:
                msg.attach(MIMEText(body, "plain"))

            # File attachments
            for att in (attachments or []):
                mimetype = att.get("mimetype", "application/octet-stream")
                main_type, sub_type = mimetype.split("/", 1)
                part = MIMEBase(main_type, sub_type)
                part.set_payload(att["data"])
                encoders.encode_base64(part)
                part.add_header(
                    "Content-Disposition",
                    "attachment",
                    filename=att["filename"],
                )
                msg.attach(part)

            raw_message = base64.urlsafe_b64encode(
                msg.as_bytes()
            ).decode("utf-8")

            self.service.users().messages().send(
                userId="me", body={"raw": raw_message},
            ).execute()

            logger.info(f"Email with attachments sent: {subject} to {to}")
            return True

        except Exception as e:
            logger.error(f"Error sending email with attachments: {e}")
            return False

    async def send_meeting_summary(
        self,
        recipients: list[str],
        meeting_title: str,
        summary_content: str,
        drive_link: str,
        meeting_date: str,
        executive_summary: str | None = None,
        tasks: list[dict] | None = None,
        recipient_name: str | None = None,
        docx_bytes: bytes | None = None,
        discussion_summary: str | None = None,
    ) -> bool:
        """
        Send a meeting summary email to team members.

        Email body is a brief intro with TLDR and a note that the full summary
        is attached as a Word document. The Word doc contains the complete
        structured summary.

        Args:
            recipients: List of recipient emails.
            meeting_title: Title of the meeting.
            summary_content: The summary text (for plain text fallback).
            drive_link: Link to full summary in Google Drive.
            meeting_date: Date of the meeting.
            executive_summary: One-line TLDR of the meeting's key outcome.
            tasks: List of task dicts for inline table.
            recipient_name: Name of recipient for personalized action items.
            docx_bytes: Word document bytes to attach (optional).
            discussion_summary: Clean prose discussion summary (preferred for email body).

        Returns:
            True if email was sent successfully.
        """
        # Clean date format (strip timestamp if present)
        clean_date = str(meeting_date)[:10] if meeting_date else ""
        subject = f"Meeting Summary: {meeting_title} ({clean_date})"

        # Build brief plain-text body (intro only — full content in attached Word doc)
        tldr_line = f"\nTLDR: {executive_summary}\n" if executive_summary else ""
        task_count = len(tasks) if tasks else 0

        # Build a brief but scannable excerpt — prefer clean prose over markdown
        excerpt_source = discussion_summary or summary_content or ""
        excerpt = ""
        if excerpt_source:
            # Strip any leading markdown headers
            lines = excerpt_source.strip().split("\n")
            clean_lines = [l for l in lines if not l.startswith("#") and not l.startswith("---")]
            full_text = "\n".join(clean_lines)
            if len(full_text) > 500:
                # Truncate at sentence boundary
                cut = full_text[:500].rfind(".")
                if cut > 250:
                    excerpt = full_text[:cut + 1]
                else:
                    cut = full_text[:500].rfind(" ")
                    excerpt = full_text[:cut] + "..." if cut > 0 else full_text[:500] + "..."
            else:
                excerpt = full_text

        body = f"""Meeting Summary: {meeting_title}
Date: {clean_date}
{tldr_line}
{excerpt}

{task_count} action items captured. Full details in the attached Word document.

View in Google Drive: {drive_link}

---
This summary was generated by Gianluigi, CropSight's AI Operations Assistant.
"""

        # Build clean HTML body — scannable summary + attachment note
        tldr_html = (
            f'<p style="font-style: italic; color: #555; font-size: 14px;">'
            f'{_html_escape(executive_summary)}</p>'
            if executive_summary else ""
        )

        excerpt_html = ""
        if excerpt:
            # Convert basic markdown to HTML (bold, bullets)
            clean = _html_escape(excerpt)
            clean = clean.replace("\n\n", "</p><p>")
            excerpt_html = f'<p style="font-size: 14px; line-height: 1.5;">{clean}</p>'

        html_body = f"""
<h2>Meeting Summary: {_html_escape(meeting_title)}</h2>
<p><strong>Date:</strong> {clean_date}</p>
{tldr_html}

{excerpt_html}

<p><strong>{task_count} action items captured.</strong> Full details in the attached Word document.</p>

<p><a href="{drive_link}">View in Google Drive</a></p>

<hr>
<p style="color: gray; font-size: 12px;">
This summary was generated by Gianluigi, CropSight's AI Operations Assistant.
</p>
"""

        # Build email with optional Word doc attachment
        if docx_bytes:
            from email.mime.base import MIMEBase
            from email import encoders

            msg = MIMEMultipart("mixed")
            msg["From"] = self.sender_email
            msg["To"] = ", ".join(recipients)
            msg["Subject"] = subject

            # HTML body as alternative part
            body_part = MIMEMultipart("alternative")
            body_part.attach(MIMEText(body, "plain"))
            body_part.attach(MIMEText(html_body, "html"))
            msg.attach(body_part)

            # Word doc attachment
            filename = f"{meeting_date} - {meeting_title}.docx"
            attachment = MIMEBase("application", "vnd.openxmlformats-officedocument.wordprocessingml.document")
            attachment.set_payload(docx_bytes)
            encoders.encode_base64(attachment)
            attachment.add_header("Content-Disposition", f"attachment; filename=\"{filename}\"")
            msg.attach(attachment)

            # Send directly (bypass send_email helper for attachment support)
            try:
                raw_message = base64.urlsafe_b64encode(msg.as_bytes()).decode("utf-8")
                self.service.users().messages().send(
                    userId="me", body={"raw": raw_message},
                ).execute()
                logger.info(f"Email sent: {subject} to {recipients}")
                return True
            except Exception as e:
                logger.error(f"Error sending email with attachment: {e}")
                return False
        else:
            return await self.send_email(
                to=recipients,
                subject=subject,
                body=body,
                html_body=html_body,
            )

    async def send_approval_request(
        self,
        meeting_title: str,
        summary_preview: str,
        draft_link: str | None = None,
        executive_summary: str | None = None,
        decisions: list[dict] | None = None,
        tasks: list[dict] | None = None,
        follow_ups: list[dict] | None = None,
        open_questions: list[dict] | None = None,
        meeting_id: str | None = None,
    ) -> bool:
        """
        Send an approval request to Eyal for a meeting summary.

        Content matches the Telegram approval message so Eyal sees
        the same information regardless of channel.

        Args:
            meeting_title: Title of the meeting.
            summary_preview: Preview of the discussion summary.
            draft_link: Optional link to draft in Google Drive.
            executive_summary: One-line TLDR of the meeting's key outcome.
            decisions: List of decision dicts from extraction.
            tasks: List of task dicts from extraction.
            follow_ups: List of follow-up meeting dicts.
            open_questions: List of open question dicts.
            meeting_id: Optional meeting UUID — first 8 chars embedded in
                        subject as [ref:...] so email replies can be routed.

        Returns:
            True if email was sent successfully.
        """
        if not settings.EYAL_EMAIL:
            logger.warning("Eyal's email not configured")
            return False

        decisions = decisions or []
        tasks = tasks or []
        follow_ups = follow_ups or []
        open_questions = open_questions or []

        # Include meeting_id short prefix so email replies can be routed
        ref_tag = f" [ref:{meeting_id[:8]}]" if meeting_id else ""
        subject = f"[APPROVAL NEEDED] Meeting Summary: {meeting_title}{ref_tag}"

        # --- HTML body (matches Telegram approval format) ---
        html_parts = [f"<h2>Approval Request: {_html_escape(meeting_title)}</h2>"]

        if executive_summary:
            html_parts.append(f"<p><em>{_html_escape(executive_summary)}</em></p>")

        if decisions:
            html_parts.append(f"<h3>Decisions ({len(decisions)})</h3><ol>")
            for d in decisions:
                html_parts.append(f"<li>{_html_escape(d.get('description', ''))}</li>")
            html_parts.append("</ol>")

        if tasks:
            rows = ""
            for t in tasks:
                priority = t.get("priority", "M")
                title = _html_escape(t.get("title", ""))
                assignee = t.get("assignee", "team")
                deadline = t.get("deadline") or "—"
                rows += (
                    f"<tr>"
                    f"<td style='padding:4px 8px;border:1px solid #ddd;'>[{priority}]</td>"
                    f"<td style='padding:4px 8px;border:1px solid #ddd;'>{title}</td>"
                    f"<td style='padding:4px 8px;border:1px solid #ddd;'>{assignee}</td>"
                    f"<td style='padding:4px 8px;border:1px solid #ddd;'>{deadline}</td>"
                    f"</tr>"
                )
            html_parts.append(
                f"<h3>Action Items ({len(tasks)})</h3>"
                f"<table style='border-collapse:collapse;width:100%;'>"
                f"<tr style='background:#f5f5f5;'>"
                f"<th style='padding:4px 8px;border:1px solid #ddd;text-align:left;'>Pri</th>"
                f"<th style='padding:4px 8px;border:1px solid #ddd;text-align:left;'>Task</th>"
                f"<th style='padding:4px 8px;border:1px solid #ddd;text-align:left;'>Assignee</th>"
                f"<th style='padding:4px 8px;border:1px solid #ddd;text-align:left;'>Deadline</th>"
                f"</tr>{rows}</table>"
            )

        if follow_ups:
            html_parts.append(f"<h3>Follow-up Meetings ({len(follow_ups)})</h3><ul>")
            for fu in follow_ups:
                title = _html_escape(fu.get("title", ""))
                led_by = fu.get("led_by", "")
                html_parts.append(f"<li>{title} (led by {led_by})</li>")
            html_parts.append("</ul>")

        if open_questions:
            html_parts.append(f"<h3>Open Questions ({len(open_questions)})</h3><ul>")
            for q in open_questions:
                question = _html_escape(q.get("question", ""))
                raised_by = q.get("raised_by", "")
                suffix = f" <em>(raised by {raised_by})</em>" if raised_by else ""
                html_parts.append(f"<li>{question}{suffix}</li>")
            html_parts.append("</ul>")

        if summary_preview:
            excerpt = summary_preview[:2000]
            if len(summary_preview) > 2000:
                excerpt += "..."
            html_parts.append(f"<h3>Discussion Summary</h3>")
            html_parts.append(
                f"<pre style='white-space:pre-wrap;font-family:Arial,sans-serif;'>"
                f"{_html_escape(excerpt)}</pre>"
            )

        if draft_link:
            html_parts.append(f'<p><a href="{draft_link}">View Full Draft in Google Drive</a></p>')

        html_parts.append("<hr>")
        html_parts.append(
            "<p>Please review and respond:<br>"
            "- Reply <b>APPROVE</b> to distribute to the team<br>"
            "- Reply with your edit instructions for changes<br>"
            "- Reply <b>REJECT</b> to discard</p>"
        )
        html_parts.append(
            '<p style="color:gray;font-size:12px;">— Gianluigi</p>'
        )

        html_body = "\n".join(html_parts)

        # --- Plain text fallback ---
        body = f"Approval Request: {meeting_title}\n\n"
        if executive_summary:
            body += f"TLDR: {executive_summary}\n\n"
        if summary_preview:
            body += f"{summary_preview[:2000]}\n\n"
        body += (
            "Please review and respond:\n"
            '- Reply "APPROVE" to distribute to the team\n'
            "- Reply with your edit instructions for changes\n"
            '- Reply "REJECT" to discard\n\n'
            "--- Gianluigi"
        )

        return await self.send_email(
            to=[settings.EYAL_EMAIL],
            subject=subject,
            body=body,
            html_body=html_body,
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
