"""
Tests for entity extraction and linking (v0.3 Tier 2).

Tests cover:
- Raw entity extraction via LLM
- LLM-based validation (two-pass filtering)
- Entity resolution (name matching)
- Entity creation and linking
- CRUD operations
- Weekly entity health check
"""

import json
import pytest
from unittest.mock import patch, MagicMock, AsyncMock

from processors.entity_extraction import (
    extract_and_link_entities,
    _extract_raw_entities,
    _validate_entities,
    _filter_known_names,
    _resolve_entity,
    review_entity_health,
)


# =========================================================================
# Test _extract_raw_entities
# =========================================================================

class TestExtractRawEntities:
    """Tests for the LLM-based raw entity extraction."""

    @pytest.mark.asyncio
    async def test_empty_transcript(self):
        """Empty transcript should return empty list."""
        _usage = {"input_tokens": 50, "output_tokens": 10, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}
        with patch("processors.entity_extraction.call_llm") as mock_llm:
            mock_llm.return_value = ('{"entities": []}', _usage)
            result = await _extract_raw_entities("", [])
            assert result == []

    @pytest.mark.asyncio
    async def test_extraction_returns_entities(self):
        """Should extract entities from transcript."""
        _usage = {"input_tokens": 50, "output_tokens": 10, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}
        entities_json = json.dumps({
            "entities": [
                {"name": "Jason Adelman", "type": "person", "context": "advisor", "speaker": "Eyal", "sentiment": "positive", "relationship": "advisor"},
                {"name": "Lavazza", "type": "organization", "context": "partner", "speaker": "Paolo", "sentiment": "neutral", "relationship": "partner"},
            ]
        })

        with patch("processors.entity_extraction.call_llm") as mock_llm:
            mock_llm.return_value = (entities_json, _usage)
            result = await _extract_raw_entities("Some transcript", ["Someone"])
            assert len(result) == 2
            assert result[0]["name"] == "Jason Adelman"
            assert result[1]["name"] == "Lavazza"

    @pytest.mark.asyncio
    async def test_participant_exclusion(self):
        """Participants should be filtered out."""
        _usage = {"input_tokens": 50, "output_tokens": 10, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}
        entities_json = json.dumps({
            "entities": [
                {"name": "Eyal", "type": "person", "context": "host", "speaker": "Paolo", "sentiment": "neutral", "relationship": "other"},
                {"name": "Jason Adelman", "type": "person", "context": "advisor", "speaker": "Eyal", "sentiment": "positive", "relationship": "advisor"},
            ]
        })

        with patch("processors.entity_extraction.call_llm") as mock_llm:
            mock_llm.return_value = (entities_json, _usage)
            result = await _extract_raw_entities("transcript", ["Eyal Zror"])
            names = [e["name"] for e in result]
            assert "Eyal" not in names
            assert "Jason Adelman" in names

    @pytest.mark.asyncio
    async def test_team_member_exclusion(self):
        """Known team members should be filtered out."""
        _usage = {"input_tokens": 50, "output_tokens": 10, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}
        entities_json = json.dumps({
            "entities": [
                {"name": "Roye Tadmor", "type": "person", "context": "CTO", "speaker": "Eyal", "sentiment": "neutral", "relationship": "other"},
                {"name": "IIA", "type": "organization", "context": "grant", "speaker": "Eyal", "sentiment": "positive", "relationship": "grant_body"},
            ]
        })

        with patch("processors.entity_extraction.call_llm") as mock_llm:
            mock_llm.return_value = (entities_json, _usage)
            result = await _extract_raw_entities("transcript", [])
            names = [e["name"] for e in result]
            assert "Roye Tadmor" not in names
            assert "IIA" in names

    @pytest.mark.asyncio
    async def test_llm_error_returns_empty(self):
        """LLM error should return empty list, not raise."""
        with patch("processors.entity_extraction.call_llm") as mock_llm:
            mock_llm.side_effect = Exception("API error")
            result = await _extract_raw_entities("transcript", [])
            assert result == []


# =========================================================================
# Test _validate_entities (two-pass LLM filter)
# =========================================================================

