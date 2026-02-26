"""
Tests for the inbound filter guardrail system.

The inbound filter (guardrails/inbound_filter.py) implements five layers:
1. Sender Verification (Telegram / Email)
2. Topic Relevance (regex-based)
3. Information Leak Prevention (sensitive pattern detection)
4. Output Sanitization (wraps leak check)
5. Audit Logging (Supabase log_action)

Plus the main entry point check_inbound_message() that orchestrates all layers.

Tests are organised by layer, matching the module structure.
"""

import pytest
from unittest.mock import MagicMock, patch


# =============================================================================
# Layer 1 — Sender Verification (Telegram)
# =============================================================================

class TestSenderVerificationTelegram:
    """Tests for verify_sender_telegram() — Telegram ID whitelist check."""

    @patch(
        "guardrails.inbound_filter.TEAM_TELEGRAM_IDS",
        {"eyal": 8190904141, "eyal zror": 8190904141},
    )
    def test_known_team_member_verified(self):
        """Eyal's Telegram ID should return verified=True with member_id='eyal'."""
        from guardrails.inbound_filter import verify_sender_telegram

        result = verify_sender_telegram(8190904141)

        assert result["verified"] is True
        assert result["member_id"] == "eyal"

    @patch(
        "guardrails.inbound_filter.TEAM_TELEGRAM_IDS",
        {"eyal": 8190904141, "eyal zror": 8190904141},
    )
    def test_unknown_telegram_id_rejected(self):
        """A random Telegram ID not in the whitelist should return verified=False."""
        from guardrails.inbound_filter import verify_sender_telegram

        result = verify_sender_telegram(9999999999)

        assert result["verified"] is False
        assert result["member_id"] is None

    @patch(
        "guardrails.inbound_filter.TEAM_TELEGRAM_IDS",
        {"eyal": 8190904141, "eyal zror": 8190904141, "roye": 555111222},
    )
    def test_returns_member_id_for_known_user(self):
        """Each known user should get the correct short member_id."""
        from guardrails.inbound_filter import verify_sender_telegram

        result = verify_sender_telegram(555111222)

        assert result["verified"] is True
        assert result["member_id"] == "roye"

    @patch(
        "guardrails.inbound_filter.TEAM_TELEGRAM_IDS",
        {"paolo vailetti": 777888999},
    )
    def test_full_name_only_extracts_first_name(self):
        """When only a full name (with space) exists, first name is used as member_id."""
        from guardrails.inbound_filter import verify_sender_telegram

        # First pass finds no short name, second pass extracts "paolo" from "paolo vailetti"
        result = verify_sender_telegram(777888999)

        assert result["verified"] is True
        assert result["member_id"] == "paolo"


# =============================================================================
# Layer 1 — Sender Verification (Email)
# =============================================================================

class TestSenderVerificationEmail:
    """Tests for verify_sender_email() — email whitelist check."""

    @patch("guardrails.inbound_filter.is_team_email", return_value=True)
    @patch(
        "guardrails.inbound_filter.TEAM_MEMBERS",
        {
            "eyal": {"name": "Eyal Zror", "email": "eyal@cropsight.io"},
            "roye": {"name": "Roye Tadmor", "email": "roye@cropsight.io"},
        },
    )
    def test_known_team_email_verified(self, _mock_is_team):
        """A known team email should return verified=True with correct member_id."""
        from guardrails.inbound_filter import verify_sender_email

        result = verify_sender_email("eyal@cropsight.io")

        assert result["verified"] is True
        assert result["member_id"] == "eyal"

    @patch("guardrails.inbound_filter.is_team_email", return_value=False)
    def test_unknown_email_rejected(self, _mock_is_team):
        """An external email should return verified=False."""
        from guardrails.inbound_filter import verify_sender_email

        result = verify_sender_email("hacker@evil.com")

        assert result["verified"] is False
        assert result["member_id"] is None

    @patch("guardrails.inbound_filter.is_team_email", return_value=True)
    @patch(
        "guardrails.inbound_filter.TEAM_MEMBERS",
        {
            "roye": {"name": "Roye Tadmor", "email": "roye@cropsight.io"},
        },
    )
    def test_returns_member_id_for_known_email(self, _mock_is_team):
        """Roye's email should map to member_id 'roye'."""
        from guardrails.inbound_filter import verify_sender_email

        result = verify_sender_email("roye@cropsight.io")

        assert result["verified"] is True
        assert result["member_id"] == "roye"


# =============================================================================
# Layer 2 — Topic Relevance
# =============================================================================

