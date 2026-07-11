"""Phase 2 PR C (groundwork) — DecisionBrief (decisions.brief_json).

A decision's living-state object, assembled deterministically (no LLM) from the
decision + its supersession chain, refreshed on approval. Groundwork for the
later weekly decision-synthesis phase.
"""
from unittest.mock import MagicMock

import processors.decision_intelligence as di
from models.schemas import DecisionBrief


class TestDecisionBriefModel:
    def test_defaults(self):
        b = DecisionBrief(summary="Ship it")
        d = b.model_dump(mode="json")
        assert d["status"] == "active" and d["chain_length"] == 1
        assert d["supersedes"] == [] and d["version"] == 1


class TestBuildDecisionBrief:
    def test_builds_and_persists_with_chain(self, monkeypatch):
        sc = MagicMock()
        sc.get_decision.return_value = {
            "id": "d2", "description": "Use PG16", "decision_status": "active",
            "rationale": "perf", "superseded_by": None, "last_referenced_at": None,
            "sensitivity": "founders",
        }
        sc.get_decision_chain.return_value = [{"id": "d1"}, {"id": "d2"}]  # d2 replaced d1
        monkeypatch.setattr(di, "supabase_client", sc)

        payload = di.build_decision_brief("d2")
        assert payload is not None
        assert payload["summary"] == "Use PG16"
        assert payload["supersedes"] == ["d1"]           # ancestor before d2 in the chain
        assert payload["chain_length"] == 2
        # persisted to brief_json
        call = sc.update_decision.call_args
        assert call.args[0] == "d2" and "brief_json" in call.kwargs

    def test_unknown_sensitivity_defaults_founders(self, monkeypatch):
        sc = MagicMock()
        sc.get_decision.return_value = {"id": "d1", "description": "x", "sensitivity": "normal"}
        sc.get_decision_chain.return_value = [{"id": "d1"}]
        monkeypatch.setattr(di, "supabase_client", sc)
        assert di.build_decision_brief("d1")["sensitivity"] == "founders"

    def test_missing_decision_returns_none(self, monkeypatch):
        sc = MagicMock()
        sc.get_decision.return_value = None
        monkeypatch.setattr(di, "supabase_client", sc)
        assert di.build_decision_brief("d1") is None
        sc.update_decision.assert_not_called()

    def test_chain_failure_is_tolerated(self, monkeypatch):
        sc = MagicMock()
        sc.get_decision.return_value = {"id": "d1", "description": "x", "decision_status": "active"}
        sc.get_decision_chain.side_effect = RuntimeError("boom")
        monkeypatch.setattr(di, "supabase_client", sc)
        payload = di.build_decision_brief("d1")
        assert payload is not None and payload["chain_length"] == 1


class TestRefreshBriefsForMeeting:
    def test_refreshes_each_decision_and_parent_deduped(self, monkeypatch):
        sc = MagicMock()
        sc.list_decisions.return_value = [
            {"id": "d2", "parent_decision_id": "d1"},  # d2 superseded d1
            {"id": "d3", "parent_decision_id": None},
        ]
        sc.get_decision.side_effect = lambda did: {"id": did, "description": "x", "decision_status": "active"}
        sc.get_decision_chain.return_value = []
        monkeypatch.setattr(di, "supabase_client", sc)

        n = di.refresh_decision_briefs_for_meeting("m1")
        assert n == 3                                    # d2, its parent d1, and d3
        built = {c.args[0] for c in sc.update_decision.call_args_list}
        assert built == {"d1", "d2", "d3"}