class TestValidateEntities:
    """Tests for the LLM-based validation pass."""

    @pytest.mark.asyncio
    async def test_keeps_valid_entities(self):
        """Should keep entities the LLM marks as valid."""
        _usage = {"input_tokens": 50, "output_tokens": 10, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}
        candidates = [
            {"name": "Jason Adelman", "type": "person", "context": "advisor", "relationship": "advisor"},
            {"name": "AWS", "type": "organization", "context": "cloud provider", "relationship": "vendor"},
            {"name": "Lavazza", "type": "organization", "context": "partner", "relationship": "partner"},
        ]

        with patch("processors.entity_extraction.call_llm") as mock_llm:
            mock_llm.return_value = ('{"keep": [1, 3]}', _usage)
            result = await _validate_entities(candidates)
            assert len(result) == 2
            names = [e["name"] for e in result]
            assert "Jason Adelman" in names
            assert "Lavazza" in names
            assert "AWS" not in names

    @pytest.mark.asyncio
    async def test_rejects_all(self):
        """Should return empty list when LLM rejects everything."""
        _usage = {"input_tokens": 50, "output_tokens": 10, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}
        candidates = [
            {"name": "Internet", "type": "technology", "context": "mentioned", "relationship": "other"},
        ]

        with patch("processors.entity_extraction.call_llm") as mock_llm:
            mock_llm.return_value = ('{"keep": []}', _usage)
            result = await _validate_entities(candidates)
            assert result == []

    @pytest.mark.asyncio
    async def test_empty_candidates(self):
        """Empty candidate list should return empty without calling LLM."""
        result = await _validate_entities([])
        assert result == []

    @pytest.mark.asyncio
    async def test_llm_error_passes_through(self):
        """On LLM error, should pass all candidates through (fail-open)."""
        candidates = [
            {"name": "Someone", "type": "person", "context": "contact", "relationship": "other"},
        ]

        with patch("processors.entity_extraction.call_llm") as mock_llm:
            mock_llm.side_effect = Exception("API error")
            result = await _validate_entities(candidates)
            assert len(result) == 1
            assert result[0]["name"] == "Someone"

    @pytest.mark.asyncio
    async def test_handles_out_of_range_indices(self):
        """Should ignore indices that are out of range."""
        _usage = {"input_tokens": 50, "output_tokens": 10, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}
        candidates = [
            {"name": "Valid Person", "type": "person", "context": "test", "relationship": "advisor"},
        ]

        with patch("processors.entity_extraction.call_llm") as mock_llm:
            mock_llm.return_value = ('{"keep": [1, 99]}', _usage)
            result = await _validate_entities(candidates)
            assert len(result) == 1
            assert result[0]["name"] == "Valid Person"


# =========================================================================
# Test _resolve_entity
# =========================================================================

# =========================================================================
# Test _filter_known_names
# =========================================================================

class TestFilterKnownNames:
    """Tests for filtering participants/team from pre-extracted stakeholders."""

    def test_filters_participants(self):
        """Should remove meeting participants."""
        candidates = [
            {"name": "Eyal Zror", "type": "person", "context": "host"},
            {"name": "Jason Adelman", "type": "person", "context": "advisor"},
        ]
        result = _filter_known_names(candidates, ["Eyal Zror"])
        assert len(result) == 1
        assert result[0]["name"] == "Jason Adelman"

    def test_filters_first_names(self):
        """Should remove first-name-only matches of participants."""
        candidates = [
            {"name": "Eyal", "type": "person", "context": "host"},
            {"name": "Lavazza", "type": "organization", "context": "partner"},
        ]
        result = _filter_known_names(candidates, ["Eyal Zror"])
        assert len(result) == 1
        assert result[0]["name"] == "Lavazza"

    def test_filters_team_members(self):
        """Should remove known CropSight team members."""
        candidates = [
            {"name": "Roye Tadmor", "type": "person", "context": "CTO"},
            {"name": "IIA", "type": "organization", "context": "grant"},
        ]
        result = _filter_known_names(candidates, [])
        names = [c["name"] for c in result]
        assert "Roye Tadmor" not in names
        assert "IIA" in names

    def test_filters_short_names(self):
        """Should remove names shorter than 2 chars."""
        candidates = [
            {"name": "X", "type": "person", "context": "unknown"},
            {"name": "Lavazza", "type": "organization", "context": "partner"},
        ]
        result = _filter_known_names(candidates, [])
        assert len(result) == 1

    def test_empty_candidates(self):
        """Empty list should return empty."""
        assert _filter_known_names([], ["Eyal"]) == []


# =========================================================================
# Test pre-extracted flow (Opus piggyback)
# =========================================================================

