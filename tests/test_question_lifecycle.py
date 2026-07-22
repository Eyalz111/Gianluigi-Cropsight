"""
Open-question lifecycle: aging + proposal-based resolution. [2026-07-22]

The component has had an inbox and no outbox since the early days: extraction
creates a question from every meeting, but the ONLY exit was a later meeting
explicitly answering the same question, which almost never fires. Live data:
100+ questions open, going back to May.

Non-negotiables pinned here:
  - aging NEVER deletes (a stale question is fully restorable)
  - resolution is PROPOSED, never auto-applied (silently closing a real
    question destroys it — "Gianluigi proposes, Eyal approves")
  - only a decision made AFTER the question was raised can answer it
"""

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest


@pytest.fixture
def sc():
    try:
        from services.supabase_client import supabase_client
    except Exception as e:  # pragma: no cover
        pytest.skip(f"cannot import supabase_client ({e})")
    return supabase_client


class _Tbl:
    """Records update payloads; returns canned rows from select()."""

    def __init__(self, rows):
        self.rows = rows
        self.updates = []
        self._pending = None

    def select(self, *a, **k): return self
    def eq(self, *a, **k): return self
    def lt(self, *a, **k): return self
    def order(self, *a, **k): return self
    def limit(self, *a, **k): return self

    def update(self, payload):
        self._pending = payload
        return self

    def execute(self):
        if self._pending is not None:
            self.updates.append(self._pending)
            self._pending = None
            return SimpleNamespace(data=[], count=0)
        return SimpleNamespace(data=self.rows, count=len(self.rows))


class TestAging:
    def test_ages_open_questions_to_stale_never_deletes(self, sc, monkeypatch):
        from processors import question_lifecycle as ql

        tbl = _Tbl([{"id": "q1", "question": "old one", "status": "open"},
                    {"id": "q2", "question": "another", "status": "open"}])
        monkeypatch.setattr(sc, "_client", MagicMock(table=lambda *a, **k: tbl))
        monkeypatch.setattr(sc, "log_action", lambda *a, **k: None)

        res = ql.age_out_questions()

        assert res["aged"] == 2
        assert all(u["status"] == "stale" for u in tbl.updates)
        assert all("status_reason" in u for u in tbl.updates), "reason recorded"
        # the decisive property: no delete anywhere in the payloads
        assert all("delete" not in str(u).lower() for u in tbl.updates)

    def test_dry_run_writes_nothing(self, sc, monkeypatch):
        from processors import question_lifecycle as ql

        tbl = _Tbl([{"id": "q1", "question": "old", "status": "open"}])
        monkeypatch.setattr(sc, "_client", MagicMock(table=lambda *a, **k: tbl))

        res = ql.age_out_questions(dry_run=True)

        assert res["scanned"] == 1 and res["aged"] == 0
        assert tbl.updates == []

    def test_restore_flips_back_to_open(self, sc, monkeypatch):
        """Aging must be fully reversible — that's what makes it safe."""
        from processors import question_lifecycle as ql

        tbl = _Tbl([])
        monkeypatch.setattr(sc, "_client", MagicMock(table=lambda *a, **k: tbl))
        monkeypatch.setattr(sc, "log_action", lambda *a, **k: None)

        assert ql.restore_question("q1") is True
        assert tbl.updates[0]["status"] == "open"

    def test_read_failure_is_not_fatal(self, sc, monkeypatch):
        from processors import question_lifecycle as ql

        def _boom(*a, **k):
            raise RuntimeError("supabase down")
        monkeypatch.setattr(sc, "_client", MagicMock(table=_boom))

        assert ql.age_out_questions()["aged"] == 0


class TestResolutionProposals:
    async def test_only_decisions_made_after_the_question_can_answer_it(self, sc, monkeypatch):
        from config.settings import settings
        from processors import question_lifecycle as ql

        monkeypatch.setattr(settings, "SEMANTIC_INDEX_ENABLED", True, raising=False)
        tbl = _Tbl([{"id": "q1", "question": "Which cloud provider do we use?",
                     "created_at": "2026-07-01T00:00:00Z", "meeting_id": "m1"}])
        monkeypatch.setattr(sc, "_client", MagicMock(table=lambda *a, **k: tbl))
        monkeypatch.setattr(sc, "get_pending_approvals_by_status", lambda *a, **k: [])

        import services.embeddings as emb
        monkeypatch.setattr(emb.embedding_service, "embed_text",
                            lambda t: _async([0.1] * 8))
        # A decision made BEFORE the question was raised must be ignored.
        monkeypatch.setattr(sc, "search_embeddings", lambda **k: [
            {"source_id": "d1", "chunk_text": "We went with AWS",
             "created_at": "2026-06-01T00:00:00Z", "similarity": 0.95},
        ])
        monkeypatch.setattr(sc, "create_pending_approval",
                            lambda **k: pytest.fail("stale decision must not propose"))

        res = await ql.propose_question_resolutions()
        assert res["proposed"] == 0

    async def test_proposes_when_a_later_decision_matches(self, sc, monkeypatch):
        from config.settings import settings
        from processors import question_lifecycle as ql

        monkeypatch.setattr(settings, "SEMANTIC_INDEX_ENABLED", True, raising=False)
        tbl = _Tbl([{"id": "q1", "question": "Which cloud provider do we use?",
                     "created_at": "2026-07-01T00:00:00Z", "meeting_id": "m1"}])
        monkeypatch.setattr(sc, "_client", MagicMock(table=lambda *a, **k: tbl))
        monkeypatch.setattr(sc, "get_pending_approvals_by_status", lambda *a, **k: [])

        import services.embeddings as emb
        monkeypatch.setattr(emb.embedding_service, "embed_text",
                            lambda t: _async([0.1] * 8))
        monkeypatch.setattr(sc, "search_embeddings", lambda **k: [
            {"source_id": "d9", "chunk_text": "Decided: AWS over Azure",
             "created_at": "2026-07-10T00:00:00Z", "similarity": 0.93},
        ])
        stored = []
        monkeypatch.setattr(sc, "create_pending_approval",
                            lambda **k: stored.append(k) or {})

        res = await ql.propose_question_resolutions()

        assert res["proposed"] == 1
        assert stored[0]["content_type"] == "question_resolved"
        assert stored[0]["content"]["question_id"] == "q1"
        assert stored[0]["content"]["decision_id"] == "d9"
        # nothing was written to the question itself — it is a PROPOSAL
        assert tbl.updates == []

    async def test_disabled_semantic_index_is_a_noop(self, sc, monkeypatch):
        from config.settings import settings
        from processors import question_lifecycle as ql

        monkeypatch.setattr(settings, "SEMANTIC_INDEX_ENABLED", False, raising=False)
        assert (await ql.propose_question_resolutions())["proposed"] == 0

    def test_apply_marks_resolved(self, sc, monkeypatch):
        from processors import question_lifecycle as ql

        tbl = _Tbl([])
        monkeypatch.setattr(sc, "_client", MagicMock(table=lambda *a, **k: tbl))

        out = ql.apply_question_resolution({"question_id": "q1", "decision_id": "d9"})

        assert out["ok"] is True
        assert tbl.updates[0]["status"] == "resolved"

    def test_apply_without_question_id_fails_cleanly(self, sc):
        from processors import question_lifecycle as ql
        assert ql.apply_question_resolution({})["ok"] is False


async def _async(value):
    return value
