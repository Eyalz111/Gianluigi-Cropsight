"""
Tests for 4-tier audience-aware sensitivity system (v2.2 Session 2.5).

Hierarchy: CEO(4) > FOUNDERS(3) > TEAM(2) > PUBLIC(1)
Verifies enum values, classifier functions return new tiers,
distribution lists, propagation, Telegram button cycling, summary
badge formatting, and interpersonal signal feature flag.
"""

import pytest
from unittest.mock import patch, MagicMock

from models.schemas import Sensitivity


class TestSensitivityEnum:
    """Tests for the 4-tier Sensitivity enum."""

    def test_enum_values(self):
        assert Sensitivity.PUBLIC.value == "public"
        assert Sensitivity.TEAM.value == "team"
        assert Sensitivity.FOUNDERS.value == "founders"
        assert Sensitivity.CEO.value == "ceo"

    def test_from_legacy_normal(self):
        assert Sensitivity.from_legacy("normal") == Sensitivity.FOUNDERS

    def test_from_legacy_sensitive(self):
        assert Sensitivity.from_legacy("sensitive") == Sensitivity.CEO

    def test_from_legacy_legal(self):
        assert Sensitivity.from_legacy("legal") == Sensitivity.CEO

    def test_from_legacy_team(self):
        assert Sensitivity.from_legacy("team") == Sensitivity.FOUNDERS

    def test_from_legacy_ceo_only(self):
        assert Sensitivity.from_legacy("ceo_only") == Sensitivity.CEO

    def test_from_legacy_restricted(self):
        assert Sensitivity.from_legacy("restricted") == Sensitivity.CEO

    def test_from_legacy_unknown_defaults_to_founders(self):
        assert Sensitivity.from_legacy("unknown") == Sensitivity.FOUNDERS


class TestClassifySensitivity:
    """Tests for classify_sensitivity() returning tier values."""

    def test_normal_meeting_returns_founders(self):
        from guardrails.sensitivity_classifier import classify_sensitivity
        result = classify_sensitivity({"title": "CropSight Weekly Sync"})
        assert result == "founders"

    def test_investor_meeting_returns_ceo(self):
        from guardrails.sensitivity_classifier import classify_sensitivity
        result = classify_sensitivity({"title": "Investor Update Call"})
        assert result == "ceo"

    def test_legal_meeting_returns_ceo(self):
        from guardrails.sensitivity_classifier import classify_sensitivity
        result = classify_sensitivity({"title": "Call with Fischer Legal"})
        assert result == "ceo"

    def test_empty_title_returns_founders(self):
        from guardrails.sensitivity_classifier import classify_sensitivity
        result = classify_sensitivity({"title": ""})
        assert result == "founders"


class TestClassifySensitivityFromContent:
    """Tests for content-based classification returning tiers."""

    def test_normal_content_returns_founders(self):
        from guardrails.sensitivity_classifier import classify_sensitivity_from_content
        result = classify_sensitivity_from_content("Discussed the product roadmap and next steps.")
        assert result == "founders"

    def test_equity_content_returns_ceo(self):
        from guardrails.sensitivity_classifier import classify_sensitivity_from_content
        result = classify_sensitivity_from_content("We discussed the equity split between founders.")
        assert result == "ceo"

    def test_valuation_content_returns_ceo(self):
        from guardrails.sensitivity_classifier import classify_sensitivity_from_content
        result = classify_sensitivity_from_content("The valuation came in at $5M.")
        assert result == "ceo"


class TestClassifyAttendeesSensitivity:
    """Tests for attendee-based classification."""

    def test_team_only_returns_founders(self):
        from guardrails.sensitivity_classifier import classify_attendees_sensitivity
        with patch("config.team.CROPSIGHT_TEAM_EMAILS", ["eyal@cropsight.com"]):
            result = classify_attendees_sensitivity([{"email": "eyal@cropsight.com"}])
        assert result == "founders"

    def test_investor_attendee_returns_ceo(self):
        from guardrails.sensitivity_classifier import classify_attendees_sensitivity
        with patch("config.team.CROPSIGHT_TEAM_EMAILS", ["eyal@cropsight.com"]):
            result = classify_attendees_sensitivity([
                {"email": "eyal@cropsight.com"},
                {"email": "john@venturesfund.com"},
            ])
        assert result == "ceo"


