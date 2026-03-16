"""
Tests for schedulers/email_watcher.py

Tests the email inbox watcher functionality:
- Approval reply detection
- Reply text extraction
- Inbox routing (team vs non-team)
- Attachment handling
- Question handling
- Duplicate prevention
- Error handling
"""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch


# Mock settings before importing modules that depend on it
@pytest.fixture(autouse=True)
def mock_settings_for_email_watcher():
    """Mock settings for all tests in this module."""
    mock_settings = MagicMock()
    mock_settings.ANTHROPIC_API_KEY = "test-key"
    mock_settings.SUPABASE_URL = "https://test.supabase.co"
    mock_settings.SUPABASE_KEY = "test-key"
    mock_settings.GOOGLE_CLIENT_ID = "test-client"
    mock_settings.GOOGLE_CLIENT_SECRET = "test-secret"
    mock_settings.GOOGLE_REFRESH_TOKEN = "test-token"
    mock_settings.GIANLUIGI_EMAIL = "gianluigi@cropsight.io"
    mock_settings.EYAL_EMAIL = "eyal@cropsight.io"
    mock_settings.ROYE_EMAIL = "roye@cropsight.io"
    mock_settings.PAOLO_EMAIL = "paolo@cropsight.io"
    mock_settings.YORAM_EMAIL = "yoram@cropsight.io"
    mock_settings.EYAL_TELEGRAM_ID = 123456789
    mock_settings.ROYE_TELEGRAM_ID = 987654321
    mock_settings.PAOLO_TELEGRAM_ID = None
    mock_settings.YORAM_TELEGRAM_ID = None
    mock_settings.CLAUDE_MODEL = "claude-sonnet-4-20250514"
    mock_settings.EMBEDDING_MODEL = "text-embedding-3-small"
    mock_settings.EMBEDDING_DIMENSION = 1536
    mock_settings.EMBEDDING_API_KEY = "test-embedding-key"
    mock_settings.OPENAI_API_KEY = "test-openai-key"
    mock_settings.EMAIL_CHECK_INTERVAL = 300

    with patch("config.settings.settings", mock_settings):
        yield mock_settings


# =============================================================================
# Tests for _is_approval_reply()
# =============================================================================

class TestIsApprovalReply:
    """Tests for approval reply subject line detection."""

    def test_approval_needed_in_subject(self, mock_settings_for_email_watcher):
        """Should detect [APPROVAL NEEDED] in subject."""
        from schedulers.email_watcher import EmailWatcher

        watcher = EmailWatcher()
        assert watcher._is_approval_reply("[APPROVAL NEEDED] Meeting Summary: MVP Focus")

    def test_re_approval_needed_in_subject(self, mock_settings_for_email_watcher):
        """Should detect Re: [APPROVAL NEEDED] in subject."""
        from schedulers.email_watcher import EmailWatcher

        watcher = EmailWatcher()
        assert watcher._is_approval_reply(
            "Re: [APPROVAL NEEDED] Meeting Summary: MVP Focus"
        )

    def test_case_insensitive(self, mock_settings_for_email_watcher):
        """Should be case insensitive."""
        from schedulers.email_watcher import EmailWatcher

        watcher = EmailWatcher()
        assert watcher._is_approval_reply("[approval needed] Summary")
        assert watcher._is_approval_reply("[Approval Needed] Summary")

    def test_regular_email_not_approval(self, mock_settings_for_email_watcher):
        """Should not match regular email subjects."""
        from schedulers.email_watcher import EmailWatcher

        watcher = EmailWatcher()
        assert not watcher._is_approval_reply("Meeting Summary: MVP Focus")
        assert not watcher._is_approval_reply("Quick question about the budget")
        assert not watcher._is_approval_reply("")

    def test_approval_keyword_without_brackets(self, mock_settings_for_email_watcher):
        """Should not match 'approval' without the [APPROVAL NEEDED] format."""
        from schedulers.email_watcher import EmailWatcher

        watcher = EmailWatcher()
        assert not watcher._is_approval_reply("Need your approval on something")