class TestTopicRelevance:
    """Tests for check_topic_relevance() — regex-based relevance classification."""

    def test_work_topic_is_relevant(self):
        """A question about the Moldova pilot should be classified as relevant."""
        from guardrails.inbound_filter import check_topic_relevance

        result = check_topic_relevance("What was decided about the Moldova pilot?")

        assert result["relevant"] is True
        assert "reason" in result

    def test_joke_request_not_relevant(self):
        """Asking for a joke should be classified as off-topic."""
        from guardrails.inbound_filter import check_topic_relevance

        result = check_topic_relevance("Tell me a joke")

        assert result["relevant"] is False
        assert "Off-topic" in result["reason"]

    def test_recipe_not_relevant(self):
        """Asking for a pasta recipe should be classified as off-topic."""
        from guardrails.inbound_filter import check_topic_relevance

        result = check_topic_relevance("Give me a recipe for pasta")

        assert result["relevant"] is False
        assert "Off-topic" in result["reason"]

    def test_cropsight_mention_is_relevant(self):
        """Any mention of CropSight should be classified as relevant."""
        from guardrails.inbound_filter import check_topic_relevance

        result = check_topic_relevance("How is CropSight doing?")

        assert result["relevant"] is True

    def test_ambiguous_message_uncertain(self):
        """A vague greeting with no work/off-topic signals should be uncertain."""
        from guardrails.inbound_filter import check_topic_relevance

        result = check_topic_relevance("Hello, I have a question")

        assert result["relevant"] == "uncertain"
        assert "No strong topic signal" in result["reason"]

    def test_meeting_topic_is_relevant(self):
        """A question about meeting tasks should be classified as relevant."""
        from guardrails.inbound_filter import check_topic_relevance

        result = check_topic_relevance("Show me tasks from last meeting")

        assert result["relevant"] is True

    def test_empty_message_is_uncertain(self):
        """An empty message should be classified as uncertain."""
        from guardrails.inbound_filter import check_topic_relevance

        result = check_topic_relevance("")

        assert result["relevant"] == "uncertain"
        assert "Empty message" in result["reason"]


# =============================================================================
# Layer 3 — Information Leak Prevention
# =============================================================================

class TestLeakPrevention:
    """Tests for check_response_for_leaks() — outbound sensitive-data detection."""

    def test_no_leak_in_clean_response(self):
        """A normal response with no sensitive content should pass through clean."""
        from guardrails.inbound_filter import check_response_for_leaks

        result = check_response_for_leaks(
            "The Moldova pilot is on track for March delivery.",
            {"channel": "telegram_group", "recipient": "group"},
        )

        assert result["leaked"] is False
        assert result["sanitized_response"] == "The Moldova pilot is on track for March delivery."
        assert result["patterns_found"] == []

    def test_detects_equity_split_in_group(self):
        """'equity split' in a group chat should be detected and redacted."""
        from guardrails.inbound_filter import check_response_for_leaks

        result = check_response_for_leaks(
            "The equity split is 60/40 between the founders.",
            {"channel": "telegram_group", "recipient": "group"},
        )

        assert result["leaked"] is True
        assert "equity split" in [p.lower() for p in result["patterns_found"]]
        assert "[Sensitive" in result["sanitized_response"]

    def test_detects_salary_in_email(self):
        """'salary' in an email response should be detected and redacted."""
        from guardrails.inbound_filter import check_response_for_leaks

        result = check_response_for_leaks(
            "The salary for the new hire is $120k.",
            {"channel": "email", "recipient": "roye@cropsight.io"},
        )

        assert result["leaked"] is True
        assert any("salary" in p.lower() for p in result["patterns_found"])
        assert "[Sensitive" in result["sanitized_response"]

    def test_allows_sensitive_in_eyal_dm(self):
        """Sensitive content in a DM to Eyal should pass through unrestricted."""
        from guardrails.inbound_filter import check_response_for_leaks

        result = check_response_for_leaks(
            "The equity split is 60/40 and the salary budget is $500k.",
            {"channel": "telegram_dm", "recipient": "eyal"},
        )

        assert result["leaked"] is False
        assert "equity split" in result["sanitized_response"]
        assert "salary" in result["sanitized_response"]
        assert result["patterns_found"] == []

    def test_detects_valuation(self):
        """'valuation' should be flagged as sensitive in non-Eyal channels."""
        from guardrails.inbound_filter import check_response_for_leaks

        result = check_response_for_leaks(
            "The current valuation is $5M.",
            {"channel": "telegram_group", "recipient": "group"},
        )

        assert result["leaked"] is True
        assert any("valuation" in p.lower() for p in result["patterns_found"])

    def test_redaction_text(self):
        """Redacted content should contain the standard redaction marker."""
        from guardrails.inbound_filter import check_response_for_leaks

        result = check_response_for_leaks(
            "Here is the term sheet details.",
            {"channel": "email", "recipient": "paolo@cropsight.io"},
        )

        assert result["leaked"] is True
        assert "[Sensitive — contact Eyal]" in result["sanitized_response"]


