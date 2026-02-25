"""
Tests for the v0.2 RAG Foundation Upgrade (Phase 0).

Tests:
1. Reciprocal Rank Fusion (RRF) merge logic
2. RRF deduplication
3. Contextual chunk embeddings
4. Cross-reference enrichment (enrich_chunks_with_context)
5. Full-text search method exists and is callable

All external services are mocked — no API keys or database needed.
"""

import pytest
from unittest.mock import MagicMock, AsyncMock, patch


# =============================================================================
# 1. Reciprocal Rank Fusion Tests
# =============================================================================

class TestReciprocalRankFusion:
    """Tests for the _reciprocal_rank_fusion() static method."""

    def test_rrf_merges_two_lists(self):
        """RRF should merge two ranked lists and return combined results."""
        from services.supabase_client import SupabaseClient

        list_a = [
            {"id": "aaa", "text": "first in A"},
            {"id": "bbb", "text": "second in A"},
            {"id": "ccc", "text": "third in A"},
        ]
        list_b = [
            {"id": "ddd", "text": "first in B"},
            {"id": "bbb", "text": "second in A"},  # duplicate
            {"id": "eee", "text": "third in B"},
        ]

        merged = SupabaseClient._reciprocal_rank_fusion(list_a, list_b, k=60)

        # "bbb" appears in both lists so it should rank highest
        merged_ids = [item["id"] for item in merged]
        assert "bbb" in merged_ids
        assert merged_ids[0] == "bbb", "Item in both lists should rank first"
        # All 5 unique items should be present
        assert len(merged) == 5

    def test_rrf_deduplication(self):
        """RRF should deduplicate items that appear in multiple lists."""
        from services.supabase_client import SupabaseClient

        # Same item in both lists
        list_a = [{"id": "same-id", "text": "from list A"}]
        list_b = [{"id": "same-id", "text": "from list A"}]

        merged = SupabaseClient._reciprocal_rank_fusion(list_a, list_b, k=60)

        assert len(merged) == 1
        assert merged[0]["id"] == "same-id"

    def test_rrf_preserves_order_for_single_list(self):
        """With one list, RRF should preserve the original order."""
        from services.supabase_client import SupabaseClient

        items = [
            {"id": "1", "score": 0.9},
            {"id": "2", "score": 0.8},
            {"id": "3", "score": 0.7},
        ]

        merged = SupabaseClient._reciprocal_rank_fusion(items, k=60)

        assert [m["id"] for m in merged] == ["1", "2", "3"]

    def test_rrf_handles_empty_lists(self):
        """RRF should handle empty lists gracefully."""
        from services.supabase_client import SupabaseClient

        merged = SupabaseClient._reciprocal_rank_fusion([], [], k=60)
        assert merged == []

    def test_rrf_handles_one_empty_one_full(self):
        """RRF should work when one list is empty and the other is not."""
        from services.supabase_client import SupabaseClient

        items = [{"id": "1"}, {"id": "2"}]
        merged = SupabaseClient._reciprocal_rank_fusion(items, [], k=60)

        assert len(merged) == 2
        assert merged[0]["id"] == "1"

    def test_rrf_custom_id_key(self):
        """RRF should use a custom id_key when specified."""
        from services.supabase_client import SupabaseClient

        list_a = [{"uuid": "aaa", "v": 1}]
        list_b = [{"uuid": "aaa", "v": 1}]

        merged = SupabaseClient._reciprocal_rank_fusion(
            list_a, list_b, id_key="uuid"
        )

        assert len(merged) == 1

    def test_rrf_scoring_boosts_items_in_multiple_lists(self):
        """Items appearing in more lists should get higher scores."""
        from services.supabase_client import SupabaseClient

        # "shared" is at rank 1 in both; "only_a" is at rank 0 in one list
        list_a = [
            {"id": "only_a"},
            {"id": "shared"},
        ]
        list_b = [
            {"id": "only_b"},
            {"id": "shared"},
        ]

        merged = SupabaseClient._reciprocal_rank_fusion(list_a, list_b, k=60)
        merged_ids = [m["id"] for m in merged]

        # "shared" has score from both lists; "only_a"/"only_b" from one each
        # shared: 1/(60+1+1) + 1/(60+1+1) = 2/62
        # only_a: 1/(60+0+1) = 1/61
        # only_b: 1/(60+0+1) = 1/61
        # So shared should come first
        assert merged_ids[0] == "shared"


# =============================================================================
# 2. Contextual Chunk Embeddings Tests
# =============================================================================

