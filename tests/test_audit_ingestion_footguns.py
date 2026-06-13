"""
Group C — ingestion atomicity + approval-gate footgun visibility (June 2026 audit).

  P1-06 — a content-hash match whose existing doc has 0 embeddings (a prior run
          cycled between create_document and store_document_embeddings) is
          RE-EMBEDDED into that row instead of being skipped forever.
  P5-05 — the approval-gate bypass flags surface a loud warning at boot.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# =============================================================================
# P1-06 — 0-embedding hash match is repaired, not skipped
# =============================================================================

class TestDocEmbeddingRepair:
    @pytest.mark.asyncio
    @patch("processors.document_processor._document_embedding_count", return_value=0)
    @patch("processors.document_processor.store_document_embeddings", new_callable=AsyncMock)
    @patch("processors.document_processor.classify_document_type", new_callable=AsyncMock)
    @patch("processors.document_processor.generate_document_summary", new_callable=AsyncMock)
    @patch("processors.document_processor.supabase_client")
    async def test_zero_embedding_match_reembeds(
        self, mock_sc, mock_summary, mock_classify, mock_embed, mock_count
    ):
        mock_sc.client.table.return_value.select.return_value.eq.return_value.limit.return_value.execute.return_value = MagicMock(
            data=[{"id": "doc-9", "title": "Term sheet", "summary": "s",
                   "document_type": "legal", "sensitivity": "ceo"}]
        )
        mock_embed.return_value = 7

        from processors.document_processor import process_document
        result = await process_document(content="body", title="Term sheet", source="drive")

        # re-embedded into the EXISTING row, not skipped
        mock_embed.assert_awaited_once()
        assert mock_embed.await_args.args[0] == "doc-9"
        # tier carried into the repair re-embed
        assert mock_embed.await_args.kwargs.get("sensitivity") == "ceo"
        assert result["document_id"] == "doc-9"
        assert result["chunk_count"] == 7
        assert result["deduplicated"] is False
        assert result.get("repaired") is True

    @pytest.mark.asyncio
    @patch("processors.document_processor._document_embedding_count", return_value=None)
    @patch("processors.document_processor.store_document_embeddings", new_callable=AsyncMock)
    @patch("processors.document_processor.classify_document_type", new_callable=AsyncMock)
    @patch("processors.document_processor.generate_document_summary", new_callable=AsyncMock)
    @patch("processors.document_processor.supabase_client")
    async def test_unknown_count_skips_to_avoid_duplicates(
        self, mock_sc, mock_summary, mock_classify, mock_embed, mock_count
    ):
        # count couldn't be read → assume embedded → skip (don't risk duplicates)
        mock_sc.client.table.return_value.select.return_value.eq.return_value.limit.return_value.execute.return_value = MagicMock(
            data=[{"id": "doc-9", "title": "T", "summary": "s", "document_type": "legal"}]
        )
        from processors.document_processor import process_document
        result = await process_document(content="body", title="T", source="drive")
        assert result["deduplicated"] is True
        mock_embed.assert_not_called()


# =============================================================================
# P5-05 — approval-gate bypass flags warn at boot
# =============================================================================

class TestBypassFlagWarnings:
    def test_auto_review_warns(self):
        from config.settings import settings
        with patch.object(settings, "APPROVAL_MODE", "auto_review"):
            warnings = settings.validate_optional()
        assert any("APPROVAL_MODE=auto_review" in w for w in warnings)

    def test_auto_distribute_warns(self):
        from config.settings import settings
        with patch.object(settings, "INTELLIGENCE_SIGNAL_AUTO_DISTRIBUTE", True):
            warnings = settings.validate_optional()
        assert any("INTELLIGENCE_SIGNAL_AUTO_DISTRIBUTE" in w for w in warnings)

    def test_continuity_auto_apply_warns(self):
        from config.settings import settings
        with patch.object(settings, "CONTINUITY_AUTO_APPLY_ENABLED", True):
            warnings = settings.validate_optional()
        assert any("CONTINUITY_AUTO_APPLY_ENABLED" in w for w in warnings)

    def test_safe_defaults_no_bypass_warning(self):
        from config.settings import settings
        with patch.object(settings, "APPROVAL_MODE", "manual"), \
             patch.object(settings, "INTELLIGENCE_SIGNAL_AUTO_DISTRIBUTE", False), \
             patch.object(settings, "CONTINUITY_AUTO_APPLY_ENABLED", False):
            warnings = settings.validate_optional()
        assert not any("Relaxes the I1 gate" in w or "auto_review" in w for w in warnings)
