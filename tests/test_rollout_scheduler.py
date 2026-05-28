"""
Tests for the rollout orchestrator (v2.5 Phase 3, chunk 5):
plan walk + applied-stage skipping, daily-tick hour gate + once-per-day,
shadow-summary best-effort, telegram apply callback (Eyal gate, idempotency,
admin-API failure, success path), Cloud Run admin env-merge contract.

Patches MODULE-level attrs (never the global settings object).
"""

from datetime import datetime, timezone, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import schedulers.rollout_scheduler as rmod


def _fixed_now(dt):
    """A stand-in for `datetime` that overrides .now() but inherits everything else.

    Subclassing real `datetime` so `datetime.strptime`/`fromisoformat`/etc. still work
    when the module's `datetime` symbol is patched with this — `_next_due_stage` uses
    strptime to parse target_date.
    """
    fixed = dt
    class _FakeDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return fixed
    return _FakeDateTime


_PLAN = [
    {"stage_id": "s1", "target_date": "2026-06-02", "description": "first",
     "env_changes": {"A": "true"}, "audit_action_summary": "input_hygiene_shadow"},
    {"stage_id": "s2", "target_date": "2026-06-04", "description": "second",
     "env_changes": {"B": "true"}, "audit_action_summary": None},
    {"stage_id": "s3", "target_date": "2026-06-06", "description": "third",
     "env_changes": {"C": "true"}, "audit_action_summary": None},
]


# =========================================================================
# Plan walk: next-due + applied skipping
# =========================================================================

class TestPlanWalk:
    def test_picks_first_unapplied_due(self):
        sched = rmod.RolloutScheduler()
        today = datetime(2026, 6, 3, 9, 0, tzinfo=rmod._ISRAEL_TZ)
        with patch.object(rmod, "ROLLOUT_PLAN", _PLAN), \
             patch.object(rmod.supabase_client, "get_audit_log", MagicMock(return_value=[])):
            stage = sched._next_due_stage(today)
        assert stage["stage_id"] == "s1"

    def test_skips_already_applied(self):
        sched = rmod.RolloutScheduler()
        today = datetime(2026, 6, 5, 9, 0, tzinfo=rmod._ISRAEL_TZ)
        rows = [{"details": {"stage_id": "s1"}}]
        with patch.object(rmod, "ROLLOUT_PLAN", _PLAN), \
             patch.object(rmod.supabase_client, "get_audit_log", MagicMock(return_value=rows)):
            stage = sched._next_due_stage(today)
        assert stage["stage_id"] == "s2"

    def test_returns_none_when_future_only(self):
        sched = rmod.RolloutScheduler()
        today = datetime(2026, 5, 28, 9, 0, tzinfo=rmod._ISRAEL_TZ)  # before s1's date
        with patch.object(rmod, "ROLLOUT_PLAN", _PLAN), \
             patch.object(rmod.supabase_client, "get_audit_log", MagicMock(return_value=[])):
            assert sched._next_due_stage(today) is None

    def test_returns_none_when_all_applied(self):
        sched = rmod.RolloutScheduler()
        today = datetime(2026, 6, 10, 9, 0, tzinfo=rmod._ISRAEL_TZ)
        rows = [{"details": {"stage_id": s["stage_id"]}} for s in _PLAN]
        with patch.object(rmod, "ROLLOUT_PLAN", _PLAN), \
             patch.object(rmod.supabase_client, "get_audit_log", MagicMock(return_value=rows)):
            assert sched._next_due_stage(today) is None


# =========================================================================
# Daily tick: hour gate, once-per-day, send path
# =========================================================================