class TestContextualChunkEmbeddings:
    """Tests for chunk_and_embed_transcript_with_context()."""

    @pytest.mark.asyncio
    async def test_context_prefix_embedded_but_not_stored(self):
        """
        The context prefix should be used for embedding generation
        but the stored chunk text should remain raw (no prefix).
        """
        from services.embeddings import EmbeddingService

        service = EmbeddingService()

        # Mock the embed_texts method to capture what's being embedded
        embedded_texts_captured = []

        async def mock_embed_texts(texts):
            embedded_texts_captured.extend(texts)
            return [[0.1] * 1536 for _ in texts]

        service.embed_texts = mock_embed_texts

        transcript = "[00:00:15] Eyal: Welcome everyone.\n[00:01:00] Roye: Thanks."
        result = await service.chunk_and_embed_transcript_with_context(
            transcript=transcript,
            meeting_id="test-meeting-id",
            meeting_title="MVP Review",
            meeting_date="2026-02-22",
            participants=["Eyal", "Roye"],
        )

        assert len(result) > 0

        # The stored text should NOT contain the context prefix
        for chunk in result:
            assert not chunk["text"].startswith("Meeting:")

        # The text sent to embed_texts SHOULD contain the context prefix
        for embedded_text in embedded_texts_captured:
            assert embedded_text.startswith("Meeting: MVP Review")
            assert "Participants: Eyal, Roye" in embedded_text

    @pytest.mark.asyncio
    async def test_context_prefix_in_metadata(self):
        """The context prefix should be stored in the metadata field."""
        from services.embeddings import EmbeddingService

        service = EmbeddingService()

        async def mock_embed_texts(texts):
            return [[0.1] * 1536 for _ in texts]

        service.embed_texts = mock_embed_texts

        transcript = "[00:00:15] Eyal: Hello.\n[00:01:00] Roye: Hi."
        result = await service.chunk_and_embed_transcript_with_context(
            transcript=transcript,
            meeting_id="test-id",
            meeting_title="Sprint Planning",
            meeting_date="2026-02-20",
            participants=["Eyal", "Roye", "Paolo"],
        )

        assert len(result) > 0
        for chunk in result:
            metadata = chunk["metadata"]
            assert "context_prefix" in metadata
            assert "Sprint Planning" in metadata["context_prefix"]
            assert "meeting_title" in metadata
            assert metadata["meeting_title"] == "Sprint Planning"
            assert "participants" in metadata
            assert "Eyal" in metadata["participants"]

    @pytest.mark.asyncio
    async def test_contextual_empty_transcript(self):
        """Empty transcript should return empty list."""
        from services.embeddings import EmbeddingService

        service = EmbeddingService()
        result = await service.chunk_and_embed_transcript_with_context(
            transcript="",
            meeting_id="test-id",
            meeting_title="Empty Meeting",
            meeting_date="2026-02-22",
            participants=[],
        )

        assert result == []

    @pytest.mark.asyncio
    async def test_contextual_preserves_chunk_structure(self):
        """Each chunk should have all expected fields."""
        from services.embeddings import EmbeddingService

        service = EmbeddingService()

        async def mock_embed_texts(texts):
            return [[0.1] * 1536 for _ in texts]

        service.embed_texts = mock_embed_texts

        transcript = "[00:00:15] Eyal: First point.\n[00:01:00] Roye: Second point."
        result = await service.chunk_and_embed_transcript_with_context(
            transcript=transcript,
            meeting_id="test-id",
            meeting_title="Test",
            meeting_date="2026-02-22",
            participants=["Eyal"],
        )

        assert len(result) > 0
        chunk = result[0]
        # Check all expected fields are present
        assert "text" in chunk
        assert "embedding" in chunk
        assert "chunk_index" in chunk
        assert "speaker" in chunk
        assert "timestamp_range" in chunk
        assert "metadata" in chunk
        # Embedding should be the right dimension
        assert len(chunk["embedding"]) == 1536


# =============================================================================
# 3. Enrich Chunks with Context Tests
# =============================================================================

