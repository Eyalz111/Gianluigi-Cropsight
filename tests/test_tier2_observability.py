"""
Tests for Tier 2 observability additions:
- T2.2 Morning brief "System State" section
- T2.4 QA scheduler _check_rejected_meetings
"""

import pytest
from unittest.mock import patch, MagicMock, AsyncMock


# =============================================================================
# T2.2 — Morning brief system_state section
# =============================================================================


class TestMorningBriefSystemState:
    def test_format_all_clear(self):
        """When everything is fine, show single 'all clear' line."""
        from processors.morning_brief import format_morning_brief

        brief = {
            "sections": [{
                "type": "system_state",
                "title": "System State",
                "watcher_status": "ok",
                "rejected_count": 0,
                "errors_24h": 0,
                "pending_queue": 0,
            }],
            "stats": {},
            "scan_ids": [],
        }
        result = format_morning_brief(brief)
        assert "System: all clear" in result

    def test_format_with_rejected_meetings(self):
        """When rejected meetings exist, show detailed breakdown."""
        from processors.morning_brief import format_morning_brief

        brief = {
            "sections": [{
                "type": "system_state",
                "title": "System State",
                "watcher_status": "ok",
                "rejected_count": 2,
                "errors_24h": 0,
                "pending_queue": 0,
            }],
            "stats": {},
            "scan_ids": [],
        }
        result = format_morning_brief(brief)
        assert "2 rejected meetings" in result
        assert "all clear" not in result

    def test_format_with_errors(self):
        """Errors in 24h trigger the with-issues variant."""
        from processors.morning_brief import format_morning_brief

        brief = {
            "sections": [{
                "type": "system_state",
                "title": "System State",
                "watcher_status": "ok",
                "rejected_count": 0,
                "errors_24h": 5,
                "pending_queue": 0,
            }],
            "stats": {},
            "scan_ids": [],
        }
        result = format_morning_brief(brief)
        assert "5 errors in 24h" in result

    def test_format_with_stale_watcher(self):
        """Stale watcher should NOT be all-clear."""
        from processors.morning_brief import format_morning_brief

        brief = {
            "sections": [{
                "type": "system_state",
                "title": "System State",
                "watcher_status": "stale",
                "rejected_count": 0,
                "errors_24h": 0,
                "pending_queue": 0,
            }],
            "stats": {},
            "scan_ids": [],
        }
        result = format_morning_brief(brief)
        assert "watcher stale" in result

    def test_format_pending_queue_shows_but_still_clean(self):
        """Pending approvals queue is displayed but doesn't count as error."""
        from processors.morning_brief import format_morning_brief

        brief = {
            "sections": [{
                "type": "system_state",
                "title": "System State",
                "watcher_status": "ok",
                "rejected_count": 0,
                "errors_24h": 0,
                "pending_queue": 3,
            }],
            "stats": {},
            "scan_ids": [],
        }
        result = format_morning_brief(brief)
        assert "3 pending approvals" in result


# =============================================================================
# T2.4 — QA scheduler _check_rejected_meetings
# =============================================================================


class TestCheckRejectedMeetings:
    def test_zero_rejected_is_clean(self):
        """No rejected meetings returns 0 counts and no issues."""
        from schedulers.qa_scheduler import _check_rejected_meetings

        issues: list[str] = []
        with patch("schedulers.qa_scheduler.supabase_client") as mock_sb:
            mock_sb.list_meetings.return_value = []
            result = _check_rejected_meetings(issues)

        assert result["rejected_meetings"] == 0
        assert result["rejected_with_orphans"] == 0
        assert result["orphan_tasks"] == 0
        assert issues == []

    def test_rejected_without_orphans_does_not_alert(self):
        """A rejected meeting with no child data is not flagged (cascaded properly)."""
        from schedulers.qa_scheduler import _check_rejected_meetings

        issues: list[str] = []
        with patch("schedulers.qa_scheduler.supabase_client") as mock_sb:
            mock_sb.list_meetings.return_value = [
                {"id": "rej-1", "title": "Orphan-free"}
            ]
            # Mock the child-table count queries to return 0
            mock_chain = MagicMock()
            mock_chain.select.return_value = mock_chain
            mock_chain.eq.return_value = mock_chain
            mock_chain.execute.return_value = MagicMock(count=0)
            mock_sb.client.table.return_value = mock_chain

            result = _check_rejected_meetings(issues)

        assert result["rejected_meetings"] == 1
        assert result["rejected_with_orphans"] == 0
        assert issues == []

    def test_rejected_with_orphans_raises_issue(self):
        """A rejected meeting with residual tasks/decisions is surfaced as an issue."""
        from schedulers.qa_scheduler import _check_rejected_meetings

        issues: list[str] = []
        with patch("schedulers.qa_scheduler.supabase_client") as mock_sb:
            mock_sb.list_meetings.return_value = [
                {"id": "rej-1", "title": "Bad cascade"}
            ]

            # Cycle through responses for tasks/decisions/embeddings
            counts = iter([3, 2, 45])

            def table_chain(_name):
                chain = MagicMock()
                chain.select.return_value = chain
                chain.eq.return_value = chain
                chain.execute.return_value = MagicMock(count=next(counts))
                return chain

            mock_sb.client.table.side_effect = table_chain

            result = _check_rejected_meetings(issues)

        assert result["rejected_meetings"] == 1
        assert result["rejected_with_orphans"] == 1
        assert result["orphan_tasks"] == 3
        assert result["orphan_decisions"] == 2
        assert result["orphan_embeddings"] == 45
        assert len(issues) == 1
        assert "cleanup_rejected_meetings" in issues[0]
        assert "3 tasks" in issues[0]
