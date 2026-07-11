"""Phase 2 PR C — propose-don't-clobber for decisions (decision_update_proposal).

Covers the shared apply (apply_decision_update), the inference guard
(propose_or_update_decision_field), and the Telegram /sync consumer. Mirrors
test_decision_intelligence.py (monkeypatch the module-level supabase_client).
"""
from unittest.mock import MagicMock

import processors.decision_intelligence as di
import processors.proposal_review as pr


class TestApplyDecisionUpdate:
    def _content(self, **kw):
        base = {"decision_id": "d1", "field": "description", "proposed": "New text"}
        base.update(kw)
        return base

    def test_approve_updates_and_marks_sticky(self, monkeypatch):
        sc = MagicMock()
        sc.get_decision.return_value = {"id": "d1"}
        monkeypatch.setattr(di, "supabase_client", sc)
        res = di.apply_decision_update(self._content(), approve=True)
        assert res["status"] == "applied"
        sc.update_decision.assert_called_once_with("d1", description="New text")
        sc.mark_decision_field_manual.assert_called_once_with("d1", "description", "eyal")

    def test_status_field_maps_to_decision_status_column(self, monkeypatch):
        sc = MagicMock()
        sc.get_decision.return_value = {"id": "d1"}
        monkeypatch.setattr(di, "supabase_client", sc)
        di.apply_decision_update(self._content(field="status", proposed="superseded"), approve=True)
        sc.update_decision.assert_called_once_with("d1", decision_status="superseded")
        sc.mark_decision_field_manual.assert_called_once_with("d1", "status", "eyal")

    def test_reject_does_nothing(self, monkeypatch):
        sc = MagicMock()
        monkeypatch.setattr(di, "supabase_client", sc)
        res = di.apply_decision_update(self._content(), approve=False)
        assert res["status"] == "rejected"
        sc.update_decision.assert_not_called()
        sc.get_decision.assert_not_called()

    def test_gone_is_safe(self, monkeypatch):
        sc = MagicMock()
        sc.get_decision.return_value = None
        monkeypatch.setattr(di, "supabase_client", sc)
        assert di.apply_decision_update(self._content(), approve=True)["status"] == "gone"
        sc.update_decision.assert_not_called()

    def test_unknown_field_invalid(self, monkeypatch):
        sc = MagicMock()
        monkeypatch.setattr(di, "supabase_client", sc)
        res = di.apply_decision_update(self._content(field="bogus"), approve=True)
        assert res["status"] == "invalid"
        sc.update_decision.assert_not_called()

    def test_missing_ids_invalid(self, monkeypatch):
        sc = MagicMock()
        monkeypatch.setattr(di, "supabase_client", sc)
        assert di.apply_decision_update({"field": "description"}, approve=True)["status"] == "invalid"


class TestProposeOrUpdateGuard:
    def test_sticky_field_proposes_not_clobbers(self, monkeypatch):
        sc = MagicMock()
        sc.get_decision.return_value = {"id": "d1", "description": "Old", "manual_description": True}
        monkeypatch.setattr(di, "supabase_client", sc)
        out = di.propose_or_update_decision_field("d1", "description", "New", source="continuity")
        assert out == "proposed"
        sc.create_decision_update_proposal.assert_called_once()
        sc.update_decision.assert_not_called()

    def test_nonsticky_field_updates_directly(self, monkeypatch):
        sc = MagicMock()
        sc.get_decision.return_value = {"id": "d1", "description": "Old", "manual_description": False}
        monkeypatch.setattr(di, "supabase_client", sc)
        out = di.propose_or_update_decision_field("d1", "description", "New")
        assert out == "updated"
        sc.update_decision.assert_called_once_with("d1", description="New")
        sc.create_decision_update_proposal.assert_not_called()

    def test_sticky_status_proposes_with_current_column(self, monkeypatch):
        sc = MagicMock()
        sc.get_decision.return_value = {"id": "d1", "decision_status": "active", "manual_status": True}
        monkeypatch.setattr(di, "supabase_client", sc)
        out = di.propose_or_update_decision_field("d1", "status", "superseded")
        assert out == "proposed"
        kw = sc.create_decision_update_proposal.call_args.kwargs
        assert kw["field"] == "status" and kw["proposed"] == "superseded"
        assert kw["current"] == "active"          # read from the decision_status column

    def test_unknown_field_noop(self, monkeypatch):
        sc = MagicMock()
        monkeypatch.setattr(di, "supabase_client", sc)
        assert di.propose_or_update_decision_field("d1", "bogus", "x") == "noop"
        sc.get_decision.assert_not_called()

    def test_missing_decision_noop(self, monkeypatch):
        sc = MagicMock()
        sc.get_decision.return_value = None
        monkeypatch.setattr(di, "supabase_client", sc)
        assert di.propose_or_update_decision_field("d1", "description", "x") == "noop"
        sc.update_decision.assert_not_called()


class TestProposalReviewConsumer:
    def test_reviewable_types_includes_update(self):
        assert "decision_update_proposal" in pr.REVIEWABLE_TYPES

    def test_label_renders(self):
        lbl = pr._label("decision_update_proposal",
                        {"field": "description", "proposed": "New", "summary": "Ship it"})
        assert "description" in lbl and "New" in lbl

    def test_apply_approve_calls_shared_apply(self, monkeypatch):
        sc = MagicMock()
        sc.get_pending_approval.return_value = {
            "content_type": "decision_update_proposal",
            "content": {"decision_id": "d1", "field": "description", "proposed": "New"},
        }
        sc.get_decision.return_value = {"id": "d1"}
        monkeypatch.setattr(pr, "supabase_client", sc)
        monkeypatch.setattr(di, "supabase_client", sc)   # shared apply uses di's client
        res = pr.apply_proposal_decision("decupd-d1-description", "approve")
        assert res["status"] == "ok" and res["decision"] == "approved"
        sc.update_decision.assert_called_once()
        sc.delete_pending_approval.assert_called_once()

    def test_apply_reject_writes_nothing(self, monkeypatch):
        sc = MagicMock()
        sc.get_pending_approval.return_value = {
            "content_type": "decision_update_proposal",
            "content": {"decision_id": "d1", "field": "description", "proposed": "New"},
        }
        monkeypatch.setattr(pr, "supabase_client", sc)
        monkeypatch.setattr(di, "supabase_client", sc)
        res = pr.apply_proposal_decision("decupd-d1-description", "reject")
        assert res["decision"] == "rejected"
        sc.update_decision.assert_not_called()
        sc.delete_pending_approval.assert_called_once()