class TestEnrichChunksWithContext:
    """Tests for enrich_chunks_with_context() on SupabaseClient."""

    def test_enriches_meeting_chunks(self):
        """Should attach meeting title, date, decisions, and tasks."""
        from services.supabase_client import SupabaseClient

        client = SupabaseClient()

        # Mock the methods called by enrich_chunks_with_context
        client.get_meeting = MagicMock(return_value={
            "id": "meeting-1",
            "title": "MVP Focus",
            "date": "2026-02-22",
            "participants": ["Eyal", "Roye"],
        })
        client.list_decisions = MagicMock(return_value=[
            {"description": "Use semantic versioning", "transcript_timestamp": "03:00"},
        ])
        client.get_tasks = MagicMock(return_value=[
            {"title": "Write API docs", "assignee": "Roye", "meeting_id": "meeting-1", "status": "pending"},
            {"title": "Unrelated task", "assignee": "Paolo", "meeting_id": "other-meeting", "status": "done"},
        ])

        chunks = [
            {
                "id": "chunk-1",
                "source_type": "meeting",
                "source_id": "meeting-1",
                "chunk_text": "We discussed the MVP.",
            }
        ]

        enriched = client.enrich_chunks_with_context(chunks)

        assert len(enriched) == 1
        assert enriched[0]["meeting_title"] == "MVP Focus"
        assert enriched[0]["meeting_date"] == "2026-02-22"
        assert enriched[0]["meeting_participants"] == ["Eyal", "Roye"]
        assert len(enriched[0]["related_decisions"]) == 1
        assert enriched[0]["related_decisions"][0]["description"] == "Use semantic versioning"
        # Only the task from meeting-1 should be included
        assert len(enriched[0]["related_tasks"]) == 1
        assert enriched[0]["related_tasks"][0]["title"] == "Write API docs"

    def test_caches_meeting_lookups(self):
        """Multiple chunks from the same meeting should only query once."""
        from services.supabase_client import SupabaseClient

        client = SupabaseClient()
        client.get_meeting = MagicMock(return_value={
            "id": "meeting-1",
            "title": "Same Meeting",
            "date": "2026-02-22",
            "participants": [],
        })
        client.list_decisions = MagicMock(return_value=[])
        client.get_tasks = MagicMock(return_value=[])

        chunks = [
            {"id": "c1", "source_type": "meeting", "source_id": "meeting-1", "chunk_text": "Chunk 1"},
            {"id": "c2", "source_type": "meeting", "source_id": "meeting-1", "chunk_text": "Chunk 2"},
            {"id": "c3", "source_type": "meeting", "source_id": "meeting-1", "chunk_text": "Chunk 3"},
        ]

        enriched = client.enrich_chunks_with_context(chunks)

        # get_meeting should only be called once (cache hit for chunks 2 and 3)
        assert client.get_meeting.call_count == 1
        assert len(enriched) == 3
        for chunk in enriched:
            assert chunk["meeting_title"] == "Same Meeting"

    def test_handles_missing_meeting(self):
        """Chunks with no matching meeting should still be returned."""
        from services.supabase_client import SupabaseClient

        client = SupabaseClient()
        client.get_meeting = MagicMock(return_value=None)

        chunks = [
            {"id": "c1", "source_type": "meeting", "source_id": "missing-id", "chunk_text": "Text"},
        ]

        enriched = client.enrich_chunks_with_context(chunks)

        assert len(enriched) == 1
        assert "meeting_title" not in enriched[0]
        # Original fields preserved
        assert enriched[0]["chunk_text"] == "Text"

    def test_skips_non_meeting_source_types(self):
        """Document chunks should not get meeting enrichment."""
        from services.supabase_client import SupabaseClient

        client = SupabaseClient()
        client.get_meeting = MagicMock()

        chunks = [
            {"id": "d1", "source_type": "document", "source_id": "doc-1", "chunk_text": "Doc text"},
        ]

        enriched = client.enrich_chunks_with_context(chunks)

        # get_meeting should NOT be called for document chunks
        client.get_meeting.assert_not_called()
        assert len(enriched) == 1
        assert enriched[0]["chunk_text"] == "Doc text"

    def test_empty_chunks_list(self):
        """Empty input should return empty output."""
        from services.supabase_client import SupabaseClient

        client = SupabaseClient()
        enriched = client.enrich_chunks_with_context([])

        assert enriched == []


# =============================================================================
# 4. Full-Text Search Method Tests
# =============================================================================