# =============================================================================
# Tests for _extract_reply_text()
# =============================================================================

class TestExtractReplyText:
    """Tests for extracting reply content from email body."""

    def test_strips_quoted_text_on_marker(self, mock_settings_for_email_watcher):
        """Should strip quoted text starting with 'On ...'."""
        from schedulers.email_watcher import EmailWatcher

        watcher = EmailWatcher()
        body = "Approve\n\nOn Mon, Feb 24, 2026 at 10:00 AM Gianluigi wrote:\n> original text"
        result = watcher._extract_reply_text(body)
        assert result == "Approve"

    def test_strips_chevron_quoted_text(self, mock_settings_for_email_watcher):
        """Should strip text starting with '>'."""
        from schedulers.email_watcher import EmailWatcher

        watcher = EmailWatcher()
        body = "Looks good to me\n> Previous message here"
        result = watcher._extract_reply_text(body)
        assert result == "Looks good to me"

    def test_strips_outlook_original_marker(self, mock_settings_for_email_watcher):
        """Should strip Outlook-style '--- Original' marker."""
        from schedulers.email_watcher import EmailWatcher

        watcher = EmailWatcher()
        body = "APPROVE\n--- Original Message ---\nBlah blah"
        result = watcher._extract_reply_text(body)
        assert result == "APPROVE"

    def test_strips_from_forwarded_marker(self, mock_settings_for_email_watcher):
        """Should strip 'From:' forwarded marker."""
        from schedulers.email_watcher import EmailWatcher

        watcher = EmailWatcher()
        body = "Change deadline to March 5\nFrom: gianluigi@cropsight.io"
        result = watcher._extract_reply_text(body)
        assert result == "Change deadline to March 5"

    def test_strips_gianluigi_signature(self, mock_settings_for_email_watcher):
        """Should strip Gianluigi's own signature."""
        from schedulers.email_watcher import EmailWatcher

        watcher = EmailWatcher()
        body = "Reject\n---\nGianluigi, CropSight AI Assistant"
        result = watcher._extract_reply_text(body)
        assert result == "Reject"

    def test_no_quoted_text(self, mock_settings_for_email_watcher):
        """Should return full text when there's no quoted portion."""
        from schedulers.email_watcher import EmailWatcher

        watcher = EmailWatcher()
        body = "What was decided about cloud providers?"
        result = watcher._extract_reply_text(body)
        assert result == "What was decided about cloud providers?"

    def test_empty_body(self, mock_settings_for_email_watcher):
        """Should handle empty body."""
        from schedulers.email_watcher import EmailWatcher

        watcher = EmailWatcher()
        assert watcher._extract_reply_text("") == ""


# =============================================================================
# Tests for _check_inbox() - routing
# =============================================================================

