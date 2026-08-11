"""A disabled scheduler must not alarm, and the map must not lie.

`_check_scheduler_health` reports any scheduler whose heartbeat has gone stale
— unless the map says it is switched off. Two ways that goes wrong, both seen
on 2026-08-11:

1. A scheduler gains a kill switch and nobody adds it here. `meeting_prep` was
   turned off and had no entry, so its heartbeat would have gone stale and the
   daily QA DM would have reported it every day forever. A disabled scheduler
   alarming is exactly the noise this work is removing.

2. An entry names a flag that does not exist. `weekly_digest` pointed at
   WEEKLY_DIGEST_ENABLED, which is not in settings, so the lookup fell through
   to True — the map claimed a switch nobody could throw. Same shape as the
   PROJECT_LEARNING_ENABLED trap.
"""
import pytest

from config.settings import settings
from schedulers.qa_scheduler import _SCHEDULER_ENABLE_FLAG, _scheduler_expected


class TestTheMapDoesNotLie:
    def test_every_named_flag_exists_in_settings(self):
        """A flag that isn't real silently defaults to 'expected'."""
        missing = [f for f in _SCHEDULER_ENABLE_FLAG.values()
                   if not hasattr(settings, f)]
        assert not missing, f"map names flags that do not exist: {missing}"

    def test_gated_schedulers_are_mapped(self):
        """Anything main.py gates on a flag must be here, or switching it off
        turns its heartbeat into a permanent daily false alarm."""
        for name in ("meeting_prep", "morning_brief", "task_reminder",
                     "transcript_watcher", "email_watcher"):
            assert name in _SCHEDULER_ENABLE_FLAG, f"{name} is gated but unmapped"

    def test_a_disabled_scheduler_is_not_expected(self, monkeypatch):
        monkeypatch.setattr(settings, "MEETING_PREP_ENABLED", False, raising=False)
        assert _scheduler_expected("meeting_prep") is False

    def test_an_enabled_scheduler_is_expected(self, monkeypatch):
        monkeypatch.setattr(settings, "MEETING_PREP_ENABLED", True, raising=False)
        assert _scheduler_expected("meeting_prep") is True

    def test_an_unmapped_scheduler_defaults_to_expected(self):
        """A new loop must never be silently ignored."""
        assert _scheduler_expected("some_brand_new_loop") is True


class TestOnlyOneQaCheckWrites:
    """Eleven of the twelve checks read; `_check_topic_state_staleness` flips
    state_json to 'stale'. That surprised the author of this test, who ran the
    whole set as a supposedly read-only probe and mutated 33 rows. Pin it so
    the write set is a deliberate choice rather than a discovery."""

    def test_the_write_set_is_exactly_one_check(self):
        import ast
        import pathlib

        tree = ast.parse(pathlib.Path("schedulers/qa_scheduler.py").read_text(
            encoding="utf-8"))
        writes = {"update", "insert", "upsert", "delete"}
        writers = {
            node.name
            for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name.startswith("_check_")
            and any(isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                    and n.func.attr in writes for n in ast.walk(node))
        }
        assert writers == {"_check_topic_state_staleness"}, (
            f"the set of QA checks that WRITE changed: {writers}")
