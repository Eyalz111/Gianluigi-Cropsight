"""Tests for the cross-decision clustering -> merge/relate proposals (synthesis phase).

No live DB: Jaccard, proposal generation (patched client chain), the bi-temporal
merge apply + relate apply + guards, and the new get_related_decisions reader.
Mirrors tests/test_topic_clustering.py.
"""
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

try:
    import processors.decision_clustering as dc
except Exception as e:
    pytest.skip(f"cannot import decision_clustering ({e})", allow_module_level=True)


def test_jaccard():
    assert dc._jaccard("Use Postgres for storage", "Use Postgres for storage") == 1.0
    assert dc._jaccard("Ship the MVP", "Hire an advisor") == 0.0


class _SelectChain:
    def __init__(self, data):
        object.__setattr__(self, "data_rows", data)

    def __getattr__(self, name):
        if name.startswith("__"):
            raise AttributeError(name)
        return lambda *a, **k: self

    def execute(self):
        return SimpleNamespace(data=self.data_rows)


def _dec(id, desc, created, label=""):
    return {"id": id, "label": label, "description": desc, "created_at": created,
            "decision_status": "active", "approval_status": "approved"}


class TestPropose:
    async def test_near_duplicate_proposes_merge_newer_wins(self, monkeypatch):
        decisions = [
            _dec("d1", "Use Postgres for storage", "2026-07-10"),  # newer
            _dec("d2", "Use Postgres for storage", "2026-07-01"),  # older
        ]
        monkeypatch.setattr(dc.supabase_client, "_client",
                            SimpleNamespace(table=lambda *a, **k: _SelectChain(decisions)))
        monkeypatch.setattr(dc.supabase_client, "get_pending_approvals_by_status", lambda s: [])
        stored = []
        monkeypatch.setattr(dc.supabase_client, "create_pending_approval", lambda **k: stored.append(k))
        res = await dc.propose_decision_consolidation(max_proposals=3)
        assert res["created"] == 1
        p = res["proposals"][0]
        assert p["proposal_type"] == "decision_merge"
        assert p["winner_id"] == "d1" and p["loser_id"] == "d2"   # newer wins
        assert stored[0]["content_type"] == "decision_merge"

    async def test_moderate_overlap_proposes_relate(self, monkeypatch):
        decisions = [
            _dec("d1", "hire senior ML engineer for the team", "2026-07-10"),
            _dec("d2", "hire ML engineer contractor", "2026-07-01"),
        ]
        monkeypatch.setattr(dc.supabase_client, "_client",
                            SimpleNamespace(table=lambda *a, **k: _SelectChain(decisions)))
        monkeypatch.setattr(dc.supabase_client, "get_pending_approvals_by_status", lambda s: [])
        monkeypatch.setattr(dc.supabase_client, "create_pending_approval", lambda **k: None)
        res = await dc.propose_decision_consolidation(max_proposals=3)
        assert res["created"] == 1
        assert res["proposals"][0]["proposal_type"] == "decision_relate"

    async def test_skips_existing_keys(self, monkeypatch):
        decisions = [_dec("d1", "Use Postgres", "2026-07-10"), _dec("d2", "Use Postgres", "2026-07-01")]
        monkeypatch.setattr(dc.supabase_client, "_client",
                            SimpleNamespace(table=lambda *a, **k: _SelectChain(decisions)))
        monkeypatch.setattr(dc.supabase_client, "get_pending_approvals_by_status",
                            lambda s: [{"content_type": "decision_merge", "content": {"key": "merge:d2:d1"}}])
        monkeypatch.setattr(dc.supabase_client, "create_pending_approval", lambda **k: None)
        res = await dc.propose_decision_consolidation(max_proposals=3)
        assert res["created"] == 0

    async def test_max_proposals_cap(self, monkeypatch):
        decisions = [_dec(f"d{i}", "Use Postgres for storage", f"2026-07-0{i}") for i in range(1, 4)]
        monkeypatch.setattr(dc.supabase_client, "_client",
                            SimpleNamespace(table=lambda *a, **k: _SelectChain(decisions)))
        monkeypatch.setattr(dc.supabase_client, "get_pending_approvals_by_status", lambda s: [])
        monkeypatch.setattr(dc.supabase_client, "create_pending_approval", lambda **k: None)
        res = await dc.propose_decision_consolidation(max_proposals=1)
        assert res["created"] == 1


