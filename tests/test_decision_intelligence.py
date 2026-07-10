"""Phase 2: decision supersession raised for Eyal's approval (never auto-flip).

Covers the producer (propose_supersessions_for_meeting), the shared apply logic
(apply_decision_supersede), the idempotent proposal helper, and both consumer
surfaces (Telegram /sync review).
"""
from unittest.mock import MagicMock

import processors.decision_intelligence as di
import processors.proposal_review as pr
from services.supabase_client import supabase_client


# ---------------------------------------------------------------------------
# Producer — propose_supersessions_for_meeting
# ---------------------------------------------------------------------------
class TestProposeSupersessions:
    def test_proposes_for_active_parent(self, monkeypatch):
        sc = MagicMock()
        sc.list_decisions.return_value = [
            {"id": "new1", "parent_decision_id": "old1", "description": "New decision"},
        ]
        sc.get_decision.return_value = {
            "id": "old1", "decision_status": "active", "description": "Old decision"}
        sc.create_decision_supersede_proposal.return_value = True
        monkeypatch.setattr(di, "supabase_client", sc)

        n = di.propose_supersessions_for_meeting("m1")

        assert n == 1
        kw = sc.create_decision_supersede_proposal.call_args.kwargs
        assert kw["new_id"] == "new1" and kw["old_id"] == "old1"
        assert kw["old_summary"] == "Old decision" and kw["new_summary"] == "New decision"

    def test_skips_decision_without_parent(self, monkeypatch):
        sc = MagicMock()
        sc.list_decisions.return_value = [{"id": "d1", "parent_decision_id": None}]
        monkeypatch.setattr(di, "supabase_client", sc)

        assert di.propose_supersessions_for_meeting("m1") == 0
        sc.create_decision_supersede_proposal.assert_not_called()

    def test_skips_when_parent_already_superseded(self, monkeypatch):
        sc = MagicMock()
        sc.list_decisions.return_value = [{"id": "new1", "parent_decision_id": "old1"}]
        sc.get_decision.return_value = {"id": "old1", "decision_status": "superseded"}
        monkeypatch.setattr(di, "supabase_client", sc)

        assert di.propose_supersessions_for_meeting("m1") == 0
        sc.create_decision_supersede_proposal.assert_not_called()

    def test_skips_when_parent_gone(self, monkeypatch):
        sc = MagicMock()
        sc.list_decisions.return_value = [{"id": "new1", "parent_decision_id": "old1"}]
        sc.get_decision.return_value = None
        monkeypatch.setattr(di, "supabase_client", sc)

        assert di.propose_supersessions_for_meeting("m1") == 0
        sc.create_decision_supersede_proposal.assert_not_called()


# ---------------------------------------------------------------------------
# Apply — apply_decision_supersede (shared by both surfaces)
# ---------------------------------------------------------------------------
class TestApplyDecisionSupersede:
    def _content(self):
        return {"old_decision_id": "old1", "new_decision_id": "new1"}

    def test_approve_marks_superseded_and_links(self, monkeypatch):
        sc = MagicMock()
        sc.get_decision.return_value = {"id": "old1", "decision_status": "active"}
        monkeypatch.setattr(di, "supabase_client", sc)

        res = di.apply_decision_supersede(self._content(), approve=True)

        assert res["status"] == "applied"
        sc.mark_decision_superseded.assert_called_once_with("old1", "new1")
        lk = sc.create_knowledge_link.call_args.kwargs
        assert lk["from_type"] == "decision" and lk["from_id"] == "new1"
        assert lk["to_type"] == "decision" and lk["to_id"] == "old1"
        assert lk["link_type"] == "supersedes"

    def test_reject_does_nothing(self, monkeypatch):
        sc = MagicMock()
        monkeypatch.setattr(di, "supabase_client", sc)

        res = di.apply_decision_supersede(self._content(), approve=False)

        assert res["status"] == "rejected"
        sc.mark_decision_superseded.assert_not_called()
        sc.get_decision.assert_not_called()

    def test_approve_gone_is_safe(self, monkeypatch):
        sc = MagicMock()
        sc.get_decision.return_value = None
        monkeypatch.setattr(di, "supabase_client", sc)

        res = di.apply_decision_supersede(self._content(), approve=True)

        assert res["status"] == "gone"
        sc.mark_decision_superseded.assert_not_called()

    def test_approve_already_superseded_is_noop(self, monkeypatch):
        sc = MagicMock()
        sc.get_decision.return_value = {"decision_status": "superseded"}
        monkeypatch.setattr(di, "supabase_client", sc)

        res = di.apply_decision_supersede(self._content(), approve=True)

        assert res["status"] == "already_superseded"
        sc.mark_decision_superseded.assert_not_called()

    def test_approve_missing_ids_invalid(self, monkeypatch):
        sc = MagicMock()
        monkeypatch.setattr(di, "supabase_client", sc)

        res = di.apply_decision_supersede({"old_decision_id": "old1"}, approve=True)
        assert res["status"] == "invalid"
        sc.mark_decision_superseded.assert_not_called()


