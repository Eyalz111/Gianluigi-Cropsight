"""Semantic index for decisions & topics (2026-07-13).

Covers: text/sensitivity extraction, delete-then-insert indexing with the
top-level sensitivity column, deindex-on-retire (closed topic / explicit),
flag-gating, backfill (dry-run + batch apply), and tier-safe topic retrieval.
"""
from unittest.mock import AsyncMock, MagicMock

import pytest

import processors.semantic_index as si


@pytest.fixture
def enabled(monkeypatch):
    monkeypatch.setattr(si, "_enabled", lambda: True)


def _mock_clients(monkeypatch, embed_return=None):
    sc = MagicMock()
    emb = MagicMock()
    emb.embed_text = AsyncMock(return_value=embed_return or [0.1] * 1536)
    emb.embed_texts = AsyncMock(return_value=[[0.1] * 1536])
    monkeypatch.setattr(si, "supabase_client", sc)
    monkeypatch.setattr(si, "embedding_service", emb)
    return sc, emb


class TestTextAndSensitivity:
    def test_decision_text_joins_label_desc_rationale(self):
        assert si._decision_text({"label": "L", "description": "D", "rationale": "R"}) == "L. D. R"

    def test_decision_text_skips_blanks(self):
        assert si._decision_text({"description": "D"}) == "D"

    def test_topic_text(self):
        assert si._topic_text({"topic_name": "T", "brief_json": {"narrative": "N"}}) == "T. N"

    def test_topic_sensitivity_from_brief(self):
        assert si._topic_sensitivity({"brief_json": {"sensitivity": "ceo"}}) == "ceo"

    def test_topic_sensitivity_defaults_founders(self):
        assert si._topic_sensitivity({}) == "founders"


class TestFlagGate:
    async def test_index_decision_noop_when_disabled(self, monkeypatch):
        monkeypatch.setattr(si, "_enabled", lambda: False)
        sc, emb = _mock_clients(monkeypatch)
        await si.index_decision({"id": "d1", "description": "D"})
        emb.embed_text.assert_not_called()
        sc.store_embeddings_batch.assert_not_called()

    def test_deindex_noop_when_disabled(self, monkeypatch):
        monkeypatch.setattr(si, "_enabled", lambda: False)
        sc, _ = _mock_clients(monkeypatch)
        si.deindex("decision", "d9")
        sc.delete_embeddings_for_source.assert_not_called()


class TestIndexDecision:
    async def test_delete_then_insert_with_top_level_sensitivity(self, enabled, monkeypatch):
        sc, emb = _mock_clients(monkeypatch)
        await si.index_decision({"id": "d1", "label": "L", "description": "D",
                                 "rationale": "R", "sensitivity": "ceo"})
        sc.delete_embeddings_for_source.assert_called_once_with("decision", "d1")
        rec = sc.store_embeddings_batch.call_args[0][0][0]
        assert rec["source_type"] == "decision" and rec["source_id"] == "d1"
        assert rec["chunk_text"] == "L. D. R" and rec["chunk_index"] == 0
        assert rec["sensitivity"] == "ceo"          # top-level column, not metadata

    async def test_skips_when_no_text(self, enabled, monkeypatch):
        sc, emb = _mock_clients(monkeypatch)
        await si.index_decision({"id": "d1"})
        emb.embed_text.assert_not_called()
        sc.store_embeddings_batch.assert_not_called()


class TestIndexTopic:
    async def test_active_topic_indexed_with_narrative_metadata(self, enabled, monkeypatch):
        sc, emb = _mock_clients(monkeypatch)
        await si.index_topic({"id": "t1", "topic_name": "T", "status": "active",
                              "brief_json": {"narrative": "N", "sensitivity": "team"}})
        rec = sc.store_embeddings_batch.call_args[0][0][0]
        assert rec["source_type"] == "topic" and rec["sensitivity"] == "team"
        assert rec["metadata"]["topic_name"] == "T" and rec["metadata"]["narrative"] == "N"

    async def test_closed_topic_is_deindexed_not_embedded(self, enabled, monkeypatch):
        sc, emb = _mock_clients(monkeypatch)
        await si.index_topic({"id": "t1", "status": "closed", "brief_json": {"narrative": "N"}})
        sc.delete_embeddings_for_source.assert_called_once_with("topic", "t1")
        emb.embed_text.assert_not_called()
        sc.store_embeddings_batch.assert_not_called()