class TestApplyMerge:
    def test_supersedes_closes_links(self, monkeypatch):
        calls = {"sup": [], "closed": [], "links": []}
        monkeypatch.setattr(dc.supabase_client, "get_decision", lambda did: {"id": did, "decision_status": "active"})
        monkeypatch.setattr(dc.supabase_client, "mark_decision_superseded", lambda o, n: calls["sup"].append((o, n)))
        monkeypatch.setattr(dc.supabase_client, "supersede_decision",
                            lambda did, superseded_by=None: calls["closed"].append((did, superseded_by)))
        monkeypatch.setattr(dc.supabase_client, "create_knowledge_link", lambda *a, **k: calls["links"].append(a))
        res = dc.apply_decision_merge({"winner_id": "w", "loser_id": "l"})
        assert res["status"] == "applied" and res["merged"] == "l" and res["into"] == "w"
        assert calls["sup"] == [("l", "w")] and calls["closed"] == [("l", "w")]
        assert any(a[4] == "supersedes" for a in calls["links"])

    def test_same_id_invalid(self):
        assert dc.apply_decision_merge({"winner_id": "x", "loser_id": "x"})["status"] == "invalid"

    def test_gone(self, monkeypatch):
        monkeypatch.setattr(dc.supabase_client, "get_decision", lambda did: None)
        assert dc.apply_decision_merge({"winner_id": "w", "loser_id": "l"})["status"] == "gone"

    def test_already_superseded(self, monkeypatch):
        monkeypatch.setattr(dc.supabase_client, "get_decision", lambda did: {"decision_status": "superseded"})
        assert dc.apply_decision_merge({"winner_id": "w", "loser_id": "l"})["status"] == "already_superseded"


class TestApplyRelate:
    def test_two_directional_links(self, monkeypatch):
        links = []
        monkeypatch.setattr(dc.supabase_client, "get_decision", lambda did: {"id": did})
        monkeypatch.setattr(dc.supabase_client, "create_knowledge_link", lambda *a, **k: links.append(a))
        res = dc.apply_decision_relate({"a_id": "a", "b_id": "b"})
        assert res["status"] == "applied"
        assert links[0] == ("decision", "a", "decision", "b", "relates_to")
        assert links[1] == ("decision", "b", "decision", "a", "relates_to")

    def test_gone(self, monkeypatch):
        monkeypatch.setattr(dc.supabase_client, "get_decision", lambda did: None)
        assert dc.apply_decision_relate({"a_id": "a", "b_id": "b"})["status"] == "gone"

    def test_dispatch_reject(self):
        assert dc.apply_decision_cluster_proposal({"proposal_type": "decision_merge"}, approve=False)["status"] == "rejected"


class TestGetRelatedDecisions:
    def test_resolves_both_directions(self, monkeypatch):
        from services.supabase_client import supabase_client as sc
        def fake_links(**k):
            if k.get("from_id") == "d1":
                return [{"to_type": "decision", "to_id": "d2"}]
            if k.get("to_id") == "d1":
                return [{"from_type": "decision", "from_id": "d3"}]
            return []
        monkeypatch.setattr(sc, "get_knowledge_links", fake_links)
        monkeypatch.setattr(sc, "get_decision", lambda did: {"id": did})
        out = sc.get_related_decisions("d1", ("relates_to",))
        assert {d["id"] for d in out} == {"d2", "d3"}

    def test_excludes_non_decision_endpoint(self, monkeypatch):
        from services.supabase_client import supabase_client as sc
        monkeypatch.setattr(sc, "get_knowledge_links",
                            lambda **k: [{"to_type": "area", "to_id": "a1"}] if k.get("from_id") == "d1" else [])
        monkeypatch.setattr(sc, "get_decision", lambda did: {"id": did})
        assert sc.get_related_decisions("d1", ("relates_to",)) == []


class TestConsumerWiring:
    def test_reviewable_types_and_labels(self):
        import processors.proposal_review as pr
        assert "decision_merge" in pr.REVIEWABLE_TYPES and "decision_relate" in pr.REVIEWABLE_TYPES
        assert "KEEP" in pr._label("decision_merge", {"winner_summary": "A", "loser_summary": "B"})
        assert "↔" in pr._label("decision_relate", {"a_summary": "A", "b_summary": "B"})

    def test_sync_apply_merge_approve(self, monkeypatch):
        import processors.proposal_review as pr
        sc = MagicMock()
        sc.get_pending_approval.return_value = {
            "content_type": "decision_merge",
            "content": {"proposal_type": "decision_merge", "winner_id": "w", "loser_id": "l"}}
        sc.get_decision.return_value = {"id": "l", "decision_status": "active"}
        monkeypatch.setattr(pr, "supabase_client", sc)
        monkeypatch.setattr(dc, "supabase_client", sc)   # the shared apply uses dc's client
        res = pr.apply_proposal_decision("dprop-x", "approve")
        assert res["status"] == "ok" and res["decision"] == "approved"
        sc.mark_decision_superseded.assert_called_once_with("l", "w")
        sc.delete_pending_approval.assert_called_once()

    def test_sync_apply_relate_reject_writes_nothing(self, monkeypatch):
        import processors.proposal_review as pr
        sc = MagicMock()
        sc.get_pending_approval.return_value = {
            "content_type": "decision_relate",
            "content": {"proposal_type": "decision_relate", "a_id": "a", "b_id": "b"}}
        monkeypatch.setattr(pr, "supabase_client", sc)
        monkeypatch.setattr(dc, "supabase_client", sc)
        res = pr.apply_proposal_decision("dprop-y", "reject")
        assert res["decision"] == "rejected"
        sc.create_knowledge_link.assert_not_called()
        sc.delete_pending_approval.assert_called_once()