# ---------------------------------------------------------------------------
# Producer helper idempotency — create_decision_supersede_proposal
# ---------------------------------------------------------------------------
def _client(existing_rows):
    m = MagicMock()
    (m.table.return_value.select.return_value.eq.return_value
     .eq.return_value.execute.return_value.data) = existing_rows
    return m


class TestProposalHelperIdempotency:
    def test_skips_when_open_proposal_exists(self, monkeypatch):
        monkeypatch.setattr(supabase_client, "_client",
                            _client([{"approval_id": "decprop-old1-new1"}]))
        cp = MagicMock()
        monkeypatch.setattr(supabase_client, "create_pending_approval", cp)

        assert supabase_client.create_decision_supersede_proposal("new1", "old1") is True
        cp.assert_not_called()

    def test_creates_when_none_open(self, monkeypatch):
        monkeypatch.setattr(supabase_client, "_client", _client([]))
        cp = MagicMock()
        monkeypatch.setattr(supabase_client, "create_pending_approval", cp)

        ok = supabase_client.create_decision_supersede_proposal(
            "new1", "old1", new_summary="N", old_summary="O", source="meeting:m1")

        assert ok is True
        cp.assert_called_once()
        kw = cp.call_args.kwargs
        assert kw["content_type"] == "decision_supersede_proposal"
        assert kw["approval_id"] == "decprop-old1-new1"
        c = kw["content"]
        assert c["old_decision_id"] == "old1" and c["new_decision_id"] == "new1"
        assert c["old_summary"] == "O" and c["new_summary"] == "N"


# ---------------------------------------------------------------------------
# Consumer — Telegram /sync review
# ---------------------------------------------------------------------------
class TestProposalReviewIntegration:
    def test_label_renders_old_and_new(self):
        lbl = pr._label(
            "decision_supersede_proposal",
            {"old_summary": "Use AWS", "new_summary": "Use GCP"},
        )
        assert "Use AWS" in lbl and "Use GCP" in lbl

    def test_type_is_reviewable(self):
        assert "decision_supersede_proposal" in pr.REVIEWABLE_TYPES

    def test_approve_applies_and_clears(self, monkeypatch):
        sc = MagicMock()
        sc.get_pending_approval.return_value = {
            "content_type": "decision_supersede_proposal",
            "content": {"old_decision_id": "old1", "new_decision_id": "new1"},
        }
        sc.get_decision.return_value = {"id": "old1", "decision_status": "active"}
        monkeypatch.setattr(pr, "supabase_client", sc)
        monkeypatch.setattr(di, "supabase_client", sc)

        res = pr.apply_proposal_decision("decprop-old1-new1", "approve")

        assert res["status"] == "ok" and res["decision"] == "approved"
        sc.mark_decision_superseded.assert_called_once_with("old1", "new1")
        sc.delete_pending_approval.assert_called_once_with("decprop-old1-new1")

    def test_reject_clears_without_applying(self, monkeypatch):
        sc = MagicMock()
        sc.get_pending_approval.return_value = {
            "content_type": "decision_supersede_proposal",
            "content": {"old_decision_id": "old1", "new_decision_id": "new1"},
        }
        monkeypatch.setattr(pr, "supabase_client", sc)
        monkeypatch.setattr(di, "supabase_client", sc)

        res = pr.apply_proposal_decision("decprop-old1-new1", "reject")

        assert res["status"] == "ok" and res["decision"] == "rejected"
        sc.mark_decision_superseded.assert_not_called()
        sc.delete_pending_approval.assert_called_once()
