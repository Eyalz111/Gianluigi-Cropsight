"""
Tests for processors/meeting_type_matcher.py (Phase 5.1).

Tests cover:
- Score calculation for each template
- Edge cases: no attendees, ambiguous titles, multiple matches
- Threshold behavior (auto/ask/none)
- Persistence/recall via calendar_classifications
- Previously-matched signal
"""

import pytest
from unittest.mock import MagicMock, patch
from datetime import datetime


# =============================================================================
# Helpers
# =============================================================================

def _make_event(title="Tech Review", attendees=None, start="2026-03-17T10:00:00+02:00"):
    """Create a minimal calendar event dict."""
    if attendees is None:
        attendees = [
            {"displayName": "Eyal Zror", "email": "eyal@cropsight.com"},
            {"displayName": "Roye Tadmor", "email": "roye@cropsight.com"},
        ]
    return {"title": title, "attendees": attendees, "start": start}


# =============================================================================
# Test score_meeting_type
# =============================================================================

class TestScoreMeetingType:
    """Tests for score_meeting_type() — scoring signals per template."""

    def test_founders_technical_title_match(self):
        """Tech review title should give +3 for founders_technical."""
        with patch("processors.meeting_type_matcher.supabase_client") as mock_db:
            mock_db.get_classification_by_title.return_value = None
            from processors.meeting_type_matcher import score_meeting_type

            event = _make_event(title="CropSight Tech Review")
            scores = score_meeting_type(event)

            # Find founders_technical score
            tech_score = next(s for s in scores if s[0] == "founders_technical")
            assert tech_score[1] >= 3
            assert "title_match" in tech_score[2]

    def test_founders_business_title_match(self):
        """Business review title should match founders_business."""
        with patch("processors.meeting_type_matcher.supabase_client") as mock_db:
            mock_db.get_classification_by_title.return_value = None
            from processors.meeting_type_matcher import score_meeting_type

            event = _make_event(
                title="BD Pipeline Review",
                attendees=[
                    {"displayName": "Eyal", "email": "eyal@test.com"},
                    {"displayName": "Paolo", "email": "paolo@test.com"},
                ],
            )
            scores = score_meeting_type(event)
            biz_score = next(s for s in scores if s[0] == "founders_business")
            assert "exact_participants" in biz_score[2]

    def test_monthly_strategic_high_score(self):
        """Monthly review with all founders should score high for monthly_strategic."""
        with patch("processors.meeting_type_matcher.supabase_client") as mock_db:
            mock_db.get_classification_by_title.return_value = None
            from processors.meeting_type_matcher import score_meeting_type

            event = _make_event(
                title="Monthly Strategic Review",
                attendees=[
                    {"displayName": "Eyal", "email": "eyal@test.com"},
                    {"displayName": "Roye", "email": "roye@test.com"},
                    {"displayName": "Paolo", "email": "paolo@test.com"},
                ],
            )
            scores = score_meeting_type(event)
            monthly = next(s for s in scores if s[0] == "monthly_strategic")
            assert monthly[1] >= 3  # title + participants

    def test_participant_exact_match_gives_2_points(self):
        """All expected participants present → +2 points."""
        with patch("processors.meeting_type_matcher.supabase_client") as mock_db:
            mock_db.get_classification_by_title.return_value = None
            from processors.meeting_type_matcher import score_meeting_type

            event = _make_event(
                title="Some Meeting",
                attendees=[
                    {"displayName": "Eyal", "email": "e@t.com"},
                    {"displayName": "Roye", "email": "r@t.com"},
                ],
            )
            scores = score_meeting_type(event)
            tech = next(s for s in scores if s[0] == "founders_technical")
            assert "exact_participants" in tech[2]

    def test_participant_partial_match_gives_1_point(self):
        """Only some expected participants → +1 point."""
        with patch("processors.meeting_type_matcher.supabase_client") as mock_db:
            mock_db.get_classification_by_title.return_value = None
            from processors.meeting_type_matcher import score_meeting_type

            # monthly_strategic expects eyal+roye+paolo, only eyal present
            event = _make_event(
                title="Random Meeting",
                attendees=[{"displayName": "Eyal", "email": "e@t.com"}],
            )
            scores = score_meeting_type(event)
            monthly = next(s for s in scores if s[0] == "monthly_strategic")
            assert "partial_participants" in monthly[2]

    def test_day_of_week_match(self):
        """Tuesday event should get +1 for founders_technical (expected Tuesday)."""
        with patch("processors.meeting_type_matcher.supabase_client") as mock_db:
            mock_db.get_classification_by_title.return_value = None
            from processors.meeting_type_matcher import score_meeting_type

            # 2026-03-17 is a Tuesday
            event = _make_event(
                title="Random Meeting",
                attendees=[],
                start="2026-03-17T10:00:00+02:00",
            )
            scores = score_meeting_type(event)
            tech = next(s for s in scores if s[0] == "founders_technical")
            assert "day_match" in tech[2]

    def test_previously_matched_signal(self):
        """Previously matched meeting type → +2 points."""
        with patch("processors.meeting_type_matcher.supabase_client") as mock_db:
            mock_db.get_classification_by_title.return_value = {
                "meeting_type": "founders_technical",
            }
            from processors.meeting_type_matcher import score_meeting_type

            event = _make_event(title="Some Unrelated Title", attendees=[])
            scores = score_meeting_type(event)
            tech = next(s for s in scores if s[0] == "founders_technical")
            assert "previously_matched" in tech[2]
            assert tech[1] >= 2

    def test_no_attendees(self):
        """Event with no attendees should still produce scores (no crash)."""
        with patch("processors.meeting_type_matcher.supabase_client") as mock_db:
            mock_db.get_classification_by_title.return_value = None
            from processors.meeting_type_matcher import score_meeting_type

            event = _make_event(title="Tech Review", attendees=[])
            scores = score_meeting_type(event)
            assert len(scores) > 0

    def test_empty_title(self):
        """Event with empty title should not crash."""
        with patch("processors.meeting_type_matcher.supabase_client") as mock_db:
            mock_db.get_classification_by_title.return_value = None
            from processors.meeting_type_matcher import score_meeting_type

            event = _make_event(title="", attendees=[])
            scores = score_meeting_type(event)
            assert all(s[1] == 0 or "day_match" in s[2] or "previously_matched" in s[2]
                       for s in scores)

    def test_scores_sorted_descending(self):
        """Results should be sorted by score descending."""
        with patch("processors.meeting_type_matcher.supabase_client") as mock_db:
            mock_db.get_classification_by_title.return_value = None
            from processors.meeting_type_matcher import score_meeting_type

            event = _make_event(title="Tech Review")
            scores = score_meeting_type(event)
            score_values = [s[1] for s in scores]
            assert score_values == sorted(score_values, reverse=True)


