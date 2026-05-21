"""
Verify the knowledge_links + supersede helpers build the right queries.

No live DB: patch the client with a chainable recorder and assert payloads /
filters. Covers: link insert payload, current-only filter, bi-temporal close
sets valid_to + superseded_at.
"""

from types import SimpleNamespace

import pytest

from models.schemas import LinkType


class _Chain:
    def __init__(self, data=None):
        object.__setattr__(self, "data_rows", data if data is not None else [])
        object.__setattr__(self, "calls", [])
        object.__setattr__(self, "inserted", None)
        object.__setattr__(self, "updated", None)

    def __getattr__(self, name):
        if name.startswith("__"):
            raise AttributeError(name)

        def method(*args, **kwargs):
            self.calls.append((name, args, kwargs))
            if name == "insert" and args:
                object.__setattr__(self, "inserted", args[0])
            if name == "update" and args:
                object.__setattr__(self, "updated", args[0])
            return self

        return method

    def execute(self):
        return SimpleNamespace(data=self.data_rows)


@pytest.fixture
def sc():
    try:
        from services.supabase_client import supabase_client
    except Exception as e:
        pytest.skip(f"Cannot import supabase_client ({e})")
    return supabase_client


def _patch(sc, monkeypatch, data=None):
    chain = _Chain(data if data is not None else [])
    # `client` is a lazy read-only property backed by `_client`; patch the backing field.
    monkeypatch.setattr(sc, "_client", SimpleNamespace(table=lambda *a, **k: chain))
    return chain


class TestKnowledgeLinks:
    def test_link_type_values(self):
        assert LinkType.BELONGS_TO == "belongs_to"
        assert LinkType.SUPERSEDES == "supersedes"
        assert LinkType.ADVANCES == "advances"

    def test_create_link_inserts_expected_fields(self, sc, monkeypatch):
        # dupe-check returns [] (no existing) -> proceeds to insert
        chain = _patch(sc, monkeypatch, data=[])
        sc.create_knowledge_link(
            "topic", "t1", "area", "a1", "belongs_to", created_by="backfill"
        )
        assert chain.inserted is not None
        assert chain.inserted["from_type"] == "topic"
        assert chain.inserted["to_type"] == "area"
        assert chain.inserted["link_type"] == "belongs_to"
        assert chain.inserted["created_by"] == "backfill"

    def test_get_links_current_only_filters_valid_to(self, sc, monkeypatch):
        chain = _patch(sc, monkeypatch)
        sc.get_knowledge_links(from_type="topic", current_only=True)
        assert any(c[0] == "is_" and c[1] == ("valid_to", "null") for c in chain.calls)

    def test_get_links_all_skips_filter(self, sc, monkeypatch):
        chain = _patch(sc, monkeypatch)
        sc.get_knowledge_links(from_type="topic", current_only=False)
        assert not any(c[0] == "is_" and c[1] == ("valid_to", "null") for c in chain.calls)

    def test_supersede_decision_sets_valid_to(self, sc, monkeypatch):
        chain = _patch(sc, monkeypatch)
        ok = sc.supersede_decision("d1", superseded_by="d2")
        assert ok is True
        assert chain.updated is not None
        assert "valid_to" in chain.updated
        assert "superseded_at" in chain.updated
        assert chain.updated["superseded_by"] == "d2"

    def test_supersede_task_sets_valid_to(self, sc, monkeypatch):
        chain = _patch(sc, monkeypatch)
        ok = sc.supersede_task("t1")
        assert ok is True
        assert chain.updated is not None
        assert "valid_to" in chain.updated and "superseded_at" in chain.updated