class TestDeindex:
    def test_deindex_calls_delete(self, enabled, monkeypatch):
        sc, _ = _mock_clients(monkeypatch)
        si.deindex("decision", "d9")
        sc.delete_embeddings_for_source.assert_called_once_with("decision", "d9")


class TestBackfill:
    async def test_decisions_dry_run_counts_no_writes(self, monkeypatch):
        sc, emb = _mock_clients(monkeypatch)
        sc.list_decisions.return_value = [{"id": "d1", "description": "D"}, {"id": "d2", "label": "L"}]
        r = await si.backfill_decisions(apply=False)
        assert r["candidates"] == 2 and r["applied"] is False
        emb.embed_texts.assert_not_called()
        sc.store_embeddings_batch.assert_not_called()

    async def test_decisions_apply_batches_embed_and_replaces(self, monkeypatch):
        sc, emb = _mock_clients(monkeypatch)
        sc.list_decisions.return_value = [{"id": "d1", "description": "D1"}, {"id": "d2", "description": "D2"}]
        emb.embed_texts = AsyncMock(return_value=[[0.1] * 1536, [0.2] * 1536])
        r = await si.backfill_decisions(apply=True)
        assert r["indexed"] == 2
        emb.embed_texts.assert_awaited_once()
        assert sc.delete_embeddings_for_source.call_count == 2   # delete-then-insert per row
        sc.store_embeddings_batch.assert_called_once()

    async def test_topics_apply_filters_to_narrative_only(self, monkeypatch):
        sc, emb = _mock_clients(monkeypatch)
        table = sc.client.table.return_value
        table.select.return_value.eq.return_value.execute.return_value.data = [
            {"id": "t1", "topic_name": "A", "status": "active", "brief_json": {"narrative": "n1"}},
            {"id": "t2", "topic_name": "B", "status": "active", "brief_json": {}},  # no narrative -> skipped
        ]
        emb.embed_texts = AsyncMock(return_value=[[0.1] * 1536])
        r = await si.backfill_topics(apply=True)
        assert r["candidates"] == 1 and r["indexed"] == 1


class TestFindRelevantTopics:
    async def test_tier_filtered_and_rendered_fields(self, monkeypatch):
        from processors import meeting_prep as mp
        emb = MagicMock(); emb.embed_text = AsyncMock(return_value=[0.1] * 1536)
        monkeypatch.setattr(mp, "embedding_service", emb)
        sc = MagicMock()
        sc.search_embeddings = MagicMock(return_value=[
            {"source_id": "t1", "metadata": {"topic_name": "Alpha", "narrative": "stands X"}, "sensitivity": "founders"},
            {"source_id": "t2", "metadata": {"topic_name": "Beta", "narrative": "stands Y"}, "sensitivity": "ceo"},
        ])
        monkeypatch.setattr(mp, "supabase_client", sc)
        out = await mp.find_relevant_topics("marketing weekly", limit=5, max_sensitivity_level=3)
        names = [t["topic_name"] for t in out]
        assert "Alpha" in names and "Beta" not in names     # ceo topic filtered at founders level
        assert out[0]["narrative"] == "stands X"
        assert sc.search_embeddings.call_args.kwargs.get("source_type") == "topic"

    def test_format_topics_section(self):
        from processors.meeting_prep import _format_topics_section
        block = _format_topics_section([{"topic_name": "Alpha", "narrative": "stands X"}])
        assert "## Where Key Topics Stand" in block and "**Alpha**" in block and "stands X" in block
        assert _format_topics_section([]) == ""
