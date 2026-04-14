"""
Personal email scanner for daily scan of Eyal's personal Gmail.

Connects read-only to Eyal's personal Gmail (separate OAuth).
Runs filter chain, classifies/extracts, queues results for morning brief.
Does NOT send its own notifications — results feed into Morning Brief.

Usage:
    from schedulers.personal_email_scanner import personal_email_scanner
    stats = await personal_email_scanner.run_daily_scan()
"""

import base64
import logging
from datetime import date, datetime, timedelta

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

from config.settings import settings
from config.team import passes_email_filter_chain
from processors.email_classifier import (
    classify_email,
    extract_email_intelligence,
    build_filter_keywords,
)
from services.supabase_client import supabase_client

logger = logging.getLogger(__name__)


class PersonalEmailScanner:
    """Scans Eyal's personal Gmail for CropSight-relevant emails."""

    def __init__(self):
        self._service = None

    def _build_service(self):
        """Build Gmail API service with Eyal's OAuth (gmail.readonly scope)."""
        if not settings.EYAL_GMAIL_REFRESH_TOKEN:
            raise RuntimeError("EYAL_GMAIL_REFRESH_TOKEN not configured")
        if not settings.GOOGLE_CLIENT_ID or not settings.GOOGLE_CLIENT_SECRET:
            raise RuntimeError("Google OAuth credentials not configured")

        credentials = Credentials(
            token=None,
            refresh_token=settings.EYAL_GMAIL_REFRESH_TOKEN,
            token_uri="https://oauth2.googleapis.com/token",
            client_id=settings.GOOGLE_CLIENT_ID,
            client_secret=settings.GOOGLE_CLIENT_SECRET,
            scopes=["https://www.googleapis.com/auth/gmail.readonly"],
        )

        if credentials.expired or not credentials.token:
            try:
                credentials.refresh(Request())
            except Exception as e:
                # Token refresh failed — alert Eyal and degrade gracefully
                logger.error(f"Personal Gmail OAuth refresh failed: {e}")
                try:
                    from services.telegram_bot import telegram_bot
                    import asyncio
                    loop = asyncio.get_event_loop()
                    if loop.is_running():
                        asyncio.create_task(
                            telegram_bot.send_to_eyal(
                                f"Personal Gmail OAuth token refresh failed: {type(e).__name__}. "
                                f"Run scripts/reauth_google.py to re-authenticate."
                            )
                        )
                except Exception:
                    pass  # Best-effort alert
                raise

        self._service = build("gmail", "v1", credentials=credentials)
        return self._service

    @property
    def service(self):
        if self._service is None:
            self._build_service()
        return self._service

    def _execute_with_retry(self, request_factory, max_retries: int = 3, base_delay: float = 1.0):
        """
        Execute a Gmail API request with retry on transient errors.

        Mirrors services/gmail.py::_execute_with_retry. On broken pipe /
        connection reset, null the service so the next request_factory()
        call builds a fresh httplib2 transport.
        """
        import time

        for attempt in range(max_retries):
            try:
                return request_factory().execute()
            except (ConnectionError, TimeoutError, OSError) as e:
                if attempt < max_retries - 1:
                    delay = base_delay * (2 ** attempt)
                    logger.warning(
                        f"Personal Gmail API retry {attempt + 1}/{max_retries}: "
                        f"{type(e).__name__}: {e}. Rebuilding service, retrying in {delay:.1f}s..."
                    )
                    self._service = None
                    time.sleep(delay)
                else:
                    raise
            except Exception as e:
                error_str = str(e).lower()
                if any(k in error_str for k in (
                    "broken pipe", "connection reset", "transport",
                    "503", "429", "500", "502", "504",
                )):
                    if attempt < max_retries - 1:
                        delay = base_delay * (2 ** attempt)
                        logger.warning(
                            f"Personal Gmail API transient retry {attempt + 1}/{max_retries}: "
                            f"{type(e).__name__}: {e}. Rebuilding service, retrying in {delay:.1f}s..."
                        )
                        self._service = None
                        time.sleep(delay)
                    else:
                        raise
                else:
                    raise

    async def run_daily_scan(self) -> dict:
        """
        Run daily scan of Eyal's personal Gmail.

        Flow:
        1. Build live keyword list (cached)
        2. Load tracked thread_ids from email_scans DB
        3. Fetch yesterday's emails (metadata only)
        4. Apply whitelist filter chain
        5. Check thread overlap with constant layer
        6. Classify with Haiku
        7. For relevant: fetch full body, extract with Sonnet
        8. Log outbound as metadata only
        9. Note attachments without downloading
        10. Log all to email_scans (approved=False)

        Returns:
            Stats dict with counts.
        """
        if not settings.EMAIL_DAILY_SCAN_ENABLED:
            logger.info("Daily email scan disabled")
            return {"status": "disabled"}

        if not settings.EYAL_GMAIL_REFRESH_TOKEN:
            logger.warning("Personal email scan skipped: EYAL_GMAIL_REFRESH_TOKEN not set")
            return {"status": "unconfigured"}

        scan_id = f"daily-{date.today().isoformat()}"
        stats = {
            "fetched": 0,
            "filtered_in": 0,
            "classified_relevant": 0,
            "classified_borderline": 0,
            "classified_false_positive": 0,
            "extracted": 0,
            "outbound_logged": 0,
            "skipped_overlap": 0,
        }

        try:
            # 1. Build live keyword list
            filter_keywords = build_filter_keywords(scan_id=scan_id)

            # 2. Load tracked thread IDs from constant layer
            constant_threads = supabase_client.get_tracked_thread_ids(
                days=settings.THREAD_TRACKING_EXPIRY_DAYS,
                scan_type="constant",
            )

            # 3. Fetch yesterday's messages (metadata only)
            yesterday = (date.today() - timedelta(days=1)).strftime("%Y/%m/%d")
            messages = self._fetch_messages_sync(since=yesterday)
            stats["fetched"] = len(messages)

            if not messages:
                logger.info("No messages found in daily scan")
                return stats

            eyal_email = settings.EYAL_PERSONAL_EMAIL.lower()

            for msg_meta in messages:
                msg_id = msg_meta.get("id", "")

                # Dedup: skip if already scanned
                if supabase_client.is_email_already_scanned(msg_id):
                    continue

                headers = self._parse_headers(msg_meta)
                sender = headers.get("from", "")
                recipient = headers.get("to", "")
                subject = headers.get("subject", "")
                thread_id = msg_meta.get("threadId")
                snippet = msg_meta.get("snippet", "")

                # Detect outbound (sent by Eyal)
                sender_email = self._extract_email(sender)
                if sender_email and sender_email.lower() == eyal_email:
                    # Log outbound as metadata only — no LLM cost
                    supabase_client.create_email_scan(
                        scan_type="daily",
                        email_id=msg_id,
                        date=date.today().isoformat(),
                        sender=sender_email,
                        recipient=self._extract_email(recipient),
                        subject=subject,
                        classification="outbound_logged",
                        direction="outbound",
                        thread_id=thread_id,
                    )
                    stats["outbound_logged"] += 1
                    continue

                # 4. Thread overlap check: defer to constant layer
                if constant_threads and thread_id and thread_id in constant_threads:
                    stats["skipped_overlap"] += 1
                    continue

                # 5. Apply whitelist filter chain
                passes, reason = passes_email_filter_chain(
                    sender=sender_email or sender,
                    recipient=self._extract_email(recipient) or recipient,
                    subject=subject,
                    tracked_thread_ids=supabase_client.get_tracked_thread_ids(
                        days=settings.THREAD_TRACKING_EXPIRY_DAYS,
                    ),
                    thread_id=thread_id,
                    filter_keywords=filter_keywords,
                )

                if not passes:
                    # Log as false_positive (no body stored, no LLM cost)
                    supabase_client.create_email_scan(
                        scan_type="daily",
                        email_id=msg_id,
                        date=date.today().isoformat(),
                        sender=sender_email,
                        subject=subject,
                        classification="filtered_out",
                        direction="inbound",
                        thread_id=thread_id,
                    )
                    continue

                stats["filtered_in"] += 1

                # 6. Classify with Haiku
                classification = await classify_email(
                    sender=sender_email or sender,
                    subject=subject,
                    body_preview=snippet,
                    filter_keywords=filter_keywords,
                )

                if classification == "relevant":
                    stats["classified_relevant"] += 1
                elif classification == "borderline":
                    stats["classified_borderline"] += 1
                else:
                    stats["classified_false_positive"] += 1

                # 7. For relevant/borderline: fetch full body, extract
                extracted_items = []
                full_body = None
                if classification in ("relevant", "borderline"):
                    try:
                        full_body = self._get_full_body_sync(msg_id)
                        if full_body:
                            extracted_items = await extract_email_intelligence(
                                sender=sender_email or sender,
                                subject=subject,
                                body=full_body,
                            )
                            if extracted_items:
                                stats["extracted"] += len(extracted_items)
                    except Exception as e:
                        logger.warning(f"Body fetch/extraction failed for {msg_id}: {e}")

                # 9. Note attachments without downloading
                attachments_noted = self._note_attachments(msg_meta)

                # 10. Log to email_scans (approved=False — for morning brief)
                supabase_client.create_email_scan(
                    scan_type="daily",
                    email_id=msg_id,
                    date=date.today().isoformat(),
                    sender=sender_email,
                    recipient=self._extract_email(recipient),
                    subject=subject,
                    classification=classification,
                    extracted_items=extracted_items if extracted_items else None,
                    thread_id=thread_id,
                    approved=False,
                    direction="inbound",
                    attachments_processed=attachments_noted if attachments_noted else None,
                    body_text=full_body if classification in ("relevant", "borderline") else None,
                )

            logger.info(f"Daily email scan complete: {stats}")
            return stats

        except Exception as e:
            logger.error(f"Daily email scan failed: {e}", exc_info=True)
            return {**stats, "error": str(e)}

    def _fetch_messages_sync(
        self,
        since: str,
        max_results: int | None = None,
    ) -> list[dict]:
        """Fetch messages using format='metadata' (headers + snippet, no body)."""
        max_results = max_results or settings.EMAIL_MAX_SCAN_RESULTS
        try:
            result = self._execute_with_retry(
                lambda: self.service.users().messages().list(
                    userId="me",
                    q=f"after:{since}",
                    maxResults=max_results,
                )
            )

            message_ids = result.get("messages", [])
            messages = []
            for msg_ref in message_ids[:max_results]:
                msg = self._execute_with_retry(
                    lambda mid=msg_ref["id"]: self.service.users().messages().get(
                        userId="me",
                        id=mid,
                        format="metadata",
                        metadataHeaders=["From", "To", "Subject", "Date"],
                    )
                )
                messages.append(msg)

            return messages
        except Exception as e:
            logger.error(f"Failed to fetch messages: {e}")
            return []

    def _get_full_body_sync(self, message_id: str) -> str:
        """Fetch full body only for relevant emails (second-stage fetch)."""
        try:
            msg = self._execute_with_retry(
                lambda: self.service.users().messages().get(
                    userId="me",
                    id=message_id,
                    format="full",
                )
            )

            payload = msg.get("payload", {})
            return self._extract_body_text(payload)
        except Exception as e:
            logger.error(f"Failed to fetch full body for {message_id}: {e}")
            return ""

    def _extract_body_text(self, payload: dict) -> str:
        """Extract plain text body from Gmail message payload."""
        # Check direct body
        if payload.get("mimeType") == "text/plain":
            data = payload.get("body", {}).get("data", "")
            if data:
                return base64.urlsafe_b64decode(data).decode("utf-8", errors="ignore")

        # Check parts
        parts = payload.get("parts", [])
        for part in parts:
            if part.get("mimeType") == "text/plain":
                data = part.get("body", {}).get("data", "")
                if data:
                    return base64.urlsafe_b64decode(data).decode("utf-8", errors="ignore")
            # Nested multipart
            if part.get("parts"):
                text = self._extract_body_text(part)
                if text:
                    return text

        return ""

    def _parse_headers(self, msg: dict) -> dict:
        """Parse message headers into a dict."""
        headers = {}
        payload = msg.get("payload", {})
        for header in payload.get("headers", []):
            name = header.get("name", "").lower()
            value = header.get("value", "")
            headers[name] = value
        return headers

    def _extract_email(self, sender: str) -> str:
        """Extract email address from 'Name <email>' format."""
        import re
        match = re.search(r'[\w.+-]+@[\w-]+\.[\w.]+', sender)
        return match.group(0).lower() if match else sender.lower()

    def _note_attachments(self, msg: dict) -> list[str] | None:
        """Note attachments without downloading. Returns list of 'filename (size)' strings."""
        payload = msg.get("payload", {})
        parts = payload.get("parts", [])
        noted = []
        for part in parts:
            filename = part.get("filename", "")
            if filename:
                size = part.get("body", {}).get("size", 0)
                size_kb = size // 1024 if size else 0
                noted.append(f"{filename} ({size_kb}KB)")
        return noted if noted else None


# Singleton
personal_email_scanner = PersonalEmailScanner()
