"""Tests for Phase 13 B2: Document ingestion architecture.

Tests cover:
- Content hash deduplication
- Document versioning (title+source)
- Expanded document types
- create_document with new fields
"""

import pytest
import hashlib
from unittest.mock import MagicMock, patch, AsyncMock


# =========================================================================
# Document types expansion
# =========================================================================

class TestDocumentTypes:

    def test_includes_cropsight_types(self):
        from processors.document_processor import DOCUMENT_TYPES
        assert "grant_proposal" in DOCUMENT_TYPES
        assert "research_paper" in DOCUMENT_TYPES
        assert "investor_deck" in DOCUMENT_TYPES
        assert "partnership_agreement" in DOCUMENT_TYPES

    def test_preserves_original_types(self):
        from processors.document_processor import DOCUMENT_TYPES
        for t in ["strategy", "legal", "technical", "pitch", "client", "other"]:
            assert t in DOCUMENT_TYPES


# =========================================================================
# Content hash deduplication
# =========================================================================

class TestContentHashDedup:

    @pytest.mark.asyncio
    @patch("processors.document_processor.store_document_embeddings", new_callable=AsyncMock)
    @patch("processors.document_processor.classify_document_type", new_callable=AsyncMock)
    @patch("processors.document_processor.generate_document_summary", new_callable=AsyncMock)
    @patch("processors.document_processor.supabase_client")
    async def test_duplicate_content_returns_existing(
        self, mock_sc, mock_summary, mock_classify, mock_embed
    ):
        """If content hash matches existing doc, return existing without re-processing."""
        content = "This is a test document about crop yields."
        content_hash = hashlib.sha256(content.encode("utf-8", errors="replace")).hexdigest()

        # Mock hash lookup returns existing doc
        mock_sc.client.table.return_value.select.return_value.eq.return_value.limit.return_value.execute.return_value = MagicMock(
            data=[{"id": "existing-doc", "title": "Crop Yields", "summary": "About crops", "document_type": "research_paper"}]
        )

        from processors.document_processor import process_document

        result = await process_document(
            content=content,
            title="Crop Yields v2",
            source="email",
        )

        assert result["deduplicated"] is True
        assert result["document_id"] == "existing-doc"
        # Should NOT have called summary/classify/embed
        mock_summary.assert_not_called()
        mock_classify.assert_not_called()
        mock_embed.assert_not_called()

    @pytest.mark.asyncio
    @patch("processors.document_processor.store_document_embeddings", new_callable=AsyncMock)
    @patch("processors.document_processor.classify_document_type", new_callable=AsyncMock)
    @patch("processors.document_processor.generate_document_summary", new_callable=AsyncMock)
    @patch("processors.document_processor.supabase_client")
    async def test_new_content_processes_normally(
        self, mock_sc, mock_summary, mock_classify, mock_embed
    ):
        """New content hash should proceed with full processing."""
        # Hash lookup returns empty
        mock_sc.client.table.return_value.select.return_value.eq.return_value.limit.return_value.execute.return_value = MagicMock(data=[])
        # Title+source lookup returns empty
        mock_sc.client.table.return_value.select.return_value.eq.return_value.eq.return_value.order.return_value.limit.return_value.execute.return_value = MagicMock(data=[])

        mock_summary.return_value = "Summary text"
        mock_classify.return_value = "technical"
        mock_sc.create_document.return_value = {"id": "new-doc"}
        mock_embed.return_value = 5

        from processors.document_processor import process_document

        result = await process_document(
            content="Brand new content.",
            title="New Doc",
            source="drive",
        )

        assert result["document_id"] == "new-doc"
        assert "deduplicated" not in result
        mock_summary.assert_called_once()
        mock_classify.assert_called_once()


# =========================================================================
# Document versioning
# =========================================================================