class TestSearchFulltext:
    """Tests for search_fulltext() on SupabaseClient."""

    def test_search_fulltext_method_exists(self):
        """The search_fulltext method should exist on SupabaseClient."""
        from services.supabase_client import SupabaseClient

        client = SupabaseClient()
        assert hasattr(client, "search_fulltext")
        assert callable(client.search_fulltext)

    def test_search_fulltext_calls_rpc(self):
        """search_fulltext should call the RPC function with correct params."""
        from services.supabase_client import SupabaseClient

        client = SupabaseClient()

        # Mock the Supabase client and RPC chain
        mock_rpc_result = MagicMock()
        mock_rpc_result.data = [
            {"id": "chunk-1", "chunk_text": "test result", "rank": 0.5},
        ]
        mock_rpc = MagicMock()
        mock_rpc.execute.return_value = mock_rpc_result
        mock_client = MagicMock()
        mock_client.rpc.return_value = mock_rpc
        client._client = mock_client

        results = client.search_fulltext("satellite data", limit=10, source_type="meeting")

        # Verify the RPC was called correctly
        mock_client.rpc.assert_called_once_with(
            "search_embeddings_fulltext",
            {
                "search_query": "satellite data",
                "match_count": 10,
                "filter_source_type": "meeting",
            },
        )
        assert len(results) == 1
        assert results[0]["chunk_text"] == "test result"

    def test_search_fulltext_defaults(self):
        """search_fulltext should use default limit=20 and source_type=None."""
        from services.supabase_client import SupabaseClient

        client = SupabaseClient()

        mock_rpc_result = MagicMock()
        mock_rpc_result.data = []
        mock_rpc = MagicMock()
        mock_rpc.execute.return_value = mock_rpc_result
        mock_client = MagicMock()
        mock_client.rpc.return_value = mock_rpc
        client._client = mock_client

        client.search_fulltext("test query")

        mock_client.rpc.assert_called_once_with(
            "search_embeddings_fulltext",
            {
                "search_query": "test query",
                "match_count": 20,
                "filter_source_type": None,
            },
        )


# =============================================================================
# 5. Hybrid Search (search_memory) Tests
# =============================================================================

class TestHybridSearchMemory:
    """Tests for the upgraded search_memory() with RRF fusion."""

    def test_search_memory_combines_vector_and_fulltext(self):
        """search_memory should merge vector and fulltext results via RRF."""
        from services.supabase_client import SupabaseClient

        client = SupabaseClient()

        # Mock vector search
        client.search_embeddings = MagicMock(return_value=[
            {"id": "v1", "chunk_text": "vector hit 1"},
            {"id": "shared", "chunk_text": "in both"},
        ])

        # Mock fulltext search
        client.search_fulltext = MagicMock(return_value=[
            {"id": "f1", "chunk_text": "fulltext hit 1"},
            {"id": "shared", "chunk_text": "in both"},
        ])

        # Mock decision and task search
        client.list_decisions = MagicMock(return_value=[])
        mock_task_result = MagicMock()
        mock_task_result.data = []
        mock_table = MagicMock()
        mock_table.select.return_value.ilike.return_value.limit.return_value.execute.return_value = mock_task_result
        mock_client = MagicMock()
        mock_client.table.return_value = mock_table
        client._client = mock_client

        results = client.search_memory(
            query_embedding=[0.1] * 1536,
            query_text="satellite data",
            limit=10,
        )

        # Should have merged embeddings
        assert len(results["embeddings"]) == 3  # v1, shared, f1 (deduplicated)
        # "shared" should be first (highest RRF score)
        assert results["embeddings"][0]["id"] == "shared"

    def test_search_memory_handles_vector_failure(self):
        """If vector search fails, fulltext results should still work."""
        from services.supabase_client import SupabaseClient

        client = SupabaseClient()

        client.search_embeddings = MagicMock(side_effect=Exception("Vector DB down"))
        client.search_fulltext = MagicMock(return_value=[
            {"id": "f1", "chunk_text": "fulltext hit"},
        ])
        client.list_decisions = MagicMock(return_value=[])
        mock_task_result = MagicMock()
        mock_task_result.data = []
        mock_table = MagicMock()
        mock_table.select.return_value.ilike.return_value.limit.return_value.execute.return_value = mock_task_result
        mock_client = MagicMock()
        mock_client.table.return_value = mock_table
        client._client = mock_client

        results = client.search_memory(
            query_embedding=[0.1] * 1536,
            query_text="test",
            limit=10,
        )

        # Should still have fulltext results
        assert len(results["embeddings"]) == 1
        assert results["embeddings"][0]["id"] == "f1"

    def test_search_memory_handles_fulltext_failure(self):
        """If fulltext search fails, vector results should still work."""
        from services.supabase_client import SupabaseClient

        client = SupabaseClient()

        client.search_embeddings = MagicMock(return_value=[
            {"id": "v1", "chunk_text": "vector hit"},
        ])
        client.search_fulltext = MagicMock(side_effect=Exception("FTS down"))
        client.list_decisions = MagicMock(return_value=[])
        mock_task_result = MagicMock()
        mock_task_result.data = []
        mock_table = MagicMock()
        mock_table.select.return_value.ilike.return_value.limit.return_value.execute.return_value = mock_task_result
        mock_client = MagicMock()
        mock_client.table.return_value = mock_table
        client._client = mock_client

        results = client.search_memory(
            query_embedding=[0.1] * 1536,
            query_text="test",
            limit=10,
        )

        assert len(results["embeddings"]) == 1
        assert results["embeddings"][0]["id"] == "v1"

    def test_search_memory_respects_limit(self):
        """search_memory should respect the limit parameter."""
        from services.supabase_client import SupabaseClient

        client = SupabaseClient()

        # Return many results
        client.search_embeddings = MagicMock(return_value=[
            {"id": f"v{i}", "chunk_text": f"hit {i}"} for i in range(20)
        ])
        client.search_fulltext = MagicMock(return_value=[])
        client.list_decisions = MagicMock(return_value=[])
        mock_task_result = MagicMock()
        mock_task_result.data = []
        mock_table = MagicMock()
        mock_table.select.return_value.ilike.return_value.limit.return_value.execute.return_value = mock_task_result
        mock_client = MagicMock()
        mock_client.table.return_value = mock_table
        client._client = mock_client

        results = client.search_memory(
            query_embedding=[0.1] * 1536,
            query_text="test",
            limit=5,
        )

        assert len(results["embeddings"]) == 5