# =============================================================================
# Test classify_meeting_type
# =============================================================================

class TestClassifyMeetingType:
    """Tests for classify_meeting_type() — threshold behavior."""

    def test_auto_confidence_when_score_gte_3(self):
        """Score >= 3 → auto confidence."""
        with patch("processors.meeting_type_matcher.supabase_client") as mock_db:
            mock_db.get_classification_by_title.return_value = None
            from processors.meeting_type_matcher import classify_meeting_type

            # Title match (+3) + participants (+2) = 5
            event = _make_event(title="CropSight Tech Review")
            meeting_type, confidence, signals = classify_meeting_type(event)

            assert confidence == "auto"
            assert meeting_type == "founders_technical"

    def test_ask_confidence_when_score_eq_2(self):
        """Score == 2 → ask confidence."""
        with patch("processors.meeting_type_matcher.supabase_client") as mock_db:
            mock_db.get_classification_by_title.return_value = None
            from processors.meeting_type_matcher import classify_meeting_type

            # Only exact participants (+2), no title match, non-Tuesday
            event = _make_event(
                title="Catch-Up",
                start="2026-03-18T10:00:00+02:00",  # Wednesday
            )
            meeting_type, confidence, signals = classify_meeting_type(event)

            # Should be "ask" because participants give +2 but title doesn't match
            assert confidence == "ask"

    def test_none_confidence_when_score_lt_2(self):
        """Score < 2 → generic with none confidence."""
        with patch("processors.meeting_type_matcher.supabase_client") as mock_db:
            mock_db.get_classification_by_title.return_value = None
            from processors.meeting_type_matcher import classify_meeting_type

            event = _make_event(
                title="Completely Unknown Meeting",
                attendees=[{"displayName": "External Person", "email": "ext@other.com"}],
                start="2026-03-15T10:00:00+02:00",  # Sunday — no day match
            )
            meeting_type, confidence, signals = classify_meeting_type(event)

            assert meeting_type == "generic"
            assert confidence == "none"

    def test_multiple_templates_picks_highest(self):
        """When multiple templates score > 0, picks the highest."""
        with patch("processors.meeting_type_matcher.supabase_client") as mock_db:
            mock_db.get_classification_by_title.return_value = None
            from processors.meeting_type_matcher import classify_meeting_type

            # "Tech Review" with Eyal+Roye → founders_technical should beat generic
            event = _make_event(title="Sprint Review")
            meeting_type, confidence, signals = classify_meeting_type(event)

            assert meeting_type == "founders_technical"

    def test_generic_fallback_for_unknown_meeting(self):
        """Unknown meeting with no signals → generic."""
        with patch("processors.meeting_type_matcher.supabase_client") as mock_db:
            mock_db.get_classification_by_title.return_value = None
            from processors.meeting_type_matcher import classify_meeting_type

            event = _make_event(
                title="Coffee Chat",
                attendees=[],
                start="2026-03-15T10:00:00+02:00",
            )
            meeting_type, confidence, signals = classify_meeting_type(event)
            assert meeting_type == "generic"