# =============================================================================
# Layer 4 — Output Sanitization
# =============================================================================

class TestSanitizeOutbound:
    """Tests for sanitize_outbound_message() — wraps leak check and returns string."""

    def test_returns_sanitized_string(self):
        """sanitize_outbound_message should return a string with redactions applied."""
        from guardrails.inbound_filter import sanitize_outbound_message

        result = sanitize_outbound_message(
            "The burn rate is too high.",
            {"channel": "telegram_group", "recipient": "group"},
        )

        assert isinstance(result, str)
        assert "[Sensitive — contact Eyal]" in result
        # The original sensitive term should be replaced
        assert "burn rate" not in result

    def test_clean_message_unchanged(self):
        """A message with no sensitive content should pass through unchanged."""
        from guardrails.inbound_filter import sanitize_outbound_message

        original = "The sprint review is scheduled for Friday."
        result = sanitize_outbound_message(
            original,
            {"channel": "telegram_group", "recipient": "group"},
        )

        assert result == original


# =============================================================================
# Layer 5 — Audit Logging
# =============================================================================

class TestAuditLogging:
    """Tests for log_inbound_interaction() — Supabase audit trail."""

    @patch("guardrails.inbound_filter.supabase_client")
    def test_log_calls_supabase(self, mock_db):
        """log_inbound_interaction should call supabase_client.log_action with correct details."""
        mock_db.log_action = MagicMock()

        from guardrails.inbound_filter import log_inbound_interaction

        log_inbound_interaction(
            sender="eyal",
            channel="telegram_dm",
            preview="What was decided?",
            verified=True,
            relevant="True",
            action="allowed",
        )

        mock_db.log_action.assert_called_once()
        call_kwargs = mock_db.log_action.call_args.kwargs

        assert call_kwargs["action"] == "inbound_interaction"
        assert call_kwargs["details"]["sender"] == "eyal"
        assert call_kwargs["details"]["channel"] == "telegram_dm"
        assert call_kwargs["details"]["verified"] is True
        assert call_kwargs["details"]["relevant"] == "True"
        assert call_kwargs["details"]["outcome"] == "allowed"
        assert call_kwargs["triggered_by"] == "eyal"

    @patch("guardrails.inbound_filter.supabase_client")
    def test_log_truncates_preview(self, mock_db):
        """Message preview should be truncated to 100 characters."""
        mock_db.log_action = MagicMock()

        from guardrails.inbound_filter import log_inbound_interaction

        long_message = "A" * 200

        log_inbound_interaction(
            sender="roye",
            channel="telegram_group",
            preview=long_message,
            verified=True,
            relevant="True",
            action="allowed",
        )

        call_kwargs = mock_db.log_action.call_args.kwargs
        assert len(call_kwargs["details"]["message_preview"]) == 100

    @patch("guardrails.inbound_filter.supabase_client")
    def test_log_uses_unknown_for_unverified_sender(self, mock_db):
        """When sender is not verified, triggered_by should be 'unknown'."""
        mock_db.log_action = MagicMock()

        from guardrails.inbound_filter import log_inbound_interaction

        log_inbound_interaction(
            sender="stranger123",
            channel="telegram_dm",
            preview="Who are you?",
            verified=False,
            relevant="unknown",
            action="deflected_unknown_sender",
        )

        call_kwargs = mock_db.log_action.call_args.kwargs
        assert call_kwargs["triggered_by"] == "unknown"


# =============================================================================
# Main Entry Point — check_inbound_message
# =============================================================================

