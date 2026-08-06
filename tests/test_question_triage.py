"""Weekly open-question re-triage.

Exists because question resolution only ran at INGESTION against the single
meeting being processed, so a question answered three meetings later stayed open
forever — 69 open at 2026-08-06, 9 already settled. This PROPOSES closures and
never closes anything itself (I1: Gianluigi proposes, Eyal approves).
"""
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest


class TestTriageIsConservative:
    def _run(self, monkeypatch, reply):
        import processors.question_triage as qt
        monkeypatch.setattr(qt, "_fetch_open_questions", lambda: [
            {"id": "q1", "question": "Should we use Aurora?", "created_at": "2026-07-01",
             "meetings": {"title": "R&D", "date": "2026-07-01"}},
        ])
        monkeypatch.setattr(qt, "_evidence", lambda: "DECISIONS TAKEN:\n[2026-07-10] Use Aurora")
        monkeypatch.setattr(qt, "call_llm", lambda **kw: (reply, {}))
        return qt.triage_open_questions()

    def test_answered_becomes_a_proposal(self, monkeypatch):
        out = self._run(monkeypatch, '[{"n":1,"verdict":"answered","reason":"decided","evidence":"Use Aurora"}]')
        assert len(out) == 1 and out[0]["question_id"] == "q1"

    def test_open_verdict_produces_no_proposal(self, monkeypatch):
        """Only non-open verdicts are proposed — an open question is left alone."""
        assert self._run(monkeypatch, '[{"n":1,"verdict":"open","reason":"still live"}]') == []

    def test_obsolete_is_proposed(self, monkeypatch):
        out = self._run(monkeypatch, '[{"n":1,"verdict":"obsolete","reason":"moved on"}]')
        assert len(out) == 1 and out[0]["verdict"] == "obsolete"

    def test_unparseable_reply_proposes_nothing(self, monkeypatch):
        """A model that answers with prose must not cause closures."""
        assert self._run(monkeypatch, "I could not parse that.") == []

    def test_out_of_range_index_is_ignored(self, monkeypatch):
        """A hallucinated row number must not close an unrelated question."""
        assert self._run(monkeypatch, '[{"n":7,"verdict":"answered","reason":"x"}]') == []

    def test_llm_failure_is_non_fatal(self, monkeypatch):
        import processors.question_triage as qt
        monkeypatch.setattr(qt, "_fetch_open_questions", lambda: [
            {"id": "q1", "question": "Q?", "created_at": "2026-07-01", "meetings": {}}])
        monkeypatch.setattr(qt, "_evidence", lambda: "DECISIONS TAKEN:\nx")
        def boom(**kw):
            raise RuntimeError("anthropic down")
        monkeypatch.setattr(qt, "call_llm", boom)
        assert qt.triage_open_questions() == []

    def test_no_evidence_skips_entirely(self, monkeypatch):
        """Without decisions to compare against, every verdict would be guesswork."""
        import processors.question_triage as qt
        monkeypatch.setattr(qt, "_fetch_open_questions", lambda: [
            {"id": "q1", "question": "Q?", "created_at": "2026-07-01", "meetings": {}}])
        monkeypatch.setattr(qt, "_evidence", lambda: "")
        assert qt.triage_open_questions() == []


class TestProposalsAreIdempotent:
    def test_existing_proposal_is_not_duplicated(self, monkeypatch):
        import processors.question_triage as qt
        sc = MagicMock()
        sc.get_pending_approval.return_value = {"approval_id": "qclose-q1"}
        monkeypatch.setattr(qt, "supabase_client", sc)
        assert qt.submit_close_proposals([{"question_id": "q1", "reason": "r"}]) == 0
        sc.upsert_pending_approval.assert_not_called()

    def test_new_proposal_is_submitted(self, monkeypatch):
        import processors.question_triage as qt
        sc = MagicMock()
        sc.get_pending_approval.return_value = None
        monkeypatch.setattr(qt, "supabase_client", sc)
        assert qt.submit_close_proposals([{"question_id": "q1", "reason": "r"}]) == 1
        assert sc.upsert_pending_approval.call_args.kwargs["approval_id"] == "qclose-q1"


class TestApplyOnlyOnApproval:
    def test_apply_closes_the_question(self, monkeypatch):
        import processors.question_triage as qt
        sc = MagicMock()
        monkeypatch.setattr(qt, "supabase_client", sc)
        res = qt.apply_close_proposal({"question_id": "q1", "reason": "decided"})
        assert res["closed"] is True
        payload = sc.client.table.return_value.update.call_args[0][0]
        assert payload["status"] == "resolved"
        assert "weekly triage" in payload["status_reason"]

    def test_apply_without_id_is_rejected(self, monkeypatch):
        import processors.question_triage as qt
        assert qt.apply_close_proposal({})["closed"] is False
