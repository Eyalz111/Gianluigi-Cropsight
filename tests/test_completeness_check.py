"""
Tests for the v2.5 PR4 completeness check.

No live DB or LLM: find_missing_items parsing (mocked LLM) + the gated
shadow/cutover/off orchestration in _apply_completeness_check.
"""

import json

import pytest

try:
    import processors.completeness_check as cc
    import processors.transcript_processor as tp
except Exception as e:
    pytest.skip(f"cannot import completeness modules ({e})", allow_module_level=True)


class TestFindMissingItems:
    def test_parses_additions(self, monkeypatch):
        out = json.dumps({
            "tasks": [{"title": "Email the investor", "assignee": "Eyal", "priority": "H"}],
            "decisions": [{"description": "Use Postgres", "label": "Infra"}],
        })
        monkeypatch.setattr(cc, "call_llm", lambda **kw: (out, {}))
        res = cc.find_missing_items("transcript", {"tasks": [], "decisions": []}, [], "MVP")
        assert len(res["tasks"]) == 1
        assert res["tasks"][0]["title"] == "Email the investor"
        # additions are deadline-less so they never trigger reminders unprompted
        assert res["tasks"][0]["deadline_confidence"] == "NONE"
        assert res["tasks"][0]["_source"] == "completeness_check"
        assert res["decisions"][0]["description"] == "Use Postgres"

    def test_empty_when_nothing_missed(self, monkeypatch):
        monkeypatch.setattr(cc, "call_llm", lambda **kw: ('{"tasks":[],"decisions":[]}', {}))
        assert cc.find_missing_items("t", {"tasks": []}, [], "MVP") == {"tasks": [], "decisions": []}

    def test_never_raises_on_garbage(self, monkeypatch):
        monkeypatch.setattr(cc, "call_llm", lambda **kw: ("not json at all", {}))
        assert cc.find_missing_items("t", {}, [], "MVP") == {"tasks": [], "decisions": []}

    def test_skips_blank_titles(self, monkeypatch):
        out = json.dumps({"tasks": [{"title": "  "}, {"title": "Real task"}], "decisions": []})
        monkeypatch.setattr(cc, "call_llm", lambda **kw: (out, {}))
        res = cc.find_missing_items("t", {}, [], "MVP")
        assert [t["title"] for t in res["tasks"]] == ["Real task"]


class TestApplyCompletenessCheck:
    def _wire(self, monkeypatch, shadow, readback, additions):
        monkeypatch.setattr(tp.settings, "KNOWLEDGE_SHADOW_MODE", shadow)
        monkeypatch.setattr(tp.settings, "KNOWLEDGE_READBACK_ENABLED", readback)
        monkeypatch.setattr(cc, "find_missing_items", lambda *a, **k: additions)
        monkeypatch.setattr(tp.supabase_client, "get_tasks", lambda **k: [])
        calls = {"shadow": 0}
        import core.shadow_run as sr
        monkeypatch.setattr(sr, "log_shadow", lambda *a, **k: calls.__setitem__("shadow", calls["shadow"] + 1))
        return calls

    async def test_off_is_noop(self, monkeypatch):
        self._wire(monkeypatch, False, False, {"tasks": [{"title": "missed"}], "decisions": []})
        extracted = {"tasks": [{"title": "a"}], "decisions": []}
        out = await tp._apply_completeness_check("t", "MVP", extracted)
        assert out == extracted  # untouched when both flags off

    async def test_shadow_logs_and_ships_unchanged(self, monkeypatch):
        calls = self._wire(monkeypatch, True, False, {"tasks": [{"title": "missed"}], "decisions": []})
        extracted = {"tasks": [{"title": "a"}], "decisions": []}
        out = await tp._apply_completeness_check("t", "MVP", extracted)
        assert [t["title"] for t in out["tasks"]] == ["a"]  # shipped unchanged
        assert calls["shadow"] == 1                          # but logged the comparison

    async def test_cutover_merges_additions(self, monkeypatch):
        calls = self._wire(monkeypatch, False, True, {"tasks": [{"title": "missed"}], "decisions": []})
        extracted = {"tasks": [{"title": "a"}], "decisions": []}
        out = await tp._apply_completeness_check("t", "MVP", extracted)
        titles = [t["title"] for t in out["tasks"]]
        assert "a" in titles and "missed" in titles
        assert calls["shadow"] == 0  # no shadow log on the real cutover path

    async def test_no_additions_ships_unchanged(self, monkeypatch):
        calls = self._wire(monkeypatch, True, False, {"tasks": [], "decisions": []})
        extracted = {"tasks": [{"title": "a"}], "decisions": []}
        out = await tp._apply_completeness_check("t", "MVP", extracted)
        assert out == extracted
        assert calls["shadow"] == 0  # nothing to compare, no log
