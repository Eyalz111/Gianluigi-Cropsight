"""Phase 1 Step 3 (P1): the task_update_proposal PRODUCER helper.

supabase_client.create_task_update_proposal emits a proposal for a sticky field
inference wants to change — consumed by decide_proposal / the /sync review /
morning brief. Idempotent per (task, field) so re-running inference doesn't stack
duplicate cards.
"""
from unittest.mock import MagicMock

from services.supabase_client import supabase_client


def _client(existing_rows):
    """Mock whose pending_approvals select chain returns existing_rows."""
    m = MagicMock()
    (m.table.return_value.select.return_value.eq.return_value
     .eq.return_value.execute.return_value.data) = existing_rows
    return m


def test_producer_skips_when_open_proposal_exists(monkeypatch):
    monkeypatch.setattr(supabase_client, "_client",
                        _client([{"approval_id": "taskprop-t1-status"}]))
    cp = MagicMock()
    monkeypatch.setattr(supabase_client, "create_pending_approval", cp)

    ok = supabase_client.create_task_update_proposal(
        "t1", "status", "done", title="X", current="pending", source="meeting:m1")

    assert ok is True
    cp.assert_not_called()  # one already open -> no duplicate card


def test_producer_creates_when_none_open(monkeypatch):
    monkeypatch.setattr(supabase_client, "_client", _client([]))
    cp = MagicMock()
    monkeypatch.setattr(supabase_client, "create_pending_approval", cp)

    ok = supabase_client.create_task_update_proposal(
        "t1", "status", "done", title="X", current="pending", source="meeting:m1")

    assert ok is True
    cp.assert_called_once()
    kw = cp.call_args.kwargs
    assert kw["content_type"] == "task_update_proposal"
    assert kw["approval_id"] == "taskprop-t1-status"
    c = kw["content"]
    assert c["task_id"] == "t1" and c["field"] == "status" and c["proposed"] == "done"
    assert c["current"] == "pending" and c["title"] == "X" and c["source"] == "meeting:m1"