class TestPreExtractedFlow:
    """Tests for using pre-extracted stakeholders from Opus."""

    @pytest.mark.asyncio
    async def test_uses_pre_extracted_skips_haiku_extraction(self):
        """Should use pre-extracted data and skip the Haiku extraction call."""
        _usage = {"input_tokens": 50, "output_tokens": 10, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}
        pre_extracted = [
            {"name": "Jason Adelman", "type": "person", "context": "advisor", "speaker": "Eyal", "relationship": "advisor"},
        ]

        with patch("processors.entity_extraction.call_llm") as mock_llm, \
             patch("processors.entity_extraction.supabase_client") as mock_db:
            # Only ONE LLM call should happen (validation), not two
            mock_llm.return_value = ('{"keep": [1]}', _usage)

            mock_db.list_entities.return_value = [
                {"id": "e1", "canonical_name": "Jason Adelman", "entity_type": "person", "aliases": ["Jason"]},
            ]
            mock_db.create_entity_mentions_batch.return_value = [{"id": "m1"}]

            result = await extract_and_link_entities(
                "meeting-123", "transcript", [], pre_extracted=pre_extracted
            )

            # Should have called LLM exactly once (validation only)
            assert mock_llm.call_count == 1
            assert len(result["existing_mentions"]) == 1

    @pytest.mark.asyncio
    async def test_falls_back_to_haiku_when_no_pre_extracted(self):
        """Without pre_extracted, should use standalone Haiku extraction."""
        _usage = {"input_tokens": 50, "output_tokens": 10, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}
        with patch("processors.entity_extraction.call_llm") as mock_llm, \
             patch("processors.entity_extraction.supabase_client") as mock_db:
            # Two LLM calls: extraction + validation
            mock_llm.side_effect = [
                ('{"entities": [{"name": "Someone", "type": "person", "context": "test", "relationship": "other"}]}', _usage),
                ('{"keep": [1]}', _usage),
            ]

            mock_db.list_entities.return_value = []
            mock_db.create_entity.return_value = {
                "id": "new-uuid", "canonical_name": "Someone", "entity_type": "person", "aliases": ["Someone"],
            }
            mock_db.create_entity_mentions_batch.return_value = [{"id": "m1"}]

            result = await extract_and_link_entities(
                "meeting-123", "transcript", []
            )

            # Should have called LLM twice (extraction + validation)
            assert mock_llm.call_count == 2
            assert len(result["new_entities"]) == 1

    @pytest.mark.asyncio
    async def test_pre_extracted_filters_team_members(self):
        """Pre-extracted data should still filter out team members."""
        _usage = {"input_tokens": 50, "output_tokens": 10, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}
        pre_extracted = [
            {"name": "Roye Tadmor", "type": "person", "context": "CTO", "relationship": "other"},
            {"name": "Lavazza", "type": "organization", "context": "partner", "relationship": "partner"},
        ]

        with patch("processors.entity_extraction.call_llm") as mock_llm, \
             patch("processors.entity_extraction.supabase_client") as mock_db:
            mock_llm.return_value = ('{"keep": [1]}', _usage)

            mock_db.list_entities.return_value = []
            mock_db.create_entity.return_value = {
                "id": "e1", "canonical_name": "Lavazza", "entity_type": "organization", "aliases": ["Lavazza"],
            }
            mock_db.create_entity_mentions_batch.return_value = [{"id": "m1"}]

            result = await extract_and_link_entities(
                "meeting-123", "transcript", [], pre_extracted=pre_extracted
            )

            # Only Lavazza should come through — Roye filtered by _filter_known_names
            assert len(result["new_entities"]) == 1
            assert result["new_entities"][0]["canonical_name"] == "Lavazza"


# =========================================================================
# Test _resolve_entity
# =========================================================================

