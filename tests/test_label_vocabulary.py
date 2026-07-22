"""
Project-label vocabulary: resolve_label + the auto-learn loop. [2026-07-22]

Phase 10 built the pieces and never connected them. `canonical_projects` held a
curated vocabulary, `match_label_to_canonical` could match it, and
`_resolve_unmatched_labels` cleaned up retroactively when a project was added —
but `store_unmatched_label` had ZERO callers, so the cleaner always ran on an
empty table, and the canonical name computed during topic threading was used to
NAME A TOPIC THREAD and then thrown away. `task.label` itself was never
rewritten. Live data: 34 distinct labels across 36 labelled tasks, and 50 topic
threads of which 46 were single-mention.

These tests pin the reconnected chain.
"""

from unittest.mock import MagicMock

import pytest


PROJECTS = [
    {"name": "Moldova Pilot", "aliases": ["Moldova PoC", "Gagauzia project"]},
    {"name": "Pre-Seed Fundraising", "aliases": ["Tnufa", "fundraising"]},
    {"name": "SatYield Accuracy Model", "aliases": ["the model", "yield model"]},
]


@pytest.fixture
def sc():
    try:
        from services.supabase_client import supabase_client
    except Exception as e:  # pragma: no cover
        pytest.skip(f"cannot import supabase_client ({e})")
    return supabase_client


class TestResolveLabel:
    @pytest.mark.parametrize("raw,expected", [
        ("Moldova Pilot", "Moldova Pilot"),
        ("moldova pilot", "Moldova Pilot"),
        ("Moldova PoC", "Moldova Pilot"),          # alias
        ("Gagauzia project", "Moldova Pilot"),     # alias
        ("Tnufa", "Pre-Seed Fundraising"),
        ("the model", "SatYield Accuracy Model"),
    ])
    def test_canonicalizes_names_and_aliases(self, sc, raw, expected, monkeypatch):
        monkeypatch.setattr(sc, "store_unmatched_label", lambda *a, **k: None)
        assert sc.resolve_label(raw, projects=PROJECTS) == expected

    def test_blank_stays_blank(self, sc):
        assert sc.resolve_label("", projects=PROJECTS) == ""
        assert sc.resolve_label(None, projects=PROJECTS) == ""

    def test_unknown_label_kept_verbatim(self, sc, monkeypatch):
        """Sheets-wins, exactly like resolve_category/resolve_assignee."""
        monkeypatch.setattr(sc, "store_unmatched_label", lambda *a, **k: None)
        monkeypatch.setattr(
            "processors.topic_threading._match_canonical_name", lambda label: None
        )
        assert sc.resolve_label("Polish Grant", projects=PROJECTS) == "Polish Grant"

    def test_capture_is_off_by_default(self, sc, monkeypatch):
        """A resolver that writes on every call would fire on every update path."""
        calls = []
        monkeypatch.setattr(sc, "store_unmatched_label",
                            lambda *a, **k: calls.append(a))
        monkeypatch.setattr(
            "processors.topic_threading._match_canonical_name", lambda label: None
        )
        sc.resolve_label("Brand New Thing", projects=PROJECTS)
        assert calls == []

    def test_capture_records_only_unmatched(self, sc, monkeypatch):
        calls = []
        monkeypatch.setattr(sc, "store_unmatched_label",
                            lambda label, **k: calls.append(label))
        monkeypatch.setattr(
            "processors.topic_threading._match_canonical_name", lambda label: None
        )
        sc.resolve_label("Brand New Thing", projects=PROJECTS, capture=True)
        sc.resolve_label("Moldova PoC", projects=PROJECTS, capture=True)  # matches
        assert calls == ["Brand New Thing"]

    def test_lookup_failure_keeps_the_label(self, sc, monkeypatch):
        def _boom(*a, **k):
            raise RuntimeError("supabase down")
        monkeypatch.setattr(sc, "get_canonical_projects", _boom)
        assert sc.resolve_label("Moldova Pilot") == "Moldova Pilot"


