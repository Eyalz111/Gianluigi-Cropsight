"""
Verify the v2.5 bi-temporal read filter is wired into the central read helpers.

This is the footgun guard for plan point #4: adding valid_to columns is useless
(and leaks superseded rows) unless get_tasks / list_decisions filter them out by
default. We assert the query-building without a live DB by patching the client
with a chainable recorder.
"""

from types import SimpleNamespace

import pytest


class _Chain:
    """Chainable query-builder mock: records calls; execute() returns .data."""

    def __init__(self, data=None):
        object.__setattr__(self, "data_rows", data if data is not None else [])
        object.__setattr__(self, "calls", [])

    def __getattr__(self, name):
        if name.startswith("__"):
            raise AttributeError(name)

        def method(*args, **kwargs):
            self.calls.append((name, args, kwargs))
            return self

        return method

    def execute(self):
        return SimpleNamespace(data=self.data_rows)


def _filtered_valid_to(chain):
    return any(c[0] == "is_" and c[1] == ("valid_to", "null") for c in chain.calls)


@pytest.fixture
def sc():
    try:
        from services.supabase_client import supabase_client
    except Exception as e:
        pytest.skip(f"Cannot import supabase_client ({e})")
    return supabase_client


def _patch(sc, monkeypatch):
    chain = _Chain([])
    # `client` is a lazy read-only property backed by `_client`; patch the backing field.
    monkeypatch.setattr(sc, "_client", SimpleNamespace(table=lambda *a, **k: chain))
    return chain


class TestBitemporalReadFilter:
    def test_get_tasks_hides_superseded_by_default(self, sc, monkeypatch):
        chain = _patch(sc, monkeypatch)
        sc.get_tasks()
        assert _filtered_valid_to(chain), "get_tasks must filter valid_to IS NULL by default"

    def test_get_tasks_include_superseded_skips_filter(self, sc, monkeypatch):
        chain = _patch(sc, monkeypatch)
        sc.get_tasks(include_superseded=True)
        assert not _filtered_valid_to(chain)

    def test_list_decisions_hides_superseded_by_default(self, sc, monkeypatch):
        chain = _patch(sc, monkeypatch)
        sc.list_decisions()
        assert _filtered_valid_to(chain), "list_decisions must filter valid_to IS NULL by default"

    def test_list_decisions_include_superseded_skips_filter(self, sc, monkeypatch):
        chain = _patch(sc, monkeypatch)
        sc.list_decisions(include_superseded=True)
        assert not _filtered_valid_to(chain)
