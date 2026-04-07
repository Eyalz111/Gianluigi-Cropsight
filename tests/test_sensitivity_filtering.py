"""
Tests for retrieval-level sensitivity filtering (v2.2 Session 2.5).

Validates TIER_LEVELS hierarchy, filter_by_sensitivity utility,
and that filtering is applied correctly in continuity and prep contexts.
"""

import pytest
from unittest.mock import patch, MagicMock

from models.schemas import TIER_LEVELS, filter_by_sensitivity, Sensitivity


class TestTierLevels:
    """Verify numeric tier hierarchy."""

    def test_hierarchy_order(self):
        assert TIER_LEVELS["public"] < TIER_LEVELS["team"]
        assert TIER_LEVELS["team"] < TIER_LEVELS["founders"]
        assert TIER_LEVELS["founders"] < TIER_LEVELS["ceo"]

    def test_exact_values(self):
        assert TIER_LEVELS == {"public": 1, "team": 2, "founders": 3, "ceo": 4}

    def test_ceo_is_highest(self):
        assert max(TIER_LEVELS.values()) == TIER_LEVELS["ceo"]

    def test_public_is_lowest(self):
        assert min(TIER_LEVELS.values()) == TIER_LEVELS["public"]


class TestFilterBySensitivity:
    """Tests for filter_by_sensitivity utility."""

    def test_empty_list_returns_empty(self):
        assert filter_by_sensitivity([], 3) == []

    def test_all_below_threshold_returned(self):
        items = [
            {"id": 1, "sensitivity": "public"},
            {"id": 2, "sensitivity": "founders"},
        ]
        result = filter_by_sensitivity(items, 3)
        assert len(result) == 2

    def test_excludes_above_threshold(self):
        items = [
            {"id": 1, "sensitivity": "ceo"},
            {"id": 2, "sensitivity": "founders"},
            {"id": 3, "sensitivity": "public"},
        ]
        result = filter_by_sensitivity(items, 3)  # FOUNDERS level
        assert len(result) == 2
        assert all(item["id"] != 1 for item in result)

    def test_ceo_level_returns_all(self):
        items = [
            {"id": 1, "sensitivity": "ceo"},
            {"id": 2, "sensitivity": "founders"},
            {"id": 3, "sensitivity": "public"},
        ]
        result = filter_by_sensitivity(items, 4)  # CEO level
        assert len(result) == 3

    def test_public_level_excludes_everything_else(self):
        items = [
            {"id": 1, "sensitivity": "ceo"},
            {"id": 2, "sensitivity": "founders"},
            {"id": 3, "sensitivity": "public"},
        ]
        result = filter_by_sensitivity(items, 1)  # PUBLIC level
        assert len(result) == 1
        assert result[0]["id"] == 3

    def test_missing_sensitivity_defaults_to_founders(self):
        items = [{"id": 1}, {"id": 2, "sensitivity": "ceo"}]
        result = filter_by_sensitivity(items, 3)
        assert len(result) == 1
        assert result[0]["id"] == 1  # default founders (3) <= 3

    def test_legacy_ceo_only_treated_as_ceo(self):
        items = [{"id": 1, "sensitivity": "ceo_only"}]
        result = filter_by_sensitivity(items, 3)
        assert len(result) == 0

    def test_legacy_restricted_treated_as_ceo(self):
        items = [{"id": 1, "sensitivity": "restricted"}]
        result = filter_by_sensitivity(items, 3)
        assert len(result) == 0

    def test_legacy_sensitive_treated_as_ceo(self):
        items = [{"id": 1, "sensitivity": "sensitive"}]
        result = filter_by_sensitivity(items, 3)
        assert len(result) == 0

    def test_legacy_normal_treated_as_founders(self):
        items = [{"id": 1, "sensitivity": "normal"}]
        result = filter_by_sensitivity(items, 3)
        assert len(result) == 1

    def test_team_tier_at_founders_level(self):
        """TEAM tier (level 2) should pass through FOUNDERS filter (level 3)."""
        items = [{"id": 1, "sensitivity": "team"}]
        result = filter_by_sensitivity(items, 3)
        assert len(result) == 1


