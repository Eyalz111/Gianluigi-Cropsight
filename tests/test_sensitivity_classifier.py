"""
Tests for guardrails/sensitivity_classifier.py

Tests the sensitivity classification for Eyal-only distribution.
"""

import pytest
from unittest.mock import patch


class TestSensitivityClassifier:
    """Tests for sensitivity classification."""

    def test_normal_meeting_classified_normal(self):
        """Regular meetings should be classified as normal."""
        from guardrails.sensitivity_classifier import classify_sensitivity

        normal_events = [
            {"title": "CropSight MVP Review"},
            {"title": "Weekly Team Sync"},
            {"title": "Product Planning"},
            {"title": "Tech Discussion"},
        ]

        for event in normal_events:
            result = classify_sensitivity(event)
            assert result == "normal", f"Expected 'normal' for '{event['title']}', got {result}"

    def test_legal_keywords_sensitive(self):
        """Legal-related meetings should be sensitive."""
        from guardrails.sensitivity_classifier import classify_sensitivity

        legal_events = [
            {"title": "Call with Lawyer"},
            {"title": "Legal Review Session"},
            {"title": "Meeting with Fischer"},
            {"title": "FBC Discussion"},
            {"title": "Zohar Call"},
        ]

        for event in legal_events:
            result = classify_sensitivity(event)
            assert result == "sensitive", f"Expected 'sensitive' for '{event['title']}', got {result}"

    def test_investor_keywords_sensitive(self):
        """Investor-related meetings should be sensitive."""
        from guardrails.sensitivity_classifier import classify_sensitivity

        investor_events = [
            {"title": "Investor Meeting"},
            {"title": "Investment Discussion"},
            {"title": "Funding Round Planning"},
            {"title": "VC Pitch Prep"},
        ]

        for event in investor_events:
            result = classify_sensitivity(event)
            assert result == "sensitive", f"Expected 'sensitive' for '{event['title']}', got {result}"

    def test_confidential_keywords_sensitive(self):
        """Confidential meetings should be sensitive."""
        from guardrails.sensitivity_classifier import classify_sensitivity

        confidential_events = [
            {"title": "NDA Review"},
            {"title": "Confidential Strategy"},
            {"title": "Founders Agreement Discussion"},
        ]

        for event in confidential_events:
            result = classify_sensitivity(event)
            assert result == "sensitive", f"Expected 'sensitive' for '{event['title']}', got {result}"

    def test_hr_equity_keywords_sensitive(self):
        """HR and equity meetings should be sensitive."""
        from guardrails.sensitivity_classifier import classify_sensitivity

        hr_events = [
            {"title": "HR Discussion"},
            {"title": "Compensation Review"},
            {"title": "Equity Allocation"},
        ]

        for event in hr_events:
            result = classify_sensitivity(event)
            assert result == "sensitive", f"Expected 'sensitive' for '{event['title']}', got {result}"

    def test_case_insensitive_matching(self):
        """Keyword matching should be case insensitive."""
        from guardrails.sensitivity_classifier import classify_sensitivity

        events = [
            {"title": "INVESTOR MEETING"},
            {"title": "Investor Meeting"},
            {"title": "investor meeting"},
        ]

        for event in events:
            result = classify_sensitivity(event)
            assert result == "sensitive", f"Expected 'sensitive' for '{event['title']}', got {result}"


class TestSensitivityFromContent:
    """Tests for content-based sensitivity classification."""

    def test_normal_content_classified_normal(self):
        """Normal meeting content should be classified as normal."""
        from guardrails.sensitivity_classifier import classify_sensitivity_from_content

        content = """
        We discussed the MVP progress today. The model accuracy is at 87%.
        Next steps include preparing the demo for the client meeting.
        """

        result = classify_sensitivity_from_content(content)
        assert result == "normal"

    def test_term_sheet_content_sensitive(self):
        """Content mentioning term sheets should be sensitive."""
        from guardrails.sensitivity_classifier import classify_sensitivity_from_content

        content = """
        We reviewed the term sheet from the VC firm.
        The valuation looks reasonable at this stage.
        """

        result = classify_sensitivity_from_content(content)
        assert result == "sensitive"

    def test_salary_content_sensitive(self):
        """Content mentioning salary should be sensitive."""
        from guardrails.sensitivity_classifier import classify_sensitivity_from_content

        content = """
        We need to have a salary discussion with the new hire.
        Current compensation structure needs review.
        """

        result = classify_sensitivity_from_content(content)
        assert result == "sensitive"


