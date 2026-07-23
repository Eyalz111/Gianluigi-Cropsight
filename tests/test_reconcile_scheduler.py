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


class TestAutoSyncInterval:
    """The N-minute auto-sync is what lets Nechama's edits land without a human
    trigger — the system syncs on a timer, so the 'group never writes' boundary
    holds. [2026-07-23]"""

    async def test_interval_off_yields_slot_only(self, monkeypatch):
        monkeypatch.setattr(settings, "RECONCILE_ENABLED", True, raising=False)
        monkeypatch.setattr(settings, "RECONCILE_INTERVAL_MINUTES", 0, raising=False)
        monkeypatch.setattr(settings, "GANTT_RECONCILE_ENABLED", False, raising=False)

        captured = {}
        async def _no_sleep(s): captured["slept"] = s
        monkeypatch.setattr(rs.asyncio, "sleep", _no_sleep)

        slot = await rs.reconcile_scheduler._sleep_until_next()
        assert slot.endswith(":midday") or slot.endswith(":prenightly")

    async def test_interval_on_produces_distinct_timestamped_slots(self, monkeypatch):
        """Each interval tick MUST be a unique slot string, or the once-a-day
        _last_slot guard would run it once and skip it all day."""
        monkeypatch.setattr(settings, "RECONCILE_ENABLED", True, raising=False)
        monkeypatch.setattr(settings, "RECONCILE_INTERVAL_MINUTES", 30, raising=False)
        monkeypatch.setattr(settings, "GANTT_RECONCILE_ENABLED", False, raising=False)
        monkeypatch.setattr(settings, "RECONCILE_MIDDAY_HOUR", 13, raising=False)
        monkeypatch.setattr(settings, "RECONCILE_PRENIGHTLY_HOUR", 2, raising=False)

        async def _no_sleep(s): pass
        monkeypatch.setattr(rs.asyncio, "sleep", _no_sleep)

        slot = await rs.reconcile_scheduler._sleep_until_next()
        # The nearest candidate is the +30m interval tick (unless we happen to be
        # within 30m of 13:00/02:00), and it carries HHMM so it is distinct.
        assert ":interval" in slot
        # HHMM stamp present → 4 date-parts (Y-m-d-HHMM), not 3
        stamp = slot.split(":", 1)[0]
        assert len(stamp.split("-")) == 4, f"interval slot needs HHMM: {slot!r}"

    async def test_interval_slot_runs_the_full_reconcile(self, monkeypatch):
        """An 'interval' slot must run tasks+decisions+meetings (the else branch),
        not the gantt-only predigest branch."""
        import processors.sheets_sync as ss
        from services.supabase_client import supabase_client

        ran = []
        async def _rec(name):
            async def _f(**kw): ran.append(name); return {"pulled": 0}
            return _f
        monkeypatch.setattr(ss, "reconcile_tasks", await _rec("tasks"))
        monkeypatch.setattr(ss, "reconcile_decisions", await _rec("decisions"))
        monkeypatch.setattr(ss, "reconcile_meetings", await _rec("meetings"))
        monkeypatch.setattr(supabase_client, "upsert_scheduler_heartbeat", lambda *a, **k: None)
        monkeypatch.setattr(supabase_client, "log_action", lambda *a, **k: None)
        monkeypatch.setattr(settings, "WORKSPACE_VIEWS_ENABLED", False, raising=False)

        ok = await rs.reconcile_scheduler._run("2026-07-23-1430:interval")
        assert ok is True
        assert "tasks" in ran and "meetings" in ran