# =============================================================================
# Test remember_meeting_type
# =============================================================================

class TestRememberMeetingType:
    """Tests for remember_meeting_type() — persistent learning."""

    def test_remember_calls_supabase(self):
        """Should update classification in Supabase."""
        with patch("processors.meeting_type_matcher.supabase_client") as mock_db:
            mock_db.update_classification_meeting_type.return_value = {"id": "1"}
            from processors.meeting_type_matcher import remember_meeting_type

            remember_meeting_type("Tech Review", "founders_technical")
            mock_db.update_classification_meeting_type.assert_called_once_with(
                "Tech Review", "founders_technical"
            )

    def test_remember_handles_error(self):
        """Should not crash on Supabase errors."""
        with patch("processors.meeting_type_matcher.supabase_client") as mock_db:
            mock_db.update_classification_meeting_type.side_effect = Exception("DB error")
            from processors.meeting_type_matcher import remember_meeting_type

            # Should not raise
            remember_meeting_type("Tech Review", "founders_technical")


# =============================================================================
# Test _title_matches_template
# =============================================================================

class TestTitleMatchesTemplate:
    """Tests for the title fuzzy matching helper."""

    def test_exact_pattern_match(self):
        """Exact pattern in title → match."""
        from processors.meeting_type_matcher import _title_matches_template

        assert _title_matches_template("Tech Review", ["tech review"])

    def test_partial_word_overlap(self):
        """Partial overlap above 60% → match."""
        from processors.meeting_type_matcher import _title_matches_template

        assert _title_matches_template(
            "CropSight Technical Review Session",
            ["technical review"],
        )

    def test_no_overlap(self):
        """No word overlap → no match."""
        from processors.meeting_type_matcher import _title_matches_template

        assert not _title_matches_template(
            "Coffee With Friends",
            ["tech review", "sprint planning"],
        )

    def test_empty_title(self):
        """Empty title → no match."""
        from processors.meeting_type_matcher import _title_matches_template

        assert not _title_matches_template("", ["tech review"])

    def test_empty_patterns(self):
        """Empty patterns list → no match."""
        from processors.meeting_type_matcher import _title_matches_template

        assert not _title_matches_template("Tech Review", [])


# =============================================================================
# Test _parse_day_of_week
# =============================================================================

