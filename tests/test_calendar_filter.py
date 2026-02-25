"""
Tests for guardrails/calendar_filter.py

Tests the multi-layer filter chain for detecting CropSight meetings.
"""

import pytest
from unittest.mock import patch, MagicMock


class TestCalendarFilter:
    """Tests for the calendar filter module."""

    def test_blocklist_blocks_personal_meetings(self):
        """Personal meeting keywords should be blocked."""
        from guardrails.calendar_filter import is_cropsight_meeting

        personal_events = [
            {"title": "MA Seminar - Hebrew University", "attendees": [], "color_id": None},
            {"title": "Doctor appointment", "attendees": [], "color_id": None},
            {"title": "Lunch with friends", "attendees": [], "color_id": None},
            {"title": "Birthday party", "attendees": [], "color_id": None},
            {"title": "Personal time", "attendees": [], "color_id": None},
        ]

        for event in personal_events:
            result = is_cropsight_meeting(event)
            assert result is False, f"Expected False for '{event['title']}', got {result}"

    def test_cropsight_prefix_detected(self):
        """CropSight prefix in title should be detected."""
        from guardrails.calendar_filter import is_cropsight_meeting

        cropsight_events = [
            {"title": "CropSight MVP Review", "attendees": [], "color_id": None},
            {"title": "CS: Weekly Sync", "attendees": [], "color_id": None},
            {"title": "CS Daily Standup", "attendees": [], "color_id": None},
            {"title": "cropsight planning", "attendees": [], "color_id": None},
        ]

        for event in cropsight_events:
            result = is_cropsight_meeting(event)
            assert result is True, f"Expected True for '{event['title']}', got {result}"

    @patch('guardrails.calendar_filter.settings')
    def test_purple_color_detected(self, mock_settings):
        """Purple calendar color (CropSight color) should be detected."""
        from guardrails.calendar_filter import is_cropsight_meeting

        mock_settings.CROPSIGHT_CALENDAR_COLOR_ID = "9"

        event = {
            "title": "Some Meeting",
            "attendees": [],
            "color_id": "9",
        }

        result = is_cropsight_meeting(event)
        assert result is True

    @patch('guardrails.calendar_filter.CROPSIGHT_TEAM_EMAILS', [
        "eyal@cropsight.io",
        "roye@cropsight.io",
        "paolo@cropsight.io",
        "yoram@cropsight.io",
    ])
    def test_two_plus_team_members_detected(self):
        """Meetings with 2+ team members should be detected as CropSight."""
        from guardrails.calendar_filter import is_cropsight_meeting

        event = {
            "title": "Random Meeting Title",
            "attendees": [
                {"email": "eyal@cropsight.io"},
                {"email": "roye@cropsight.io"},
            ],
            "color_id": None,
        }

        result = is_cropsight_meeting(event)
        assert result is True

    @patch('guardrails.calendar_filter.CROPSIGHT_TEAM_EMAILS', [
        "eyal@cropsight.io",
        "roye@cropsight.io",
    ])
    def test_one_team_member_uncertain(self):
        """Meetings with only 1 team member should be uncertain."""
        from guardrails.calendar_filter import is_cropsight_meeting

        event = {
            "title": "External Meeting",
            "attendees": [
                {"email": "eyal@cropsight.io"},
                {"email": "external@company.com"},
            ],
            "color_id": None,
        }

        result = is_cropsight_meeting(event)
        assert result is None  # Uncertain

    def test_empty_title_uncertain(self):
        """Meetings with no identifying features should be uncertain."""
        from guardrails.calendar_filter import is_cropsight_meeting

        event = {
            "title": "Meeting",
            "attendees": [],
            "color_id": None,
        }

        result = is_cropsight_meeting(event)
        assert result is None  # Uncertain

    def test_blocklist_takes_priority_over_prefix(self):
        """Blocklist should take priority even if title has CropSight prefix."""
        from guardrails.calendar_filter import is_cropsight_meeting

        # This is an edge case - unlikely but tests priority
        event = {
            "title": "CropSight Personal Day Off",  # Has CS prefix but also "personal"
            "attendees": [],
            "color_id": None,
        }

        result = is_cropsight_meeting(event)
        assert result is False  # Blocklist wins


class TestFormatUncertainMeetingQuestion:
    """Tests for the question formatting function."""

    def test_format_basic_question(self):
        """Test basic question formatting."""
        from guardrails.calendar_filter import format_uncertain_meeting_question

        event = {
            "title": "Product Discussion",
            "start": "2026-02-24T10:00:00Z",
            "attendees": [
                {"displayName": "John Doe", "email": "john@example.com"},
            ],
        }

        question = format_uncertain_meeting_question(event)

        assert "Product Discussion" in question
        assert "2026-02-24T10:00:00Z" in question
        assert "John Doe" in question

    def test_format_with_many_attendees(self):
        """Test formatting with many attendees shows 'and X others'."""
        from guardrails.calendar_filter import format_uncertain_meeting_question

        event = {
            "title": "Big Meeting",
            "start": "2026-02-24T10:00:00Z",
            "attendees": [
                {"displayName": "Person 1", "email": "p1@example.com"},
                {"displayName": "Person 2", "email": "p2@example.com"},
                {"displayName": "Person 3", "email": "p3@example.com"},
                {"displayName": "Person 4", "email": "p4@example.com"},
                {"displayName": "Person 5", "email": "p5@example.com"},
            ],
        }

        question = format_uncertain_meeting_question(event)

        assert "and 2 others" in question

    def test_format_uses_email_when_no_display_name(self):
        """Test that email is used when displayName is missing."""
        from guardrails.calendar_filter import format_uncertain_meeting_question

        event = {
            "title": "Meeting",
            "start": "2026-02-24T10:00:00Z",
            "attendees": [
                {"email": "test@example.com"},
            ],
        }

        question = format_uncertain_meeting_question(event)

        assert "test@example.com" in question
