"""Tests for Cross-cutting X1: QA Agent scheduler.

Tests cover:
- run_qa_check() returns structured report
- Extraction quality detection
- Distribution completeness detection
- Scheduler health detection
- Data integrity checks
- format_qa_report() output
- QA scheduler lifecycle
- Morning brief integration
- MCP tool registration
"""

import pytest
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch, AsyncMock

from config.settings import settings


# =========================================================================
# run_qa_check: basic structure
# =========================================================================

class TestRunQACheck:

    @patch("schedulers.qa_scheduler.supabase_client")
    def test_returns_structured_report(self, mock_sc):
        mock_sc.list_meetings.return_value = []
        mock_sc.get_pending_approvals_by_status.return_value = []
        mock_sc.get_scheduler_heartbeats.return_value = []
        mock_sc.get_tasks.return_value = []

        from schedulers.qa_scheduler import run_qa_check

        report = run_qa_check()
        assert "timestamp" in report
        assert "checks" in report
        assert "issues" in report
        assert "score" in report
        assert report["score"] in ("healthy", "warning", "critical")

    @patch("schedulers.qa_scheduler.supabase_client")
    def test_healthy_when_no_issues(self, mock_sc):
        mock_sc.list_meetings.return_value = []
        mock_sc.get_pending_approvals_by_status.return_value = []
        mock_sc.get_scheduler_heartbeats.return_value = []
        mock_sc.get_tasks.return_value = []

        from schedulers.qa_scheduler import run_qa_check

        report = run_qa_check()
        assert report["score"] == "healthy"
        assert len(report["issues"]) == 0


# =========================================================================
# Extraction quality
# =========================================================================

class TestExtractionQuality:

    @patch("schedulers.qa_scheduler.supabase_client")
    def test_empty_extraction_flagged(self, mock_sc):
        mock_sc.list_meetings.return_value = [
            {"id": "m1", "title": "Empty Meeting", "date": "2026-04-01"},
        ]
        mock_sc.list_decisions.return_value = []
        mock_sc.get_tasks.return_value = []
        mock_sc.get_pending_approvals_by_status.return_value = []
        mock_sc.get_scheduler_heartbeats.return_value = []

        from schedulers.qa_scheduler import run_qa_check

        report = run_qa_check()
        eq = report["checks"]["extraction_quality"]
        assert eq["empty_extractions"] == 1
        assert any("Empty extraction" in i for i in report["issues"])

    @patch("schedulers.qa_scheduler.supabase_client")
    def test_good_extraction_no_issue(self, mock_sc):
        mock_sc.list_meetings.return_value = [
            {"id": "m1", "title": "Good Meeting", "date": "2026-04-01"},
        ]
        mock_sc.list_decisions.return_value = [{"id": "d1"}]
        mock_sc.get_tasks.return_value = [{"id": "t1", "meeting_id": "m1"}]
        mock_sc.get_pending_approvals_by_status.return_value = []
        mock_sc.get_scheduler_heartbeats.return_value = []

        from schedulers.qa_scheduler import run_qa_check

        report = run_qa_check()
        eq = report["checks"]["extraction_quality"]
        assert eq["empty_extractions"] == 0

    @patch("schedulers.qa_scheduler.supabase_client")
    def test_rejected_meeting_not_flagged_as_empty(self, mock_sc):
        # A rejected meeting is a tombstone: reject cascade-deletes its children,
        # so 0 tasks/decisions is CORRECT, not an empty-extraction fault. It must
        # be excluded (else every reject fires a daily false alarm). [2026-07-25]
        mock_sc.list_meetings.return_value = [
            {"id": "m1", "title": "Rejected Meeting", "date": "2026-04-01",
             "approval_status": "rejected"},
        ]
        mock_sc.list_decisions.return_value = []
        mock_sc.get_tasks.return_value = []
        mock_sc.get_pending_approvals_by_status.return_value = []
        mock_sc.get_scheduler_heartbeats.return_value = []

        from schedulers.qa_scheduler import run_qa_check

        report = run_qa_check()
        eq = report["checks"]["extraction_quality"]
        assert eq["empty_extractions"] == 0
        assert eq["meetings_checked"] == 0        # rejected tombstone filtered out
        assert not any("Empty extraction" in i for i in report["issues"])


# =========================================================================
# Scheduler health
# =========================================================================

