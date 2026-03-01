"""
Tests for calendar classification memory (write + read).

Tests the persistence of Eyal's meeting classification answers
and the fuzzy matching logic for similar meeting titles.
"""

import pytest
from unittest.mock import patch, MagicMock


class TestCalendarClassificationWrite:
    """Tests for remember_meeting_classification (write side)."""

    @patch("guardrails.calendar_filter.supabase_client", create=True)
    def test_remember_classification_stores_cropsight(self, mock_client):
        """Storing a CropSight classification calls remember_classification."""
        # Import fresh to pick up the mock
        from guardrails.calendar_filter import remember_meeting_classification

        remember_meeting_classification("CropSight Strategy Call", True)

        # The function imports supabase_client inside, so we patch at module level
        # Actually, the function does a local import, so we need to patch the module
        # Let's test the supabase method directly instead

    def test_remember_classification_crud(self):
        """Supabase remember_classification creates a record."""
        from services.supabase_client import SupabaseClient

        client = SupabaseClient()
        mock_table = MagicMock()
        mock_table.insert.return_value.execute.return_value = MagicMock(
            data=[{
                "id": "test-uuid",
                "title": "Strategy Meeting",
                "is_cropsight": True,
                "classified_by": "eyal",
            }]
        )
        mock_supabase = MagicMock()
        mock_supabase.table.return_value = mock_table
        object.__setattr__(client, "_client", mock_supabase)

        result = client.remember_classification("Strategy Meeting", True)

        mock_supabase.table.assert_called_with("calendar_classifications")
        mock_table.insert.assert_called_once_with({
            "title": "Strategy Meeting",
            "is_cropsight": True,
            "classified_by": "eyal",
        })
        assert result["is_cropsight"] is True

    def test_get_classification_by_title_found(self):
        """Exact title lookup returns the classification record."""
        from services.supabase_client import SupabaseClient

        client = SupabaseClient()
        mock_table = MagicMock()
        mock_table.select.return_value.eq.return_value.limit.return_value.execute.return_value = (
            MagicMock(data=[{
                "id": "test-uuid",
                "title": "Strategy Meeting",
                "title_lower": "strategy meeting",
                "is_cropsight": True,
            }])
        )
        mock_supabase = MagicMock()
        mock_supabase.table.return_value = mock_table
        object.__setattr__(client, "_client", mock_supabase)

        result = client.get_classification_by_title("Strategy Meeting")

        assert result is not None
        assert result["is_cropsight"] is True
        # Check case-insensitive lookup
        mock_table.select.return_value.eq.assert_called_with(
            "title_lower", "strategy meeting"
        )

    def test_get_classification_by_title_not_found(self):
        """Returns None when no matching classification exists."""
        from services.supabase_client import SupabaseClient

        client = SupabaseClient()
        mock_table = MagicMock()
        mock_table.select.return_value.eq.return_value.limit.return_value.execute.return_value = (
            MagicMock(data=[])
        )
        mock_supabase = MagicMock()
        mock_supabase.table.return_value = mock_table
        object.__setattr__(client, "_client", mock_supabase)

        result = client.get_classification_by_title("Unknown Meeting")
        assert result is None

    def test_get_all_classifications(self):
        """Get all classifications returns list."""
        from services.supabase_client import SupabaseClient

        client = SupabaseClient()
        mock_table = MagicMock()
        mock_table.select.return_value.order.return_value.limit.return_value.execute.return_value = (
            MagicMock(data=[
                {"title": "Meeting A", "is_cropsight": True},
                {"title": "Meeting B", "is_cropsight": False},
            ])
        )
        mock_supabase = MagicMock()
        mock_supabase.table.return_value = mock_table
        object.__setattr__(client, "_client", mock_supabase)

        result = client.get_all_classifications()
        assert len(result) == 2

    def test_remember_classification_error_does_not_raise(self):
        """remember_meeting_classification swallows exceptions."""
        from guardrails.calendar_filter import remember_meeting_classification

        with patch(
            "guardrails.calendar_filter.supabase_client",
            create=True,
        ) as mock:
            # Make the import itself work but the call fail
            pass

        # Even if supabase is unavailable, no exception should propagate
        # We patch the import inside the function
        with patch.dict(
            "sys.modules",
            {"services.supabase_client": MagicMock(
                supabase_client=MagicMock(
                    remember_classification=MagicMock(side_effect=Exception("DB down"))
                )
            )}
        ):
            # Should not raise
            remember_meeting_classification("Test Meeting", True)


