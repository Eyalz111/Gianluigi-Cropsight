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
    def __init__(self):
        object.__setattr__(self, "updated", None)

    def __getattr__(self, name):
        if name.startswith("__"):
            raise AttributeError(name)

        def method(*args, **kwargs):
            if name == "update" and args:
                object.__setattr__(self, "updated", args[0])
            return self

        return method

    def execute(self):
        return SimpleNamespace(data=[])


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
        assert chain.updated == {"manual_deadline": False}
