"""Blank-summary regression (2026-07-17 incident).

A team-tier meeting summary went out with no action items and 'none' assignees
while the DB held 19 real tasks. Cause: distribution fetched EVERY task with
get_tasks()'s default limit=100 (ordered deadline ASC, NULLs last) and then
filtered to the meeting in Python — the meeting's fresh NULL-deadline rows were
never inside that window, so the filter yielded [].

Covers: the server-side meeting_id filter, the distribution read that must use
it, and the rail that refuses to send content the DB contradicts.
"""
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# --- fake DB emulating the real read semantics (order + LIMIT, then rows) ----

# 100 tasks from OTHER meetings with early deadlines — they fill the window.
_OTHER = [
    {"meeting_id": "other", "title": f"old {i}", "assignee": "X", "deadline": "2026-03-26"}
    for i in range(100)
]
# The meeting's own tasks: NULL deadlines, so they sort LAST in deadline ASC.
_MINE = [
    {"meeting_id": "m1", "title": f"real task {i}", "assignee": "Eyal Zror", "deadline": None}
    for i in range(19)
]


def _fake_get_tasks(*, status=None, include_pending=False, meeting_id=None,
                    limit=100, **kw):
    """Emulate supabase get_tasks: filter server-side, THEN apply limit."""
    rows = _OTHER + _MINE          # deadline ASC — NULL-deadline rows land last
    if meeting_id:
        rows = [r for r in rows if r["meeting_id"] == meeting_id]
    return rows[:limit]


class TestServerSideMeetingFilter:
    """get_tasks must be able to narrow to one meeting in the QUERY, not after."""

    def test_meeting_id_is_applied_as_a_query_filter(self):
        from services.supabase_client import SupabaseClient

        chain = MagicMock()
        chain.select.return_value = chain
        chain.eq.return_value = chain
        chain.is_.return_value = chain
        chain.neq.return_value = chain
        chain.order.return_value = chain
        chain.limit.return_value = chain
        chain.execute.return_value = SimpleNamespace(data=[])

        sb = SupabaseClient.__new__(SupabaseClient)
        # `client` is a lazy property backed by _client; seed it so no real
        # connection is opened.
        sb._client = SimpleNamespace(table=lambda *a, **k: chain)
        sb.get_tasks(status=None, include_pending=True, meeting_id="m1", limit=500)

        assert ("meeting_id", "m1") in [c.args for c in chain.eq.call_args_list], \
            "meeting_id must filter server-side, before LIMIT truncates the window"
        assert chain.limit.call_args.args == (500,)


class TestDistributionReadsTheMeetingsTasks:
    """The incident itself: distribution must not lose tasks to the limit window."""

    async def test_distributed_content_has_the_meetings_tasks(self, monkeypatch):
        import guardrails.approval_flow as af

        sc = MagicMock()
        sc.get_pending_approval.return_value = {"approval_id": "m1"}
        sc.get_meeting.return_value = {
            "id": "m1", "approval_status": "pending", "title": "M",
            "sensitivity": "team", "summary": "S", "date": "2026-07-16",
        }
        sc.get_tasks.side_effect = _fake_get_tasks
        sc.list_decisions.return_value = []
        sc.list_follow_up_meetings.return_value = []
        sc.get_open_questions.return_value = []
        monkeypatch.setattr(af, "supabase_client", sc)
        monkeypatch.setattr(af, "_row_to_pending_info",
                            lambda row: {"type": "meeting_summary", "content": {}})
        monkeypatch.setattr(af, "cancel_auto_publish", lambda *a, **k: None)
        monkeypatch.setattr(af, "cancel_approval_reminders", lambda *a, **k: None)
        monkeypatch.setattr(af, "update_approval_status", AsyncMock())
        monkeypatch.setattr(af, "_promote_children_to_approved", lambda *a, **k: None)

        captured = {}

        async def fake_dist(meeting_id, content, sensitivity):
            captured["content"] = content
            return {"email_sent": True}

        monkeypatch.setattr(af, "distribute_approved_content", fake_dist)

        res = await af.process_response("m1", "approve", force_action="approve")

        assert res["action"] == "approved"
        # Pre-fix this was [] — the team got a summary with no action items.
        assert len(captured["content"]["tasks"]) == 19
        assert all(t["meeting_id"] == "m1" for t in captured["content"]["tasks"])


class TestDistributionIntactnessRail:
    """Last line of defence: never send content the DB positively contradicts."""

    def _sc_with_count(self, count, *, raises=False):
        chain = MagicMock()
        if raises:
            chain.select.return_value.eq.return_value.execute.side_effect = RuntimeError("db down")
        else:
            chain.select.return_value.eq.return_value.execute.return_value = \
                SimpleNamespace(count=count)
        return SimpleNamespace(client=SimpleNamespace(table=lambda *a, **k: chain))

    def test_blank_tasks_with_rows_in_db_is_blocked(self, monkeypatch):
        import guardrails.approval_flow as af
        monkeypatch.setattr(af, "supabase_client", self._sc_with_count(19))
        ok, reason = af._distribution_content_intact("m1", {"tasks": []})
        assert ok is False
        assert "19" in reason and "tasks" in reason

    def test_intact_content_passes(self, monkeypatch):
        import guardrails.approval_flow as af
        monkeypatch.setattr(af, "supabase_client", self._sc_with_count(19))
        ok, _ = af._distribution_content_intact(
            "m1", {"tasks": _MINE, "decisions": [{"d": 1}],
                   "open_questions": [{"q": 1}], "follow_ups": [{"f": 1}]},
        )
        assert ok is True

    def test_genuinely_empty_meeting_still_distributes(self, monkeypatch):
        """No rows either side — a real short meeting must not be blocked."""
        import guardrails.approval_flow as af
        monkeypatch.setattr(af, "supabase_client", self._sc_with_count(0))
        ok, _ = af._distribution_content_intact(
            "m1", {"tasks": [], "decisions": [], "open_questions": [], "follow_ups": []},
        )
        assert ok is True

    def test_count_failure_does_not_block(self, monkeypatch):
        """The rail is a net — a transient DB error must not stop a real send."""
        import guardrails.approval_flow as af
        monkeypatch.setattr(af, "supabase_client", self._sc_with_count(0, raises=True))
        ok, _ = af._distribution_content_intact("m1", {"tasks": []})
        assert ok is True

    async def test_blocked_distribution_sends_nothing(self, monkeypatch):
        import guardrails.approval_flow as af
        monkeypatch.setattr(af, "supabase_client", self._sc_with_count(19))
        drive = MagicMock()
        monkeypatch.setattr(af, "drive_service", drive)
        sheets = MagicMock()
        monkeypatch.setattr(af, "sheets_service", sheets)
        alert = AsyncMock()
        monkeypatch.setattr("services.alerting.send_system_alert", alert)

        res = await af.distribute_approved_content(
            "m1", {"title": "M", "summary": "S", "tasks": []}, sensitivity="team",
        )

        assert res["blocked"] is True
        assert res["email_sent"] is False and res["telegram_sent"] is False
        drive.save_meeting_summary.assert_not_called()
        sheets.add_task.assert_not_called()
        alert.assert_awaited_once()          # Eyal is told, loudly