class TestParseDayOfWeek:
    """Tests for day-of-week parsing."""

    def test_parses_tuesday(self):
        """2026-03-17 is a Tuesday."""
        from processors.meeting_type_matcher import _parse_day_of_week

        assert _parse_day_of_week("2026-03-17T10:00:00+02:00") == "Tuesday"

    def test_parses_friday(self):
        """2026-03-20 is a Friday."""
        from processors.meeting_type_matcher import _parse_day_of_week

        assert _parse_day_of_week("2026-03-20T10:00:00+02:00") == "Friday"

    def test_empty_string(self):
        from processors.meeting_type_matcher import _parse_day_of_week

        assert _parse_day_of_week("") is None

    def test_invalid_string(self):
        from processors.meeting_type_matcher import _parse_day_of_week

        assert _parse_day_of_week("not-a-date") is None


# =============================================================================
# Test LLM fallback classification
# =============================================================================

class TestLLMFallbackClassification:
    """Tests for LLM-based classification fallback."""

    def test_hebrew_title_triggers_llm(self):
        """Hebrew title with low fuzzy score should trigger LLM fallback."""
        with patch("processors.meeting_type_matcher.supabase_client") as mock_db, \
             patch("processors.meeting_type_matcher._classify_with_llm") as mock_llm:
            mock_db.get_classification_by_title.return_value = None
            mock_llm.return_value = "founders_technical"

            from processors.meeting_type_matcher import classify_meeting_type

            event = {
                "title": "\u05e1\u05e7\u05d9\u05e8\u05d4 \u05d8\u05db\u05e0\u05d9\u05ea \u05e9\u05d1\u05d5\u05e2\u05d9\u05ea",  # Hebrew: "Weekly Technical Review"
                "attendees": [
                    {"displayName": "External Person", "email": "ext@other.com"},
                ],
                "start": "2026-03-15T10:00:00+02:00",  # Sunday — no day match
            }

            meeting_type, confidence, signals = classify_meeting_type(event)
            # Should use LLM result with "ask" confidence
            assert meeting_type == "founders_technical"
            assert confidence == "ask"
            assert "llm_classification" in signals
            mock_llm.assert_called_once()

    def test_english_high_score_skips_llm(self):
        """English title with high score should NOT trigger LLM."""
        with patch("processors.meeting_type_matcher.supabase_client") as mock_db, \
             patch("processors.meeting_type_matcher._classify_with_llm") as mock_llm:
            mock_db.get_classification_by_title.return_value = None

            from processors.meeting_type_matcher import classify_meeting_type

            event = {
                "title": "Tech Review",
                "attendees": [
                    {"displayName": "Eyal Zror", "email": "eyal@cropsight.com"},
                    {"displayName": "Roye Tadmor", "email": "roye@cropsight.com"},
                ],
                "start": "2026-03-17T10:00:00+02:00",
            }

            classify_meeting_type(event)
            mock_llm.assert_not_called()

    def test_llm_returns_generic_no_change(self):
        """LLM returning generic should still result in generic/none."""
        with patch("processors.meeting_type_matcher.supabase_client") as mock_db, \
             patch("processors.meeting_type_matcher._classify_with_llm") as mock_llm:
            mock_db.get_classification_by_title.return_value = None
            mock_llm.return_value = "generic"

            from processors.meeting_type_matcher import classify_meeting_type

            event = {
                "title": "Random meeting",
                "attendees": [],
                "start": "2026-03-17T10:00:00",
            }

            meeting_type, confidence, signals = classify_meeting_type(event)
            assert meeting_type == "generic"
            assert confidence == "none"

    def test_llm_failure_degrades_gracefully(self):
        """LLM failure should fall back to generic/none."""
        with patch("processors.meeting_type_matcher.supabase_client") as mock_db, \
             patch("processors.meeting_type_matcher._classify_with_llm") as mock_llm:
            mock_db.get_classification_by_title.return_value = None
            mock_llm.return_value = None  # LLM failed

            from processors.meeting_type_matcher import classify_meeting_type

            event = {
                "title": "\u05e4\u05d2\u05d9\u05e9\u05d4 \u05db\u05dc\u05dc\u05d9\u05ea",  # Hebrew: "General meeting"
                "attendees": [],
                "start": "2026-03-17T10:00:00",
            }

            meeting_type, confidence, signals = classify_meeting_type(event)
            assert meeting_type == "generic"
            assert confidence == "none"
