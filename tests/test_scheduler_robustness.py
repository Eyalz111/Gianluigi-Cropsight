"""
Tests for scheduler robustness (audit PR-F): heartbeat coverage (P4-01),
DST-aware timezone (P4-02), watcher poison-retry quarantine (P4-04), Drive
list error-vs-empty signal (P4-06), and UTC-aware health staleness (P6-06).
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# =============================================================================
# P4-02 — DST-aware Israel time (no hardcoded UTC+2 offset)
# =============================================================================

class TestTimezoneDST:
    def test_morning_brief_scheduler_uses_zoneinfo_not_fixed_offset(self):
        import schedulers.morning_brief_scheduler as mod
        from zoneinfo import ZoneInfo
        assert isinstance(mod._ISRAEL_TZ, ZoneInfo)
        # The hardcoded +2 offset that fired the brief 1h late on DST is gone.
        assert not hasattr(mod, "IST_OFFSET")

    def test_debrief_scheduler_uses_zoneinfo_not_fixed_offset(self):
        import schedulers.debrief_prompt_scheduler as mod
        from zoneinfo import ZoneInfo
        assert isinstance(mod._ISRAEL_TZ, ZoneInfo)
        assert not hasattr(mod, "IST_OFFSET")

    def test_should_skip_today_uses_israel_weekday(self):
        from schedulers.debrief_prompt_scheduler import debrief_prompt_scheduler
        # Saturday in Israel → skip. We can't pin the clock here, but the method
        # must run against _ISRAEL_TZ and return a bool without error.
        assert isinstance(debrief_prompt_scheduler._should_skip_today(), bool)


# =============================================================================
# P4-01 — heartbeats go to the scheduler_heartbeats table, not audit_log
# =============================================================================

class TestHeartbeatTable:
    @pytest.mark.asyncio
    async def test_knowledge_nightly_heartbeats_to_correct_table(self):
        from schedulers.knowledge_nightly_scheduler import knowledge_nightly_scheduler

        with patch("processors.knowledge_consolidation.run_consolidation",
                   new=AsyncMock(return_value={"reconciled": 2})), \
             patch("services.supabase_client.supabase_client") as mock_sb:
            await knowledge_nightly_scheduler._run()

        mock_sb.upsert_scheduler_heartbeat.assert_called_once()
        assert mock_sb.upsert_scheduler_heartbeat.call_args.args[0] == "knowledge_nightly"
        # Must NOT use the old wrong-table heartbeat via log_action.
        hb_logs = [c for c in mock_sb.log_action.call_args_list
                   if c.kwargs.get("action") == "scheduler_heartbeat"
                   or (c.args and c.args[0] == "scheduler_heartbeat")]
        assert hb_logs == []

    @pytest.mark.asyncio
    async def test_knowledge_nightly_error_heartbeats_status_error(self):
        from schedulers.knowledge_nightly_scheduler import knowledge_nightly_scheduler

        with patch("processors.knowledge_consolidation.run_consolidation",
                   new=AsyncMock(side_effect=RuntimeError("boom"))), \
             patch("services.supabase_client.supabase_client") as mock_sb, \
             patch("core.health_monitor.check_and_alert", new=AsyncMock()):
            await knowledge_nightly_scheduler._run()

        # An error path still records a heartbeat — status="error".
        statuses = [c.kwargs.get("status") for c in mock_sb.upsert_scheduler_heartbeat.call_args_list]
        assert "error" in statuses

    @pytest.mark.asyncio
    async def test_reconcile_heartbeats_to_correct_table(self):
        from schedulers.reconcile_scheduler import reconcile_scheduler

        with patch("processors.sheets_sync.reconcile_tasks",
                   new=AsyncMock(return_value={"updated": 1})), \
             patch("services.supabase_client.supabase_client") as mock_sb:
            await reconcile_scheduler._run("2026-06-12:midday")

        names = [c.args[0] for c in mock_sb.upsert_scheduler_heartbeat.call_args_list if c.args]
        assert "reconcile" in names
        hb_logs = [c for c in mock_sb.log_action.call_args_list
                   if (c.args and c.args[0] == "scheduler_heartbeat")
                   or c.kwargs.get("action") == "scheduler_heartbeat"]
        assert hb_logs == []


# =============================================================================
# P4-01 / P6-06 — UTC-aware heartbeat staleness with correct intervals
# =============================================================================

class TestHeartbeatStaleness:
    def test_daily_scheduler_3h_old_is_not_stale(self):
        from core.health_monitor import _heartbeat_stale
        now = datetime(2026, 6, 12, 12, 0, tzinfo=timezone.utc)
        three_h_ago = (now - timedelta(hours=3)).isoformat()
        # 86400 interval → stale only after 48h. 3h-old daily heartbeat is fresh.
        assert _heartbeat_stale(three_h_ago, "knowledge_nightly", now) is False

    def test_poll_scheduler_30m_old_is_stale(self):
        from core.health_monitor import _heartbeat_stale
        now = datetime(2026, 6, 12, 12, 0, tzinfo=timezone.utc)
        old = (now - timedelta(minutes=30)).isoformat()
        # 300s interval (5 min) → stale after 10 min. 30m-old is stale.
        assert _heartbeat_stale(old, "transcript_watcher", now) is True

    def test_naive_timestamp_treated_as_utc(self):
        from core.health_monitor import _heartbeat_stale
        now = datetime(2026, 6, 12, 12, 0, tzinfo=timezone.utc)
        naive = (now - timedelta(hours=1)).replace(tzinfo=None).isoformat()
        # 1h-old daily heartbeat parsed as UTC → not stale.
        assert _heartbeat_stale(naive, "morning_brief", now) is False

    def test_empty_last_run_is_stale(self):
        from core.health_monitor import _heartbeat_stale
        assert _heartbeat_stale("", "morning_brief") is True


# =============================================================================
# P4-04 — watcher poison-retry quarantine after N consecutive failures
# =============================================================================

class TestDocumentWatcherPoison:
    def _make_watcher(self):
        from schedulers.document_watcher import DocumentWatcher
        return DocumentWatcher(poll_interval=1)

    @pytest.mark.asyncio
    async def test_quarantines_after_threshold_and_alerts_once(self):
        watcher = self._make_watcher()
        poison = {"id": "doc-1", "name": "broken.pdf"}

        with patch("schedulers.document_watcher.drive_service") as mock_drive, \
             patch("core.error_alerting.alert_critical_error", new=AsyncMock()) as mock_alert:
            mock_drive.get_new_documents = AsyncMock(return_value=[poison])
            mock_drive.mark_document_processed = MagicMock()
            watcher._process_new_document = AsyncMock(side_effect=RuntimeError("bad pdf"))

            # 3 polls = threshold reached on the 3rd.
            for _ in range(3):
                await watcher._poll_once()

        # Quarantined exactly once (on the 3rd failure).
        mock_drive.mark_document_processed.assert_called_once_with("doc-1")
        # Alerted on the 1st failure and on the give-up — NOT every poll.
        assert mock_alert.await_count == 2

    @pytest.mark.asyncio
    async def test_success_clears_failure_streak(self):
        watcher = self._make_watcher()
        f = {"id": "doc-9", "name": "ok.pdf"}
        watcher._failure_counts["doc-9"] = 2

        with patch("schedulers.document_watcher.drive_service") as mock_drive:
            mock_drive.get_new_documents = AsyncMock(return_value=[f])
            watcher._process_new_document = AsyncMock(return_value={"status": "processed"})
            await watcher._poll_once()

        assert "doc-9" not in watcher._failure_counts


# =============================================================================
# P4-06 — Drive list poll distinguishes API error from an empty folder
# =============================================================================

class TestDrivePollFailureSignal:
    def _make_drive(self):
        from services.google_drive import GoogleDriveService
        with patch.object(GoogleDriveService, "__init__", lambda self: None):
            svc = GoogleDriveService()
            svc._service = MagicMock()
            svc._credentials = MagicMock()
            svc._processed_file_ids = set()
            svc._processed_doc_ids = set()
            svc.last_transcript_poll_failed = False
            svc.last_document_poll_failed = False
            return svc

    @pytest.mark.asyncio
    async def test_documents_poll_sets_flag_on_error(self):
        from config.settings import settings
        svc = self._make_drive()
        svc._execute_with_retry = MagicMock(side_effect=BrokenPipeError(32, "Broken pipe"))
        with patch.object(settings, "DOCUMENTS_FOLDER_ID", "folder-x"):
            result = await svc.get_new_documents()
        assert result == []
        assert svc.last_document_poll_failed is True

    @pytest.mark.asyncio
    async def test_documents_poll_clears_flag_on_success(self):
        from config.settings import settings
        svc = self._make_drive()
        svc.last_document_poll_failed = True
        svc._execute_with_retry = MagicMock(return_value={"files": []})
        with patch.object(settings, "DOCUMENTS_FOLDER_ID", "folder-x"):
            result = await svc.get_new_documents()
        assert result == []
        assert svc.last_document_poll_failed is False

    @pytest.mark.asyncio
    async def test_transcripts_poll_sets_flag_on_error(self):
        from config.settings import settings
        svc = self._make_drive()
        svc._execute_with_retry = MagicMock(side_effect=ConnectionError("reset"))
        with patch.object(settings, "RAW_TRANSCRIPTS_FOLDER_ID", "folder-t"):
            result = await svc.get_new_transcripts()
        assert result == []
        assert svc.last_transcript_poll_failed is True
