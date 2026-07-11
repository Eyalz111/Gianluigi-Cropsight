"""
Phase 2 (editable Decisions sheet) — unit tests for the decision snapshot +
sticky-flag accessors on supabase_client. Parallel to test_task_proposals.py.

These are the substrate the decision reconcile (PR B) builds on. No live DB —
we assert the guards, the entity_type/keying, and the payloads via a fake chain.
"""

from types import SimpleNamespace

import pytest


@pytest.fixture
def sc():
    try:
        from services.supabase_client import supabase_client
    except Exception as e:
        pytest.skip(f"cannot import supabase_client ({e})")
    return supabase_client


class _Chain:
    """Fake PostgREST fluent chain that records the last insert/update payload."""

    def __init__(self, select_data=None):
        object.__setattr__(self, "updated", None)
        object.__setattr__(self, "inserted", None)
        object.__setattr__(self, "_select_data", select_data if select_data is not None else [])

    def __getattr__(self, name):
        if name.startswith("__"):
            raise AttributeError(name)

        def method(*args, **kwargs):
            if name == "update" and args:
                object.__setattr__(self, "updated", args[0])
            if name == "insert" and args:
                object.__setattr__(self, "inserted", args[0])
            return self

        return method

    def execute(self):
        # The SELECT existence-check reads .data; insert/update ignore the return.
        return SimpleNamespace(data=self._select_data)


class TestDecisionManualFlags:
    def test_mark_rejects_unknown_field(self, sc):
        assert sc.mark_decision_field_manual("d1", "bogus", "sheet_edit") is False

    def test_clear_rejects_unknown_field(self, sc):
        assert sc.clear_decision_manual_flag("d1", "bogus") is False

    def test_mark_valid_field_sets_flag(self, sc, monkeypatch):
        chain = _Chain()
        monkeypatch.setattr(sc, "_client", SimpleNamespace(table=lambda *a, **k: chain))
        assert sc.mark_decision_field_manual("d1", "status", "sheet_edit") is True
        assert chain.updated["manual_status"] is True
        assert chain.updated["manual_set_source"] == "sheet_edit"
        assert "manual_set_at" in chain.updated

    def test_mark_accepts_all_editable_fields(self, sc, monkeypatch):
        for field in ("description", "label", "rationale", "confidence", "status"):
            chain = _Chain()
            monkeypatch.setattr(sc, "_client", SimpleNamespace(table=lambda *a, **k: chain))
            assert sc.mark_decision_field_manual("d1", field, "telegram") is True
            assert chain.updated[f"manual_{field}"] is True

    def test_clear_valid_field_unsets_flag(self, sc, monkeypatch):
        chain = _Chain()
        monkeypatch.setattr(sc, "_client", SimpleNamespace(table=lambda *a, **k: chain))
        assert sc.clear_decision_manual_flag("d1", "rationale") is True
        assert chain.updated == {"manual_rationale": False}


class TestDecisionSnapshot:
    def test_upsert_inserts_decision_row_when_absent(self, sc, monkeypatch):
        chain = _Chain(select_data=[])  # no existing snapshot -> insert path
        monkeypatch.setattr(sc, "_client", SimpleNamespace(table=lambda *a, **k: chain))
        ok = sc.upsert_decision_snapshot(
            "d1", 5, "Ship the MVP", label="Product", rationale="fastest to signal",
            confidence="4", decision_status="active",
        )
        assert ok is True
        payload = chain.inserted
        assert payload["entity_type"] == "decision"
        assert payload["decision_id"] == "d1"
        assert payload["description"] == "Ship the MVP"
        assert payload["confidence"] == 4                    # coerced str -> int
        assert payload["decision_status"] == "active"
        assert "snapshot_at" in payload

    def test_upsert_updates_when_present(self, sc, monkeypatch):
        chain = _Chain(select_data=[{"id": "snap1"}])  # existing -> update path
        monkeypatch.setattr(sc, "_client", SimpleNamespace(table=lambda *a, **k: chain))
        assert sc.upsert_decision_snapshot("d1", 5, "text") is True
        assert chain.updated is not None
        assert chain.updated["entity_type"] == "decision"

    def test_upsert_coerces_blank_confidence_to_null(self, sc, monkeypatch):
        chain = _Chain(select_data=[])
        monkeypatch.setattr(sc, "_client", SimpleNamespace(table=lambda *a, **k: chain))
        sc.upsert_decision_snapshot("d1", 1, "t", confidence="")
        assert chain.inserted["confidence"] is None

    def test_get_snapshots_keyed_by_decision_id(self, sc, monkeypatch):
        rows = [
            {"decision_id": "d1", "description": "a"},
            {"decision_id": "d2", "description": "b"},
            {"decision_id": None, "description": "orphan"},  # skipped
        ]
        chain = _Chain(select_data=rows)
        monkeypatch.setattr(sc, "_client", SimpleNamespace(table=lambda *a, **k: chain))
        out = sc.get_decision_snapshots()
        assert set(out.keys()) == {"d1", "d2"}
        assert out["d1"]["description"] == "a"
