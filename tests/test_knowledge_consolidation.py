"""
Tests for the v2.5 PR7/8 nightly consolidation.

No live DB or LLM: dedupe + staleness + apply/shadow behavior with a patched
client; reconcile is stubbed out.
"""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

try:
    import processors.knowledge_consolidation as kc
except Exception as e:
    pytest.skip(f"cannot import knowledge_consolidation ({e})", allow_module_level=True)


class _Chain:
    def __init__(self, data):
        object.__setattr__(self, "data_rows", data)

    def __getattr__(self, name):
        if name.startswith("__"):
            raise AttributeError(name)
        if name == "not_":
            return self
        return lambda *a, **k: self

    def execute(self):
        return SimpleNamespace(data=self.data_rows)


def test_dedupe_facts_removes_dups():
    brief = {"facts": [{"text": "A"}, {"text": "a"}, {"text": "B"}]}
    assert kc._dedupe_facts(brief) is True
    assert len(brief["facts"]) == 2


def test_dedupe_facts_no_change():
    brief = {"facts": [{"text": "A"}, {"text": "B"}]}
    assert kc._dedupe_facts(brief) is False


def test_parse_dt():
    assert kc._parse_dt("2026-05-01T00:00:00+00:00") is not None
    assert kc._parse_dt("garbage") is None


class TestRunConsolidation:
    def _wire(self, monkeypatch, topics, shadow):
        monkeypatch.setattr(kc.supabase_client, "_client", SimpleNamespace(table=lambda *a, **k: _Chain(topics)))
        writes = []
        monkeypatch.setattr(kc.supabase_client, "update_topic_brief", lambda tid, brief: writes.append((tid, brief)))
        monkeypatch.setattr(kc.supabase_client, "log_action", lambda **k: None)
        monkeypatch.setattr(kc, "_reconcile_brief", lambda name, brief: None)  # no LLM
        monkeypatch.setattr(kc.settings, "KNOWLEDGE_SHADOW_MODE", shadow)
        return writes

    async def test_marks_stale_and_writes_when_applied(self, monkeypatch):
        old = (datetime.now(timezone.utc) - timedelta(days=40)).isoformat()
        topics = [{"id": "t1", "topic_name": "Idle", "status": "active", "last_updated": old,
                   "brief_json": {"current_status": "active", "facts": [], "version": 1}}]
        writes = self._wire(monkeypatch, topics, shadow=False)  # shadow off -> apply
        res = await kc.run_consolidation()
        assert res["staled"] == 1 and res["applied"] is True
        assert writes and writes[0][1]["current_status"] == "stale"

    async def test_shadow_logs_without_writing(self, monkeypatch):
        old = (datetime.now(timezone.utc) - timedelta(days=40)).isoformat()
        topics = [{"id": "t1", "topic_name": "Idle", "status": "active", "last_updated": old,
                   "brief_json": {"current_status": "active", "facts": [], "version": 1}}]
        writes = self._wire(monkeypatch, topics, shadow=True)  # shadow on -> log only
        res = await kc.run_consolidation()
        assert res["staled"] == 1 and res["applied"] is False
        assert writes == []

    async def test_recent_active_deduped_not_staled(self, monkeypatch):
        recent = datetime.now(timezone.utc).isoformat()
        topics = [{"id": "t1", "topic_name": "Active", "status": "active", "last_updated": recent,
                   "brief_json": {"current_status": "active", "facts": [{"text": "a"}, {"text": "a"}], "version": 1}}]
        writes = self._wire(monkeypatch, topics, shadow=False)
        res = await kc.run_consolidation()
        assert res["staled"] == 0
        assert res["deduped"] == 1
        assert writes  # changed (dedup) -> written