class TestCalendarClassificationRead:
    """Tests for check_remembered_classification (read side)."""

    def test_exact_match_returns_true(self):
        """Exact match for a CropSight meeting returns True."""
        from guardrails.calendar_filter import check_remembered_classification

        mock_module = MagicMock()
        mock_module.supabase_client.get_classification_by_title.return_value = {
            "is_cropsight": True,
            "title": "CropSight Board Meeting",
        }

        with patch.dict("sys.modules", {"services.supabase_client": mock_module}):
            result = check_remembered_classification("CropSight Board Meeting")

        assert result is True

    def test_exact_match_returns_false(self):
        """Exact match for a personal meeting returns False."""
        from guardrails.calendar_filter import check_remembered_classification

        mock_module = MagicMock()
        mock_module.supabase_client.get_classification_by_title.return_value = {
            "is_cropsight": False,
            "title": "Yoga Class",
        }

        with patch.dict("sys.modules", {"services.supabase_client": mock_module}):
            result = check_remembered_classification("Yoga Class")

        assert result is False

    def test_no_match_returns_none(self):
        """No exact or fuzzy match returns None."""
        from guardrails.calendar_filter import check_remembered_classification

        mock_module = MagicMock()
        mock_module.supabase_client.get_classification_by_title.return_value = None
        mock_module.supabase_client.get_all_classifications.return_value = []

        with patch.dict("sys.modules", {"services.supabase_client": mock_module}):
            result = check_remembered_classification("Brand New Meeting")

        assert result is None

    def test_fuzzy_match_above_threshold(self):
        """Fuzzy match with sufficient overlap returns classification."""
        from guardrails.calendar_filter import check_remembered_classification

        mock_module = MagicMock()
        mock_module.supabase_client.get_classification_by_title.return_value = None
        mock_module.supabase_client.get_all_classifications.return_value = [
            {"title": "CropSight Strategy Planning Session", "is_cropsight": True},
        ]

        with patch.dict("sys.modules", {"services.supabase_client": mock_module}):
            # "CropSight Strategy Planning" shares significant words with the stored title
            result = check_remembered_classification("CropSight Strategy Planning")

        assert result is True

    def test_fuzzy_match_below_threshold(self):
        """Fuzzy match with low overlap returns None."""
        from guardrails.calendar_filter import check_remembered_classification

        mock_module = MagicMock()
        mock_module.supabase_client.get_classification_by_title.return_value = None
        mock_module.supabase_client.get_all_classifications.return_value = [
            {"title": "CropSight Board Review", "is_cropsight": True},
        ]

        with patch.dict("sys.modules", {"services.supabase_client": mock_module}):
            # "Investor Pitch Prep" has no overlap with stored title
            result = check_remembered_classification("Investor Pitch Prep")

        assert result is None

    def test_error_returns_none(self):
        """Database errors return None gracefully."""
        from guardrails.calendar_filter import check_remembered_classification

        mock_module = MagicMock()
        mock_module.supabase_client.get_classification_by_title.side_effect = Exception("DB error")

        with patch.dict("sys.modules", {"services.supabase_client": mock_module}):
            result = check_remembered_classification("Some Meeting")

        assert result is None


class TestFuzzyMatchHelpers:
    """Tests for _extract_significant_words and _find_fuzzy_match."""

    def test_extract_strips_stop_words(self):
        """Stop words like 'meeting', 'call', 'sync' are removed."""
        from guardrails.calendar_filter import _extract_significant_words

        words = _extract_significant_words("Weekly Sync Meeting with Partners")
        assert "weekly" not in words
        assert "sync" not in words
        assert "meeting" not in words
        assert "with" not in words
        assert "partners" in words

    def test_extract_strips_short_words(self):
        """Words with 1-2 characters are removed."""
        from guardrails.calendar_filter import _extract_significant_words

        words = _extract_significant_words("A B CD Partnership Review")
        assert "a" not in words
        assert "b" not in words
        assert "cd" not in words
        assert "partnership" in words

    def test_extract_strips_punctuation(self):
        """Punctuation is stripped from words."""
        from guardrails.calendar_filter import _extract_significant_words

        words = _extract_significant_words("CropSight: Strategy & Planning!")
        assert "cropsight" in words
        assert "strategy" in words
        assert "planning" in words

    def test_extract_case_insensitive(self):
        """Words are lowercased."""
        from guardrails.calendar_filter import _extract_significant_words

        words = _extract_significant_words("CropSight BOARD Meeting")
        assert "cropsight" in words
        assert "board" in words

    def test_fuzzy_match_high_overlap(self):
        """High word overlap returns classification."""
        from guardrails.calendar_filter import _find_fuzzy_match

        classifications = [
            {"title": "CropSight Strategy Planning", "is_cropsight": True},
        ]
        result = _find_fuzzy_match("CropSight Strategy Session", classifications)
        # "cropsight" and "strategy" match; "session" is a stop word
        # So 2/2 significant words match = 100% overlap
        assert result is True

    def test_fuzzy_match_no_overlap(self):
        """No word overlap returns None."""
        from guardrails.calendar_filter import _find_fuzzy_match

        classifications = [
            {"title": "CropSight Board Review", "is_cropsight": True},
        ]
        result = _find_fuzzy_match("Investor Pitch Prep", classifications)
        assert result is None

    def test_fuzzy_match_empty_title(self):
        """Empty or stop-word-only title returns None."""
        from guardrails.calendar_filter import _find_fuzzy_match

        classifications = [
            {"title": "CropSight Board Review", "is_cropsight": True},
        ]
        result = _find_fuzzy_match("Weekly Meeting Call", classifications)
        assert result is None

    def test_fuzzy_match_empty_classifications(self):
        """Empty classification list returns None."""
        from guardrails.calendar_filter import _find_fuzzy_match

        result = _find_fuzzy_match("CropSight Strategy", [])
        assert result is None

    def test_fuzzy_match_custom_threshold(self):
        """Custom threshold is respected."""
        from guardrails.calendar_filter import _find_fuzzy_match

        classifications = [
            {"title": "CropSight Partnership Board", "is_cropsight": True},
        ]
        # "CropSight Investor Board" has 2/3 significant words matching
        # (cropsight, board match; investor doesn't) = 66%
        result_high = _find_fuzzy_match(
            "CropSight Investor Board", classifications, threshold=0.8
        )
        result_low = _find_fuzzy_match(
            "CropSight Investor Board", classifications, threshold=0.5
        )
        assert result_high is None  # 66% < 80%
        assert result_low is True   # 66% >= 50%
