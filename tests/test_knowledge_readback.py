"""
Tests for the v2.5 PR3 read-back loop.

Covers: the prompt knowledge_section, the sensitivity filter on injected
briefs, build_knowledge_context assembly/None, and the shadow-vs-cutover
orchestration in _extract_with_readback. No live DB or LLM.
"""

from types import SimpleNamespace

import pytest

from core.system_prompt import get_summary_extraction_prompt


class _Chain:
    def __init__(self, data):
        object.__setattr__(self, "data_rows", data)

    def __getattr__(self, name):
        if name.startswith("__"):
            raise AttributeError(name)
        if name == "not_":
            return self  # PostgREST .not_ is an attribute; .is_(...) is called on it
        return lambda *a, **k: self

    def execute(self):
        return SimpleNamespace(data=self.data_rows)


# =============================================================================
# Prompt section
# =============================================================================

class TestPromptKnowledgeSection:
    def _prompt(self, knowledge_context):
        return get_summary_extraction_prompt(
            transcript="t", meeting_title="MVP", meeting_date="2026-05-01",
            participants=["Eyal"], knowledge_context=knowledge_context,
        )

    def test_section_absent_without_context(self):
        assert "KNOWLEDGE BASE" not in self._prompt(None)

    def test_section_present_with_context(self):
        p = self._prompt("Topic 'Moldova' [blocked]: waiting on permit.")
        assert "KNOWLEDGE BASE" in p
        assert "waiting on permit" in p


# =============================================================================
# Sensitivity filter on injected briefs
# =============================================================================

class TestSensitivityFilter:
    @pytest.fixture
    def patch_topics(self, monkeypatch):
        try:
            from services.supabase_client import supabase_client
        except Exception as e:
            pytest.skip(f"cannot import supabase_client ({e})")
        rows = [
            {"topic_name": "Moldova Pilot", "brief_json": {
                "sensitivity": "ceo", "narrative": "investor terms", "current_status": "active", "open_items": []}},
            {"topic_name": "Moldova delivery", "brief_json": {
                "sensitivity": "founders", "narrative": "permit pending", "current_status": "blocked", "open_items": []}},
        ]
        monkeypatch.setattr(supabase_client, "_client", SimpleNamespace(table=lambda *a, **k: _Chain(rows)))

    def test_ceo_brief_excluded_for_founders_meeting(self, patch_topics):
        from processors.knowledge_readback import _relevant_topic_briefs
        out = _relevant_topic_briefs("Moldova sync", [], meeting_level=3)  # founders
        joined = " ".join(out)
        assert "Moldova delivery" in joined          # founders brief kept
        assert "investor terms" not in joined         # CEO brief filtered out

    def test_ceo_brief_included_for_ceo_meeting(self, patch_topics):
        from processors.knowledge_readback import _relevant_topic_briefs
        out = _relevant_topic_briefs("Moldova sync", [], meeting_level=4)  # ceo
        assert any("investor terms" in line for line in out)


# =============================================================================
# build_knowledge_context assembly
# =============================================================================

class TestBuildContext:
    async def test_none_when_empty(self, monkeypatch):
        import processors.knowledge_readback as kr
        monkeypatch.setattr(kr, "_relevant_topic_briefs", lambda *a, **k: [])

        async def _no_chunks(*a, **k):
            return []

        monkeypatch.setattr(kr, "_relevant_chunks", _no_chunks)
        assert await kr.build_knowledge_context("MVP", [], "founders") is None

    async def test_assembles_and_caps(self, monkeypatch):
        import processors.knowledge_readback as kr
        monkeypatch.setattr(kr, "_relevant_topic_briefs", lambda *a, **k: ["Topic 'X' [active]: long " + "y" * 5000])

        async def _no_chunks(*a, **k):
            return []

        monkeypatch.setattr(kr, "_relevant_chunks", _no_chunks)
        ctx = await kr.build_knowledge_context("MVP", [], "founders", budget_chars=500)
        assert ctx is not None
        assert len(ctx) <= 500 + len(" …(truncated)")
        assert "truncated" in ctx


# =============================================================================
# Shadow vs cutover orchestration
# =============================================================================

class TestReadbackOrchestration:
    @pytest.fixture
    def tp(self):
        try:
            import processors.transcript_processor as tp
        except Exception as e:
            pytest.skip(f"cannot import transcript_processor ({e})")
        return tp

    def _wire(self, tp, monkeypatch, shadow, readback):
        async def fake_extract(*args, knowledge_context=None, **kwargs):
            tag = "augmented" if knowledge_context else "baseline"
            return {"tasks": [{"title": tag}], "decisions": []}

        async def fake_ctx(*a, **k):
            return "CTX"

        calls = {"shadow": 0}

        def fake_log_shadow(*a, **k):
            calls["shadow"] += 1

        monkeypatch.setattr(tp, "extract_structured_data", fake_extract)
        import processors.knowledge_readback as kr
        monkeypatch.setattr(kr, "build_knowledge_context", fake_ctx)
        import core.shadow_run as sr
        monkeypatch.setattr(sr, "log_shadow", fake_log_shadow)
        monkeypatch.setattr(tp.settings, "KNOWLEDGE_SHADOW_MODE", shadow)
        monkeypatch.setattr(tp.settings, "KNOWLEDGE_READBACK_ENABLED", readback)
        return calls

    async def test_shadow_ships_baseline_and_logs(self, tp, monkeypatch):
        calls = self._wire(tp, monkeypatch, shadow=True, readback=False)
        result = await tp._extract_with_readback("t", "MVP", ["Eyal"], "2026-05-01", 30, "founders")
        assert result["tasks"][0]["title"] == "baseline"  # ships baseline
        assert calls["shadow"] == 1                         # logged the comparison

    async def test_cutover_ships_augmented(self, tp, monkeypatch):
        calls = self._wire(tp, monkeypatch, shadow=False, readback=True)
        result = await tp._extract_with_readback("t", "MVP", ["Eyal"], "2026-05-01", 30, "founders")
        assert result["tasks"][0]["title"] == "augmented"
        assert calls["shadow"] == 0

    async def test_neither_ships_baseline_no_log(self, tp, monkeypatch):
        calls = self._wire(tp, monkeypatch, shadow=False, readback=False)
        result = await tp._extract_with_readback("t", "MVP", ["Eyal"], "2026-05-01", 30, "founders")
        assert result["tasks"][0]["title"] == "baseline"
        assert calls["shadow"] == 0