class TestAddCanonicalProjectIdempotent:
    """Was a plain .insert() that returned None on conflict, surfaced by MCP as
    "Failed to create project — may already exist". Auto-learn hits that path
    constantly."""

    def test_existing_project_merges_aliases_instead_of_failing(self, sc, monkeypatch):
        existing = {"id": "p1", "name": "Moldova Pilot",
                    "aliases": ["Moldova PoC"], "description": ""}
        state = {"updated": None}

        class _Tbl:
            def select(self, *a, **k): return self
            def eq(self, *a, **k): return self
            def limit(self, *a, **k): return self
            def execute(self): return MagicMock(data=[existing])
            def update(self, payload):
                state["updated"] = payload
                return self

        monkeypatch.setattr(sc, "_client", MagicMock(table=lambda *a, **k: _Tbl()))
        monkeypatch.setattr(sc, "_resolve_unmatched_labels", lambda *a, **k: 0)

        out = sc.add_canonical_project("Moldova Pilot", aliases=["Moldova wheat"])

        assert out is not None, "must not return None for an existing project"
        assert "Moldova wheat" in state["updated"]["aliases"]
        assert "Moldova PoC" in state["updated"]["aliases"], "existing aliases preserved"


class TestProjectAutoLearn:
    def _rows(self, label, n_meetings):
        return [{"label": label, "meeting_id": f"m{i}", "meeting_title": f"Meeting {i}"}
                for i in range(n_meetings)]

    def test_proposes_label_seen_in_multiple_meetings(self, sc, monkeypatch):
        from processors import project_learning as pl

        monkeypatch.setattr(sc, "get_unmatched_labels",
                            lambda days=0: self._rows("Polish Grant", 3))
        monkeypatch.setattr(sc, "get_canonical_projects", lambda status=None: PROJECTS)
        monkeypatch.setattr(sc, "get_pending_approvals_by_status", lambda *a, **k: [])
        stored = []
        monkeypatch.setattr(sc, "create_pending_approval",
                            lambda **k: stored.append(k) or {})

        res = pl.propose_new_projects()

        assert res["proposed"] == 1
        assert stored[0]["content"]["name"] == "Polish Grant"
        assert stored[0]["content_type"] == "project_new"

    def test_single_mention_label_is_not_proposed(self, sc, monkeypatch):
        """One-off labels are usually a paraphrase, not a project — proposing
        them is what filled topic_threads with 46 single-mention rows."""
        from processors import project_learning as pl

        monkeypatch.setattr(sc, "get_unmatched_labels",
                            lambda days=0: self._rows("One Off Thing", 1))
        monkeypatch.setattr(sc, "get_canonical_projects", lambda status=None: PROJECTS)
        monkeypatch.setattr(sc, "get_pending_approvals_by_status", lambda *a, **k: [])
        monkeypatch.setattr(sc, "create_pending_approval",
                            lambda **k: pytest.fail("must not propose"))

        assert pl.propose_new_projects()["proposed"] == 0

    def test_label_that_became_canonical_is_not_reproposed(self, sc, monkeypatch):
        from processors import project_learning as pl

        monkeypatch.setattr(sc, "get_unmatched_labels",
                            lambda days=0: self._rows("Moldova PoC", 4))
        monkeypatch.setattr(sc, "get_canonical_projects", lambda status=None: PROJECTS)
        monkeypatch.setattr(sc, "get_pending_approvals_by_status", lambda *a, **k: [])
        monkeypatch.setattr(sc, "create_pending_approval",
                            lambda **k: pytest.fail("already canonical"))

        assert pl.propose_new_projects()["proposed"] == 0

    def test_pending_proposal_is_not_duplicated(self, sc, monkeypatch):
        from processors import project_learning as pl

        monkeypatch.setattr(sc, "get_unmatched_labels",
                            lambda days=0: self._rows("Polish Grant", 3))
        monkeypatch.setattr(sc, "get_canonical_projects", lambda status=None: PROJECTS)
        monkeypatch.setattr(sc, "get_pending_approvals_by_status", lambda *a, **k: [
            {"content_type": "project_new", "content": {"key": "polish grant"}}
        ])
        monkeypatch.setattr(sc, "create_pending_approval",
                            lambda **k: pytest.fail("already pending"))

        assert pl.propose_new_projects()["proposed"] == 0

    def test_most_common_spelling_wins(self, sc, monkeypatch):
        """The vocabulary should read the way the team writes it."""
        from processors import project_learning as pl

        rows = ([{"label": "polish grant", "meeting_id": "m1"}]
                + [{"label": "Polish Grant", "meeting_id": f"m{i}"} for i in (2, 3)])
        monkeypatch.setattr(sc, "get_unmatched_labels", lambda days=0: rows)
        monkeypatch.setattr(sc, "get_canonical_projects", lambda status=None: PROJECTS)
        monkeypatch.setattr(sc, "get_pending_approvals_by_status", lambda *a, **k: [])
        stored = []
        monkeypatch.setattr(sc, "create_pending_approval",
                            lambda **k: stored.append(k) or {})

        pl.propose_new_projects()
        assert stored[0]["content"]["name"] == "Polish Grant"