class TestDocumentVersioning:

    @pytest.mark.asyncio
    @patch("processors.document_processor.store_document_embeddings", new_callable=AsyncMock)
    @patch("processors.document_processor.classify_document_type", new_callable=AsyncMock)
    @patch("processors.document_processor.generate_document_summary", new_callable=AsyncMock)
    @patch("processors.document_processor.supabase_client")
    async def test_version_increments_on_title_match(
        self, mock_sc, mock_summary, mock_classify, mock_embed
    ):
        """Same title+source should increment version."""
        # Hash lookup: no match (different content)
        hash_result = MagicMock(data=[])

        # Title+source lookup: existing v2
        title_result = MagicMock(data=[{"id": "prev-doc", "version": 2, "title": "Budget"}])

        call_count = [0]
        def table_factory(name):
            chain = MagicMock()
            chain.select.return_value = chain
            chain.eq.return_value = chain
            chain.order.return_value = chain
            chain.limit.return_value = chain
            if call_count[0] == 0:
                chain.execute.return_value = hash_result
                call_count[0] += 1
            else:
                chain.execute.return_value = title_result
            return chain

        mock_sc.client.table.side_effect = table_factory

        mock_summary.return_value = "Updated budget"
        mock_classify.return_value = "strategy"
        mock_sc.create_document.return_value = {"id": "new-v3"}
        mock_embed.return_value = 3

        from processors.document_processor import process_document

        result = await process_document(
            content="Updated budget content.",
            title="Budget",
            source="drive",
        )

        # Should have called create_document with version=3
        call_kwargs = mock_sc.create_document.call_args[1]
        assert call_kwargs["version"] == 3
        assert result["version"] == 3

    @pytest.mark.asyncio
    @patch("processors.document_processor.store_document_embeddings", new_callable=AsyncMock)
    @patch("processors.document_processor.classify_document_type", new_callable=AsyncMock)
    @patch("processors.document_processor.generate_document_summary", new_callable=AsyncMock)
    @patch("processors.document_processor.supabase_client")
    async def test_first_version_is_1(
        self, mock_sc, mock_summary, mock_classify, mock_embed
    ):
        """New document with no title match should be version 1."""
        # Both lookups return empty
        empty = MagicMock(data=[])
        chain = MagicMock()
        chain.select.return_value = chain
        chain.eq.return_value = chain
        chain.order.return_value = chain
        chain.limit.return_value = chain
        chain.execute.return_value = empty
        mock_sc.client.table.return_value = chain

        mock_summary.return_value = "New doc"
        mock_classify.return_value = "other"
        mock_sc.create_document.return_value = {"id": "first-doc"}
        mock_embed.return_value = 2

        from processors.document_processor import process_document

        result = await process_document(
            content="First version content.",
            title="Brand New",
            source="upload",
        )

        call_kwargs = mock_sc.create_document.call_args[1]
        assert call_kwargs["version"] == 1


# =========================================================================
# create_document with new fields
# =========================================================================

class TestCreateDocumentNewFields:

    @patch("services.supabase_client.supabase_client")
    def test_content_hash_included(self, mock_sc):
        mock_sc.client.table.return_value.insert.return_value.execute.return_value = MagicMock(
            data=[{"id": "doc-1"}]
        )

        from services.supabase_client import SupabaseClient
        SupabaseClient.create_document(
            mock_sc,
            title="Test",
            source="drive",
            content_hash="abc123",
            version=2,
        )

        insert_arg = mock_sc.client.table.return_value.insert.call_args[0][0]
        assert insert_arg["content_hash"] == "abc123"
        assert insert_arg["version"] == 2

    @patch("services.supabase_client.supabase_client")
    def test_no_hash_omits_field(self, mock_sc):
        mock_sc.client.table.return_value.insert.return_value.execute.return_value = MagicMock(
            data=[{"id": "doc-1"}]
        )

        from services.supabase_client import SupabaseClient
        SupabaseClient.create_document(
            mock_sc,
            title="Test",
            source="email",
        )

        insert_arg = mock_sc.client.table.return_value.insert.call_args[0][0]
        assert "content_hash" not in insert_arg
        assert insert_arg["version"] == 1


# =========================================================================
# Classification prompt update
# =========================================================================

class TestClassificationPrompt:

    def test_prompt_includes_cropsight_context(self):
        import inspect
        import processors.document_processor as module
        source = inspect.getsource(module.classify_document_type)
        assert "CropSight" in source
        assert "AgTech" in source

    def test_prompt_includes_new_categories(self):
        import inspect
        import processors.document_processor as module
        source = inspect.getsource(module.classify_document_type)
        assert "grant_proposal" in source
        assert "research_paper" in source
        assert "investor_deck" in source
        assert "partnership_agreement" in source


# =========================================================================
# Migration
# =========================================================================

class TestPhase13Migration:

    def test_migration_file_exists(self):
        import os
        assert os.path.exists("scripts/migrate_v2_phase13.sql")

    def test_migration_includes_versioning(self):
        with open("scripts/migrate_v2_phase13.sql") as f:
            content = f.read()
        assert "version INTEGER DEFAULT 1" in content
        assert "content_hash TEXT" in content
        assert "idx_documents_content_hash" in content
        assert "idx_documents_title_source" in content