class TestContinuityFiltering:
    """Verify CEO items are excluded from FOUNDERS-level context."""

    def test_build_meeting_continuity_filters_at_founders_level(self):
        """Meetings with CEO sensitivity should be excluded at FOUNDERS level."""
        from processors.meeting_continuity import build_meeting_continuity_context

        mock_meetings = [
            {"id": "m1", "title": "Ops Review", "date": "2026-04-01", "sensitivity": "founders"},
            {"id": "m2", "title": "Investor Call", "date": "2026-04-02", "sensitivity": "ceo"},
        ]

        with patch("processors.meeting_continuity.supabase_client") as mock_sc:
            mock_sc.get_meetings_by_participant_overlap.return_value = mock_meetings
            mock_sc.list_decisions.return_value = []
            mock_sc.get_tasks.return_value = []
            mock_sc.get_open_questions.return_value = []

            result = build_meeting_continuity_context(
                participants=["Eyal"],
                max_sensitivity_level=3,  # FOUNDERS
            )

        # Only the founders-level meeting should appear
        assert result is not None
        assert "Ops Review" in result
        assert "Investor Call" not in result

    def test_build_meeting_continuity_ceo_level_shows_all(self):
        """CEO level should include everything."""
        from processors.meeting_continuity import build_meeting_continuity_context

        mock_meetings = [
            {"id": "m1", "title": "Ops Review", "date": "2026-04-01", "sensitivity": "founders"},
            {"id": "m2", "title": "Investor Call", "date": "2026-04-02", "sensitivity": "ceo"},
        ]

        with patch("processors.meeting_continuity.supabase_client") as mock_sc:
            mock_sc.get_meetings_by_participant_overlap.return_value = mock_meetings
            mock_sc.list_decisions.return_value = []
            mock_sc.get_tasks.return_value = []
            mock_sc.get_open_questions.return_value = []

            result = build_meeting_continuity_context(
                participants=["Eyal"],
                max_sensitivity_level=4,  # CEO
            )

        assert result is not None
        assert "Ops Review" in result
        assert "Investor Call" in result

    def test_sub_items_filtered_within_meeting(self):
        """CEO decisions/tasks should be filtered even if meeting is founders."""
        from processors.meeting_continuity import build_meeting_continuity_context

        mock_meetings = [
            {"id": "m1", "title": "Mixed Meeting", "date": "2026-04-01", "sensitivity": "founders"},
        ]
        mock_decisions = [
            {"id": "d1", "description": "Public decision", "sensitivity": "founders"},
            {"id": "d2", "description": "Secret investor term", "sensitivity": "ceo"},
        ]

        with patch("processors.meeting_continuity.supabase_client") as mock_sc:
            mock_sc.get_meetings_by_participant_overlap.return_value = mock_meetings
            mock_sc.list_decisions.return_value = mock_decisions
            mock_sc.get_tasks.return_value = []
            mock_sc.get_open_questions.return_value = []

            result = build_meeting_continuity_context(
                participants=["Eyal"],
                max_sensitivity_level=3,
            )

        assert result is not None
        assert "Public decision" in result
        assert "Secret investor term" not in result


class TestEmbeddingSensitivity:
    """Verify embeddings include sensitivity from source meeting."""

    @pytest.mark.asyncio
    async def test_embeddings_include_sensitivity_field(self):
        from unittest.mock import AsyncMock
        from processors.transcript_processor import generate_and_store_embeddings

        mock_chunks = [
            {
                "text": "Test chunk",
                "chunk_index": 0,
                "speaker": None,
                "timestamp_range": None,
                "embedding": [0.1] * 10,
                "metadata": {},
            }
        ]

        with patch("processors.transcript_processor.supabase_client") as mock_sc, \
             patch("processors.transcript_processor.embedding_service") as mock_embed:
            mock_sc.get_meeting.return_value = {"title": "Test", "date": "2026-04-01", "participants": []}
            mock_embed.chunk_and_embed_transcript_with_context = AsyncMock(return_value=mock_chunks)

            await generate_and_store_embeddings("m1", "transcript text", sensitivity="ceo")

            call_args = mock_sc.store_embeddings_batch.call_args
            records = call_args[0][0]
            assert records[0]["sensitivity"] == "ceo"

    @pytest.mark.asyncio
    async def test_embeddings_default_sensitivity_founders(self):
        from unittest.mock import AsyncMock
        from processors.transcript_processor import generate_and_store_embeddings

        mock_chunks = [
            {
                "text": "Test chunk",
                "chunk_index": 0,
                "speaker": None,
                "timestamp_range": None,
                "embedding": [0.1] * 10,
                "metadata": {},
            }
        ]

        with patch("processors.transcript_processor.supabase_client") as mock_sc, \
             patch("processors.transcript_processor.embedding_service") as mock_embed:
            mock_sc.get_meeting.return_value = {"title": "Test", "date": "2026-04-01", "participants": []}
            mock_embed.chunk_and_embed_transcript_with_context = AsyncMock(return_value=mock_chunks)

            await generate_and_store_embeddings("m1", "transcript text")

            call_args = mock_sc.store_embeddings_batch.call_args
            records = call_args[0][0]
            assert records[0]["sensitivity"] == "founders"
