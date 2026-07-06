"""Telegram /sync proposal-review flow (topic merges / task tweaks).

[proposal-review 2026-07-06]
"""

from unittest.mock import MagicMock

import pytest

import processors.proposal_review as pr


def _chain(rows):
    """Build a supabase client mock whose select chain returns `rows`."""
    m = MagicMock()
    (m.client.table.return_value.select.return_value.eq.return_value
     .in_.return_value.order.return_value.execute.return_value.data) = rows
    return m


def test_list_pending_proposals_labels(monkeypatch):
    rows = [
        {"approval_id": "kprop-1", "content_type": "topic_merge",
         "content": {"loser_name": "Investor Deck Flow", "winner_name": "Investor Deck"}},
        {"approval_id": "tprop-2", "content_type": "task_update_proposal",
         "content": {"field": "deadline", "proposed": "2026-08-01"}},
    ]
    monkeypatch.setattr(pr, "supabase_client", _chain(rows))
    props = pr.list_pending_proposals()
    assert [p["proposal_id"] for p in props] == ["kprop-1", "tprop-2"]
    assert "Investor Deck Flow" in props[0]["label"] and "Investor Deck" in props[0]["label"]
    assert "deadline" in props[1]["label"]


def test_apply_topic_merge_approve_merges_and_clears(monkeypatch):
    fake = MagicMock()
    fake.get_pending_approval.return_value = {
        "content_type": "topic_merge",
        "content": {"loser_name": "A", "winner_name": "B"},
    }
    monkeypatch.setattr(pr, "supabase_client", fake)
    monkeypatch.setattr("processors.topic_clustering.apply_topic_proposal",
                        lambda c: {"merged": "loser", "into": "winner"})
    res = pr.apply_proposal_decision("kprop-1", "approve")
    assert res["decision"] == "approved"
    assert res["result"] == {"merged": "loser", "into": "winner"}
    fake.delete_pending_approval.assert_called_once_with("kprop-1")
    fake.log_action.assert_called_once()


def test_apply_topic_merge_reject_does_not_merge(monkeypatch):
    fake = MagicMock()
    fake.get_pending_approval.return_value = {"content_type": "topic_merge", "content": {}}
    monkeypatch.setattr(pr, "supabase_client", fake)
    called = {"merged": False}
    monkeypatch.setattr("processors.topic_clustering.apply_topic_proposal",
                        lambda c: called.__setitem__("merged", True))
    res = pr.apply_proposal_decision("kprop-1", "reject")
    assert res["decision"] == "rejected"
    assert called["merged"] is False  # reject must NOT apply the merge
    fake.delete_pending_approval.assert_called_once_with("kprop-1")


def test_apply_task_update_approve_writes_field(monkeypatch):
    fake = MagicMock()
    fake.get_pending_approval.return_value = {
        "content_type": "task_update_proposal",
        "content": {"task_id": "t1", "field": "deadline", "proposed": "2026-08-01"},
    }
    monkeypatch.setattr(pr, "supabase_client", fake)
    res = pr.apply_proposal_decision("tprop-1", "approve")
    assert res["decision"] == "approved"
    fake.update_task.assert_called_once()
    kw = fake.update_task.call_args
    assert kw.args[0] == "t1"
    assert kw.kwargs.get("deadline") == "2026-08-01"
    assert kw.kwargs.get("deadline_confidence") == "EXPLICIT"


def test_apply_decision_gone(monkeypatch):
    fake = MagicMock()
    fake.get_pending_approval.return_value = None
    monkeypatch.setattr(pr, "supabase_client", fake)
    assert pr.apply_proposal_decision("kprop-x", "approve")["status"] == "gone"
    fake.delete_pending_approval.assert_not_called()