class TestCheckInboundMessage:
    """Tests for check_inbound_message() — orchestrates all layers."""

    @pytest.mark.asyncio
    @patch("guardrails.inbound_filter.supabase_client")
    @patch(
        "guardrails.inbound_filter.TEAM_TELEGRAM_IDS",
        {"eyal": 999, "eyal zror": 999},
    )
    async def test_verified_relevant_message_allowed(self, mock_db):
        """A known team member asking a work question should be allowed through."""
        mock_db.log_action = MagicMock()

        from guardrails.inbound_filter import check_inbound_message

        result = await check_inbound_message(
            message="What was decided about the Moldova pilot?",
            sender_id="eyal",
            channel="telegram_dm",
            telegram_user_id=999,
        )

        assert result["allowed"] is True
        assert result["deflection_message"] is None
        assert result["member_id"] == "eyal"
        assert result["audit_logged"] is True

    @pytest.mark.asyncio
    @patch("guardrails.inbound_filter.supabase_client")
    @patch(
        "guardrails.inbound_filter.TEAM_TELEGRAM_IDS",
        {"eyal": 999},
    )
    async def test_unknown_sender_deflected(self, mock_db):
        """An unknown Telegram user should be deflected with the standard message."""
        mock_db.log_action = MagicMock()

        from guardrails.inbound_filter import check_inbound_message

        result = await check_inbound_message(
            message="What is the Moldova status?",
            sender_id="unknown_user",
            channel="telegram_dm",
            telegram_user_id=12345,
        )

        assert result["allowed"] is False
        assert "CropSight team members" in result["deflection_message"]
        assert result["member_id"] is None

    @pytest.mark.asyncio
    @patch("guardrails.inbound_filter.supabase_client")
    @patch(
        "guardrails.inbound_filter.TEAM_TELEGRAM_IDS",
        {"eyal": 999, "eyal zror": 999},
    )
    async def test_off_topic_deflected(self, mock_db):
        """A verified team member asking for a joke should be deflected."""
        mock_db.log_action = MagicMock()

        from guardrails.inbound_filter import check_inbound_message

        result = await check_inbound_message(
            message="Tell me a joke",
            sender_id="eyal",
            channel="telegram_dm",
            telegram_user_id=999,
        )

        assert result["allowed"] is False
        assert "CropSight-related" in result["deflection_message"]
        assert result["member_id"] == "eyal"

    @pytest.mark.asyncio
    @patch("guardrails.inbound_filter.supabase_client")
    @patch(
        "guardrails.inbound_filter.TEAM_TELEGRAM_IDS",
        {"eyal": 999, "eyal zror": 999},
    )
    async def test_uncertain_relevance_allowed(self, mock_db):
        """A verified member with an ambiguous message should be allowed (flagged but passes)."""
        mock_db.log_action = MagicMock()

        from guardrails.inbound_filter import check_inbound_message

        result = await check_inbound_message(
            message="Hello, I have a question",
            sender_id="eyal",
            channel="telegram_dm",
            telegram_user_id=999,
        )

        assert result["allowed"] is True
        assert result["deflection_message"] is None
        assert result["member_id"] == "eyal"

    @pytest.mark.asyncio
    @patch("guardrails.inbound_filter.supabase_client")
    @patch("guardrails.inbound_filter.is_team_email", return_value=True)
    @patch(
        "guardrails.inbound_filter.TEAM_MEMBERS",
        {
            "paolo": {"name": "Paolo Vailetti", "email": "paolo@cropsight.io"},
        },
    )
    async def test_email_sender_verified(self, _mock_is_team, mock_db):
        """Using sender_email instead of telegram_user_id should verify via email path."""
        mock_db.log_action = MagicMock()

        from guardrails.inbound_filter import check_inbound_message

        result = await check_inbound_message(
            message="What is the status of the pilot?",
            sender_id="paolo",
            channel="email",
            sender_email="paolo@cropsight.io",
        )

        assert result["allowed"] is True
        assert result["member_id"] == "paolo"

    @pytest.mark.asyncio
    @patch("guardrails.inbound_filter.supabase_client")
    @patch(
        "guardrails.inbound_filter.TEAM_TELEGRAM_IDS",
        {"eyal": 999, "eyal zror": 999},
    )
    async def test_audit_logged(self, mock_db):
        """Every inbound check result should contain audit_logged=True."""
        mock_db.log_action = MagicMock()

        from guardrails.inbound_filter import check_inbound_message

        # Allowed case
        result_allowed = await check_inbound_message(
            message="Show me the sprint tasks",
            sender_id="eyal",
            channel="telegram_dm",
            telegram_user_id=999,
        )
        assert result_allowed["audit_logged"] is True

        # Deflected case (unknown sender)
        result_deflected = await check_inbound_message(
            message="Hello",
            sender_id="stranger",
            channel="telegram_dm",
            telegram_user_id=12345,
        )
        assert result_deflected["audit_logged"] is True

        # Both paths should have called log_action
        assert mock_db.log_action.call_count == 2