class TestCheckInbox:
    """Tests for inbox checking and message routing."""

    @pytest.mark.asyncio
    async def test_routes_team_question_email(self, mock_settings_for_email_watcher):
        """Should route a question email from a team member through the agent."""
        from schedulers.email_watcher import EmailWatcher

        watcher = EmailWatcher()

        mock_messages = [
            {
                "id": "msg-001",
                "from": "Eyal Zror <eyal@cropsight.io>",
                "subject": "What was our latest revenue?",
                "body": "Can you look up our latest revenue numbers?",
                "attachments": [],
            }
        ]

        with patch("schedulers.email_watcher.gmail_service") as mock_gmail, \
             patch("schedulers.email_watcher.supabase_client") as mock_supa, \
             patch("schedulers.email_watcher.is_team_email", return_value=True), \
             patch("schedulers.email_watcher.get_team_member_by_email",
                   return_value={"name": "Eyal Zror", "role": "CEO"}):
            mock_gmail.get_unread_messages = AsyncMock(return_value=mock_messages)
            mock_gmail.mark_as_read = AsyncMock(return_value=True)
            mock_gmail.send_email = AsyncMock(return_value=True)

            # Mock the agent import inside _handle_question
            mock_agent = AsyncMock()
            mock_agent.process_message = AsyncMock(
                return_value={"response": "Revenue is $1M."}
            )
            with patch(
                "core.agent.gianluigi_agent", mock_agent
            ):
                await watcher._check_inbox()

            # Should have marked as read
            mock_gmail.mark_as_read.assert_called_once_with("msg-001")
            # Should be in processed IDs
            assert "msg-001" in watcher._processed_ids

    @pytest.mark.asyncio
    async def test_skips_non_team_email(self, mock_settings_for_email_watcher):
        """Should skip and mark as read emails from non-team members."""
        from schedulers.email_watcher import EmailWatcher

        watcher = EmailWatcher()

        mock_messages = [
            {
                "id": "msg-002",
                "from": "spammer@spam.com",
                "subject": "You won a prize!",
                "body": "Click here to claim...",
                "attachments": [],
            }
        ]

        with patch("schedulers.email_watcher.gmail_service") as mock_gmail, \
             patch("schedulers.email_watcher.supabase_client"), \
             patch("schedulers.email_watcher.is_team_email", return_value=False):
            mock_gmail.get_unread_messages = AsyncMock(return_value=mock_messages)
            mock_gmail.mark_as_read = AsyncMock(return_value=True)

            await watcher._check_inbox()

            # Should mark non-team email as read
            mock_gmail.mark_as_read.assert_called_once_with("msg-002")
            # Should be in processed IDs
            assert "msg-002" in watcher._processed_ids

    @pytest.mark.asyncio
    async def test_routes_approval_reply(self, mock_settings_for_email_watcher):
        """Should route approval reply emails to approval handler."""
        from schedulers.email_watcher import EmailWatcher

        watcher = EmailWatcher()

        meeting_id = "abcd1234-5678-9abc-def0-123456789abc"
        mock_messages = [
            {
                "id": "msg-003",
                "from": "Eyal Zror <eyal@cropsight.io>",
                "subject": f"Re: [APPROVAL NEEDED] Meeting Summary: MVP Focus [ref:{meeting_id[:8]}]",
                "body": "Approve\n\nOn Mon, Feb 24...",
                "attachments": [],
            }
        ]

        with patch("schedulers.email_watcher.gmail_service") as mock_gmail, \
             patch("schedulers.email_watcher.supabase_client") as mock_supa, \
             patch("schedulers.email_watcher.is_team_email", return_value=True), \
             patch("schedulers.email_watcher.get_team_member_by_email",
                   return_value={"name": "Eyal Zror", "role": "CEO"}), \
             patch(
                 "guardrails.approval_flow.process_response",
                 new_callable=AsyncMock,
                 return_value={"action": "approved", "next_step": "distributed"},
             ) as mock_process:
            mock_gmail.get_unread_messages = AsyncMock(return_value=mock_messages)
            mock_gmail.mark_as_read = AsyncMock(return_value=True)
            # Return a pending approval whose id starts with our ref prefix
            mock_supa.get_pending_approvals.return_value = [
                {"approval_id": meeting_id, "status": "pending"}
            ]

            await watcher._check_inbox()

            # Should have called process_response with the full meeting_id
            mock_process.assert_called_once()
            call_kwargs = mock_process.call_args[1]
            assert call_kwargs["meeting_id"] == meeting_id
            assert call_kwargs["response_source"] == "email"

    @pytest.mark.asyncio
    async def test_routes_attachment_email(self, mock_settings_for_email_watcher):
        """Should route emails with attachments to document ingestion."""
        from schedulers.email_watcher import EmailWatcher

        watcher = EmailWatcher()

        mock_messages = [
            {
                "id": "msg-004",
                "from": "Roye Tadmor <roye@cropsight.io>",
                "subject": "New spec document",
                "body": "Attached the latest spec.",
                "attachments": [
                    {
                        "filename": "spec.txt",
                        "mimeType": "text/plain",
                        "attachmentId": "att-001",
                        "size": 1024,
                    }
                ],
            }
        ]

        with patch("schedulers.email_watcher.gmail_service") as mock_gmail, \
             patch("schedulers.email_watcher.supabase_client") as mock_supa, \
             patch("schedulers.email_watcher.is_team_email", return_value=True), \
             patch("schedulers.email_watcher.get_team_member_by_email",
                   return_value={"name": "Roye Tadmor", "role": "CTO"}), \
             patch("services.embeddings.embedding_service") as mock_embed:
            mock_gmail.get_unread_messages = AsyncMock(return_value=mock_messages)
            mock_gmail.mark_as_read = AsyncMock(return_value=True)
            mock_gmail.download_attachment = AsyncMock(
                return_value=b"This is the spec content."
            )

            mock_supa.create_document = MagicMock(
                return_value={"id": "doc-001", "title": "spec.txt"}
            )
            mock_supa.store_embeddings_batch = MagicMock(return_value=[])

            mock_embed.chunk_and_embed_document = AsyncMock(
                return_value=[
                    {
                        "text": "This is the spec content.",
                        "embedding": [0.1] * 1536,
                        "chunk_index": 0,
                        "metadata": {"document_id": "doc-001"},
                    }
                ]
            )

            await watcher._check_inbox()

            # Should have downloaded the attachment
            mock_gmail.download_attachment.assert_called_once_with(
                message_id="msg-004",
                attachment_id="att-001",
            )
            # Should have created a document record
            mock_supa.create_document.assert_called_once()
            # Should have stored embeddings
            mock_supa.store_embeddings_batch.assert_called_once()

    @pytest.mark.asyncio
    async def test_duplicate_prevention(self, mock_settings_for_email_watcher):
        """Should skip already-processed messages."""
        from schedulers.email_watcher import EmailWatcher

        watcher = EmailWatcher()
        # Pre-mark this message as processed
        watcher._processed_ids.add("msg-005")

        mock_messages = [
            {
                "id": "msg-005",
                "from": "Eyal Zror <eyal@cropsight.io>",
                "subject": "Already processed",
                "body": "This was already seen.",
                "attachments": [],
            }
        ]

        with patch("schedulers.email_watcher.gmail_service") as mock_gmail, \
             patch("schedulers.email_watcher.supabase_client"):
            mock_gmail.get_unread_messages = AsyncMock(return_value=mock_messages)
            mock_gmail.mark_as_read = AsyncMock()

            await watcher._check_inbox()

            # Should NOT have called mark_as_read (skipped entirely)
            mock_gmail.mark_as_read.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_unread_messages(self, mock_settings_for_email_watcher):
        """Should handle empty inbox gracefully."""
        from schedulers.email_watcher import EmailWatcher

        watcher = EmailWatcher()

        with patch("schedulers.email_watcher.gmail_service") as mock_gmail, \
             patch("schedulers.email_watcher.supabase_client"):
            mock_gmail.get_unread_messages = AsyncMock(return_value=[])

            # Should not raise
            await watcher._check_inbox()
            assert len(watcher._processed_ids) == 0

    @pytest.mark.asyncio
    async def test_error_handling_single_message(
        self, mock_settings_for_email_watcher
    ):
        """Single message failure should not stop processing of other messages."""
        from schedulers.email_watcher import EmailWatcher

        watcher = EmailWatcher()

        mock_messages = [
            {
                "id": "msg-bad",
                "from": "Eyal Zror <eyal@cropsight.io>",
                "subject": "This will fail",
                "body": "Will cause an error",
                "attachments": [],
            },
            {
                "id": "msg-good",
                "from": "not-a-team-member@external.com",
                "subject": "This should still process",
                "body": "Second message",
                "attachments": [],
            },
        ]

        with patch("schedulers.email_watcher.gmail_service") as mock_gmail, \
             patch("schedulers.email_watcher.supabase_client") as mock_supa:
            mock_gmail.get_unread_messages = AsyncMock(return_value=mock_messages)
            mock_gmail.mark_as_read = AsyncMock(return_value=True)

            # Make is_team_email raise an exception for the first message,
            # but work normally for the second
            original_is_team = None
            call_count = 0

            def side_effect_is_team(email):
                nonlocal call_count
                call_count += 1
                if call_count == 1:
                    raise RuntimeError("Simulated failure")
                return False  # Second message is not team

            with patch(
                "schedulers.email_watcher.is_team_email",
                side_effect=side_effect_is_team,
            ):
                await watcher._check_inbox()

            # First message should have failed (not in processed_ids)
            assert "msg-bad" not in watcher._processed_ids
            # Second message should have been processed
            assert "msg-good" in watcher._processed_ids

    @pytest.mark.asyncio
    async def test_question_triggers_agent_and_reply(
        self, mock_settings_for_email_watcher
    ):
        """Question email should process with agent and send reply."""
        from schedulers.email_watcher import EmailWatcher

        watcher = EmailWatcher()

        mock_agent = AsyncMock()
        mock_agent.process_message = AsyncMock(
            return_value={"response": "Here is your answer."}
        )

        from services.conversation_memory import ConversationMemory

        with patch("schedulers.email_watcher.gmail_service") as mock_gmail, \
             patch("schedulers.email_watcher.supabase_client") as mock_supa, \
             patch("schedulers.email_watcher.conversation_memory", ConversationMemory()):
            mock_gmail.send_email = AsyncMock(return_value=True)

            with patch("core.agent.gianluigi_agent", mock_agent):
                await watcher._handle_question(
                    msg_id="msg-q1",
                    subject="Budget question",
                    body="What is our Q1 budget?",
                    sender_email="eyal@cropsight.io",
                    member_name="Eyal Zror",
                )

            # Should have called the agent with conversation history
            mock_agent.process_message.assert_called_once_with(
                user_message="What is our Q1 budget?",
                user_id="eyal",
                conversation_history=[],
            )

            # Should have sent reply email
            mock_gmail.send_email.assert_called_once()
            send_call = mock_gmail.send_email.call_args
            assert send_call[1]["to"] == ["eyal@cropsight.io"]
            assert send_call[1]["subject"] == "Re: Budget question"
            assert "Here is your answer." in send_call[1]["body"]

            # Should have logged the action (sync)
            mock_supa.log_action.assert_called_once()
            log_call = mock_supa.log_action.call_args
            assert log_call[1]["action"] == "email_question_answered"

    @pytest.mark.asyncio
    async def test_question_uses_subject_when_body_empty(
        self, mock_settings_for_email_watcher
    ):
        """Should use subject as question when body is empty."""
        from schedulers.email_watcher import EmailWatcher

        watcher = EmailWatcher()

        mock_agent = AsyncMock()
        mock_agent.process_message = AsyncMock(
            return_value={"response": "OK"}
        )

        from services.conversation_memory import ConversationMemory

        with patch("schedulers.email_watcher.gmail_service") as mock_gmail, \
             patch("schedulers.email_watcher.supabase_client"), \
             patch("schedulers.email_watcher.conversation_memory", ConversationMemory()):
            mock_gmail.send_email = AsyncMock(return_value=True)

            with patch("core.agent.gianluigi_agent", mock_agent):
                await watcher._handle_question(
                    msg_id="msg-q2",
                    subject="What is our runway?",
                    body="",
                    sender_email="eyal@cropsight.io",
                    member_name="Eyal Zror",
                )

            # Agent should receive the subject as the question
            mock_agent.process_message.assert_called_once_with(
                user_message="What is our runway?",
                user_id="eyal",
                conversation_history=[],
            )

    @pytest.mark.asyncio
    async def test_attachment_with_no_id_skipped(
        self, mock_settings_for_email_watcher
    ):
        """Attachments without an attachmentId should be skipped."""
        from schedulers.email_watcher import EmailWatcher

        watcher = EmailWatcher()

        msg = {
            "id": "msg-att-noid",
            "from": "Roye Tadmor <roye@cropsight.io>",
            "subject": "Inline image",
            "body": "See the image.",
            "attachments": [
                {
                    "filename": "image.png",
                    "mimeType": "image/png",
                    "attachmentId": "",  # empty
                    "size": 512,
                }
            ],
        }

        with patch("schedulers.email_watcher.gmail_service") as mock_gmail, \
             patch("schedulers.email_watcher.supabase_client"):
            mock_gmail.download_attachment = AsyncMock()

            await watcher._handle_attachments("msg-att-noid", msg, "Roye Tadmor")

            # Should NOT have called download_attachment
            mock_gmail.download_attachment.assert_not_called()

    @pytest.mark.asyncio
    async def test_attachment_empty_content_skipped(
        self, mock_settings_for_email_watcher
    ):
        """Attachments that download as empty bytes should be skipped."""
        from schedulers.email_watcher import EmailWatcher

        watcher = EmailWatcher()

        msg = {
            "id": "msg-att-empty",
            "attachments": [
                {
                    "filename": "empty.txt",
                    "mimeType": "text/plain",
                    "attachmentId": "att-empty",
                    "size": 0,
                }
            ],
        }

        with patch("schedulers.email_watcher.gmail_service") as mock_gmail, \
             patch("schedulers.email_watcher.supabase_client") as mock_supa:
            mock_gmail.download_attachment = AsyncMock(return_value=b"")

            await watcher._handle_attachments(
                "msg-att-empty", msg, "Roye Tadmor"
            )

            # Should not have created a document (empty content)
            mock_supa.create_document.assert_not_called()