class TestResolveEntity:
    """Tests for local entity name resolution."""

    def _make_entities(self):
        return [
            {"id": "e1", "canonical_name": "Jason Adelman", "entity_type": "person", "aliases": ["Jason", "J. Adelman"]},
            {"id": "e2", "canonical_name": "Lavazza", "entity_type": "organization", "aliases": ["Lavazza Group"]},
            {"id": "e3", "canonical_name": "Moldova Pilot", "entity_type": "project", "aliases": ["Moldova PoC", "Moldova deployment"]},
        ]

    def test_exact_match(self):
        """Exact canonical name match (case-insensitive)."""
        entities = self._make_entities()
        result = _resolve_entity("jason adelman", "person", entities)
        assert result is not None
        assert result["id"] == "e1"

    def test_alias_match(self):
        """Should match on alias."""
        entities = self._make_entities()
        result = _resolve_entity("Lavazza Group", "organization", entities)
        assert result is not None
        assert result["id"] == "e2"

    def test_partial_name_match_person(self):
        """Partial match should work for persons."""
        entities = self._make_entities()
        result = _resolve_entity("Jason", "person", entities)
        assert result is not None
        assert result["id"] == "e1"

    def test_no_match(self):
        """Unrecognized name should return None."""
        entities = self._make_entities()
        result = _resolve_entity("Unknown Person", "person", entities)
        assert result is None

    def test_partial_not_for_organizations(self):
        """Partial match should not cross entity types (not person)."""
        entities = self._make_entities()
        # "Lav" should not match "Lavazza" via partial (not a person type)
        result = _resolve_entity("Lav", "organization", entities)
        assert result is None

    def test_empty_existing(self):
        """Empty entity list should return None."""
        result = _resolve_entity("Jason", "person", [])
        assert result is None


# =========================================================================
# Test extract_and_link_entities (integration)
# =========================================================================

class TestExtractAndLinkEntities:
    """Tests for the full extraction + linking pipeline."""

    @pytest.mark.asyncio
    async def test_new_entity_creation(self):
        """Should create new entity when no match exists."""
        _usage = {"input_tokens": 50, "output_tokens": 10, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}
        entities_json = json.dumps({
            "entities": [
                {"name": "New Person", "type": "person", "context": "new contact", "speaker": "Eyal", "sentiment": "positive", "relationship": "advisor"},
            ]
        })

        with patch("processors.entity_extraction.call_llm") as mock_llm, \
             patch("processors.entity_extraction.supabase_client") as mock_db:
            mock_llm.side_effect = [
                (entities_json, _usage),
                ('{"keep": [1]}', _usage),
            ]

            mock_db.list_entities.return_value = []
            mock_db.create_entity.return_value = {
                "id": "new-uuid", "canonical_name": "New Person", "entity_type": "person", "aliases": ["New Person"],
            }
            mock_db.create_entity_mentions_batch.return_value = [{"id": "m1"}]

            result = await extract_and_link_entities("meeting-123", "transcript", [])
            assert len(result["new_entities"]) == 1
            assert result["new_entities"][0]["canonical_name"] == "New Person"
            assert result["total_mentions"] == 1
            mock_db.create_entity.assert_called_once()

    @pytest.mark.asyncio
    async def test_existing_entity_linking(self):
        """Should link to existing entity when match found."""
        _usage = {"input_tokens": 50, "output_tokens": 10, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}
        entities_json = json.dumps({
            "entities": [
                {"name": "Jason Adelman", "type": "person", "context": "advisor", "speaker": "Eyal", "sentiment": "positive", "relationship": "advisor"},
            ]
        })

        with patch("processors.entity_extraction.call_llm") as mock_llm, \
             patch("processors.entity_extraction.supabase_client") as mock_db:
            mock_llm.side_effect = [
                (entities_json, _usage),
                ('{"keep": [1]}', _usage),
            ]

            mock_db.list_entities.return_value = [
                {"id": "existing-uuid", "canonical_name": "Jason Adelman", "entity_type": "person", "aliases": ["Jason"]},
            ]
            mock_db.create_entity_mentions_batch.return_value = [{"id": "m1"}]

            result = await extract_and_link_entities("meeting-123", "transcript", [])
            assert len(result["new_entities"]) == 0
            assert len(result["existing_mentions"]) == 1
            assert result["existing_mentions"][0]["entity_id"] == "existing-uuid"
            mock_db.create_entity.assert_not_called()

    @pytest.mark.asyncio
    async def test_batch_mentions_created(self):
        """Should batch-create mentions for all entities."""
        _usage = {"input_tokens": 50, "output_tokens": 10, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}
        entities_json = json.dumps({
            "entities": [
                {"name": "Ferrero", "type": "organization", "context": "partner", "speaker": "Paolo", "sentiment": "positive", "relationship": "partner"},
                {"name": "Lavazza", "type": "organization", "context": "partner", "speaker": "Paolo", "sentiment": "neutral", "relationship": "partner"},
            ]
        })

        with patch("processors.entity_extraction.call_llm") as mock_llm, \
             patch("processors.entity_extraction.supabase_client") as mock_db:
            mock_llm.side_effect = [
                (entities_json, _usage),
                ('{"keep": [1, 2]}', _usage),
            ]

            mock_db.list_entities.return_value = []
            mock_db.create_entity.side_effect = [
                {"id": "e1", "canonical_name": "Ferrero", "entity_type": "organization", "aliases": ["Ferrero"]},
                {"id": "e2", "canonical_name": "Lavazza", "entity_type": "organization", "aliases": ["Lavazza"]},
            ]
            mock_db.create_entity_mentions_batch.return_value = [{"id": "m1"}, {"id": "m2"}]

            result = await extract_and_link_entities("meeting-123", "transcript", [])
            assert len(result["new_entities"]) == 2
            assert result["total_mentions"] == 2

            # Verify batch call received 2 mentions
            call_args = mock_db.create_entity_mentions_batch.call_args[0][0]
            assert len(call_args) == 2

    @pytest.mark.asyncio
    async def test_empty_extraction(self):
        """No entities found should return empty result."""
        _usage = {"input_tokens": 50, "output_tokens": 10, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}
        with patch("processors.entity_extraction.call_llm") as mock_llm, \
             patch("processors.entity_extraction.supabase_client") as mock_db:
            mock_llm.return_value = ('{"entities": []}', _usage)

            result = await extract_and_link_entities("meeting-123", "short meeting", [])
            assert result["new_entities"] == []
            assert result["existing_mentions"] == []
            assert result["total_mentions"] == 0

    @pytest.mark.asyncio
    async def test_validation_filters_junk(self):
        """Validation pass should remove junk that extraction let through."""
        _usage = {"input_tokens": 50, "output_tokens": 10, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}
        entities_json = json.dumps({
            "entities": [
                {"name": "Jason Adelman", "type": "person", "context": "advisor", "speaker": "Eyal", "sentiment": "positive", "relationship": "advisor"},
                {"name": "AWS", "type": "organization", "context": "cloud infra", "speaker": "Roye", "sentiment": "neutral", "relationship": "vendor"},
            ]
        })

        with patch("processors.entity_extraction.call_llm") as mock_llm, \
             patch("processors.entity_extraction.supabase_client") as mock_db:
            mock_llm.side_effect = [
                (entities_json, _usage),
                ('{"keep": [1]}', _usage),
            ]

            mock_db.list_entities.return_value = [
                {"id": "e1", "canonical_name": "Jason Adelman", "entity_type": "person", "aliases": ["Jason"]},
            ]
            mock_db.create_entity_mentions_batch.return_value = [{"id": "m1"}]

            result = await extract_and_link_entities("meeting-123", "transcript", [])
            # Only Jason should come through — AWS filtered by validation
            assert len(result["existing_mentions"]) == 1
            assert result["existing_mentions"][0]["entity_name"] == "Jason Adelman"
            assert result["total_mentions"] == 1


