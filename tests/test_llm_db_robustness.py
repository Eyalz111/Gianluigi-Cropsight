"""
Tests for LLM + DB-layer robustness (audit PR-G2):
  P3-11 — embed_texts keeps 1:1 alignment (an empty chunk mid-list no longer
          shifts every later chunk onto the wrong vector).
  P6-08 — call_llm raises a clean error (not IndexError) on empty content.
  P6-02 — get_changes_since queries status "done", not the nonexistent "completed".
  P6-03 — search_meetings date filter keeps only in-range chunks (fail-open).
  P3-09 — boot-reconstruction reads return [] on a transient blip.
  P3-10 — log_action returns {} on an audit-insert blip instead of raising.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# =============================================================================
# P3-11 — embed_texts 1:1 alignment
# =============================================================================

class _FakeItem:
    def __init__(self, index, embedding):
        self.index = index
        self.embedding = embedding


class _FakeResp:
    def __init__(self, data):
        self.data = data


def _make_embedding_service(dim=4):
    from services.embeddings import EmbeddingService
    svc = EmbeddingService.__new__(EmbeddingService)
    svc.model = "test-model"
    svc.dimension = dim
    # `client` is a read-only property over `_client`; seed _client so the
    # property returns our mock instead of building a real AsyncOpenAI.
    svc._client = MagicMock()
    svc._clean_text = lambda t: t  # bypass truncation/cleaning
    return svc


class TestEmbedTextsAlignment:
    @pytest.mark.asyncio
    async def test_empty_text_midlist_preserves_alignment(self):
        svc = _make_embedding_service(dim=4)
        # Only "a" and "c" are embedded (indices 0,1 in the request).
        svc.client.embeddings.create = AsyncMock(return_value=_FakeResp([
            _FakeItem(0, [1.0, 1.0, 1.0, 1.0]),
            _FakeItem(1, [2.0, 2.0, 2.0, 2.0]),
        ]))

        result = await svc.embed_texts(["a", "   ", "c"])

        assert len(result) == 3
        assert result[0] == [1.0, 1.0, 1.0, 1.0]   # "a"
        assert result[1] == [0.0, 0.0, 0.0, 0.0]   # empty → zero placeholder
        assert result[2] == [2.0, 2.0, 2.0, 2.0]   # "c" — NOT shifted into slot 1

    @pytest.mark.asyncio
    async def test_all_empty_returns_zero_vectors(self):
        svc = _make_embedding_service(dim=3)
        svc.client.embeddings.create = AsyncMock()
        result = await svc.embed_texts(["", "  "])
        assert result == [[0.0, 0.0, 0.0], [0.0, 0.0, 0.0]]
        svc.client.embeddings.create.assert_not_called()  # no empty API call

    @pytest.mark.asyncio
    async def test_count_mismatch_raises(self):
        svc = _make_embedding_service(dim=4)
        # Request 2, API returns only 1 → must raise, not return misaligned.
        svc.client.embeddings.create = AsyncMock(return_value=_FakeResp([
            _FakeItem(0, [1.0, 1.0, 1.0, 1.0]),
        ]))
        with pytest.raises(ValueError):
            await svc.embed_texts(["a", "b"])


# =============================================================================
# P6-08 — call_llm guards an empty content block
# =============================================================================

class TestCallLlmEmptyContent:
    def test_empty_content_raises_clean_error(self):
        from core import llm

        mock_client = MagicMock()
        mock_resp = MagicMock()
        mock_resp.content = []          # truncated/overloaded → no content
        mock_resp.stop_reason = "max_tokens"
        mock_client.messages.create.return_value = mock_resp

        with patch.object(llm, "get_client", return_value=mock_client):
            with pytest.raises(RuntimeError):
                llm.call_llm(
                    prompt="hi", model="claude-haiku-4-5",
                    max_tokens=16, call_site="test",
                )


# =============================================================================
# P6-02 — get_changes_since uses status "done"
# =============================================================================

class TestGetChangesSinceStatus:
    def test_queries_done_not_completed(self):
        from services.supabase_client import supabase_client

        chain = MagicMock()
        # Every chained call returns the same mock; execute returns empty data.
        chain.table.return_value = chain
        chain.select.return_value = chain
        chain.eq.return_value = chain
        chain.gte.return_value = chain
        chain.lte.return_value = chain
        chain.limit.return_value = chain
        chain.execute.return_value = MagicMock(data=[])

        # `client` is a read-only property over `_client`; patch the backing attr.
        with patch.object(supabase_client, "_client", chain):
            supabase_client.get_changes_since("2026-06-01")

        eq_calls = [c.args for c in chain.eq.call_args_list]
        assert ("status", "done") in eq_calls
        assert ("status", "completed") not in eq_calls


# =============================================================================
# P6-03 — search_meetings date filter (fail-open)
# =============================================================================

class TestSearchMeetingsDateFilter:
    def test_filters_to_in_range_meetings(self):
        from core.agent import GianluigiAgent
        from services.supabase_client import supabase_client

        results = [
            {"source_id": "m-april", "chunk_text": "x"},
            {"source_id": "m-may", "chunk_text": "y"},
        ]
        with patch.object(supabase_client, "list_meetings",
                          return_value=[{"id": "m-april"}]):
            filtered = GianluigiAgent._filter_chunks_by_meeting_date(
                results, "2026-04-01", "2026-04-30"
            )
        assert [r["source_id"] for r in filtered] == ["m-april"]

    def test_unparseable_date_fails_open(self):
        from core.agent import GianluigiAgent
        results = [{"source_id": "m-1"}]
        out = GianluigiAgent._filter_chunks_by_meeting_date(results, "not-a-date", None)
        assert out == results  # unfiltered, not empty


# =============================================================================
# P3-09 / P3-10 — DB-layer defensive returns
# =============================================================================

class TestDbDefensiveReturns:
    def test_get_pending_auto_publishes_returns_empty_on_error(self):
        from services.supabase_client import supabase_client
        mock_client = MagicMock()
        mock_client.table.side_effect = RuntimeError("supabase down")
        with patch.object(supabase_client, "_client", mock_client):
            assert supabase_client.get_pending_auto_publishes() == []

    def test_get_pending_approvals_for_reminders_returns_empty_on_error(self):
        from services.supabase_client import supabase_client
        mock_client = MagicMock()
        mock_client.table.side_effect = RuntimeError("supabase down")
        with patch.object(supabase_client, "_client", mock_client):
            assert supabase_client.get_pending_approvals_for_reminders() == []

    def test_log_action_returns_empty_dict_on_error(self):
        from services.supabase_client import supabase_client
        mock_client = MagicMock()
        mock_client.table.side_effect = RuntimeError("audit insert failed")
        with patch.object(supabase_client, "_client", mock_client):
            # Must NOT raise — a caller that logs after its primary write relies on this.
            assert supabase_client.log_action("some_action") == {}
