"""
Tests for the small correctness cleanups:
  P3-16 — clearing a task deadline (deadline=None) also clears the confidence to
          'NONE' (a stale 'EXPLICIT' on a NULL deadline is contradictory).
  P2-19 — the Gantt-slide section table skips a degenerate week range instead of
          ZeroDivision / negative column widths.
  P6-10 — the dead imports were removed (modules still import).
"""

from unittest.mock import MagicMock, patch


def _chain(execute_data):
    chain = MagicMock()
    for m in ("table", "select", "insert", "update", "delete", "eq", "order", "limit"):
        getattr(chain, m).return_value = chain
    chain.execute.return_value = MagicMock(data=execute_data)
    return chain


class TestDeadlineClearConfidence:
    def test_clear_deadline_forces_confidence_none(self):
        from services.supabase_client import supabase_client
        chain = _chain([{"id": "t1", "deadline": None, "deadline_confidence": "NONE"}])
        with patch.object(supabase_client, "_client", chain):
            # Clearing with the default confidence (EXPLICIT) must be overridden.
            supabase_client.update_task_deadline("t1", None)
        payload = chain.update.call_args.args[0]
        assert payload["deadline_confidence"] == "NONE"
        assert payload["deadline"] is None

    def test_set_deadline_keeps_explicit(self):
        from datetime import date
        from services.supabase_client import supabase_client
        chain = _chain([{"id": "t1"}])
        with patch.object(supabase_client, "_client", chain):
            supabase_client.update_task_deadline("t1", date(2026, 7, 1))
        payload = chain.update.call_args.args[0]
        assert payload["deadline_confidence"] == "EXPLICIT"


class TestGanttSlideRangeGuard:
    def test_invalid_week_range_skips_without_crash(self):
        from processors.gantt_slide import _add_section_table
        # end (5) < start (10) → num_weeks <= 0 → must return y_offset, not crash.
        out = _add_section_table(
            slide=None, section_name="X", items=[],
            week_range=(10, 5), current_week=1, y_offset=2.5,
        )
        assert out == 2.5


class TestDeadImportsRemoved:
    def test_modules_import_without_dead_names(self):
        import importlib
        import core.analyst_agent as aa
        import core.system_prompt as sp
        importlib.reload(aa)
        importlib.reload(sp)
        # The removed names must no longer be module attributes.
        assert not hasattr(aa, "get_client")
        assert not hasattr(sp, "get_team_member_names")
