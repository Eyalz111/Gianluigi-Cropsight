"""
Tests for the residual I3 (tier-leak) fixes:
  P2-09 — knowledge synthesis RAG chunks are filtered by source-meeting tier so a
          CEO-tier chunk can't bleed into a lower-tier topic brief.
  P1-09 — ingested documents carry a sensitivity tier (flag-gated): the column is
          only written when provided, and the tier is stamped on chunk metadata.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# =============================================================================
# P2-09 — RAG chunks filtered by source-meeting tier
# =============================================================================

class TestRagChunkTierFilter:
    def _mock_sc(self, hits, meeting_rows):
        mock_sc = MagicMock()
        mock_sc.search_embeddings.return_value = hits
        (mock_sc.client.table.return_value
            .select.return_value
            .in_.return_value
            .execute.return_value) = MagicMock(data=meeting_rows)
        return mock_sc

    @pytest.mark.asyncio
    async def test_higher_tier_chunk_is_dropped(self):
        from processors import knowledge_synthesis as ks
        hits = [
            {"source_id": "m-team", "chunk_text": "team chunk"},
            {"source_id": "m-ceo", "chunk_text": "ceo secret"},
        ]
        rows = [
            {"id": "m-team", "sensitivity": "team"},
            {"id": "m-ceo", "sensitivity": "ceo"},
        ]
        with patch.object(ks, "supabase_client", self._mock_sc(hits, rows)), \
             patch("services.embeddings.embedding_service.embed_text",
                   new=AsyncMock(return_value=[0.1, 0.2, 0.3, 0.4])):
            # Topic is team-tier (level 2): the ceo chunk (level 4) must be dropped.
            out = await ks._rag_chunks("Pilot topic", max_level=2)
        assert out == ["team chunk"]

    @pytest.mark.asyncio
    async def test_unknown_tier_treated_as_founders(self):
        from processors import knowledge_synthesis as ks
        hits = [{"source_id": "m-x", "chunk_text": "mystery chunk"}]
        rows = []  # no tier row for m-x → default founders (level 3)
        with patch.object(ks, "supabase_client", self._mock_sc(hits, rows)), \
             patch("services.embeddings.embedding_service.embed_text",
                   new=AsyncMock(return_value=[0.1, 0.2, 0.3, 0.4])):
            # team-tier topic (level 2) → a founders chunk (level 3) is dropped.
            out = await ks._rag_chunks("Topic", max_level=2)
        assert out == []

    @pytest.mark.asyncio
    async def test_ceo_topic_keeps_all(self):
        from processors import knowledge_synthesis as ks
        hits = [
            {"source_id": "m-team", "chunk_text": "team chunk"},
            {"source_id": "m-ceo", "chunk_text": "ceo chunk"},
        ]
        rows = [
            {"id": "m-team", "sensitivity": "team"},
            {"id": "m-ceo", "sensitivity": "ceo"},
        ]
        with patch.object(ks, "supabase_client", self._mock_sc(hits, rows)), \
             patch("services.embeddings.embedding_service.embed_text",
                   new=AsyncMock(return_value=[0.1, 0.2, 0.3, 0.4])):
            out = await ks._rag_chunks("Topic", max_level=4)  # ceo
        assert out == ["team chunk", "ceo chunk"]


# =============================================================================
# P1-09 — documents carry a sensitivity tier (flag-gated)
# =============================================================================

class TestDocumentSensitivity:
    def _insert_chain(self):
        chain = MagicMock()
        chain.table.return_value = chain
        chain.insert.return_value = chain
        chain.execute.return_value = MagicMock(data=[{"id": "doc-1"}])
        return chain

    def test_create_document_writes_sensitivity_when_provided(self):
        from services.supabase_client import supabase_client
        chain = self._insert_chain()
        with patch.object(supabase_client, "_client", chain), \
             patch.object(supabase_client, "log_action"):
            supabase_client.create_document(title="T", source="drive", sensitivity="ceo")
        insert_arg = chain.insert.call_args.args[0]
        assert insert_arg.get("sensitivity") == "ceo"

    def test_create_document_omits_sensitivity_when_none(self):
        from services.supabase_client import supabase_client
        chain = self._insert_chain()
        with patch.object(supabase_client, "_client", chain), \
             patch.object(supabase_client, "log_action"):
            supabase_client.create_document(title="T", source="drive")
        insert_arg = chain.insert.call_args.args[0]
        # No column write before the migration — feature stays dark.
        assert "sensitivity" not in insert_arg

    @pytest.mark.asyncio
    async def test_store_document_embeddings_stamps_metadata(self):
        from processors import document_processor as dp
        chunks = [
            {"text": "a", "chunk_index": 0, "embedding": [0.1], "metadata": {"document_id": "doc-1"}},
            {"text": "b", "chunk_index": 1, "embedding": [0.2], "metadata": {}},
        ]
        with patch.object(dp.embedding_service, "chunk_and_embed_document",
                          new=AsyncMock(return_value=chunks)), \
             patch.object(dp.supabase_client, "store_embeddings_batch") as mock_store:
            n = await dp.store_document_embeddings("doc-1", "content", sensitivity="founders")
        assert n == 2
        records = mock_store.call_args.args[0]
        assert all(r["metadata"]["sensitivity"] == "founders" for r in records)
        # Original metadata is preserved alongside the stamp.
        assert records[0]["metadata"]["document_id"] == "doc-1"

    @pytest.mark.asyncio
    async def test_store_document_embeddings_no_stamp_when_none(self):
        from processors import document_processor as dp
        chunks = [{"text": "a", "chunk_index": 0, "embedding": [0.1], "metadata": {}}]
        with patch.object(dp.embedding_service, "chunk_and_embed_document",
                          new=AsyncMock(return_value=chunks)), \
             patch.object(dp.supabase_client, "store_embeddings_batch") as mock_store:
            await dp.store_document_embeddings("doc-1", "content", sensitivity=None)
        records = mock_store.call_args.args[0]
        assert "sensitivity" not in records[0]["metadata"]
