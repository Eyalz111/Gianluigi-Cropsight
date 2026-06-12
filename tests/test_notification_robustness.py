"""
Tests for notification robustness:
  P2-16 — a task_mentions QUERY error must not be read as "zero mentions" (which
          would flag every open task stale and flood Eyal's weekly alert).
  P2-17 — if the intelligence-signal approval row can't be created, send Eyal a
          distinct alert and SKIP the normal "tap to approve" ping (which would
          dead-end) + the reminders.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def _query_chain(execute_value=None, execute_error=None):
    chain = MagicMock()
    for m in ("table", "select", "eq"):
        getattr(chain, m).return_value = chain
    if execute_error is not None:
        chain.execute.side_effect = execute_error
    else:
        chain.execute.return_value = execute_value
    return chain


# =============================================================================
# P2-16 — stale-task detection ignores a query error
# =============================================================================

class TestStaleTaskQueryError:
    _OLD_TASK = {
        "id": "t1", "title": "Old task", "assignee": "Eyal",
        "created_at": "2026-01-01T00:00:00",  # well over 7 days old
    }

    def _get_tasks(self, status=None, limit=None):
        return [dict(self._OLD_TASK)] if status == "pending" else []

    def test_query_error_does_not_flag_stale(self):
        from processors import proactive_alerts as pa
        chain = _query_chain(execute_error=RuntimeError("RLS misconfig"))
        with patch.object(pa.supabase_client, "get_tasks", side_effect=self._get_tasks), \
             patch.object(pa.supabase_client, "_client", chain):
            alerts = pa._check_stale_tasks()
        assert all("Old task" not in str(a) for a in alerts), \
            "a task_mentions query error must NOT flag the task stale"

    def test_genuine_zero_still_flags_stale(self):
        from processors import proactive_alerts as pa
        chain = _query_chain(execute_value=MagicMock(count=0))
        with patch.object(pa.supabase_client, "get_tasks", side_effect=self._get_tasks), \
             patch.object(pa.supabase_client, "_client", chain):
            alerts = pa._check_stale_tasks()
        assert any("Old task" in str(a) for a in alerts), \
            "a genuine zero-mention task should still be flagged stale"


# =============================================================================
# P2-17 — un-registered signal alerts Eyal and skips the dead-end ping
# =============================================================================

class TestSignalApprovalCreationFailure:
    @pytest.mark.asyncio
    async def test_failed_creation_alerts_and_skips_ping(self):
        from processors import intelligence_signal_agent as isa

        mock_spine = MagicMock()
        mock_spine.send_to_eyal = AsyncMock(return_value=True)

        with patch.object(isa.supabase_client, "create_pending_approval",
                          side_effect=RuntimeError("db down")), \
             patch.object(isa.supabase_client, "update_intelligence_signal"), \
             patch("services.orchestrator.spine.comms_spine", mock_spine), \
             patch.object(isa, "format_telegram_notification") as mock_fmt:
            await isa._submit_for_approval(
                signal_id="sig-1", drive_link="http://x", week_number=14,
                flags=[], research_source="perplexity", watchlist_changes=None,
            )

        # Eyal is alerted that the signal couldn't be registered...
        mock_spine.send_to_eyal.assert_awaited_once()
        assert "could not be registered" in mock_spine.send_to_eyal.call_args.args[0]
        # ...and the normal "tap to approve" ping is NOT built/sent.
        mock_fmt.assert_not_called()
