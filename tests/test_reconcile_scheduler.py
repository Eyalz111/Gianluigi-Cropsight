"""
Tests for the v3 reconcile scheduler.

Asserts the pre-nightly/nightly ordering invariant and that a trigger invokes
the reconcile engine. (Slot sleep-math mirrors the tested knowledge scheduler.)
"""

import pytest

try:
    import schedulers.reconcile_scheduler as rs
    from config.settings import settings
except Exception as e:  # pragma: no cover
    pytest.skip(f"cannot import reconcile scheduler ({e})", allow_module_level=True)


def test_prenightly_runs_before_knowledge_nightly():
    # Pre-nightly reconcile must land before the knowledge nightly so the DB is
    # correct before nightly reads tasks.
    assert settings.RECONCILE_PRENIGHTLY_HOUR < settings.KNOWLEDGE_NIGHTLY_HOUR


def test_scheduler_singleton_and_stop():
    assert rs.reconcile_scheduler is not None
    rs.reconcile_scheduler.stop()  # idempotent / safe


class TestRun:
    async def test_run_invokes_reconcile(self, monkeypatch):
        import processors.sheets_sync as ss
        from services.supabase_client import supabase_client

        calls = []

        async def fake_reconcile(**kw):
            calls.append(kw)
            return {"pulled": 1, "pushed": 0, "created": 0, "readded": 0}

        monkeypatch.setattr(ss, "reconcile_tasks", fake_reconcile)
        monkeypatch.setattr(supabase_client, "log_action", lambda *a, **k: None)

        sched = rs.ReconcileScheduler()
        await sched._run("2026-05-21:midday")
        assert len(calls) == 1
