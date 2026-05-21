"""
Unit tests for v2.5 knowledge models + backfill matching logic.

Pure (no DB): pydantic round-trips and the conservative area-matching helpers.
"""

import pytest

from models.schemas import (
    AreaBrief,
    BriefCitation,
    BriefFact,
    KnowledgeLink,
    LinkType,
    OpenItem,
    Sensitivity,
    TopicBrief,
    TopicStatus,
)


class TestKnowledgeModels:
    def test_topic_brief_roundtrip(self):
        brief = TopicBrief(
            narrative="Moldova pilot scoped to Gagauzia wheat.",
            facts=[BriefFact(text="Pilot site = Gagauzia", sensitivity=Sensitivity.FOUNDERS)],
            current_status=TopicStatus.BLOCKED,
            open_items=[OpenItem(kind="blocker", description="regional permit")],
            citations=[BriefCitation(source_id="m1", meeting_title="BD sync")],
        )
        restored = TopicBrief.model_validate(brief.model_dump())
        assert restored.current_status == TopicStatus.BLOCKED
        assert restored.facts[0].sensitivity == Sensitivity.FOUNDERS
        assert restored.open_items[0].description == "regional permit"
        assert restored.version == 1

    def test_area_brief_roundtrip(self):
        area = AreaBrief(narrative="R&D area", strategic_state="on track")
        assert AreaBrief.model_validate(area.model_dump()).narrative == "R&D area"

    def test_per_fact_sensitivity_not_collapsed(self):
        # A CEO-tier fact keeps its own tier; it does not force the brief default.
        ceo_fact = BriefFact(text="investor terms", sensitivity=Sensitivity.CEO)
        default_fact = BriefFact(text="general status")
        assert ceo_fact.sensitivity == Sensitivity.CEO
        assert default_fact.sensitivity == Sensitivity.FOUNDERS

    def test_knowledge_link_enum_serializes_to_string(self):
        link = KnowledgeLink(
            from_type="topic", from_id="t1",
            to_type="area", to_id="a1",
            link_type=LinkType.BELONGS_TO,
        )
        dumped = link.model_dump()
        assert dumped["link_type"] == "belongs_to"
        assert dumped["created_by"] == "auto"
        assert dumped["confidence"] is None


class TestBackfillMatching:
    """The conservative topic->area matcher (pure functions, no DB)."""

    def _helpers(self):
        try:
            from scripts.backfill_knowledge_v25 import _best_area, _words
        except Exception as e:  # supabase import may fail without creds
            pytest.skip(f"Cannot import backfill helpers ({e})")
        return _words, _best_area

    def test_words_strips_stopwords_and_punctuation(self):
        _words, _ = self._helpers()
        assert _words("The Moldova Pilot!") == {"moldova", "pilot"}

    def test_best_area_matches_on_shared_word(self):
        _, _best_area = self._helpers()
        areas = [
            {"name": "PRODUCT & TECHNOLOGY", "gantt_section": "PRODUCT & TECHNOLOGY"},
            {"name": "CLIENT DELIVERY", "gantt_section": "CLIENT DELIVERY"},
        ]
        match = _best_area("Moldova delivery", areas)
        assert match is not None and match["name"] == "CLIENT DELIVERY"

    def test_best_area_returns_none_when_uncertain(self):
        # No meaningful overlap -> leave area_id NULL (routed to 1d proposals).
        _, _best_area = self._helpers()
        areas = [{"name": "PRODUCT & TECHNOLOGY", "gantt_section": "PRODUCT & TECHNOLOGY"}]
        assert _best_area("Quarterly offsite", areas) is None
