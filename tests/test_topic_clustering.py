"""
Tests for the v2.5 PR10 topic clustering -> consolidation proposals.

No live DB: Jaccard similarity, merge-proposal generation (patched client), and
apply_topic_proposal's structural moves (re-point + supersede + close, or
assign), including the same-topic guard.
"""

from types import SimpleNamespace

import pytest

try:
    import processors.topic_clustering as tc
except Exception as e:
    pytest.skip(f"cannot import topic_clustering ({e})", allow_module_level=True)


def test_jaccard():
    assert tc._jaccard("Moldova Pilot", "Moldova Pilot Project") == 1.0  # 'project' is a stopword
    assert tc._jaccard("Moldova", "Fundraising") == 0.0


class _SelectChain:
    def __init__(self, data):
        object.__setattr__(self, "data_rows", data)

    def __getattr__(self, name):
        if name.startswith("__"):
            raise AttributeError(name)
        return lambda *a, **k: self

    def execute(self):
        return SimpleNamespace(data=self.data_rows)


class TestPropose:
    async def test_proposes_merge_for_near_duplicates(self, monkeypatch):
        topics = [
            {"id": "t1", "topic_name": "Moldova Pilot", "area_id": "a1", "meeting_count": 5, "status": "active"},
            {"id": "t2", "topic_name": "Moldova Pilot Project", "area_id": "a1", "meeting_count": 2, "status": "active"},
        ]
        monkeypatch.setattr(tc.supabase_client, "_client", SimpleNamespace(table=lambda *a, **k: _SelectChain(topics)))
        monkeypatch.setattr(tc.supabase_client, "get_pending_approvals_by_status", lambda s: [])
        monkeypatch.setattr(tc.supabase_client, "get_areas", lambda: [])
        stored = []
        monkeypatch.setattr(tc.supabase_client, "create_pending_approval", lambda **k: stored.append(k))

        res = await tc.propose_topic_consolidation(max_proposals=3)
        assert res["created"] == 1
        p = res["proposals"][0]
        assert p["proposal_type"] == "topic_merge"
        assert p["winner_id"] == "t1" and p["loser_id"] == "t2"  # winner = more meetings
        assert stored and stored[0]["content_type"] == "topic_merge"

    async def test_skips_existing_proposal_keys(self, monkeypatch):
        topics = [
            {"id": "t1", "topic_name": "Moldova Pilot", "meeting_count": 5, "status": "active"},
            {"id": "t2", "topic_name": "Moldova Pilot", "meeting_count": 2, "status": "active"},
        ]
        monkeypatch.setattr(tc.supabase_client, "_client", SimpleNamespace(table=lambda *a, **k: _SelectChain(topics)))
        # Pretend a merge proposal for the loser already exists.
        monkeypatch.setattr(tc.supabase_client, "get_pending_approvals_by_status",
                            lambda s: [{"content_type": "topic_merge", "content": {"key": "merge:t2"}}])
        monkeypatch.setattr(tc.supabase_client, "get_areas", lambda: [])
        monkeypatch.setattr(tc.supabase_client, "create_pending_approval", lambda **k: None)

        res = await tc.propose_topic_consolidation(max_proposals=3)
        assert res["created"] == 0  # de-duplicated against the existing proposal


class _UpdateChain:
    def __init__(self, name, sink):
        self.name = name
        self.sink = sink
        self.payload = None

    def update(self, payload):
        self.payload = payload
        return self

    def eq(self, *a, **k):
        return self

    def execute(self):
        self.sink.append((self.name, self.payload))
        return SimpleNamespace(data=[])


class TestApply:
    def test_merge_repoints_supersedes_and_closes(self, monkeypatch):
        updates = []
        links = []
        monkeypatch.setattr(tc.supabase_client, "_client",
                            SimpleNamespace(table=lambda name: _UpdateChain(name, updates)))
        monkeypatch.setattr(tc.supabase_client, "create_knowledge_link", lambda *a, **k: links.append(a))

        res = tc.apply_topic_proposal({"proposal_type": "topic_merge", "winner_id": "w", "loser_id": "l"})
        assert res == {"merged": "l", "into": "w"}
        # mentions re-pointed to winner
        assert ("topic_thread_mentions", {"topic_id": "w"}) in updates
        # loser closed bi-temporally
        closed = [p for n, p in updates if n == "topic_threads"][0]
        assert closed["status"] == "closed" and "valid_to" in closed
        # supersedes link recorded
        assert any(a[4] == "supersedes" for a in links)

    def test_merge_same_topic_guard(self):
        res = tc.apply_topic_proposal({"proposal_type": "topic_merge", "winner_id": "x", "loser_id": "x"})
        assert "error" in res

    def test_assign_sets_area(self, monkeypatch):
        set_calls = []
        monkeypatch.setattr(tc.supabase_client, "set_topic_area", lambda tid, aid: set_calls.append((tid, aid)))
        monkeypatch.setattr(tc.supabase_client, "create_knowledge_link", lambda *a, **k: None)
        res = tc.apply_topic_proposal({"proposal_type": "topic_assign", "topic_id": "t", "area_id": "a"})
        assert res == {"assigned": "t", "to": "a"}
        assert set_calls == [("t", "a")]
