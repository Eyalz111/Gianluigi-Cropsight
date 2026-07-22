"""
Tests for the v3 PR5 sticky-flag helpers behind the task-proposal MCP tools.

The MCP tools (get_task_proposals / approve_task_proposal / clear_manual_flag)
are thin wrappers over these supabase_client helpers + pending_approvals; tool
registration is covered by test_mcp_server. Here we verify the field guards and
the update payloads (no live DB).
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
    def __init__(self, rows=None):
        object.__setattr__(self, "updated", None)
        # rows returned by execute() — lets a test drive the select() probe that
        # clear_manual_flag uses to decide whether any sticky field remains.
        object.__setattr__(self, "rows", rows if rows is not None else [])

    def __getattr__(self, name):
        if name.startswith("__"):
            raise AttributeError(name)

        def method(*args, **kwargs):
            if name == "update" and args:
                object.__setattr__(self, "updated", args[0])
            return self

        return method

    def execute(self):
        return SimpleNamespace(data=self.rows)


class TestManualFlagHelpers:
    def test_mark_rejects_unknown_field(self, sc):
        assert sc.mark_task_field_manual("t1", "bogus", "sheet_edit") is False

    def test_clear_rejects_unknown_field(self, sc):
        assert sc.clear_manual_flag("t1", "bogus") is False

    def test_mark_valid_field_sets_flag(self, sc, monkeypatch):
        chain = _Chain()
        monkeypatch.setattr(sc, "_client", SimpleNamespace(table=lambda *a, **k: chain))
        assert sc.mark_task_field_manual("t1", "status", "sheet_edit") is True
        assert chain.updated["manual_status"] is True
        assert chain.updated["manual_set_source"] == "sheet_edit"
        assert "manual_set_at" in chain.updated

    def test_clear_valid_field_unsets_flag(self, sc, monkeypatch):
        chain = _Chain()
        monkeypatch.setattr(sc, "_client", SimpleNamespace(table=lambda *a, **k: chain))
        assert sc.clear_manual_flag("t1", "deadline") is True
        assert chain.updated["manual_deadline"] is False

    def test_clear_last_flag_also_clears_provenance(self, sc, monkeypatch):
        """With no sticky field left, manual_set_at/source must not linger —
        stale provenance claims a human edit that has been released. [2026-07-22]"""
        chain = _Chain(rows=[{
            "manual_status": False, "manual_deadline": True, "manual_priority": False,
            "manual_assignee": False, "manual_title": False, "manual_label": False,
        }])
        monkeypatch.setattr(sc, "_client", SimpleNamespace(table=lambda *a, **k: chain))
        assert sc.clear_manual_flag("t1", "deadline") is True
        assert chain.updated["manual_deadline"] is False
        assert chain.updated["manual_set_at"] is None
        assert chain.updated["manual_set_source"] is None

    def test_clear_one_of_several_keeps_provenance(self, sc, monkeypatch):
        """Another field is still sticky -> provenance must survive."""
        chain = _Chain(rows=[{
            "manual_status": True, "manual_deadline": True, "manual_priority": False,
            "manual_assignee": False, "manual_title": False, "manual_label": False,
        }])
        monkeypatch.setattr(sc, "_client", SimpleNamespace(table=lambda *a, **k: chain))
        assert sc.clear_manual_flag("t1", "deadline") is True
        assert chain.updated["manual_deadline"] is False
        assert "manual_set_at" not in chain.updated
        assert "manual_set_source" not in chain.updated