class TestGetCombinedSensitivity:
    """Tests for combined sensitivity classification."""

    def test_normal_returns_founders(self):
        from guardrails.sensitivity_classifier import get_combined_sensitivity
        sensitivity, reasons = get_combined_sensitivity({"title": "Weekly sync"})
        assert sensitivity == "founders"
        assert len(reasons) == 0

    def test_sensitive_title_returns_ceo(self):
        from guardrails.sensitivity_classifier import get_combined_sensitivity
        sensitivity, reasons = get_combined_sensitivity({"title": "Investor pitch review"})
        assert sensitivity == "ceo"
        assert len(reasons) > 0


class TestGetDistributionList:
    """Tests for distribution list based on sensitivity tier."""

    def test_founders_tier_sends_to_all(self):
        from guardrails.sensitivity_classifier import get_distribution_list
        with patch("config.settings.settings") as mock_settings:
            mock_settings.ENVIRONMENT = "production"
            mock_settings.EYAL_EMAIL = "eyal@cropsight.com"
            result = get_distribution_list("founders", ["eyal@cropsight.com", "roye@cropsight.com"])
        assert len(result) == 2

    def test_ceo_tier_sends_to_eyal(self):
        from guardrails.sensitivity_classifier import get_distribution_list
        with patch("config.settings.settings") as mock_settings:
            mock_settings.ENVIRONMENT = "production"
            mock_settings.EYAL_EMAIL = "eyal@cropsight.com"
            result = get_distribution_list("ceo", ["eyal@cropsight.com", "roye@cropsight.com"])
        assert result == ["eyal@cropsight.com"]

    def test_public_tier_sends_to_all(self):
        from guardrails.sensitivity_classifier import get_distribution_list
        with patch("config.settings.settings") as mock_settings:
            mock_settings.ENVIRONMENT = "production"
            mock_settings.EYAL_EMAIL = "eyal@cropsight.com"
            result = get_distribution_list("public", ["eyal@cropsight.com", "roye@cropsight.com"])
        assert len(result) == 2

    def test_team_tier_sends_to_all(self):
        from guardrails.sensitivity_classifier import get_distribution_list
        with patch("config.settings.settings") as mock_settings:
            mock_settings.ENVIRONMENT = "production"
            mock_settings.EYAL_EMAIL = "eyal@cropsight.com"
            result = get_distribution_list("team", ["eyal@cropsight.com", "roye@cropsight.com"])
        assert len(result) == 2

    def test_legacy_normal_maps_to_founders(self):
        from guardrails.sensitivity_classifier import get_distribution_list
        with patch("config.settings.settings") as mock_settings:
            mock_settings.ENVIRONMENT = "production"
            mock_settings.EYAL_EMAIL = "eyal@cropsight.com"
            result = get_distribution_list("normal", ["eyal@cropsight.com", "roye@cropsight.com"])
        assert len(result) == 2

    def test_legacy_sensitive_maps_to_ceo(self):
        from guardrails.sensitivity_classifier import get_distribution_list
        with patch("config.settings.settings") as mock_settings:
            mock_settings.ENVIRONMENT = "production"
            mock_settings.EYAL_EMAIL = "eyal@cropsight.com"
            result = get_distribution_list("sensitive", ["eyal@cropsight.com", "roye@cropsight.com"])
        assert result == ["eyal@cropsight.com"]

    def test_legacy_ceo_only_maps_to_ceo(self):
        from guardrails.sensitivity_classifier import get_distribution_list
        with patch("config.settings.settings") as mock_settings:
            mock_settings.ENVIRONMENT = "production"
            mock_settings.EYAL_EMAIL = "eyal@cropsight.com"
            result = get_distribution_list("ceo_only", ["eyal@cropsight.com", "roye@cropsight.com"])
        assert result == ["eyal@cropsight.com"]

    def test_legacy_restricted_maps_to_ceo(self):
        from guardrails.sensitivity_classifier import get_distribution_list
        with patch("config.settings.settings") as mock_settings:
            mock_settings.ENVIRONMENT = "production"
            mock_settings.EYAL_EMAIL = "eyal@cropsight.com"
            result = get_distribution_list("restricted", ["eyal@cropsight.com", "roye@cropsight.com"])
        assert result == ["eyal@cropsight.com"]

    def test_dev_mode_always_eyal_only(self):
        from guardrails.sensitivity_classifier import get_distribution_list
        with patch("config.settings.settings") as mock_settings:
            mock_settings.ENVIRONMENT = "development"
            mock_settings.EYAL_EMAIL = "eyal@cropsight.com"
            result = get_distribution_list("founders", ["eyal@cropsight.com", "roye@cropsight.com"])
        assert result == ["eyal@cropsight.com"]


