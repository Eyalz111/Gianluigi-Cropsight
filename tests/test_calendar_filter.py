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


from contextlib import ExitStack


def _enforce_strict():
    """Context manager: strict calendar filter ENFORCED (flag on, not shadow)."""
    from config.settings import settings
    stack = ExitStack()
    stack.enter_context(patch.object(settings, "STRICT_CALENDAR_FILTER", True))
    stack.enter_context(patch.object(settings, "INPUT_HYGIENE_SHADOW_MODE", False))
    stack.enter_context(patch.object(settings, "CROPSIGHT_CALENDAR_COLOR_ID", "3"))
    return stack


class TestStrictCalendarFilter:
    """The strict chain (STRICT_CALENDAR_FILTER on): purple/business/stakeholder/prefix; no 2+ gmail branch."""

    def test_purple_still_caught(self):
        from guardrails.calendar_filter import is_cropsight_meeting
        with _enforce_strict():
            assert is_cropsight_meeting(
                {"title": "Some meeting", "attendees": [], "color_id": "3"}
            ) is True

    def test_business_domain_attendee_detected(self):
        from guardrails.calendar_filter import is_cropsight_meeting
        with _enforce_strict():
            event = {"title": "Call", "attendees": [{"email": "x@cropsight.io"}], "color_id": None}
            assert is_cropsight_meeting(event) is True

    def test_known_stakeholder_domain_detected(self):
        """Ad-hoc call with a known client on their own domain — caught even without purple."""
        from guardrails.calendar_filter import is_cropsight_meeting
        with _enforce_strict(), patch(
            "guardrails.calendar_filter.is_known_stakeholder_domain", return_value=True
        ):
            event = {"title": "Sync", "attendees": [{"email": "contact@moldova-client.md"}], "color_id": None}
            assert is_cropsight_meeting(event) is True

    def test_two_personal_gmails_now_uncertain(self):
        """The old leak: 2 personal gmails + no color -> now uncertain (excluded)."""
        from guardrails.calendar_filter import is_cropsight_meeting
        with _enforce_strict(), patch(
            "guardrails.calendar_filter.is_known_stakeholder_domain", return_value=False
        ):
            event = {
                "title": "Coffee",
                "attendees": [{"email": "eyalz111@gmail.com"}, {"email": "tadmoroye@gmail.com"}],
                "color_id": None,
            }
            assert is_cropsight_meeting(event) is None

    def test_blocklist_wins_over_business_domain(self):
        from guardrails.calendar_filter import is_cropsight_meeting
        with _enforce_strict():
            event = {"title": "Dentist", "attendees": [{"email": "x@cropsight.io"}], "color_id": "3"}
            assert is_cropsight_meeting(event) is False

    def test_prefix_detected(self):
        from guardrails.calendar_filter import is_cropsight_meeting
        with _enforce_strict():
            assert is_cropsight_meeting(
                {"title": "CropSight sync", "attendees": [], "color_id": None}
            ) is True

    def test_uncertain_when_no_signal(self):
        from guardrails.calendar_filter import is_cropsight_meeting
        with _enforce_strict(), patch(
            "guardrails.calendar_filter.is_known_stakeholder_domain", return_value=False
        ):
            assert is_cropsight_meeting(
                {"title": "Meeting", "attendees": [], "color_id": None}
            ) is None


class TestCalendarShadowMode:
    """In shadow, the strict decision is logged but the LEGACY decision is returned."""

    def test_shadow_returns_legacy_and_logs_delta(self):
        from config.settings import settings
        from guardrails import calendar_filter as cf
        event = {
            "title": "Coffee",
            "attendees": [{"email": "a@gmail.com"}, {"email": "b@gmail.com"}],
            "color_id": None,
        }
        with patch.object(settings, "STRICT_CALENDAR_FILTER", True), \
             patch.object(settings, "INPUT_HYGIENE_SHADOW_MODE", True), \
             patch("guardrails.calendar_filter.CROPSIGHT_TEAM_EMAILS", ["a@gmail.com", "b@gmail.com"]), \
             patch("guardrails.calendar_filter.is_known_stakeholder_domain", return_value=False), \
             patch.object(cf, "_log_calendar_shadow") as mock_log:
            result = cf.is_cropsight_meeting(event)
        assert result is True            # legacy 2+ branch returned
        mock_log.assert_called_once()    # strict (None) != legacy (True) -> delta logged


class TestShouldIncludeMeeting:
    """should_include_meeting honors STRICT_UNCERTAIN_EXCLUSION only when enforcing."""

    def test_excludes_uncertain_when_enforcing(self):
        from config.settings import settings
        from guardrails.calendar_filter import should_include_meeting
        event = {
            "title": "Coffee",
            "attendees": [{"email": "eyalz111@gmail.com"}, {"email": "tadmoroye@gmail.com"}],
            "color_id": None,
        }
        with patch.object(settings, "STRICT_CALENDAR_FILTER", True), \
             patch.object(settings, "INPUT_HYGIENE_SHADOW_MODE", False), \
             patch.object(settings, "STRICT_UNCERTAIN_EXCLUSION", True), \
             patch("guardrails.calendar_filter.is_known_stakeholder_domain", return_value=False):
            assert should_include_meeting(event) is False  # strict -> None -> excluded

    def test_includes_uncertain_during_shadow(self):
        from config.settings import settings
        from guardrails.calendar_filter import should_include_meeting
        event = {
            "title": "Coffee",
            "attendees": [{"email": "a@gmail.com"}, {"email": "b@gmail.com"}],
            "color_id": None,
        }
        with patch.object(settings, "STRICT_CALENDAR_FILTER", True), \
             patch.object(settings, "INPUT_HYGIENE_SHADOW_MODE", True), \
             patch.object(settings, "STRICT_UNCERTAIN_EXCLUSION", True), \
             patch("guardrails.calendar_filter.CROPSIGHT_TEAM_EMAILS", ["a@gmail.com", "b@gmail.com"]), \
             patch("guardrails.calendar_filter.is_known_stakeholder_domain", return_value=False):
            # shadow returns legacy (True via 2+), exclusion suppressed during shadow
            assert should_include_meeting(event) is True


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


class TestStringAttendeesDefensive:
    """Regression: some calendar events arrive with attendees as plain email strings
    (not dicts). Pre-fix this crashed prep_ping_scheduler with
    `'str' object has no attribute 'get'` (live error 2026-05-28)."""

    def test_participant_emails_handles_string_attendees_and_organizer(self):
        from guardrails.calendar_filter import _participant_emails
        # Attendees as strings, organizer as string (both shapes seen in the wild).
        event = {
            "attendees": ["a@cropsight.io", "b@x.com"],
            "organizer": "o@cropsight.io",
        }
        emails = _participant_emails(event)
        assert "o@cropsight.io" in emails
        assert "a@cropsight.io" in emails
        assert "b@x.com" in emails

    def test_has_sufficient_team_members_handles_strings(self):
        from guardrails.calendar_filter import _has_sufficient_team_members
        # Contract here is "does not crash" — count depends on CROPSIGHT_TEAM_EMAILS.
        result = _has_sufficient_team_members(["roye@cropsight.io", "paolo@cropsight.io", "x@external.com"])
        assert isinstance(result, bool)

    def test_format_uncertain_handles_string_attendees(self):
        from guardrails.calendar_filter import format_uncertain_meeting_question
        event = {
            "title": "Meeting",
            "start": "2026-05-29T10:00:00Z",
            "attendees": ["a@x.com", "b@y.com"],  # strings, not dicts
        }
        question = format_uncertain_meeting_question(event)
        assert "a@x.com" in question and "b@y.com" in question
