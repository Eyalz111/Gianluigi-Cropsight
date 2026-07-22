"""Within-meeting duplicate action-item FLAGGING (2026-07-22).

Detect-and-flag only: an LLM identifies task pairs that are the same underlying
action worded differently (the "send the Scope-of-Work doc to Ido" vs "send the
deployment task-list doc to Ido" class). We never auto-merge — a wrong merge
would silently drop a real task. These tests pin the index validation (no
hallucinated/self/out-of-range pairs reach the card) and the flag rendering.
"""
import json
from unittest.mock import patch

import processors.duplicate_task_detector as d


_TASKS = [
    {"title": "Arrange a company credit card", "assignee": "Eyal"},
    {"title": "Integrate data pipeline with database via API", "assignee": "Roye"},
    {"title": "Send the Scope-of-Work doc to Ido before the meeting", "assignee": "Roye"},
    {"title": "Send the deployment task-list doc to Ido before the meeting", "assignee": "Roye"},
]


def _llm(payload):
    return patch.object(d, "call_llm", lambda **k: (json.dumps(payload), None))


class TestDetect:
    async def test_flags_same_action_pair(self):
        with _llm({"duplicates": [{"a": 3, "b": 4, "reason": "both send Ido a pre-meeting doc"}]}):
            pairs = await d.detect_duplicate_task_pairs(_TASKS)
        assert pairs == [{"a": 3, "b": 4, "reason": "both send Ido a pre-meeting doc"}]

    async def test_normalizes_order_and_dedupes(self):
        with _llm({"duplicates": [{"a": 4, "b": 3, "reason": "x"}, {"a": 3, "b": 4, "reason": "y"}]}):
            pairs = await d.detect_duplicate_task_pairs(_TASKS)
        assert len(pairs) == 1 and pairs[0]["a"] == 3 and pairs[0]["b"] == 4

    async def test_drops_self_and_out_of_range(self):
        with _llm({"duplicates": [
            {"a": 2, "b": 2, "reason": "self"},          # self -> drop
            {"a": 1, "b": 99, "reason": "oor"},          # out of range -> drop
            {"a": "x", "b": 3, "reason": "bad type"},    # non-int -> drop
            {"a": 3, "b": 4, "reason": "valid"},
        ]}):
            pairs = await d.detect_duplicate_task_pairs(_TASKS)
        assert pairs == [{"a": 3, "b": 4, "reason": "valid"}]

    async def test_under_two_tasks_returns_empty(self):
        assert await d.detect_duplicate_task_pairs([_TASKS[0]]) == []
        assert await d.detect_duplicate_task_pairs([]) == []

    async def test_no_duplicates(self):
        with _llm({"duplicates": []}):
            assert await d.detect_duplicate_task_pairs(_TASKS) == []

    async def test_llm_error_is_fail_open(self):
        def _boom(**k):
            raise RuntimeError("llm down")
        with patch.object(d, "call_llm", _boom):
            assert await d.detect_duplicate_task_pairs(_TASKS) == []

    async def test_unparseable_response_returns_empty(self):
        with _llm_raw("not json at all"):
            assert await d.detect_duplicate_task_pairs(_TASKS) == []


def _llm_raw(text):
    return patch.object(d, "call_llm", lambda **k: (text, None))


class TestFormat:
    def test_empty_pairs_no_banner(self):
        assert d.format_duplicate_flag([], _TASKS) == ""

    def test_banner_names_both_items(self):
        out = d.format_duplicate_flag([{"a": 3, "b": 4, "reason": "both send Ido a doc"}], _TASKS)
        assert "Possible duplicate" in out
        assert "#3" in out and "#4" in out
        assert "both send Ido a doc" in out