class TestSchedulerHealth:

    @patch("schedulers.qa_scheduler.supabase_client")
    def test_stale_heartbeat_flagged(self, mock_sc):
        # orphan_cleanup is always-expected (not flag-gated) with a 24h interval,
        # so a 72h-old heartbeat exceeds 2x and is stale. Reads last_run_at — the
        # column the heartbeat upsert actually writes. [2026-07-25 audit fault #2]
        old = (datetime.now(timezone.utc) - timedelta(hours=72)).isoformat()
        mock_sc.list_meetings.return_value = []
        mock_sc.get_pending_approvals_by_status.return_value = []
        mock_sc.get_tasks.return_value = []
        mock_sc.get_scheduler_heartbeats.return_value = [
            {"scheduler_name": "orphan_cleanup", "last_run_at": old},
        ]

        from schedulers.qa_scheduler import run_qa_check

        report = run_qa_check()
        sh = report["checks"]["scheduler_health"]
        assert "orphan_cleanup" in sh["stale"]
        assert any("orphan_cleanup" in i for i in report["issues"])

    @patch("schedulers.qa_scheduler.supabase_client")
    def test_fresh_heartbeat_ok(self, mock_sc):
        fresh = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        mock_sc.list_meetings.return_value = []
        mock_sc.get_pending_approvals_by_status.return_value = []
        mock_sc.get_tasks.return_value = []
        mock_sc.get_scheduler_heartbeats.return_value = [
            {"scheduler_name": "orphan_cleanup", "last_run_at": fresh},
        ]

        from schedulers.qa_scheduler import run_qa_check

        report = run_qa_check()
        sh = report["checks"]["scheduler_health"]
        assert sh["stale"] == []

    @patch("schedulers.qa_scheduler.supabase_client")
    def test_missing_heartbeat_flagged(self, mock_sc):
        mock_sc.list_meetings.return_value = []
        mock_sc.get_pending_approvals_by_status.return_value = []
        mock_sc.get_tasks.return_value = []
        mock_sc.get_scheduler_heartbeats.return_value = [
            {"scheduler_name": "ghost_scheduler", "last_run_at": None},
        ]

        from schedulers.qa_scheduler import run_qa_check

        report = run_qa_check()
        sh = report["checks"]["scheduler_health"]
        assert "ghost_scheduler" in sh["missing"]

    @patch("schedulers.qa_scheduler.supabase_client")
    def test_disabled_scheduler_skipped(self, mock_sc):
        # A scheduler whose enable-flag is OFF is intentionally not running: a
        # long-old heartbeat must be SKIPPED, not reported stale/missing — else the
        # QA score sticks at 'critical' forever and buries real issues. This is the
        # core of the fault-#2 fix. [2026-07-25 audit]
        old = (datetime.now(timezone.utc) - timedelta(days=40)).isoformat()
        mock_sc.list_meetings.return_value = []
        mock_sc.get_pending_approvals_by_status.return_value = []
        mock_sc.get_tasks.return_value = []
        mock_sc.get_scheduler_heartbeats.return_value = [
            {"scheduler_name": "alert_scheduler", "last_run_at": old},
        ]

        from schedulers.qa_scheduler import run_qa_check

        with patch.object(settings, "ALERT_SCHEDULER_ENABLED", False):
            report = run_qa_check()
        sh = report["checks"]["scheduler_health"]
        assert "alert_scheduler" in sh["skipped_disabled"]
        assert "alert_scheduler" not in sh["stale"]
        assert "alert_scheduler" not in sh["missing"]

    @patch("schedulers.qa_scheduler.supabase_client")
    def test_weekly_scheduler_not_falsely_stale(self, mock_sc):
        # A weekly scheduler that last ran 6 days ago is within 2x its 7-day
        # expected interval — the old flat 48h threshold false-flagged it. Delegates
        # to health_monitor's per-scheduler interval map. [2026-07-25 audit]
        six_days = (datetime.now(timezone.utc) - timedelta(days=6)).isoformat()
        mock_sc.list_meetings.return_value = []
        mock_sc.get_pending_approvals_by_status.return_value = []
        mock_sc.get_tasks.return_value = []
        mock_sc.get_scheduler_heartbeats.return_value = [
            {"scheduler_name": "knowledge_weekly", "last_run_at": six_days},
        ]

        from schedulers.qa_scheduler import run_qa_check

        with patch.object(settings, "KNOWLEDGE_WEEKLY_ENABLED", True):
            report = run_qa_check()
        sh = report["checks"]["scheduler_health"]
        assert sh["stale"] == []
        assert "knowledge_weekly" not in sh["missing"]


# =========================================================================
# Format report
# =========================================================================