# =========================================================================
# Test Entity CRUD (Supabase client methods)
# =========================================================================

class TestEntityCRUD:
    """Test supabase_client entity methods with mocked client."""

    def _make_client(self):
        from services.supabase_client import SupabaseClient
        client = SupabaseClient()
        mock = MagicMock()
        object.__setattr__(client, "_client", mock)
        return client, mock

    def test_create_entity(self):
        """Should insert entity record."""
        db, mock = self._make_client()
        mock.table.return_value.insert.return_value.execute.return_value = MagicMock(
            data=[{"id": "e1", "canonical_name": "Test", "entity_type": "person"}]
        )
        result = db.create_entity("Test", "person")
        assert result["canonical_name"] == "Test"
        mock.table.assert_called_with("entities")

    def test_find_entity_by_name_canonical(self):
        """Should find by canonical name (case-insensitive)."""
        db, mock = self._make_client()
        mock.table.return_value.select.return_value.ilike.return_value.execute.return_value = MagicMock(
            data=[{"id": "e1", "canonical_name": "Jason Adelman"}]
        )
        result = db.find_entity_by_name("jason adelman")
        assert result is not None
        assert result["canonical_name"] == "Jason Adelman"

    def test_find_entity_by_alias(self):
        """Should find by alias when canonical doesn't match."""
        db, mock = self._make_client()
        # First query (ilike) returns nothing
        mock.table.return_value.select.return_value.ilike.return_value.execute.return_value = MagicMock(data=[])
        # Second query (contains) finds it
        mock.table.return_value.select.return_value.contains.return_value.execute.return_value = MagicMock(
            data=[{"id": "e1", "canonical_name": "Jason Adelman", "aliases": ["Jason"]}]
        )
        result = db.find_entity_by_name("Jason")
        assert result is not None
        assert result["id"] == "e1"

    def test_get_entity_timeline(self):
        """Should return chronological mentions."""
        db, mock = self._make_client()
        mock.table.return_value.select.return_value.eq.return_value.order.return_value.limit.return_value.execute.return_value = MagicMock(
            data=[
                {"id": "m1", "mention_text": "Jason in Feb meeting"},
                {"id": "m2", "mention_text": "Jason in Mar meeting"},
            ]
        )
        result = db.get_entity_timeline("e1")
        assert len(result) == 2


