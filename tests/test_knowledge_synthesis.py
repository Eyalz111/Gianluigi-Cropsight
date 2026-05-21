"""
Unit tests for v2.5 PR2 knowledge synthesis.

No live DB or LLM: pure assembly/parsing helpers, plus a mocked end-to-end
synthesize_topic_brief and an idempotency check on synthesize_all_topics.
"""

import json
from types import SimpleNamespace

import pytest

try:
    from processors import knowledge_synthesis as ks
except Exception as e:  # supabase/llm import may fail without creds
    pytest.skip(f"cannot import knowledge_synthesis ({e})", allow_module_level=True)

from models.schemas import Sensitivity, TopicStatus


SOURCES = [
    {"idx": 0, "id": "m1", "title": "BD sync", "date": "2026-04-02", "tier": "founders",
     "summary": "discussed pilot", "context": "Gagauzia", "decisions_made": ["pick Gagauzia"]},
    {"idx": 1, "id": "m2", "title": "Legal call", "date": "2026-04-09", "tier": "ceo",
     "summary": "investor terms", "context": "term sheet", "decisions_made": []},
]


class TestParsing:
    def test_parse_plain_json(self):
        assert ks._parse_json('{"a": 1}') == {"a": 1}

    def test_parse_fenced_json(self):
        assert ks._parse_json('```json\n{"a": 1}\n```') == {"a": 1}

    def test_parse_garbage_returns_none(self):
        assert ks._parse_json("not json at all") is None

    def test_to_sensitivity_defaults_founders(self):
        assert ks._to_sensitivity(None) == Sensitivity.FOUNDERS
        assert ks._to_sensitivity("ceo") == Sensitivity.CEO
        assert ks._to_sensitivity("bogus") == Sensitivity.FOUNDERS

    def test_max_sensitivity(self):
        assert ks._max_sensitivity([Sensitivity.FOUNDERS, Sensitivity.CEO]) == Sensitivity.CEO
        assert ks._max_sensitivity([]) == Sensitivity.FOUNDERS


class TestAssembleTopicBrief:
    def _data(self):
        return {
            "narrative": "Pilot scoped to Gagauzia; blocked on permit.",
            "current_status": "blocked",
            "key_facts": [
                {"text": "Pilot site = Gagauzia", "source": 0},
                {"text": "Investor term sheet drafted", "source": 1},
                {"text": "Synthesized cross-meeting fact", "source": None},
            ],
            "open_items": [{"kind": "blocker", "description": "regional permit", "owner": "Paolo"}],
            "stakeholders": ["Paolo", "Eyal"],
            "recent_decisions": [{"text": "pick Gagauzia", "date": "2026-04-02", "meeting_title": "BD sync"}],
            "risks": ["permit delay"],
            "next_actions": ["file permit"],
        }

    def test_per_fact_sensitivity_tagging(self):
        brief = ks._assemble_topic_brief(self._data(), SOURCES)
        # fact from a FOUNDERS source stays FOUNDERS; from a CEO source becomes CEO
        assert brief.facts[0].sensitivity == Sensitivity.FOUNDERS
        assert brief.facts[1].sensitivity == Sensitivity.CEO
        # null-source fact falls back to the most-restrictive source tier (CEO)
        assert brief.facts[2].sensitivity == Sensitivity.CEO

    def test_brief_level_sensitivity_is_max(self):
        brief = ks._assemble_topic_brief(self._data(), SOURCES)
        assert brief.sensitivity == Sensitivity.CEO  # not collapsed away — max across sources

    def test_status_and_citations(self):
        brief = ks._assemble_topic_brief(self._data(), SOURCES)
        assert brief.current_status == TopicStatus.BLOCKED
        assert brief.facts[0].citation.source_id == "m1"
        assert len(brief.citations) == 2
        assert brief.open_items[0].description == "regional permit"
        assert brief.recent_decisions[0].text == "pick Gagauzia"

    def test_bad_status_defaults_active(self):
        data = self._data()
        data["current_status"] = "nonsense"
        assert ks._assemble_topic_brief(data, SOURCES).current_status == TopicStatus.ACTIVE


class TestSynthesizeTopicEndToEnd:
    async def test_synthesize_topic_brief_mocked(self, monkeypatch):
        monkeypatch.setattr(ks, "_gather_topic_sources", lambda topic_id: SOURCES)
        llm_json = json.dumps({
            "narrative": "n", "current_status": "active",
            "key_facts": [{"text": "f1", "source": 0}],
            "open_items": [], "stakeholders": [], "recent_decisions": [],
            "risks": [], "next_actions": [],
        })
        monkeypatch.setattr(ks, "call_llm", lambda **kw: (llm_json, {}))

        brief = await ks.synthesize_topic_brief(
            {"id": "t1", "topic_name": "Moldova Pilot"}, use_rag=False
        )
        assert brief is not None
        assert brief["current_status"] == "active"
        assert brief["facts"][0]["text"] == "f1"
        assert brief["facts"][0]["sensitivity"] == "founders"

    async def test_no_sources_returns_none(self, monkeypatch):
        monkeypatch.setattr(ks, "_gather_topic_sources", lambda topic_id: [])
        brief = await ks.synthesize_topic_brief(
            {"id": "t1", "topic_name": "Empty"}, use_rag=False
        )
        assert brief is None


class _Chain:
    def __init__(self, data):
        object.__setattr__(self, "data_rows", data)

    def __getattr__(self, name):
        if name.startswith("__"):
            raise AttributeError(name)
        return lambda *a, **k: self

    def execute(self):
        return SimpleNamespace(data=self.data_rows)


class TestIdempotency:
    async def test_skips_topics_with_existing_brief(self, monkeypatch):
        topics = [
            {"id": "t1", "topic_name": "A", "area_id": None, "brief_json": None},
            {"id": "t2", "topic_name": "B", "area_id": None, "brief_json": {"x": 1}},
        ]
        monkeypatch.setattr(
            ks.supabase_client, "_client",
            SimpleNamespace(table=lambda *a, **k: _Chain(topics)),
        )

        async def _fake_synth(topic, use_rag=True):
            return {"current_status": "active"}

        monkeypatch.setattr(ks, "synthesize_topic_brief", _fake_synth)

        # dry_run avoids any write path; force=False must skip the topic that has a brief
        result = await ks.synthesize_all_topics(force=False, dry_run=True)
        assert result["synthesized"] == 1
        assert result["skipped"] == 1
        assert result["failed"] == 0
