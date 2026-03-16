"""
Tests for schedulers/personal_email_scanner.py and config/team.py filter chain.

Tests the daily personal email scanning pipeline:
- Scan enable/disable and configuration guards
- Deduplication of already-scanned emails
- Outbound email logging
- Thread overlap detection with constant layer
- Filter chain application
- Classification and extraction for relevant emails
- Attachment noting without download
- Helper methods: _extract_email, _parse_headers

Tests the email filter chain in config/team.py:
- Team member whitelist
- Blocklist rejection
- Keyword matching
- Tracked thread passthrough
- Stakeholder domain matching
- Default rejection for unrelated emails
"""

import base64
import pytest
from unittest.mock import AsyncMock, MagicMock, patch


@pytest.fixture(autouse=True)
def mock_settings():
    """Mock settings for all tests in this module."""
    mock = MagicMock()
    mock.EMAIL_DAILY_SCAN_ENABLED = True
    mock.EYAL_GMAIL_REFRESH_TOKEN = "test-token"
    mock.EYAL_PERSONAL_EMAIL = "eyal@personal.com"
    mock.EMAIL_MAX_SCAN_RESULTS = 50
    mock.THREAD_TRACKING_EXPIRY_DAYS = 30
    mock.GOOGLE_CLIENT_ID = "test-id"
    mock.GOOGLE_CLIENT_SECRET = "test-secret"
    mock.model_simple = "claude-haiku"
    mock.model_agent = "claude-sonnet"
    mock.EYAL_EMAIL = "eyal@cropsight.io"
    mock.ROYE_EMAIL = "roye@cropsight.io"
    mock.PAOLO_EMAIL = "paolo@cropsight.io"
    mock.YORAM_EMAIL = "yoram@cropsight.io"
    mock.EYAL_TELEGRAM_ID = 123456789
    mock.ROYE_TELEGRAM_ID = None
    mock.PAOLO_TELEGRAM_ID = None
    mock.YORAM_TELEGRAM_ID = None
    mock.TELEGRAM_EYAL_CHAT_ID = None
    mock.personal_contacts_blocklist_list = ["blocked@personal.com"]
    mock.PERSONAL_CONTACTS_BLOCKLIST = "blocked@personal.com"
    with patch("config.settings.settings", mock), \
         patch("schedulers.personal_email_scanner.settings", mock), \
         patch("config.team.settings", mock):
        yield mock


def _make_gmail_message(
    msg_id="msg-1",
    thread_id="thread-1",
    sender="alice@example.com",
    recipient="eyal@personal.com",
    subject="Test Subject",
    snippet="Hello there",
    parts=None,
):
    """Helper to build a Gmail API metadata-format message."""
    headers = [
        {"name": "From", "value": sender},
        {"name": "To", "value": recipient},
        {"name": "Subject", "value": subject},
        {"name": "Date", "value": "Mon, 15 Mar 2026 10:00:00 +0000"},
    ]
    payload = {"headers": headers}
    if parts:
        payload["parts"] = parts
    return {
        "id": msg_id,
        "threadId": thread_id,
        "snippet": snippet,
        "payload": payload,
    }


# =============================================================================
# TestPersonalEmailScanner
# =============================================================================


