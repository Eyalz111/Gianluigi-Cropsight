"""Tests for the weekly decision-narrative synthesis (LLM half).

No live DB/LLM: patch build_decision_brief + supabase_client + call_llm. Covers
the prompt, narrative-only overlay, sensitivity ceiling, malformed-JSON no-clobber,
and run_decision_synthesis selection/cap. Mirrors tests/test_knowledge_synthesis.py.
"""
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

try:
    import processors.decision_synthesis as ds
except Exception as e:
    pytest.skip(f"cannot import decision_synthesis ({e})", allow_module_level=True)


def _base(**kw):
    b = {"summary": "S", "narrative": "", "status": "active", "rationale": "R",
         "supersedes": [], "superseded_by": None, "related": [], "chain_length": 1,
         "last_referenced_at": None, "sensitivity": "founders",
         "last_synthesized_at": None, "version": 1}
    b.update(kw)
    return b


class TestPromptAndAssemble:
    def test_prompt_includes_chain_and_related(self):
        p = ds._build_decision_prompt(
            {"description": "Ship MVP", "decision_status": "active", "rationale": "speed"},
            [{"description": "Build prototype", "created_at": "2026-06-01", "decision_status": "superseded"}],
            [{"description": "Hire ML eng"}],
        )
        assert "Ship MVP" in p and "Build prototype" in p and "Hire ML eng" in p and "speed" in p

    def test_empty_chain_renders_placeholder(self):
        p = ds._build_decision_prompt({"description": "X", "decision_status": "active"}, [], [])
        assert "(no prior versions)" in p and "(none)" in p

    def test_assemble_overlays_only_narrative_and_related(self):
        out = ds._assemble_decision_brief(_base(supersedes=["x"], chain_length=2),
                                          {"narrative": "new prose"}, ["r1", "r2"])
        assert out["narrative"] == "new prose"
        assert out["related"] == ["r1", "r2"]
        # everything else copied from base verbatim
        assert out["summary"] == "S" and out["status"] == "active"
        assert out["supersedes"] == ["x"] and out["chain_length"] == 2


class TestSynthesize:
    def _wire(self, monkeypatch, base, chain, related, llm_out):
        monkeypatch.setattr(ds, "build_decision_brief", lambda did: dict(base))
        sc = MagicMock()
        sc.get_decision_chain.return_value = chain
        sc.get_related_decisions.return_value = related
        monkeypatch.setattr(ds, "supabase_client", sc)
        monkeypatch.setattr(ds, "call_llm", lambda **k: (llm_out, {}))
        return sc

    async def test_writes_narrative(self, monkeypatch):
        sc = self._wire(monkeypatch, _base(), [], [], '{"narrative": "We chose Postgres for performance."}')
        out = await ds.synthesize_decision_brief({"id": "d1", "description": "Use PG", "sensitivity": "founders"})
        assert out["narrative"] == "We chose Postgres for performance."
        assert sc.update_decision.call_args.kwargs["brief_json"]["narrative"].startswith("We chose")

    async def test_malformed_json_returns_none_no_clobber(self, monkeypatch):
        sc = self._wire(monkeypatch, _base(), [], [], "not json at all")
        out = await ds.synthesize_decision_brief({"id": "d1", "description": "x", "sensitivity": "founders"})
        assert out is None
        sc.update_decision.assert_not_called()

    async def test_populates_related_ids(self, monkeypatch):
        related = [{"id": "r1", "description": "rel", "sensitivity": "founders"}]
        sc = self._wire(monkeypatch, _base(), [], related, '{"narrative": "n"}')
        out = await ds.synthesize_decision_brief({"id": "d1", "description": "x", "sensitivity": "founders"})
        assert out["related"] == ["r1"]

    async def test_sensitivity_ceiling_drops_ceo_input(self, monkeypatch):
        chain = [{"id": "c1", "description": "SECRET ceo context", "sensitivity": "ceo",
                  "created_at": "2026-07-01", "decision_status": "active"}]
        captured = {}
        monkeypatch.setattr(ds, "build_decision_brief", lambda did: _base())
        sc = MagicMock()
        sc.get_decision_chain.return_value = chain
        sc.get_related_decisions.return_value = []
        monkeypatch.setattr(ds, "supabase_client", sc)
        def fake_llm(**k):
            captured["prompt"] = k["prompt"]
            return ('{"narrative": "ok"}', {})
        monkeypatch.setattr(ds, "call_llm", fake_llm)
        await ds.synthesize_decision_brief({"id": "d1", "description": "pub", "sensitivity": "founders"})
        assert "SECRET ceo context" not in captured["prompt"]   # ceo input dropped for a founders decision


class _Chain:
    def __init__(self, data):
        object.__setattr__(self, "data_rows", data)

    def __getattr__(self, name):
        if name.startswith("__"):
            raise AttributeError(name)
        return lambda *a, **k: self

    def execute(self):
        return SimpleNamespace(data=self.data_rows)


class TestRunSynthesis:
    async def test_caps_and_counts(self, monkeypatch):
        rows = [{"id": f"d{i}", "description": "x", "sensitivity": "founders",
                 "last_referenced_at": "2026-07-11"} for i in range(5)]
        sc = MagicMock()
        sc.client.table.return_value = _Chain(rows)   # every bounded query returns the same 5
        monkeypatch.setattr(ds, "supabase_client", sc)
        called = []
        async def fake_syn(d):
            called.append(d["id"])
            return {"narrative": "x"}
        monkeypatch.setattr(ds, "synthesize_decision_brief", fake_syn)
        res = await ds.run_decision_synthesis(days=7, max_decisions=3)
        assert res["candidates"] == 5 and res["synthesized"] == 3 and res["capped"] is True
        assert len(called) == 3
