"""
Weekly cost report + out-of-credits alerting.

  - core/llm.py detects the Anthropic "credit balance too low" error, alerts Eyal
    ONCE (deduped), writes a durable audit row, and RE-RAISES (callers unchanged).
  - processors/cost_report.build_cost_report formats a Telegram + markdown report
    off the token_usage ledger (no LLM).
  - the scheduler only fires in its Sunday window and dedups per ISO week.
"""

from datetime import datetime
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

import pytest


# =============================================================================
# Credit-error detection + alert (core/llm.py)
# =============================================================================

class TestCreditErrorDetection:
    def test_is_credit_error(self):
        import core.llm as llm
        assert llm._is_credit_error(Exception("Error 400 - Your credit balance is too low to access"))
        assert llm._is_credit_error(Exception("credit ... too low"))
        assert not llm._is_credit_error(Exception("overloaded_error: please retry"))
        assert not llm._is_credit_error(Exception("rate_limit_error"))

    def test_call_llm_credit_error_alerts_once_and_reraises(self, monkeypatch):
        import core.llm as llm
        # client whose create() raises the credit error
        client = MagicMock()
        client.messages.create.side_effect = Exception(
            "Error code: 400 - Your credit balance is too low to access the Anthropic API."
        )
        monkeypatch.setattr(llm, "get_client", lambda: client)

        noted = []
        monkeypatch.setattr(llm, "_note_anthropic_credit_exhausted", lambda e: noted.append(str(e)))

        with pytest.raises(Exception):
            llm.call_llm(prompt="x", model="m", max_tokens=5, call_site="t")
        assert len(noted) == 1  # detected + routed to the alert helper

    def test_note_dedups_and_writes_audit(self, monkeypatch):
        import core.llm as llm
        llm._last_credit_alert_ts = 0.0  # reset cooldown
        logged = []
        sc = MagicMock()
        sc.log_action = lambda *a, **k: logged.append((a, k))
        monkeypatch.setattr("services.supabase_client.supabase_client", sc, raising=False)
        # no loop registered → it should still log the audit row, not raise
        monkeypatch.setattr(llm, "_main_loop", None)

        llm._note_anthropic_credit_exhausted(Exception("credit balance is too low"))
        llm._note_anthropic_credit_exhausted(Exception("credit balance is too low"))  # within cooldown

        # audit written once (second call is deduped by the cooldown)
        assert sum(1 for a, _ in logged if a and a[0] == "anthropic_credit_exhausted") == 1


# =============================================================================
# Cost report formatting (processors/cost_report.py)
# =============================================================================

class TestCostReportBuild:
    def _fake_summary(self, total):
        return {
            "by_model": {"claude-opus-4-6": {"cost": total * 0.8, "calls": 5,
                                             "input_tokens": 1000, "output_tokens": 500}},
            "by_call_site": {"transcript_extraction": {"cost": total * 0.7, "calls": 3},
                             "task_dedup": {"cost": total * 0.1, "calls": 9}},
            "daily_trend": [{"date": f"2026-06-{d:02d}", "cost": 0.5} for d in range(1, 15)],
        }

    def test_build_report_structure(self, monkeypatch):
        from processors import cost_report as cr
        monkeypatch.setattr(cr.supabase_client, "get_token_usage_summary", lambda days=7: [])
        # 14-day daily_trend = 14 × 0.5 → last7=3.5, prev7=3.5; 7-day summary drives the body
        monkeypatch.setattr(cr, "compute_cost_summary", lambda recs: self._fake_summary(5.0))

        # GCP export not configured → estimate fallback
        monkeypatch.setattr(
            "services.gcp_billing.get_gcp_mtd_costs",
            lambda: {"available": False, "reason": "not set"},
        )
        r = cr.build_cost_report()
        assert r["total_7d"] == pytest.approx(3.5)
        assert "Weekly Claude spend" in r["telegram"]
        assert "transcript_extraction" in r["telegram"]
        assert "# CropSight — Weekly Cost Report" in r["doc"]
        assert "NOT in this figure" in r["telegram"]  # estimate fallback

    def test_build_report_with_real_gcp(self, monkeypatch):
        from processors import cost_report as cr
        monkeypatch.setattr(cr.supabase_client, "get_token_usage_summary", lambda days=7: [])
        monkeypatch.setattr(cr, "compute_cost_summary", lambda recs: self._fake_summary(5.0))
        monkeypatch.setattr(
            "services.gcp_billing.get_gcp_mtd_costs",
            lambda: {"available": True, "cloud_run_usd": 41.2, "total_usd": 47.8,
                     "by_service": [("Cloud Run", 41.2), ("Cloud SQL", 6.6)], "reason": "ok"},
        )
        r = cr.build_cost_report()
        assert "actual" in r["telegram"]
        assert "$41.20" in r["telegram"]                 # real Cloud Run number shown
        assert "GCP (actual" in r["doc"]
        assert "NOT in this figure" not in r["telegram"]  # estimate replaced


# =============================================================================
# Scheduler window + fire-once (schedulers/cost_report_scheduler.py)
# =============================================================================

class TestCostReportScheduler:
    async def test_fires_in_window_dedups_per_week(self, monkeypatch):
        from schedulers import cost_report_scheduler as crs
        from config.settings import settings

        sched = crs.CostReportScheduler()
        monkeypatch.setattr(settings, "COST_REPORT_DAY", 6)   # Sunday
        monkeypatch.setattr(settings, "COST_REPORT_HOUR", 8)

        sent = []
        async def _send(key):
            sched._sent_weeks.add(key)   # the real _send_report records fire-once before sending
            sent.append(key)
        monkeypatch.setattr(sched, "_send_report", _send)

        sunday_8am = datetime(2026, 6, 14, 8, 30, tzinfo=ZoneInfo("Asia/Jerusalem"))  # 06-14 is Sunday
        monday = datetime(2026, 6, 15, 8, 30, tzinfo=ZoneInfo("Asia/Jerusalem"))

        class _DT(datetime):
            _now = sunday_8am
            @classmethod
            def now(cls, tz=None):
                return cls._now
        monkeypatch.setattr(crs, "datetime", _DT)

        assert await sched._check_and_send() is True     # fires Sunday in-window
        assert await sched._check_and_send() is False     # same week → deduped
        assert len(sent) == 1

        _DT._now = monday
        assert await sched._check_and_send() is False     # Monday → outside window