# =============================================================================
# Tests for start/stop lifecycle
# =============================================================================

class TestEmailWatcherLifecycle:
    """Tests for start and stop behavior."""

    def test_stop_sets_running_false(self, mock_settings_for_email_watcher):
        """stop() should set _running to False."""
        from schedulers.email_watcher import EmailWatcher

        watcher = EmailWatcher()
        watcher._running = True
        watcher.stop()
        assert watcher._running is False

    def test_default_check_interval(self, mock_settings_for_email_watcher):
        """Default check interval should be 300 seconds."""
        from schedulers.email_watcher import EmailWatcher

        watcher = EmailWatcher()
        assert watcher.check_interval == 300

    def test_custom_check_interval(self, mock_settings_for_email_watcher):
        """Should accept custom check interval."""
        from schedulers.email_watcher import EmailWatcher

        watcher = EmailWatcher(check_interval=60)
        assert watcher.check_interval == 60

    def test_singleton_exists(self, mock_settings_for_email_watcher):
        """Module should export a singleton email_watcher instance."""
        from schedulers.email_watcher import email_watcher

        assert email_watcher is not None
        assert hasattr(email_watcher, "start")
        assert hasattr(email_watcher, "stop")


# =============================================================================
# Tests for email address extraction
# =============================================================================

