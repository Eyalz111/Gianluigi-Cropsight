"""
Tests for entity extraction and linking (v0.3 Tier 2).

Tests cover:
- Raw entity extraction via LLM
- Entity resolution (name matching)
- Entity creation and linking
- CRUD operations
"""

import json
import pytest
from unittest.mock import patch, MagicMock, AsyncMock

from processors.entity_extraction import (
    extract_and_link_entities,
    _extract_raw_entities,
    _resolve_entity,
)


# =========================================================================
# Test _extract_raw_entities
# =========================================================================

class TestExtractRawEntities:
    """Tests for the LLM-based raw entity extraction."""

    @pytest.mark.asyncio
    async def test_empty_transcript(self):
        """Empty transcript should return empty list."""
        with patch("processors.entity_extraction.Anthropic") as mock_cls:
            mock_client = MagicMock()
            mock_cls.return_value = mock_client
            mock_response = MagicMock()
            mock_response.content = [MagicMock(text='{"entities": []}')]
            mock_client.messages.create.return_value = mock_response

            result = await _extract_raw_entities("", [])
            assert result == []

    @pytest.mark.asyncio
    async def test_extraction_returns_entities(self):
        """Should extract entities from transcript."""
        entities_json = json.dumps({
            "entities": [
                {"name": "Jason Adelman", "type": "person", "context": "advisor", "speaker": "Eyal", "sentiment": "positive", "timestamp": "5:30"},
                {"name": "Lavazza", "type": "organization", "context": "partner", "speaker": "Paolo", "sentiment": "neutral", "timestamp": "12:00"},
            ]
        })

        with patch("processors.entity_extraction.Anthropic") as mock_cls:
            mock_client = MagicMock()
            mock_cls.return_value = mock_client
            mock_response = MagicMock()
            mock_response.content = [MagicMock(text=entities_json)]
            mock_client.messages.create.return_value = mock_response

            result = await _extract_raw_entities("Some transcript", ["Someone"])
            assert len(result) == 2
            assert result[0]["name"] == "Jason Adelman"
            assert result[1]["name"] == "Lavazza"

    @pytest.mark.asyncio
    async def test_participant_exclusion(self):
        """Participants should be filtered out."""
        entities_json = json.dumps({
            "entities": [
                {"name": "Eyal", "type": "person", "context": "host", "speaker": "Paolo", "sentiment": "neutral", "timestamp": ""},
                {"name": "Jason Adelman", "type": "person", "context": "advisor", "speaker": "Eyal", "sentiment": "positive", "timestamp": ""},
            ]
        })

        with patch("processors.entity_extraction.Anthropic") as mock_cls:
            mock_client = MagicMock()
            mock_cls.return_value = mock_client
            mock_response = MagicMock()
            mock_response.content = [MagicMock(text=entities_json)]
            mock_client.messages.create.return_value = mock_response

            result = await _extract_raw_entities("transcript", ["Eyal Zror"])
            # "Eyal" should be excluded (first name of participant)
            names = [e["name"] for e in result]
            assert "Eyal" not in names
            assert "Jason Adelman" in names

    @pytest.mark.asyncio
    async def test_team_member_exclusion(self):
        """Known team members should be filtered out."""
        entities_json = json.dumps({
            "entities": [
                {"name": "Roye Tadmor", "type": "person", "context": "CTO", "speaker": "Eyal", "sentiment": "neutral", "timestamp": ""},
                {"name": "IIA", "type": "organization", "context": "grant", "speaker": "Eyal", "sentiment": "positive", "timestamp": ""},
            ]
        })

        with patch("processors.entity_extraction.Anthropic") as mock_cls:
            mock_client = MagicMock()
            mock_cls.return_value = mock_client
            mock_response = MagicMock()
            mock_response.content = [MagicMock(text=entities_json)]
            mock_client.messages.create.return_value = mock_response

            result = await _extract_raw_entities("transcript", [])
            names = [e["name"] for e in result]
            assert "Roye Tadmor" not in names
            assert "IIA" in names

    @pytest.mark.asyncio
    async def test_llm_error_returns_empty(self):
        """LLM error should return empty list, not raise."""
        with patch("processors.entity_extraction.Anthropic") as mock_cls:
            mock_client = MagicMock()
            mock_cls.return_value = mock_client
            mock_client.messages.create.side_effect = Exception("API error")

            result = await _extract_raw_entities("transcript", [])
            assert result == []


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
        entities_json = json.dumps({
            "entities": [
                {"name": "New Person", "type": "person", "context": "new contact", "speaker": "Eyal", "sentiment": "positive", "timestamp": ""},
            ]
        })

        with patch("processors.entity_extraction.Anthropic") as mock_cls, \
             patch("processors.entity_extraction.supabase_client") as mock_db:
            mock_client = MagicMock()
            mock_cls.return_value = mock_client
            mock_response = MagicMock()
            mock_response.content = [MagicMock(text=entities_json)]
            mock_client.messages.create.return_value = mock_response

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
        entities_json = json.dumps({
            "entities": [
                {"name": "Jason Adelman", "type": "person", "context": "advisor", "speaker": "Eyal", "sentiment": "positive", "timestamp": ""},
            ]
        })

        with patch("processors.entity_extraction.Anthropic") as mock_cls, \
             patch("processors.entity_extraction.supabase_client") as mock_db:
            mock_client = MagicMock()
            mock_cls.return_value = mock_client
            mock_response = MagicMock()
            mock_response.content = [MagicMock(text=entities_json)]
            mock_client.messages.create.return_value = mock_response

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
        entities_json = json.dumps({
            "entities": [
                {"name": "Ferrero", "type": "organization", "context": "partner", "speaker": "Paolo", "sentiment": "positive", "timestamp": "5:00"},
                {"name": "Lavazza", "type": "organization", "context": "partner", "speaker": "Paolo", "sentiment": "neutral", "timestamp": "10:00"},
            ]
        })

        with patch("processors.entity_extraction.Anthropic") as mock_cls, \
             patch("processors.entity_extraction.supabase_client") as mock_db:
            mock_client = MagicMock()
            mock_cls.return_value = mock_client
            mock_response = MagicMock()
            mock_response.content = [MagicMock(text=entities_json)]
            mock_client.messages.create.return_value = mock_response

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
        with patch("processors.entity_extraction.Anthropic") as mock_cls, \
             patch("processors.entity_extraction.supabase_client") as mock_db:
            mock_client = MagicMock()
            mock_cls.return_value = mock_client
            mock_response = MagicMock()
            mock_response.content = [MagicMock(text='{"entities": []}')]
            mock_client.messages.create.return_value = mock_response

            result = await extract_and_link_entities("meeting-123", "short meeting", [])
            assert result["new_entities"] == []
            assert result["existing_mentions"] == []
            assert result["total_mentions"] == 0


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