class TestCheckAndRemind:
    _SETTINGS = SimpleNamespace(ROLLOUT_CHECK_HOUR=9, ROLLOUT_CHECK_INTERVAL=3600)

    async def test_skips_outside_check_hour(self):
        sched = rmod.RolloutScheduler()
        with patch.object(rmod, "settings", self._SETTINGS), \
             patch.object(rmod, "datetime", _fixed_now(datetime(2026, 6, 3, 14, 0, tzinfo=rmod._ISRAEL_TZ))), \
             patch.object(sched, "_send_reminder", AsyncMock()) as sr:
            assert await sched._check_and_remind() is None
        sr.assert_not_awaited()

    async def test_fires_once_per_day(self):
        sched = rmod.RolloutScheduler()
        now = datetime(2026, 6, 3, 9, 0, tzinfo=rmod._ISRAEL_TZ)
        with patch.object(rmod, "settings", self._SETTINGS), \
             patch.object(rmod, "datetime", _fixed_now(now)), \
             patch.object(rmod, "ROLLOUT_PLAN", _PLAN), \
             patch.object(rmod.supabase_client, "get_audit_log", MagicMock(return_value=[])), \
             patch.object(sched, "_send_reminder", AsyncMock()) as sr:
            stage1 = await sched._check_and_remind()
            stage2 = await sched._check_and_remind()
        assert stage1 is not None and stage2 is None
        assert sr.await_count == 1

    async def test_sends_for_due_stage(self):
        sched = rmod.RolloutScheduler()
        now = datetime(2026, 6, 3, 9, 30, tzinfo=rmod._ISRAEL_TZ)
        with patch.object(rmod, "settings", self._SETTINGS), \
             patch.object(rmod, "datetime", _fixed_now(now)), \
             patch.object(rmod, "ROLLOUT_PLAN", _PLAN), \
             patch.object(rmod.supabase_client, "get_audit_log", MagicMock(return_value=[])), \
             patch.object(sched, "_send_reminder", AsyncMock()) as sr:
            stage = await sched._check_and_remind()
        assert stage["stage_id"] == "s1"
        sr.assert_awaited_once()


# =========================================================================
# Shadow summary (best-effort, no crash on missing data)
# =========================================================================

class TestShadowSummary:
    def test_no_action_returns_empty(self):
        assert rmod.RolloutScheduler._shadow_summary(None) == ""

    def test_counts_24h_and_5d(self):
        now = datetime.now(timezone.utc)
        rows = [
            {"created_at": now.isoformat()},                              # within 24h
            {"created_at": (now - timedelta(hours=12)).isoformat()},      # within 24h
            {"created_at": (now - timedelta(days=3)).isoformat()},        # within 5d
            {"created_at": (now - timedelta(days=10)).isoformat()},       # too old
            {"created_at": "garbage"},                                    # skipped
        ]
        with patch.object(rmod.supabase_client, "get_audit_log", MagicMock(return_value=rows)):
            out = rmod.RolloutScheduler._shadow_summary("input_hygiene_shadow")
        assert "2 events last 24h" in out
        assert "3 last 5 days" in out

    def test_empty_rows(self):
        with patch.object(rmod.supabase_client, "get_audit_log", MagicMock(return_value=[])):
            assert "no events yet" in rmod.RolloutScheduler._shadow_summary("x")


# =========================================================================
# Send reminder: markup, audit log of reminder
# =========================================================================

class TestSendReminder:
    async def test_markup_and_audit(self):
        sched = rmod.RolloutScheduler()
        with patch.object(rmod.comms_spine, "send_to_eyal", AsyncMock(return_value=True)) as send, \
             patch.object(rmod.supabase_client, "get_audit_log", MagicMock(return_value=[])), \
             patch.object(rmod.supabase_client, "log_action", MagicMock()) as log:
            await sched._send_reminder(_PLAN[0])
        # Apply button present with the right callback
        kb = send.call_args.kwargs["reply_markup"].inline_keyboard
        assert kb[0][0].callback_data == "rollout_apply:s1"
        # Reminder text contains the stage id + env preview
        text = send.call_args.args[0]
        assert "s1" in text and "A=true" in text
        # Audit log of the reminder
        log.assert_called_once()
        assert log.call_args.kwargs["action"] == "rollout_reminder_sent"


# =========================================================================
# Cloud Run admin: env-merge contract (real google client mocked)
# =========================================================================

class TestCloudRunAdmin:
    async def test_merges_env_preserving_others_and_replacing_existing(self):
        from services.cloud_run_admin import CloudRunAdmin

        # Build a fake service object the way google.cloud.run_v2 returns it.
        class _Env:
            def __init__(self, name, value):
                self.name = name; self.value = value
        class _Container:
            def __init__(self):
                self.env = [_Env("KEEP_ME", "keep"), _Env("STRICT_CALENDAR_FILTER", "false")]
        class _Template:
            def __init__(self): self.containers = [_Container()]
        class _Service:
            def __init__(self):
                self.template = _Template()
                self.latest_ready_revision = "projects/p/locations/r/revisions/gianluigi-00099-abc"

        fake_service = _Service()
        client = MagicMock()
        client.get_service.return_value = fake_service
        op = MagicMock(); op.result.return_value = fake_service
        client.update_service.return_value = op

        admin = CloudRunAdmin()
        with patch.object(admin, "_get_client", return_value=client), \
             patch("google.cloud.run_v2.EnvVar", _Env, create=True):
            res = await admin.apply_env_changes({"STRICT_CALENDAR_FILTER": "true", "NEW_KEY": "v"})

        # KEEP_ME untouched; STRICT_CALENDAR_FILTER replaced; NEW_KEY appended.
        envs = {e.name: e.value for e in fake_service.template.containers[0].env}
        assert envs["KEEP_ME"] == "keep"
        assert envs["STRICT_CALENDAR_FILTER"] == "true"
        assert envs["NEW_KEY"] == "v"
        # Short revision id returned.
        assert res["revision"] == "gianluigi-00099-abc"
        assert set(res["applied"]) == {"STRICT_CALENDAR_FILTER", "NEW_KEY"}
        client.update_service.assert_called_once()