# =============================================================================
# 6. Query Response Prompt Tests
# =============================================================================

class TestQueryResponsePrompt:
    """Tests for the updated get_query_response_prompt()."""

    def test_includes_meeting_title_in_prompt(self):
        """Prompt should include meeting title when available in enriched results."""
        from core.system_prompt import get_query_response_prompt

        search_results = {
            "embeddings": [
                {
                    "source_type": "meeting",
                    "chunk_text": "We discussed satellite imagery.",
                    "timestamp_range": "05:00-06:00",
                    "speaker": "Eyal",
                    "meeting_title": "MVP Focus",
                    "meeting_date": "2026-02-22",
                    "related_decisions": [
                        {"description": "Use Sentinel-2 imagery"},
                    ],
                    "related_tasks": [
                        {"title": "Download sample data", "assignee": "Roye", "status": "pending"},
                    ],
                }
            ],
            "decisions": [],
            "tasks": [],
        }

        prompt = get_query_response_prompt("What satellite data?", search_results)

        assert "MVP Focus" in prompt
        assert "2026-02-22" in prompt
        assert "Speaker: Eyal" in prompt
        assert "We discussed satellite imagery." in prompt
        assert "Use Sentinel-2 imagery" in prompt
        assert "Download sample data" in prompt

    def test_handles_results_without_enrichment(self):
        """Prompt should work with non-enriched results (backward compatible)."""
        from core.system_prompt import get_query_response_prompt

        search_results = {
            "embeddings": [
                {
                    "source_type": "meeting",
                    "chunk_text": "Just plain text.",
                    "timestamp_range": "01:00-02:00",
                    "speaker": "Roye",
                }
            ],
            "decisions": [],
            "tasks": [],
        }

        prompt = get_query_response_prompt("What happened?", search_results)

        assert "Just plain text." in prompt
        assert "Speaker: Roye" in prompt
        assert "[meeting]" in prompt

    def test_handles_empty_results(self):
        """Prompt should show 'no results' messages for empty search."""
        from core.system_prompt import get_query_response_prompt

        search_results = {
            "embeddings": [],
            "decisions": [],
            "tasks": [],
        }

        prompt = get_query_response_prompt("anything?", search_results)

        assert "No relevant transcript excerpts found." in prompt
        assert "No relevant decisions found." in prompt
        assert "No relevant tasks found." in prompt

    def test_decision_handles_non_dict_meetings(self):
        """Decision formatting should handle missing/non-dict meetings field."""
        from core.system_prompt import get_query_response_prompt

        search_results = {
            "embeddings": [],
            "decisions": [
                {
                    "description": "Use v2 API",
                    "transcript_timestamp": "12:00",
                    "meetings": None,  # non-dict
                },
            ],
            "tasks": [],
        }

        prompt = get_query_response_prompt("API version?", search_results)

        assert "Use v2 API" in prompt
        assert "Unknown meeting" in prompt