class TestEmailExtraction:
    """Tests for email address parsing from sender field."""

    @pytest.mark.asyncio
    async def test_extracts_email_from_name_angle_brackets(
        self, mock_settings_for_email_watcher
    ):
        """Should extract email from 'Name <email>' format."""
        from schedulers.email_watcher import EmailWatcher
        import re

        watcher = EmailWatcher()

        sender = "Eyal Zror <eyal@cropsight.io>"
        email_match = re.search(r'[\w.+-]+@[\w-]+\.[\w.]+', sender)
        assert email_match is not None
        assert email_match.group(0) == "eyal@cropsight.io"

    @pytest.mark.asyncio
    async def test_extracts_plain_email(self, mock_settings_for_email_watcher):
        """Should handle plain email address without name."""
        from schedulers.email_watcher import EmailWatcher
        import re

        sender = "eyal@cropsight.io"
        email_match = re.search(r'[\w.+-]+@[\w-]+\.[\w.]+', sender)
        assert email_match is not None
        assert email_match.group(0) == "eyal@cropsight.io"


# =============================================================================
# Phase 4: Tests for _extract_and_log() and attachment filtering
# =============================================================================

class TestExtractAndLog:
    """Tests for the Phase 4 email extraction and queuing."""

    @pytest.mark.asyncio
    async def test_queues_with_approved_false(self, mock_settings_for_email_watcher):
        """Extracted emails should be queued with approved=False for morning brief."""
        from schedulers.email_watcher import EmailWatcher

        watcher = EmailWatcher()
        msg = {
            "id": "msg-123",
            "from": "Roye Tadmor <roye@cropsight.io>",
            "subject": "CropSight update",
            "body": "The Moldova pilot is progressing well.",
            "threadId": "thread-456",
        }

        with patch("schedulers.email_watcher.supabase_client") as mock_sb, \
             patch("processors.email_classifier.classify_email", new_callable=AsyncMock, return_value="relevant"), \
             patch("processors.email_classifier.extract_email_intelligence", new_callable=AsyncMock, return_value=[{"type": "information", "text": "Moldova pilot progressing"}]):

            mock_sb.is_email_already_scanned.return_value = False

            await watcher._extract_and_log(msg, "roye@cropsight.io", "Roye Tadmor")

            mock_sb.create_email_scan.assert_called_once()
            call_kwargs = mock_sb.create_email_scan.call_args
            assert call_kwargs[1]["approved"] is False
            assert call_kwargs[1]["scan_type"] == "constant"
            assert call_kwargs[1]["classification"] == "relevant"

    @pytest.mark.asyncio
    async def test_skips_already_scanned(self, mock_settings_for_email_watcher):
        """Should skip emails already in email_scans."""
        from schedulers.email_watcher import EmailWatcher

        watcher = EmailWatcher()
        msg = {"id": "msg-dupe", "subject": "Test", "body": "", "from": "x"}

        with patch("schedulers.email_watcher.supabase_client") as mock_sb:
            mock_sb.is_email_already_scanned.return_value = True

            await watcher._extract_and_log(msg, "eyal@cropsight.io", "Eyal")

            mock_sb.create_email_scan.assert_not_called()

    @pytest.mark.asyncio
    async def test_skips_approval_replies(self, mock_settings_for_email_watcher):
        """Should skip approval reply emails."""
        from schedulers.email_watcher import EmailWatcher

        watcher = EmailWatcher()
        msg = {"id": "msg-appr", "subject": "Re: [APPROVAL NEEDED] Summary", "body": "Approved", "from": "x"}

        with patch("schedulers.email_watcher.supabase_client") as mock_sb:
            mock_sb.is_email_already_scanned.return_value = False

            await watcher._extract_and_log(msg, "eyal@cropsight.io", "Eyal")

            mock_sb.create_email_scan.assert_not_called()


class TestAttachmentFiltering:
    """Tests for Phase 4 attachment type/size filtering."""

    def test_blocked_types_skipped(self, mock_settings_for_email_watcher):
        """Blocked attachment types (.exe, .zip, etc.) should be skipped."""
        from schedulers.email_watcher import BLOCKED_ATTACHMENT_TYPES
        assert ".exe" in BLOCKED_ATTACHMENT_TYPES
        assert ".zip" in BLOCKED_ATTACHMENT_TYPES
        assert ".bat" in BLOCKED_ATTACHMENT_TYPES

    def test_allowed_types(self, mock_settings_for_email_watcher):
        """Allowed attachment types should include common document formats."""
        from schedulers.email_watcher import ALLOWED_ATTACHMENT_TYPES
        assert ".pdf" in ALLOWED_ATTACHMENT_TYPES
        assert ".docx" in ALLOWED_ATTACHMENT_TYPES
        assert ".csv" in ALLOWED_ATTACHMENT_TYPES

    def test_max_attachment_size(self, mock_settings_for_email_watcher):
        """Max attachment size should be 25MB."""
        from schedulers.email_watcher import MAX_ATTACHMENT_SIZE
        assert MAX_ATTACHMENT_SIZE == 25 * 1024 * 1024