# =========================================================================
# Telegram callback: Eyal gate, idempotency, admin failure, success
# =========================================================================

class TestRolloutApplyCallback:
    def _bot(self):
        from services.telegram_bot import TelegramBot
        bot = TelegramBot.__new__(TelegramBot)
        bot.eyal_chat_id = "999"
        bot.send_message = AsyncMock()
        return bot

    def _query(self, chat_id):
        q = MagicMock()
        q.message.chat_id = chat_id
        q.edit_message_text = AsyncMock()
        return q

    async def test_non_eyal_blocked(self):
        bot = self._bot()
        q = self._query(111)
        with patch("processors.rollout_plan.get_stage", return_value=_PLAN[0]), \
             patch("services.cloud_run_admin.cloud_run_admin.apply_env_changes", AsyncMock()) as apply:
            await bot._handle_rollout_apply(q, "s1")
        apply.assert_not_awaited()
        q.edit_message_text.assert_not_awaited()

    async def test_unknown_stage(self):
        bot = self._bot()
        q = self._query(999)
        with patch("processors.rollout_plan.get_stage", return_value=None):
            await bot._handle_rollout_apply(q, "ghost")
        q.edit_message_text.assert_awaited()
        assert "Unknown stage" in q.edit_message_text.call_args.args[0]

    async def test_already_applied_is_idempotent(self):
        bot = self._bot()
        q = self._query(999)
        applied_rows = [{"details": {"stage_id": "s1"}}]
        with patch("processors.rollout_plan.get_stage", return_value=_PLAN[0]), \
             patch("services.supabase_client.supabase_client.get_audit_log",
                   MagicMock(return_value=applied_rows)), \
             patch("services.cloud_run_admin.cloud_run_admin.apply_env_changes", AsyncMock()) as apply:
            await bot._handle_rollout_apply(q, "s1")
        apply.assert_not_awaited()
        assert "already applied" in q.edit_message_text.call_args.args[0]

    async def test_apply_failure_logs_and_surfaces(self):
        bot = self._bot()
        q = self._query(999)
        with patch("processors.rollout_plan.get_stage", return_value=_PLAN[0]), \
             patch("services.supabase_client.supabase_client.get_audit_log",
                   MagicMock(return_value=[])), \
             patch("services.cloud_run_admin.cloud_run_admin.apply_env_changes",
                   AsyncMock(side_effect=RuntimeError("perm denied"))), \
             patch("services.supabase_client.supabase_client.log_action") as log:
            await bot._handle_rollout_apply(q, "s1")
        # last edit names the failure
        msgs = [c.args[0] for c in q.edit_message_text.call_args_list]
        assert any("Apply failed" in m for m in msgs)
        # rollout_apply_failed logged (not rollout_applied)
        actions = [c.kwargs.get("action") for c in log.call_args_list]
        assert "rollout_apply_failed" in actions
        assert "rollout_applied" not in actions

    async def test_apply_success_logs_rollout_applied(self):
        bot = self._bot()
        q = self._query(999)
        with patch("processors.rollout_plan.get_stage", return_value=_PLAN[0]), \
             patch("services.supabase_client.supabase_client.get_audit_log",
                   MagicMock(return_value=[])), \
             patch("services.cloud_run_admin.cloud_run_admin.apply_env_changes",
                   AsyncMock(return_value={"revision": "gianluigi-00099-abc", "applied": ["A"]})), \
             patch("services.supabase_client.supabase_client.log_action") as log:
            await bot._handle_rollout_apply(q, "s1")
        # success edit
        msgs = [c.args[0] for c in q.edit_message_text.call_args_list]
        assert any("Applied s1" in m and "gianluigi-00099-abc" in m for m in msgs)
        log.assert_called_once()
        assert log.call_args.kwargs["action"] == "rollout_applied"
        details = log.call_args.kwargs["details"]
        assert details["stage_id"] == "s1"
        assert details["revision"] == "gianluigi-00099-abc"