class TestPersonalEmailScanner:
    """Tests for the PersonalEmailScanner class."""

    @pytest.mark.asyncio
    async def test_skip_when_disabled(self, mock_settings):
        """Returns disabled status when EMAIL_DAILY_SCAN_ENABLED is False."""
        mock_settings.EMAIL_DAILY_SCAN_ENABLED = False

        from schedulers.personal_email_scanner import PersonalEmailScanner

        scanner = PersonalEmailScanner()
        result = await scanner.run_daily_scan()

        assert result == {"status": "disabled"}

    @pytest.mark.asyncio
    async def test_skip_when_unconfigured(self, mock_settings):
        """Returns unconfigured status when no refresh token is set."""
        mock_settings.EYAL_GMAIL_REFRESH_TOKEN = ""

        from schedulers.personal_email_scanner import PersonalEmailScanner

        scanner = PersonalEmailScanner()
        result = await scanner.run_daily_scan()

        assert result == {"status": "unconfigured"}

    @pytest.mark.asyncio
    @patch("schedulers.personal_email_scanner.supabase_client")
    @patch("schedulers.personal_email_scanner.build_filter_keywords")
    async def test_dedup_already_scanned(self, mock_bfk, mock_db, mock_settings):
        """Emails already in email_scans are skipped via dedup check."""
        mock_bfk.return_value = ["cropsight"]
        mock_db.get_tracked_thread_ids.return_value = set()
        mock_db.is_email_already_scanned.return_value = True

        msg = _make_gmail_message(msg_id="already-scanned")

        from schedulers.personal_email_scanner import PersonalEmailScanner

        scanner = PersonalEmailScanner()
        scanner._fetch_messages_sync = MagicMock(return_value=[msg])

        result = await scanner.run_daily_scan()

        assert result["fetched"] == 1
        assert result["filtered_in"] == 0
        assert result["outbound_logged"] == 0
        mock_db.is_email_already_scanned.assert_called_once_with("already-scanned")
        mock_db.create_email_scan.assert_not_called()

    @pytest.mark.asyncio
    @patch("schedulers.personal_email_scanner.supabase_client")
    @patch("schedulers.personal_email_scanner.build_filter_keywords")
    async def test_outbound_logged(self, mock_bfk, mock_db, mock_settings):
        """Sent emails are logged with direction='outbound' and no LLM classification."""
        mock_bfk.return_value = ["cropsight"]
        mock_db.get_tracked_thread_ids.return_value = set()
        mock_db.is_email_already_scanned.return_value = False

        msg = _make_gmail_message(
            msg_id="out-1",
            sender="eyal@personal.com",
            recipient="partner@company.com",
            subject="Re: CropSight update",
        )

        from schedulers.personal_email_scanner import PersonalEmailScanner

        scanner = PersonalEmailScanner()
        scanner._fetch_messages_sync = MagicMock(return_value=[msg])

        result = await scanner.run_daily_scan()

        assert result["outbound_logged"] == 1
        mock_db.create_email_scan.assert_called_once()
        call_kwargs = mock_db.create_email_scan.call_args
        assert "outbound" in str(call_kwargs)

    @pytest.mark.asyncio
    @patch("schedulers.personal_email_scanner.supabase_client")
    @patch("schedulers.personal_email_scanner.build_filter_keywords")
    async def test_thread_overlap_skipped(self, mock_bfk, mock_db, mock_settings):
        """Emails in constant layer threads are skipped."""
        mock_bfk.return_value = ["cropsight"]
        mock_db.get_tracked_thread_ids.return_value = {"thread-overlap"}
        mock_db.is_email_already_scanned.return_value = False

        msg = _make_gmail_message(
            msg_id="overlap-1",
            thread_id="thread-overlap",
            sender="external@company.com",
        )

        from schedulers.personal_email_scanner import PersonalEmailScanner

        scanner = PersonalEmailScanner()
        scanner._fetch_messages_sync = MagicMock(return_value=[msg])

        result = await scanner.run_daily_scan()

        assert result["skipped_overlap"] == 1
        assert result["filtered_in"] == 0

    @pytest.mark.asyncio
    @patch("schedulers.personal_email_scanner.passes_email_filter_chain")
    @patch("schedulers.personal_email_scanner.supabase_client")
    @patch("schedulers.personal_email_scanner.build_filter_keywords")
    async def test_filter_chain_applied(self, mock_bfk, mock_db, mock_filter, mock_settings):
        """Non-matching emails are filtered out by the filter chain."""
        mock_bfk.return_value = ["cropsight"]
        mock_db.get_tracked_thread_ids.return_value = set()
        mock_db.is_email_already_scanned.return_value = False
        mock_filter.return_value = (False, "no_match")

        msg = _make_gmail_message(
            msg_id="filtered-1",
            sender="random@stranger.com",
            subject="Unrelated email",
        )

        from schedulers.personal_email_scanner import PersonalEmailScanner

        scanner = PersonalEmailScanner()
        scanner._fetch_messages_sync = MagicMock(return_value=[msg])

        result = await scanner.run_daily_scan()

        assert result["filtered_in"] == 0
        # Should log as filtered_out
        mock_db.create_email_scan.assert_called_once()
        call_kwargs = mock_db.create_email_scan.call_args
        assert "filtered_out" in str(call_kwargs)

    @pytest.mark.asyncio
    @patch("schedulers.personal_email_scanner.extract_email_intelligence")
    @patch("schedulers.personal_email_scanner.classify_email")
    @patch("schedulers.personal_email_scanner.passes_email_filter_chain")
    @patch("schedulers.personal_email_scanner.supabase_client")
    @patch("schedulers.personal_email_scanner.build_filter_keywords")
    async def test_classify_and_extract(
        self, mock_bfk, mock_db, mock_filter, mock_classify, mock_extract, mock_settings
    ):
        """Relevant emails get full body fetch and intelligence extraction."""
        mock_bfk.return_value = ["cropsight"]
        mock_db.get_tracked_thread_ids.return_value = set()
        mock_db.is_email_already_scanned.return_value = False
        mock_filter.return_value = (True, "team_member")
        mock_classify.return_value = "relevant"
        mock_extract.return_value = [{"type": "task", "text": "Follow up on deal"}]

        msg = _make_gmail_message(
            msg_id="relevant-1",
            sender="roye@cropsight.io",
            subject="CropSight sprint update",
        )

        from schedulers.personal_email_scanner import PersonalEmailScanner

        scanner = PersonalEmailScanner()
        scanner._fetch_messages_sync = MagicMock(return_value=[msg])
        scanner._get_full_body_sync = MagicMock(return_value="Full email body here.")

        result = await scanner.run_daily_scan()

        assert result["filtered_in"] == 1
        assert result["classified_relevant"] == 1
        assert result["extracted"] == 1
        mock_classify.assert_called_once()
        mock_extract.assert_called_once()
        scanner._get_full_body_sync.assert_called_once_with("relevant-1")

    def test_attachment_noting(self):
        """Attachments are noted with filename and size without downloading."""
        from schedulers.personal_email_scanner import PersonalEmailScanner

        scanner = PersonalEmailScanner()
        msg = _make_gmail_message(
            parts=[
                {
                    "filename": "proposal.pdf",
                    "mimeType": "application/pdf",
                    "body": {"size": 102400},
                },
                {
                    "filename": "logo.png",
                    "mimeType": "image/png",
                    "body": {"size": 20480},
                },
                {
                    "filename": "",
                    "mimeType": "text/plain",
                    "body": {"data": "aGVsbG8=", "size": 5},
                },
            ],
        )

        result = scanner._note_attachments(msg)

        assert result is not None
        assert len(result) == 2
        assert "proposal.pdf" in result[0]
        assert "100KB" in result[0]
        assert "logo.png" in result[1]
        assert "20KB" in result[1]

    def test_attachment_noting_no_attachments(self):
        """Returns None when no attachments are present."""
        from schedulers.personal_email_scanner import PersonalEmailScanner

        scanner = PersonalEmailScanner()
        msg = _make_gmail_message()
        result = scanner._note_attachments(msg)
        assert result is None

    def test_extract_email_helper(self):
        """Parses email address from 'Name <email>' format."""
        from schedulers.personal_email_scanner import PersonalEmailScanner

        scanner = PersonalEmailScanner()

        assert scanner._extract_email("Eyal Zror <eyal@cropsight.io>") == "eyal@cropsight.io"
        assert scanner._extract_email("plain@email.com") == "plain@email.com"
        assert scanner._extract_email("UPPER@CASE.COM") == "upper@case.com"
        assert scanner._extract_email("Name <user.name+tag@domain.co.il>") == "user.name+tag@domain.co.il"

    def test_parse_headers(self):
        """Parses Gmail metadata headers into a lowercase-keyed dict."""
        from schedulers.personal_email_scanner import PersonalEmailScanner

        scanner = PersonalEmailScanner()
        msg = _make_gmail_message(
            sender="Alice <alice@example.com>",
            recipient="bob@example.com",
            subject="Meeting Notes",
        )

        headers = scanner._parse_headers(msg)

        assert headers["from"] == "Alice <alice@example.com>"
        assert headers["to"] == "bob@example.com"
        assert headers["subject"] == "Meeting Notes"
        assert headers["date"] == "Mon, 15 Mar 2026 10:00:00 +0000"

    def test_extract_body_text_plain(self):
        """Extracts plain text from a simple text/plain payload."""
        from schedulers.personal_email_scanner import PersonalEmailScanner

        scanner = PersonalEmailScanner()
        body_text = "Hello, this is the email body."
        encoded = base64.urlsafe_b64encode(body_text.encode()).decode()

        payload = {
            "mimeType": "text/plain",
            "body": {"data": encoded},
        }

        result = scanner._extract_body_text(payload)
        assert result == body_text

    def test_extract_body_text_multipart(self):
        """Extracts plain text from multipart payload with parts."""
        from schedulers.personal_email_scanner import PersonalEmailScanner

        scanner = PersonalEmailScanner()
        body_text = "Multipart body content"
        encoded = base64.urlsafe_b64encode(body_text.encode()).decode()

        payload = {
            "mimeType": "multipart/alternative",
            "parts": [
                {
                    "mimeType": "text/plain",
                    "body": {"data": encoded},
                },
                {
                    "mimeType": "text/html",
                    "body": {"data": base64.urlsafe_b64encode(b"<p>HTML</p>").decode()},
                },
            ],
        }

        result = scanner._extract_body_text(payload)
        assert result == body_text

    def test_extract_body_text_empty(self):
        """Returns empty string when no text/plain part exists."""
        from schedulers.personal_email_scanner import PersonalEmailScanner

        scanner = PersonalEmailScanner()
        payload = {"mimeType": "text/html", "body": {"data": ""}}
        result = scanner._extract_body_text(payload)
        assert result == ""


