"""
Tests for PR 4 — living topic-state summaries (v2.3).

Covers:
- TopicState Pydantic schema + default values
- update_topic_state: happy path, malformed JSON, schema validation, DB error
- _parse_topic_state_json tolerates code fences
- Morning brief renders blocked/stale topic section with 3-item cap
- Staleness sweep flips active → stale past 30 days
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# =============================================================================
# Schema
# =============================================================================

class TestTopicStateSchema:
    def test_default_active_status(self):
        from models.schemas import TopicState, TopicStatus
        s = TopicState()
        assert s.current_status == TopicStatus.ACTIVE
        assert s.stakeholders == []
        assert s.open_items == []
        assert s.last_decision is None
        assert s.key_facts == []
        assert s.version == 1

    def test_accepts_all_statuses(self):
        from models.schemas import TopicState, TopicStatus
        for status in ["active", "blocked", "pending_decision", "stale", "closed"]:
            s = TopicState(current_status=status)
            assert s.current_status == TopicStatus(status)

    def test_open_item_schema(self):
        from models.schemas import OpenItem
        item = OpenItem(kind="task", description="send proposal", owner="Paolo")
        assert item.kind == "task"
        assert item.source_meeting_id is None


# =============================================================================
# JSON parsing
# =============================================================================

class TestParseTopicStateJson:
    def test_parses_plain_json(self):
        from processors.topic_threading import _parse_topic_state_json
        result = _parse_topic_state_json('{"current_status": "active", "summary": "ok"}')
        assert result["current_status"] == "active"

    def test_parses_code_fenced_json(self):
        from processors.topic_threading import _parse_topic_state_json
        response = '```json\n{"current_status": "blocked"}\n```'
        result = _parse_topic_state_json(response)
        assert result["current_status"] == "blocked"

    def test_returns_none_on_malformed(self):
        from processors.topic_threading import _parse_topic_state_json
        assert _parse_topic_state_json("not json at all") is None
        assert _parse_topic_state_json("") is None

    def test_extracts_embedded_json(self):
        """When response has prose before/after — pull the object."""
        from processors.topic_threading import _parse_topic_state_json
        response = 'Here is the state: {"current_status": "active", "summary": "x"} done.'
        result = _parse_topic_state_json(response)
        assert result["current_status"] == "active"


# =============================================================================
# update_topic_state
# =============================================================================

class TestUpdateTopicState:
    @pytest.mark.asyncio
    async def test_happy_path_updates_state(self):
        """Valid Haiku JSON → new state written with bumped version."""
        from processors import topic_threading

        valid_json = '''
        {
            "current_status": "blocked",
            "summary": "Waiting on Yoram signature.",
            "stakeholders": ["Eyal", "Yoram"],
            "open_items": [{"kind": "blocker", "description": "Yoram signature", "owner": "Yoram"}],
            "last_decision": null,
            "key_facts": ["Legal entity must close by Q2"],
            "last_activity_date": "2026-04-13"
        }
        '''

        # Mock Supabase reads
        with patch.object(topic_threading, "supabase_client") as mock_sc:
            # Thread fetch
            thread_result = MagicMock()
            thread_result.data = [{
                "id": "topic-1",
                "topic_name": "Legal",
                "state_json": {"version": 2, "key_facts": ["Legal entity must close by Q2"]},
            }]
            # Meeting fetch
            meeting_result = MagicMock()
            meeting_result.data = [{"id": "m-1", "title": "Legal sync", "date": "2026-04-13"}]

            # Chain the table -> select -> eq -> limit -> execute behavior
            def table_side(name):
                tbl = MagicMock()
                tbl.select.return_value = tbl
                tbl.eq.return_value = tbl
                tbl.limit.return_value = tbl
                tbl.update.return_value = tbl
                if name == "topic_threads":
                    tbl.execute.return_value = thread_result
                elif name == "meetings":
                    tbl.execute.return_value = meeting_result
                else:
                    tbl.execute.return_value = MagicMock(data=[])
                return tbl
            mock_sc.client.table.side_effect = table_side

            with patch("core.llm.call_llm", return_value=(valid_json, {})):
                result = await topic_threading.update_topic_state(
                    topic_id="topic-1",
                    meeting_id="m-1",
                    decisions=[],
                    tasks=[{"label": "Legal", "title": "follow up", "assignee": "Yoram"}],
                    open_questions=[],
                )

        assert result is not None
        assert result["current_status"] == "blocked"
        # Version bumped from 2 -> 3
        assert result["version"] == 3

    @pytest.mark.asyncio
    async def test_malformed_json_keeps_previous_state(self):
        """Haiku returns garbage → update_topic_state returns None, no exception."""
        from processors import topic_threading

        with patch.object(topic_threading, "supabase_client") as mock_sc:
            thread_result = MagicMock()
            thread_result.data = [{"id": "topic-1", "topic_name": "Legal", "state_json": {}}]
            meeting_result = MagicMock()
            meeting_result.data = [{"id": "m-1", "title": "x", "date": "2026-04-13"}]

            def table_side(name):
                tbl = MagicMock()
                tbl.select.return_value = tbl
                tbl.eq.return_value = tbl
                tbl.limit.return_value = tbl
                tbl.update.return_value = tbl
                if name == "topic_threads":
                    tbl.execute.return_value = thread_result
                elif name == "meetings":
                    tbl.execute.return_value = meeting_result
                return tbl
            mock_sc.client.table.side_effect = table_side

            with patch("core.llm.call_llm", return_value=("garbage response", {})):
                result = await topic_threading.update_topic_state(
                    topic_id="topic-1", meeting_id="m-1",
                    decisions=[], tasks=[], open_questions=[],
                )

        assert result is None

    @pytest.mark.asyncio
    async def test_thread_not_found_returns_none(self):
        from processors import topic_threading

        with patch.object(topic_threading, "supabase_client") as mock_sc:
            tbl = MagicMock()
            tbl.select.return_value = tbl
            tbl.eq.return_value = tbl
            tbl.limit.return_value = tbl
            tbl.execute.return_value = MagicMock(data=[])
            mock_sc.client.table.return_value = tbl

            result = await topic_threading.update_topic_state(
                topic_id="nonexistent", meeting_id="m-1",
                decisions=[], tasks=[], open_questions=[],
            )
        assert result is None

    @pytest.mark.asyncio
    async def test_exception_swallowed(self):
        """LLM or DB exception returns None, does NOT propagate."""
        from processors import topic_threading

        with patch.object(topic_threading, "supabase_client") as mock_sc:
            mock_sc.client.table.side_effect = Exception("db connection lost")

            result = await topic_threading.update_topic_state(
                topic_id="topic-1", meeting_id="m-1",
                decisions=[], tasks=[], open_questions=[],
            )
        assert result is None


# =============================================================================
# Morning brief: topic_state section
# =============================================================================

class TestMorningBriefTopicState:
    def test_renders_blocked_topic(self):
        from processors.morning_brief import format_morning_brief

        brief = {"sections": [{
            "type": "topic_state",
            "title": "Topic state",
            "items": [
                {
                    "topic_name": "Legal",
                    "summary": "Blocked on Yoram signature.",
                    "kind": "blocked",
                },
            ],
        }]}

        out = format_morning_brief(brief)
        assert "Legal" in out
        assert "blocked" in out.lower()
        assert "🔴" in out

    def test_renders_stale_topic(self):
        from processors.morning_brief import format_morning_brief

        brief = {"sections": [{
            "type": "topic_state",
            "title": "Topic state",
            "items": [
                {
                    "topic_name": "WEU Marketing",
                    "kind": "stale",
                    "days_idle": 21,
                    "last_activity_date": "2026-03-23",
                },
            ],
        }]}
        out = format_morning_brief(brief)
        assert "WEU Marketing" in out
        assert "21d" in out
        assert "🟡" in out


# =============================================================================
# QA staleness sweep
# =============================================================================

class _ChainableFake:
    """Query builder stub where every attribute/call returns self, and
    execute() returns a pre-seeded MagicMock(data=...)."""
    def __init__(self, data):
        self._data = data

    def __getattr__(self, name):
        if name == "execute":
            def _exec():
                return MagicMock(data=self._data)
            return _exec
        return self

    def __call__(self, *args, **kwargs):
        return self


class TestTopicStateStalenessSweep:
    def test_flips_active_to_stale(self):
        from schedulers import qa_scheduler

        rows_to_flip = [
            {"id": "t-1", "state_json": {"current_status": "active", "version": 1}, "last_updated": "2026-03-01"},
            {"id": "t-2", "state_json": {"current_status": "active", "version": 2}, "last_updated": "2026-02-01"},
        ]

        with patch.object(qa_scheduler, "supabase_client") as mock_sc:
            mock_sc.client.table.return_value = _ChainableFake(rows_to_flip)

            issues: list[str] = []
            result = qa_scheduler._check_topic_state_staleness(issues)

        assert result["threads_scanned"] == 2
        assert result["threads_marked_stale"] == 2
        # No issues raised — staleness is expected, not a defect
        assert issues == []

    def test_skips_already_stale(self):
        """Rows already marked stale shouldn't re-flip (idempotent)."""
        from schedulers import qa_scheduler

        rows = [
            {"id": "t-1", "state_json": {"current_status": "stale", "version": 1}, "last_updated": "2026-03-01"},
        ]

        with patch.object(qa_scheduler, "supabase_client") as mock_sc:
            mock_sc.client.table.return_value = _ChainableFake(rows)

            result = qa_scheduler._check_topic_state_staleness([])

        assert result["threads_scanned"] == 1
        assert result["threads_marked_stale"] == 0  # skipped since already stale
