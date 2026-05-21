"""
Tests for the v2.5 PR6 on-event brief refresh (_sync_brief_from_state).

Verifies the merge keeps rich fields, refreshes current-state fields, tags new
facts with the meeting's tier, writes the right links, and never raises.
"""

import pytest

try:
    import processors.topic_threading as tt
except Exception as e:
    pytest.skip(f"cannot import topic_threading ({e})", allow_module_level=True)


def test_max_tier():
    assert tt._max_tier("founders", "ceo") == "ceo"
    assert tt._max_tier("public", "team") == "team"
    assert tt._max_tier(None, None) == "founders"


class TestSyncBriefFromState:
    def _capture(self, monkeypatch):
        captured = {}
        monkeypatch.setattr(
            tt.supabase_client, "update_topic_brief",
            lambda tid, brief: captured.update({"id": tid, "brief": brief}) or True,
        )
        links = []
        monkeypatch.setattr(
            tt.supabase_client, "create_knowledge_link",
            lambda *a, **k: links.append(a),
        )
        return captured, links

    def test_merges_state_preserving_rich_fields(self, monkeypatch):
        captured, links = self._capture(monkeypatch)
        thread = {"id": "t1", "area_id": "a1", "brief_json": {
            "narrative": "old", "facts": [{"text": "old fact", "sensitivity": "founders"}],
            "risks": ["existing risk"], "next_actions": ["existing action"],
            "current_status": "active", "version": 3,
        }}
        state = {
            "summary": "new current state", "current_status": "blocked",
            "key_facts": ["new fact"],
            "open_items": [{"kind": "blocker", "description": "permit"}],
            "stakeholders": ["Paolo"],
            "last_decision": {"text": "go Gagauzia", "date": "2026-05-01"},
        }
        meeting = {"id": "m1", "title": "BD", "date": "2026-05-01", "sensitivity": "ceo"}

        tt._sync_brief_from_state(thread, state, meeting, [{"id": "d1"}], [{"id": "tk1"}])

        brief = captured["brief"]
        assert brief["narrative"] == "new current state"
        assert brief["current_status"] == "blocked"
        # rich fields preserved (on-event does not recompute these)
        assert brief["risks"] == ["existing risk"]
        assert brief["next_actions"] == ["existing action"]
        # facts: old kept (founders), new appended tagged with the meeting tier (ceo)
        texts = {f["text"]: f["sensitivity"] for f in brief["facts"]}
        assert texts["old fact"] == "founders"
        assert texts["new fact"] == "ceo"
        # newest decision prepended; version bumped; tier escalated
        assert brief["recent_decisions"][0]["text"] == "go Gagauzia"
        assert brief["version"] == 4
        assert brief["sensitivity"] == "ceo"
        # links: belongs_to (topic->area) + 2 advances (decision/task->topic)
        link_types = [a[4] for a in links]
        assert "belongs_to" in link_types
        assert link_types.count("advances") == 2

    def test_never_raises(self, monkeypatch):
        def _boom(*a, **k):
            raise RuntimeError("db down")

        monkeypatch.setattr(tt.supabase_client, "update_topic_brief", _boom)
        monkeypatch.setattr(tt.supabase_client, "create_knowledge_link", lambda *a, **k: None)
        # Must swallow — the approval flow must never break on a brief-sync failure.
        tt._sync_brief_from_state(
            {"id": "t1", "brief_json": {}},
            {"summary": "s", "key_facts": []},
            {"id": "m1", "sensitivity": "founders"},
            [], [],
        )
