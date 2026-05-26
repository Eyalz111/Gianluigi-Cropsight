"""
Tests for PR1 input-hygiene: identity/domain robustness, the email business
gate, and the sharpened classifier + shadow logging.

Follows the repo rule: patch ATTRIBUTES on the real settings singleton via
patch.object(settings, ...) — never replace the settings object.
"""

from unittest.mock import patch

import config.team as team
from config.settings import settings


# =========================================================================
# A3 — identity / business-domain helpers
# =========================================================================

class TestBusinessIdentity:
    def test_cropsight_domains_are_business(self):
        assert team.is_business_identity("eyal.zror@cropsight.io") is True
        assert team.is_business_identity("anyone@cropsight.com") is True

    def test_personal_gmail_is_not_business(self):
        # The crux: personal gmails must NOT be a business signal (calendar leak).
        assert team.is_business_identity("eyalz111@gmail.com") is False
        assert team.is_business_identity("stranger@gmail.com") is False

    def test_handles_display_name_form(self):
        assert team.is_business_identity("Eyal Zror <eyal.zror@cropsight.io>") is True

    def test_is_team_email_recognizes_work_identity(self):
        # Email recognition (separate from the business signal) includes the work address.
        assert team.is_team_email("eyal.zror@cropsight.io") is True

    def test_get_member_by_work_identity(self):
        member = team.get_team_member_by_email("eyal.zror@cropsight.io")
        assert member is not None and member["name"] == "Eyal Zror"


# =========================================================================
# A2 — email business gate (passes_email_filter_chain)
# =========================================================================

class TestEmailFilterChain:
    def test_business_domain_passes(self):
        passes, reason = team.passes_email_filter_chain(
            sender="someone@cropsight.io", recipient="x@example.com", subject="hi"
        )
        assert passes is True and reason == "business_domain"

    def test_blocklisted_sender_rejected(self):
        with patch("config.team.is_personal_contact_blocked", return_value=True):
            passes, reason = team.passes_email_filter_chain(
                sender="friend@gmail.com", recipient="x@example.com", subject="CropSight news"
            )
        assert passes is False and reason == "blocked_contact"

    def test_cold_inbound_with_keyword_not_dropped(self):
        """A first-contact investor from an unknown domain mentioning CropSight still passes."""
        with patch("config.team.is_known_stakeholder_domain", return_value=False):
            passes, reason = team.passes_email_filter_chain(
                sender="partner@unknown-vc.com",
                recipient="gianluigi@example.com",
                subject="Interested in the CropSight pilot",
                filter_keywords=["cropsight"],
            )
        assert passes is True and reason.startswith("keyword")


# =========================================================================
# A2 — sharpened classifier prompt + shadow logging
# =========================================================================

class TestSharpenedClassifier:
    def test_sharpened_prompt_differs(self):
        from processors.email_classifier import _classification_system
        legacy = _classification_system("cropsight", sharpened=False)
        sharp = _classification_system("cropsight", sharpened=True)
        assert "merely mentions" in sharp
        assert "merely mentions" not in legacy

    async def test_enforce_uses_sharpened_prompt(self):
        from processors import email_classifier as ec
        with patch.object(settings, "EMAIL_BUSINESS_GATE", True), \
             patch.object(settings, "INPUT_HYGIENE_SHADOW_MODE", False), \
             patch("processors.email_classifier.call_llm", return_value=("false_positive", {})) as mock_llm:
            result = await ec.classify_email(
                sender="mom@gmail.com", subject="dinner — saw something about wheat", body_preview="..."
            )
        assert result == "false_positive"
        assert mock_llm.call_count == 1
        assert "merely mentions" in mock_llm.call_args.kwargs["system"]

    async def test_gate_off_uses_legacy_prompt(self):
        from processors import email_classifier as ec
        with patch.object(settings, "EMAIL_BUSINESS_GATE", False), \
             patch("processors.email_classifier.call_llm", return_value=("relevant", {})) as mock_llm:
            await ec.classify_email(sender="x@cropsight.io", subject="update", body_preview="...")
        assert mock_llm.call_count == 1
        assert "merely mentions" not in mock_llm.call_args.kwargs["system"]

    async def test_shadow_double_classifies_and_logs_delta(self):
        from processors import email_classifier as ec
        # legacy call -> relevant; sharpened call -> false_positive => delta logged, legacy returned
        with patch.object(settings, "EMAIL_BUSINESS_GATE", True), \
             patch.object(settings, "INPUT_HYGIENE_SHADOW_MODE", True), \
             patch("processors.email_classifier.call_llm",
                   side_effect=[("relevant", {}), ("false_positive", {})]) as mock_llm, \
             patch("processors.email_classifier._log_email_shadow") as mock_log:
            result = await ec.classify_email(
                sender="mom@gmail.com", subject="wheat article", body_preview="..."
            )
        assert result == "relevant"          # legacy returned during shadow
        assert mock_llm.call_count == 2       # double-classified
        mock_log.assert_called_once()