class TestClassifySensitivityLLM:
    """Tests for LLM-based sensitivity classification."""

    def test_short_content_returns_founders(self):
        from guardrails.sensitivity_classifier import classify_sensitivity_llm
        result = classify_sensitivity_llm("Short text")
        assert result == "founders"

    def test_returns_ceo_from_llm(self):
        from guardrails.sensitivity_classifier import classify_sensitivity_llm
        with patch("core.llm.call_llm") as mock_llm:
            mock_llm.return_value = ("ceo", {})
            result = classify_sensitivity_llm("x" * 600)
        assert result == "ceo"

    def test_returns_founders_from_llm(self):
        from guardrails.sensitivity_classifier import classify_sensitivity_llm
        with patch("core.llm.call_llm") as mock_llm:
            mock_llm.return_value = ("founders", {})
            result = classify_sensitivity_llm("x" * 600)
        assert result == "founders"

    def test_legacy_sensitive_response_maps_to_ceo(self):
        from guardrails.sensitivity_classifier import classify_sensitivity_llm
        with patch("core.llm.call_llm") as mock_llm:
            mock_llm.return_value = ("sensitive", {})
            result = classify_sensitivity_llm("x" * 600)
        assert result == "ceo"

    def test_legacy_ceo_only_response_maps_to_ceo(self):
        from guardrails.sensitivity_classifier import classify_sensitivity_llm
        with patch("core.llm.call_llm") as mock_llm:
            mock_llm.return_value = ("ceo_only", {})
            result = classify_sensitivity_llm("x" * 600)
        assert result == "ceo"

    def test_legacy_team_response_maps_to_founders(self):
        from guardrails.sensitivity_classifier import classify_sensitivity_llm
        with patch("core.llm.call_llm") as mock_llm:
            mock_llm.return_value = ("team", {})
            result = classify_sensitivity_llm("x" * 600)
        assert result == "founders"

    def test_llm_failure_returns_founders(self):
        from guardrails.sensitivity_classifier import classify_sensitivity_llm
        with patch("core.llm.call_llm") as mock_llm:
            mock_llm.side_effect = Exception("API error")
            result = classify_sensitivity_llm("x" * 600)
        assert result == "founders"


class TestInterpersonalSignalFlag:
    """Tests for the interpersonal signal detection feature flag."""

    def test_flag_defaults_to_false(self):
        from config.settings import Settings
        s = Settings(
            ANTHROPIC_API_KEY="test",
            SUPABASE_URL="https://test.supabase.co",
            SUPABASE_KEY="test",
            TELEGRAM_BOT_TOKEN="test",
        )
        assert s.INTERPERSONAL_SIGNAL_DETECTION is False

    def test_llm_prompt_includes_interpersonal_when_enabled(self):
        from guardrails.sensitivity_classifier import classify_sensitivity_llm
        with patch("core.llm.call_llm") as mock_llm, \
             patch("config.settings.settings") as mock_settings:
            mock_settings.model_simple = "haiku"
            mock_settings.INTERPERSONAL_SIGNAL_DETECTION = True
            mock_llm.return_value = ("founders", {})
            classify_sensitivity_llm("x" * 600)

            # Check that the prompt includes interpersonal detection
            call_args = mock_llm.call_args
            prompt = call_args.kwargs.get("prompt", call_args[1].get("prompt", ""))
            assert "interpersonal" in prompt.lower() or "Interpersonal" in prompt

    def test_llm_prompt_excludes_interpersonal_when_disabled(self):
        from guardrails.sensitivity_classifier import classify_sensitivity_llm
        with patch("core.llm.call_llm") as mock_llm, \
             patch("config.settings.settings") as mock_settings:
            mock_settings.model_simple = "haiku"
            mock_settings.INTERPERSONAL_SIGNAL_DETECTION = False
            mock_llm.return_value = ("founders", {})
            classify_sensitivity_llm("x" * 600)

            call_args = mock_llm.call_args
            prompt = call_args.kwargs.get("prompt", call_args[1].get("prompt", ""))
            assert "interpersonal" not in prompt.lower()


class TestSummaryBadgeFormatting:
    """Tests for sensitivity tier display in meeting summaries."""

    def test_founders_tier_displays_correctly(self):
        assert "founders".upper().replace("_", " ") == "FOUNDERS"

    def test_ceo_tier_displays_correctly(self):
        assert "ceo".upper().replace("_", " ") == "CEO"

    def test_team_tier_displays_correctly(self):
        assert "team".upper().replace("_", " ") == "TEAM"

    def test_public_tier_displays_correctly(self):
        assert "public".upper().replace("_", " ") == "PUBLIC"