class TestSensitivityReason:
    """Tests for getting sensitivity reason."""

    def test_get_reason_for_sensitive(self):
        """Should return reason for sensitive meetings."""
        from guardrails.sensitivity_classifier import get_sensitivity_reason

        event = {"title": "Investor Pitch Prep"}
        reason = get_sensitivity_reason(event)

        assert reason is not None
        assert "investor" in reason.lower()

    def test_get_reason_for_normal(self):
        """Should return None for normal meetings."""
        from guardrails.sensitivity_classifier import get_sensitivity_reason

        event = {"title": "Team Standup"}
        reason = get_sensitivity_reason(event)

        assert reason is None


class TestAttendeesSensitivity:
    """Tests for attendee-based sensitivity classification."""

    def test_external_lawyer_sensitive(self):
        """External lawyers should trigger sensitive classification."""
        from guardrails.sensitivity_classifier import classify_attendees_sensitivity

        with patch('config.team.CROPSIGHT_TEAM_EMAILS', ["eyal@cropsight.io", "roye@cropsight.io"]):
            attendees = [
                {"email": "eyal@cropsight.io", "displayName": "Eyal"},
                {"email": "john@lawfirm.com", "displayName": "John, Attorney"},
            ]

            result = classify_attendees_sensitivity(attendees)
            assert result == "sensitive"

    def test_external_investor_sensitive(self):
        """External investors should trigger sensitive classification."""
        from guardrails.sensitivity_classifier import classify_attendees_sensitivity

        with patch('config.team.CROPSIGHT_TEAM_EMAILS', ["eyal@cropsight.io", "roye@cropsight.io"]):
            attendees = [
                {"email": "eyal@cropsight.io", "displayName": "Eyal"},
                {"email": "partner@vcfund.capital", "displayName": "VC Partner"},
            ]

            result = classify_attendees_sensitivity(attendees)
            assert result == "sensitive"

    def test_regular_external_normal(self):
        """Regular external attendees should be normal."""
        from guardrails.sensitivity_classifier import classify_attendees_sensitivity

        with patch('config.team.CROPSIGHT_TEAM_EMAILS', ["eyal@cropsight.io", "roye@cropsight.io"]):
            attendees = [
                {"email": "eyal@cropsight.io", "displayName": "Eyal"},
                {"email": "client@farming.com", "displayName": "Client Name"},
            ]

            result = classify_attendees_sensitivity(attendees)
            assert result == "normal"


class TestCombinedSensitivity:
    """Tests for combined sensitivity from multiple sources."""

    def test_combined_normal_meeting(self):
        """All-normal signals should result in normal classification."""
        from guardrails.sensitivity_classifier import get_combined_sensitivity

        event = {
            "title": "Product Planning",
            "attendees": [],
        }

        sensitivity, reasons = get_combined_sensitivity(event)

        assert sensitivity == "normal"
        assert len(reasons) == 0

    def test_combined_title_sensitive(self):
        """Sensitive title should result in sensitive classification."""
        from guardrails.sensitivity_classifier import get_combined_sensitivity

        event = {
            "title": "Investor Meeting",
            "attendees": [],
        }

        sensitivity, reasons = get_combined_sensitivity(event)

        assert sensitivity == "sensitive"
        assert len(reasons) >= 1
        assert any("title" in r.lower() for r in reasons)

    def test_combined_multiple_reasons(self):
        """Multiple sensitive signals should all be reported."""
        from guardrails.sensitivity_classifier import get_combined_sensitivity

        event = {
            "title": "Investor Discussion",
            "attendees": [],
        }
        content = "We discussed the term sheet valuation."

        sensitivity, reasons = get_combined_sensitivity(event, content=content)

        assert sensitivity == "sensitive"
        assert len(reasons) >= 2