class TestFormatQAReport:

    def test_healthy_report(self):
        from schedulers.qa_scheduler import format_qa_report

        report = {
            "timestamp": "2026-04-02T06:00:00",
            "score": "healthy",
            "checks": {
                "extraction_quality": {"meetings_checked": 3, "empty_extractions": 0, "low_extractions": 0},
                "distribution_completeness": {"approvals_checked": 2, "undistributed": 0},
                "scheduler_health": {"schedulers_checked": 4, "stale": [], "missing": []},
                "data_integrity": {"tasks_without_meeting": 0, "meetings_without_embeddings": 0},
            },
            "issues": [],
        }

        formatted = format_qa_report(report)
        assert "OK" in formatted
        assert "3 meetings checked" in formatted
        assert "All clean" in formatted

    def test_warning_report_shows_issues(self):
        from schedulers.qa_scheduler import format_qa_report

        report = {
            "timestamp": "2026-04-02T06:00:00",
            "score": "warning",
            "checks": {
                "extraction_quality": {"meetings_checked": 3, "empty_extractions": 1, "low_extractions": 0},
                "distribution_completeness": {"approvals_checked": 0, "undistributed": 0},
                "scheduler_health": {"schedulers_checked": 2, "stale": ["email_watcher"], "missing": []},
                "data_integrity": {"tasks_without_meeting": 0, "meetings_without_embeddings": 0},
            },
            "issues": ["Empty extraction: Test Meeting", "Stale: email_watcher"],
        }

        formatted = format_qa_report(report)
        assert "WARN" in formatted
        assert "Issues (2)" in formatted
        assert "email_watcher" in formatted


# =========================================================================
# Scheduler lifecycle
# =========================================================================

class TestQASchedulerLifecycle:

    def test_stop_sets_flag(self):
        from schedulers.qa_scheduler import QAScheduler

        scheduler = QAScheduler()
        scheduler._running = True
        scheduler.stop()
        assert not scheduler._running

    def test_last_report_initially_none(self):
        from schedulers.qa_scheduler import QAScheduler

        scheduler = QAScheduler()
        assert scheduler.last_report is None

    @patch("schedulers.qa_scheduler.supabase_client")
    def test_last_report_populated_after_check(self, mock_sc):
        mock_sc.list_meetings.return_value = []
        mock_sc.get_pending_approvals_by_status.return_value = []
        mock_sc.get_scheduler_heartbeats.return_value = []
        mock_sc.get_tasks.return_value = []

        from schedulers.qa_scheduler import QAScheduler, run_qa_check

        scheduler = QAScheduler()
        scheduler._last_report = run_qa_check()
        assert scheduler.last_report is not None
        assert "score" in scheduler.last_report


# =========================================================================
# Morning brief integration
# =========================================================================

class TestMorningBriefQAIntegration:

    def test_morning_brief_includes_qa_section(self):
        """Verify morning brief source includes QA health section."""
        import inspect
        import processors.morning_brief as module
        source = inspect.getsource(module.compile_morning_brief)
        assert "qa_health" in source
        assert "qa_scheduler" in source


# =========================================================================
# MCP tool
# =========================================================================

class TestMCPQACheckTool:

    def test_mcp_includes_qa_tool(self):
        import inspect
        import services.mcp_server as module
        source = inspect.getsource(module)
        assert "run_qa_check" in source
        assert "Run on-demand QA quality check" in source


# =========================================================================
# Score calculation
# =========================================================================

class TestScoreCalculation:

    @patch("schedulers.qa_scheduler.supabase_client")
    def test_critical_score_many_issues(self, mock_sc):
        """4+ issues should produce 'critical' score."""
        # Create a scenario with multiple issues
        old = (datetime.now(timezone.utc) - timedelta(hours=100)).isoformat()
        mock_sc.list_meetings.return_value = [
            {"id": f"m{i}", "title": f"Meeting {i}", "date": "2026-04-01"}
            for i in range(4)
        ]
        mock_sc.list_decisions.return_value = []
        mock_sc.get_tasks.return_value = []
        mock_sc.get_pending_approvals_by_status.return_value = []
        mock_sc.get_scheduler_heartbeats.return_value = [
            {"scheduler_name": "s1", "last_run_at": old},
        ]
        mock_sc.get_meeting.return_value = {"id": "m1"}
        mock_sc.client.table.return_value.select.return_value.eq.return_value.limit.return_value.execute.return_value = MagicMock(data=[])

        from schedulers.qa_scheduler import run_qa_check

        report = run_qa_check()
        assert report["score"] == "critical"
        assert len(report["issues"]) >= 4