# =============================================================================
# TestEmailFilterChain
# =============================================================================


class TestEmailFilterChain:
    """Tests for config/team.py passes_email_filter_chain and helpers."""

    @patch("config.team.is_team_email", side_effect=lambda e: e == "eyal@cropsight.io")
    def test_team_member_passes(self, _mock_team):
        """Team member email passes the filter chain."""
        from config.team import passes_email_filter_chain

        passes, reason = passes_email_filter_chain(
            sender="eyal@cropsight.io",
            recipient="someone@example.com",
            subject="Random subject",
        )
        assert passes is True
        assert reason == "team_member"

    @patch("config.team.is_team_email", side_effect=lambda e: e == "roye@cropsight.io")
    def test_team_member_as_recipient_passes(self, _mock_team):
        """Email sent TO a team member also passes."""
        from config.team import passes_email_filter_chain

        passes, reason = passes_email_filter_chain(
            sender="external@company.com",
            recipient="roye@cropsight.io",
            subject="Hello",
        )
        assert passes is True
        assert reason == "team_member"

    def test_blocked_contact_rejected(self):
        """Blocklisted email is rejected immediately."""
        from config.team import passes_email_filter_chain

        passes, reason = passes_email_filter_chain(
            sender="blocked@personal.com",
            recipient="eyal@personal.com",
            subject="CropSight meeting notes",
        )
        assert passes is False
        assert reason == "blocked_contact"

    def test_keyword_match_passes(self):
        """Subject containing a CropSight keyword passes."""
        from config.team import passes_email_filter_chain

        passes, reason = passes_email_filter_chain(
            sender="unknown@random.com",
            recipient="eyal@personal.com",
            subject="Update on Moldova pilot status",
            filter_keywords=["cropsight", "moldova", "agtech"],
        )
        assert passes is True
        assert "keyword" in reason

    def test_tracked_thread_passes(self):
        """Email in a tracked thread passes."""
        from config.team import passes_email_filter_chain

        passes, reason = passes_email_filter_chain(
            sender="unknown@random.com",
            recipient="eyal@personal.com",
            subject="Re: something",
            tracked_thread_ids={"thread-abc", "thread-xyz"},
            thread_id="thread-abc",
        )
        assert passes is True
        assert reason == "tracked_thread"

    def test_no_match_rejected(self):
        """Unrelated email with no matching rule is rejected."""
        from config.team import passes_email_filter_chain

        passes, reason = passes_email_filter_chain(
            sender="spam@nowhere.com",
            recipient="eyal@personal.com",
            subject="Buy discount watches now",
            filter_keywords=["cropsight"],
        )
        assert passes is False
        assert reason == "no_match"

    @patch("services.supabase_client.supabase_client")
    def test_stakeholder_domain_passes(self, mock_db):
        """Known organization domain from entity registry passes."""
        mock_db.list_entities.return_value = [
            {"canonical_name": "lavazza", "aliases": ["lavazza group"]},
        ]

        from config.team import passes_email_filter_chain

        passes, reason = passes_email_filter_chain(
            sender="contact@lavazza.com",
            recipient="eyal@personal.com",
            subject="Partnership discussion",
            filter_keywords=["cropsight"],
        )
        assert passes is True
        assert reason == "stakeholder_domain"

    def test_is_personal_contact_blocked(self):
        """Blocked contacts are correctly identified."""
        from config.team import is_personal_contact_blocked

        assert is_personal_contact_blocked("blocked@personal.com") is True
        assert is_personal_contact_blocked("BLOCKED@PERSONAL.COM") is True
        assert is_personal_contact_blocked("allowed@personal.com") is False

    def test_is_known_stakeholder_domain_generic_rejected(self):
        """Generic email providers are never matched as stakeholder domains."""
        from config.team import is_known_stakeholder_domain

        assert is_known_stakeholder_domain("user@gmail.com") is False
        assert is_known_stakeholder_domain("user@yahoo.com") is False
        assert is_known_stakeholder_domain("user@outlook.com") is False

    def test_is_known_stakeholder_domain_no_at_sign(self):
        """Emails without @ are rejected."""
        from config.team import is_known_stakeholder_domain

        assert is_known_stakeholder_domain("not-an-email") is False