# =========================================================================
# Test review_entity_health
# =========================================================================

class TestEntityHealthCheck:
    """Tests for the weekly entity registry health check."""

    def test_auto_cleans_team_members(self):
        """Should auto-delete entities that are team members."""
        with patch("processors.entity_extraction.supabase_client") as mock_db:
            mock_db.list_entities.side_effect = [
                [
                    {"id": "e1", "canonical_name": "Eyal Zror", "entity_type": "person", "created_at": "2026-02-25T10:00:00"},
                    {"id": "e2", "canonical_name": "Lavazza", "entity_type": "organization", "created_at": "2026-02-25T10:00:00"},
                ],
                # After cleanup, only Lavazza remains
                [{"id": "e2", "canonical_name": "Lavazza", "entity_type": "organization"}],
            ]
            mock_db.get_entity_mentions.return_value = [{"id": "m1"}]

            mock_table = MagicMock()
            mock_db.client.table.return_value = mock_table
            mock_table.delete.return_value.eq.return_value.execute.return_value = MagicMock()

            result = review_entity_health()
            assert "Eyal Zror" in result["auto_cleaned"]
            assert len(result["auto_cleaned"]) == 1

    def test_detects_orphans(self):
        """Should flag entities with no mentions."""
        with patch("processors.entity_extraction.supabase_client") as mock_db:
            mock_db.list_entities.side_effect = [
                [
                    {"id": "e1", "canonical_name": "Lonely Corp", "entity_type": "organization", "created_at": "2026-02-20T10:00:00"},
                ],
                # Second call after cleanup
                [{"id": "e1", "canonical_name": "Lonely Corp", "entity_type": "organization"}],
            ]
            mock_db.get_entity_mentions.return_value = []  # No mentions

            result = review_entity_health()
            assert len(result["orphans"]) == 1
            assert result["orphans"][0]["name"] == "Lonely Corp"

    def test_flags_new_this_week(self):
        """Should list entities created in the last 7 days."""
        from datetime import datetime
        recent = datetime.now().isoformat()

        with patch("processors.entity_extraction.supabase_client") as mock_db:
            mock_db.list_entities.side_effect = [
                [
                    {"id": "e1", "canonical_name": "Fresh Corp", "entity_type": "organization", "created_at": recent},
                ],
                [{"id": "e1", "canonical_name": "Fresh Corp", "entity_type": "organization"}],
            ]
            mock_db.get_entity_mentions.return_value = [{"id": "m1"}]

            result = review_entity_health()
            assert len(result["new_this_week"]) == 1
            assert result["new_this_week"][0]["name"] == "Fresh Corp"

    def test_old_entity_not_flagged_as_new(self):
        """Entity from 2 weeks ago should not be in new_this_week."""
        from datetime import datetime, timedelta
        old = (datetime.now() - timedelta(days=14)).isoformat()

        with patch("processors.entity_extraction.supabase_client") as mock_db:
            mock_db.list_entities.side_effect = [
                [
                    {"id": "e1", "canonical_name": "Old Corp", "entity_type": "organization", "created_at": old},
                ],
                [{"id": "e1", "canonical_name": "Old Corp", "entity_type": "organization"}],
            ]
            mock_db.get_entity_mentions.return_value = [{"id": "m1"}]

            result = review_entity_health()
            assert result["new_this_week"] == []

    def test_empty_registry(self):
        """Empty entity registry should return clean result."""
        with patch("processors.entity_extraction.supabase_client") as mock_db:
            mock_db.list_entities.return_value = []

            result = review_entity_health()
            assert result["auto_cleaned"] == []
            assert result["orphans"] == []
            assert result["new_this_week"] == []
            assert result["total_entities"] == 0
