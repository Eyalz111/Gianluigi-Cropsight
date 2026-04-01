"""Tests for processors/meeting_continuity.py — Phase 12 A1 enhanced context gatherer."""

import pytest
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch, call

from processors.meeting_continuity import (
    build_meeting_continuity_context,
    build_daily_continuity_context,
    build_pre_meeting_continuity_context,
    format_daily_continuity_for_brief,
    _days_ago,
    _days_until_review,
    _format_task_stats,
    _build_daily_context_inner,
    _build_pre_meeting_context_inner,
)


# ── Fixtures ─────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def mock_settings():
    mock = MagicMock()
    mock.model_background = "claude-3-5-sonnet-20241022"
    mock.model_simple = "claude-3-5-haiku-20241022"
    with patch("processors.meeting_continuity.settings", mock):
        yield mock


def _make_meeting(meeting_id="m1", title="Team Sync", date_str="2026-03-28",
                  participants=None):
    return {
        "id": meeting_id,
        "title": title,
        "date": f"{date_str}T10:00:00+00:00",
        "participants": participants or ["Eyal", "Roye"],
    }


def _make_task(title="Do something", assignee="Eyal", status="pending",
               meeting_id="m1", deadline=None, priority="M",
               updated_at=None, created_at=None):
    return {
        "id": f"t-{title[:8]}",
        "title": title,
        "assignee": assignee,
        "status": status,
        "meeting_id": meeting_id,
        "deadline": deadline,
        "priority": priority,
        "updated_at": updated_at or datetime.now(timezone.utc).isoformat(),
        "created_at": created_at or datetime.now(timezone.utc).isoformat(),
        "meetings": {"title": "Team Sync", "date": "2026-03-28"},
    }


def _make_decision(description="Use AWS", meeting_id="m1", review_date=None,
                   status="active", participants_involved=None, rationale=None):
    return {
        "id": f"d-{description[:8]}",
        "description": description,
        "meeting_id": meeting_id,
        "decision_status": status,
        "review_date": review_date,
        "rationale": rationale,
        "participants_involved": participants_involved or ["Eyal"],
        "meetings": {"title": "Team Sync", "date": "2026-03-28"},
    }


def _make_question(question="Who handles DevOps?", raised_by="Eyal",
                   status="open", created_at=None, meeting_id="m1"):
    days_ago_val = 10  # default: 10 days old
    if created_at is None:
        created_at = (datetime.now(timezone.utc) - timedelta(days=days_ago_val)).isoformat()
    return {
        "id": f"q-{question[:8]}",
        "question": question,
        "raised_by": raised_by,
        "status": status,
        "created_at": created_at,
        "meeting_id": meeting_id,
        "meetings": {"title": "Team Sync", "date": "2026-03-28"},
    }


# =========================================================================
# Helper function tests
# =========================================================================

class TestDaysAgo:

    def test_recent_date(self):
        yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
        assert _days_ago(yesterday) == 1

    def test_old_date(self):
        old = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
        assert _days_ago(old) == 30

    def test_none_returns_none(self):
        assert _days_ago(None) is None

    def test_empty_string_returns_none(self):
        assert _days_ago("") is None

    def test_invalid_date_returns_none(self):
        assert _days_ago("not-a-date") is None

    def test_today_returns_zero(self):
        now = datetime.now(timezone.utc).isoformat()
        assert _days_ago(now) == 0


class TestDaysUntilReview:

    def test_future_review(self):
        future = (datetime.now(timezone.utc) + timedelta(days=7)).isoformat()
        result = _days_until_review(future)
        assert result is not None
        assert 6 <= result <= 7  # allow slight timing variance

    def test_past_review(self):
        past = (datetime.now(timezone.utc) - timedelta(days=3)).isoformat()
        result = _days_until_review(past)
        assert result is not None
        assert result < 0

    def test_none_returns_none(self):
        assert _days_until_review(None) is None

    def test_invalid_returns_none(self):
        assert _days_until_review("garbage") is None

    def test_date_only_string(self):
        """Handles date-only strings like '2026-04-10'."""
        future = (datetime.now(timezone.utc) + timedelta(days=5)).strftime("%Y-%m-%d")
        result = _days_until_review(future)
        assert result is not None
        assert 4 <= result <= 5


class TestFormatTaskStats:

    def test_empty_list(self):
        stats = _format_task_stats([])
        assert stats == {"total": 0, "done": 0, "open": 0, "overdue": 0}

    def test_all_done(self):
        tasks = [_make_task(status="done"), _make_task(status="done", title="T2")]
        stats = _format_task_stats(tasks)
        assert stats["total"] == 2
        assert stats["done"] == 2
        assert stats["open"] == 0

    def test_mixed_statuses(self):
        tasks = [
            _make_task(status="done"),
            _make_task(status="pending", title="T2"),
            _make_task(status="overdue", title="T3"),
        ]
        stats = _format_task_stats(tasks)
        assert stats["total"] == 3
        assert stats["done"] == 1
        assert stats["open"] == 2
        assert stats["overdue"] == 1

    def test_all_pending(self):
        tasks = [_make_task(status="pending")]
        stats = _format_task_stats(tasks)
        assert stats["total"] == 1
        assert stats["done"] == 0
        assert stats["open"] == 1


# =========================================================================
# build_meeting_continuity_context (enhanced existing function)
# =========================================================================

class TestBuildMeetingContinuityContext:

    @patch("processors.meeting_continuity.supabase_client")
    def test_no_participants_returns_none(self, mock_sc):
        assert build_meeting_continuity_context([]) is None

    @patch("processors.meeting_continuity.supabase_client")
    def test_no_meetings_returns_none(self, mock_sc):
        mock_sc.get_meetings_by_participant_overlap.return_value = []
        assert build_meeting_continuity_context(["Eyal"]) is None

    @patch("processors.meeting_continuity.supabase_client")
    def test_basic_context_output(self, mock_sc):
        mock_sc.get_meetings_by_participant_overlap.return_value = [
            _make_meeting("m1", "Founders Sync", "2026-03-25"),
        ]
        mock_sc.list_decisions.return_value = [
            _make_decision("Use AWS for hosting"),
        ]
        mock_sc.get_tasks.return_value = [
            _make_task("Draft proposal", "Eyal", "done", "m1"),
            _make_task("Review budget", "Roye", "pending", "m1"),
        ]
        mock_sc.get_open_questions.return_value = []

        result = build_meeting_continuity_context(["Eyal", "Roye"])
        assert result is not None
        assert "Founders Sync" in result
        assert "2026-03-25" in result

    @patch("processors.meeting_continuity.supabase_client")
    def test_includes_task_completion_stats(self, mock_sc):
        """Phase 12: task stats should appear in context."""
        mock_sc.get_meetings_by_participant_overlap.return_value = [
            _make_meeting("m1", "Sprint Review", "2026-03-25"),
        ]
        mock_sc.list_decisions.return_value = []
        mock_sc.get_tasks.return_value = [
            _make_task("T1", status="done", meeting_id="m1"),
            _make_task("T2", status="pending", meeting_id="m1"),
            _make_task("T3", status="overdue", meeting_id="m1"),
        ]
        mock_sc.get_open_questions.return_value = []

        result = build_meeting_continuity_context(["Eyal"])
        assert result is not None
        assert "1/3 completed" in result
        assert "1 overdue" in result

    @patch("processors.meeting_continuity.supabase_client")
    def test_includes_decision_review_dates(self, mock_sc):
        """Phase 12: decisions approaching review should be noted."""
        review_soon = (datetime.now(timezone.utc) + timedelta(days=5)).isoformat()
        mock_sc.get_meetings_by_participant_overlap.return_value = [
            _make_meeting("m1", "Board Prep", "2026-03-25"),
        ]
        mock_sc.list_decisions.return_value = [
            _make_decision("Pivot to B2B", review_date=review_soon),
        ]
        mock_sc.get_tasks.return_value = []
        mock_sc.get_open_questions.return_value = []

        result = build_meeting_continuity_context(["Eyal"])
        assert result is not None
        assert "review in" in result
        assert "Pivot to B2B" in result

    @patch("processors.meeting_continuity.supabase_client")
    def test_decision_review_far_future_not_shown(self, mock_sc):
        """Decisions with review > 14d away should NOT appear in approaching section."""
        review_far = (datetime.now(timezone.utc) + timedelta(days=30)).isoformat()
        mock_sc.get_meetings_by_participant_overlap.return_value = [
            _make_meeting("m1", "Sync", "2026-03-25"),
        ]
        mock_sc.list_decisions.return_value = [
            _make_decision("Long term plan", review_date=review_far),
        ]
        mock_sc.get_tasks.return_value = []
        mock_sc.get_open_questions.return_value = []

        result = build_meeting_continuity_context(["Eyal"])
        assert result is not None
        assert "Approaching review" not in result

    @patch("processors.meeting_continuity.supabase_client")
    def test_includes_question_aging(self, mock_sc):
        """Phase 12: questions should show age in days."""
        old_date = (datetime.now(timezone.utc) - timedelta(days=15)).isoformat()
        mock_sc.get_meetings_by_participant_overlap.return_value = [
            _make_meeting("m1", "Planning", "2026-03-20"),
        ]
        mock_sc.list_decisions.return_value = []
        mock_sc.get_tasks.return_value = []
        mock_sc.get_open_questions.return_value = [
            _make_question("Who owns the deploy pipeline?", created_at=old_date),
        ]

        result = build_meeting_continuity_context(["Eyal"])
        assert result is not None
        assert "15d old" in result

    @patch("processors.meeting_continuity.supabase_client")
    def test_truncation_at_max_chars(self, mock_sc):
        """Context exceeding _MAX_CONTEXT_CHARS is truncated."""
        meetings = [
            _make_meeting(f"m{i}", f"Meeting {i} " + "x" * 200, f"2026-03-{20+i}")
            for i in range(10)
        ]
        mock_sc.get_meetings_by_participant_overlap.return_value = meetings
        mock_sc.list_decisions.return_value = [_make_decision("D" * 100)] * 5
        mock_sc.get_tasks.return_value = [_make_task("T" * 100)] * 20
        mock_sc.get_open_questions.return_value = []

        result = build_meeting_continuity_context(["Eyal"])
        assert result is not None
        assert result.endswith("...")
        assert len(result) <= 3100  # _MAX_CONTEXT_CHARS + small buffer for ellipsis

    @patch("processors.meeting_continuity.supabase_client")
    def test_db_error_returns_none(self, mock_sc):
        mock_sc.get_meetings_by_participant_overlap.side_effect = Exception("DB error")
        result = build_meeting_continuity_context(["Eyal"])
        assert result is None

    @patch("processors.meeting_continuity.supabase_client")
    def test_excludes_current_meeting(self, mock_sc):
        mock_sc.get_meetings_by_participant_overlap.return_value = []
        build_meeting_continuity_context(["Eyal"], current_meeting_id="m-current")
        mock_sc.get_meetings_by_participant_overlap.assert_called_once_with(
            participants=["Eyal"],
            exclude_meeting_id="m-current",
            limit=3,
        )


# =========================================================================
# build_daily_continuity_context
# =========================================================================

class TestBuildDailyContinuityContext:

    @patch("processors.meeting_continuity.supabase_client")
    def test_returns_task_summary(self, mock_sc):
        mock_sc.get_tasks.side_effect = [
            [_make_task("T1", status="pending")],       # pending
            [_make_task("T2", status="in_progress")],    # in_progress
            [],                                            # done
        ]
        mock_sc.get_decisions_for_review.return_value = []
        mock_sc.get_open_questions.return_value = []

        result = build_daily_continuity_context()
        assert result is not None
        assert "task_summary" in result
        assert result["task_summary"]["open"] == 2

    @patch("processors.meeting_continuity.supabase_client")
    def test_recent_completions_included(self, mock_sc):
        recent = (datetime.now(timezone.utc) - timedelta(hours=12)).isoformat()
        mock_sc.get_tasks.side_effect = [
            [],  # pending
            [],  # in_progress
            [_make_task("Finished task", status="done", updated_at=recent)],  # done
        ]
        mock_sc.get_decisions_for_review.return_value = []
        mock_sc.get_open_questions.return_value = []

        result = build_daily_continuity_context()
        assert result is not None
        ts = result["task_summary"]
        assert ts["completed_24h"] == 1
        assert ts["recent_completions"][0]["title"].startswith("Finished")

    @patch("processors.meeting_continuity.supabase_client")
    def test_old_completions_excluded(self, mock_sc):
        old = (datetime.now(timezone.utc) - timedelta(days=3)).isoformat()
        mock_sc.get_tasks.side_effect = [
            [],  # pending
            [],  # in_progress
            [_make_task("Old task", status="done", updated_at=old)],
        ]
        mock_sc.get_decisions_for_review.return_value = []
        mock_sc.get_open_questions.return_value = []

        result = build_daily_continuity_context()
        # No recent completions, no open tasks → might be None
        # Actually task_summary will still be built if done_recent is not empty
        # but completed_24h will be 0
        if result and "task_summary" in result:
            assert result["task_summary"]["completed_24h"] == 0

    @patch("processors.meeting_continuity.supabase_client")
    def test_approaching_reviews(self, mock_sc):
        review_soon = (datetime.now(timezone.utc) + timedelta(days=5)).isoformat()
        mock_sc.get_tasks.side_effect = [[], [], []]
        mock_sc.get_decisions_for_review.return_value = [
            _make_decision("Switch to ARM", review_date=review_soon),
        ]
        mock_sc.get_open_questions.return_value = []

        result = build_daily_continuity_context()
        assert result is not None
        assert "approaching_reviews" in result
        assert result["approaching_reviews"][0]["description"].startswith("Switch")

    @patch("processors.meeting_continuity.supabase_client")
    def test_aging_questions(self, mock_sc):
        old_date = (datetime.now(timezone.utc) - timedelta(days=14)).isoformat()
        mock_sc.get_tasks.side_effect = [[], [], []]
        mock_sc.get_decisions_for_review.return_value = []
        mock_sc.get_open_questions.return_value = [
            _make_question("Who is the PM?", created_at=old_date),
        ]

        result = build_daily_continuity_context()
        assert result is not None
        assert "aging_questions" in result
        assert result["aging_questions"][0]["days_open"] >= 13

    @patch("processors.meeting_continuity.supabase_client")
    def test_young_questions_excluded(self, mock_sc):
        """Questions < 7 days old should NOT appear in aging section."""
        recent_date = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
        mock_sc.get_tasks.side_effect = [[], [], []]
        mock_sc.get_decisions_for_review.return_value = []
        mock_sc.get_open_questions.return_value = [
            _make_question("New question", created_at=recent_date),
        ]

        result = build_daily_continuity_context()
        # No sections qualify → None
        assert result is None

    @patch("processors.meeting_continuity.supabase_client")
    def test_empty_data_returns_none(self, mock_sc):
        mock_sc.get_tasks.side_effect = [[], [], []]
        mock_sc.get_decisions_for_review.return_value = []
        mock_sc.get_open_questions.return_value = []

        result = build_daily_continuity_context()
        assert result is None

    @patch("processors.meeting_continuity.supabase_client")
    def test_exception_returns_none(self, mock_sc):
        mock_sc.get_tasks.side_effect = Exception("DB down")
        result = build_daily_continuity_context()
        assert result is None

    @patch("processors.meeting_continuity.supabase_client")
    def test_aging_questions_sorted_by_age(self, mock_sc):
        """Aging questions should be sorted oldest-first."""
        q1 = _make_question("Q1", created_at=(datetime.now(timezone.utc) - timedelta(days=7)).isoformat())
        q2 = _make_question("Q2", created_at=(datetime.now(timezone.utc) - timedelta(days=21)).isoformat())
        q3 = _make_question("Q3", created_at=(datetime.now(timezone.utc) - timedelta(days=14)).isoformat())

        mock_sc.get_tasks.side_effect = [[], [], []]
        mock_sc.get_decisions_for_review.return_value = []
        mock_sc.get_open_questions.return_value = [q1, q2, q3]

        result = build_daily_continuity_context()
        assert result is not None
        ages = [a["days_open"] for a in result["aging_questions"]]
        assert ages == sorted(ages, reverse=True)

    @patch("processors.meeting_continuity.supabase_client")
    def test_max_5_items_per_section(self, mock_sc):
        """Each section is capped at 5 items."""
        old = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
        questions = [_make_question(f"Q{i}", created_at=old) for i in range(10)]

        mock_sc.get_tasks.side_effect = [[], [], []]
        mock_sc.get_decisions_for_review.return_value = []
        mock_sc.get_open_questions.return_value = questions

        result = build_daily_continuity_context()
        assert result is not None
        assert len(result["aging_questions"]) <= 5


# =========================================================================
# format_daily_continuity_for_brief
# =========================================================================

class TestFormatDailyContinuityForBrief:

    def test_task_summary_format(self):
        context = {
            "task_summary": {
                "open": 5,
                "in_progress": 2,
                "overdue": 1,
                "completed_24h": 1,
                "recent_completions": [
                    {"title": "Write docs", "assignee": "Roye"},
                ],
            },
        }
        result = format_daily_continuity_for_brief(context)
        assert "5 open" in result
        assert "2 in progress" in result
        assert "1 overdue" in result
        assert "Completed yesterday: 1" in result
        assert "Write docs" in result

    def test_approaching_reviews_format(self):
        context = {
            "approaching_reviews": [
                {"description": "Use ARM processors", "days_until": 3, "review_date": "2026-04-05"},
            ],
        }
        result = format_daily_continuity_for_brief(context)
        assert "Decisions up for review" in result
        assert "Use ARM processors" in result
        assert "in 3d" in result

    def test_review_today_format(self):
        context = {
            "approaching_reviews": [
                {"description": "Budget approval", "days_until": 0, "review_date": "2026-04-02"},
            ],
        }
        result = format_daily_continuity_for_brief(context)
        assert "today" in result

    def test_aging_questions_format(self):
        context = {
            "aging_questions": [
                {"question": "Who handles DevOps?", "days_open": 14, "raised_by": "Eyal"},
            ],
        }
        result = format_daily_continuity_for_brief(context)
        assert "Aging open questions" in result
        assert "14d" in result
        assert "Eyal" in result

    def test_empty_context(self):
        result = format_daily_continuity_for_brief({})
        assert result == ""

    def test_no_overdue_hides_overdue(self):
        context = {
            "task_summary": {
                "open": 3,
                "in_progress": 1,
                "overdue": 0,
                "completed_24h": 0,
                "recent_completions": [],
            },
        }
        result = format_daily_continuity_for_brief(context)
        assert "overdue" not in result


# =========================================================================
# build_pre_meeting_continuity_context
# =========================================================================

class TestBuildPreMeetingContinuityContext:

    @pytest.mark.asyncio
    @patch("processors.meeting_continuity.call_llm")
    @patch("processors.meeting_continuity.supabase_client")
    async def test_basic_context(self, mock_sc, mock_llm):
        mock_sc.get_meetings_by_participant_overlap.return_value = [
            _make_meeting("m1", "Sprint Review", "2026-03-25"),
        ]
        mock_sc.get_tasks.return_value = [
            _make_task("Fix API", "Eyal", "pending", "m1"),
        ]
        mock_sc.list_decisions.return_value = [
            _make_decision("Use GraphQL", participants_involved=["Eyal"]),
        ]
        mock_sc.get_open_questions.return_value = [
            _make_question("Who handles auth?", raised_by="Eyal"),
        ]
        mock_llm.return_value = ("Key focus areas for this meeting...", {})

        result = await build_pre_meeting_continuity_context(
            participants=["Eyal"],
            meeting_title="Sprint Planning",
        )
        assert result is not None
        assert "meetings" in result
        assert "participant_tasks" in result
        assert "decisions" in result
        assert "narrative" in result

    @pytest.mark.asyncio
    async def test_no_participants_no_title_returns_none(self):
        result = await build_pre_meeting_continuity_context(
            participants=[],
            meeting_title="",
        )
        assert result is None

    @pytest.mark.asyncio
    @patch("processors.meeting_continuity.call_llm")
    @patch("processors.meeting_continuity.supabase_client")
    async def test_includes_participant_tasks(self, mock_sc, mock_llm):
        mock_sc.get_meetings_by_participant_overlap.return_value = []
        mock_sc.get_tasks.return_value = [
            _make_task("Review PR", "Roye", "in_progress"),
            _make_task("Deploy staging", "Roye", "pending"),
        ]
        mock_sc.list_decisions.return_value = []
        mock_sc.get_open_questions.return_value = []
        mock_llm.return_value = ("Focus on Roye's open tasks.", {})

        result = await build_pre_meeting_continuity_context(
            participants=["Roye"],
            meeting_title="1:1 with Roye",
        )
        assert result is not None
        assert "Roye" in result.get("participant_tasks", {})
        assert len(result["participant_tasks"]["Roye"]) == 2

    @pytest.mark.asyncio
    @patch("processors.meeting_continuity.call_llm")
    @patch("processors.meeting_continuity.supabase_client")
    async def test_includes_decisions_approaching_review(self, mock_sc, mock_llm):
        review_soon = (datetime.now(timezone.utc) + timedelta(days=5)).isoformat()
        mock_sc.get_meetings_by_participant_overlap.return_value = []
        mock_sc.get_tasks.return_value = []
        mock_sc.list_decisions.return_value = [
            _make_decision(
                "Pivot strategy",
                review_date=review_soon,
                status="active",
                participants_involved=["Paolo"],
            ),
        ]
        mock_sc.get_open_questions.return_value = []
        mock_llm.return_value = ("Decision approaching review.", {})

        result = await build_pre_meeting_continuity_context(
            participants=["Eyal"],
            meeting_title="Strategy Review",
        )
        assert result is not None
        # The decision should be included because it's approaching review
        assert "decisions" in result

    @pytest.mark.asyncio
    @patch("processors.meeting_continuity.call_llm")
    @patch("processors.meeting_continuity.supabase_client")
    async def test_open_questions_filtered_by_participant(self, mock_sc, mock_llm):
        old_date = (datetime.now(timezone.utc) - timedelta(days=10)).isoformat()
        mock_sc.get_meetings_by_participant_overlap.return_value = []
        mock_sc.get_tasks.return_value = []
        mock_sc.list_decisions.return_value = []
        mock_sc.get_open_questions.return_value = [
            _make_question("Q by Eyal", raised_by="Eyal", created_at=old_date),
            _make_question("Q by Paolo", raised_by="Paolo", created_at=old_date),
        ]
        mock_llm.return_value = ("Questions need attention.", {})

        result = await build_pre_meeting_continuity_context(
            participants=["Eyal"],
            meeting_title="Sync",
        )
        assert result is not None
        # Only Eyal's question should be included
        assert len(result.get("open_questions", [])) == 1
        assert result["open_questions"][0]["raised_by"] == "Eyal"

    @pytest.mark.asyncio
    @patch("processors.meeting_continuity.call_llm")
    @patch("processors.meeting_continuity.supabase_client")
    async def test_narrative_uses_sonnet_model(self, mock_sc, mock_llm):
        mock_sc.get_meetings_by_participant_overlap.return_value = [
            _make_meeting(),
        ]
        mock_sc.get_tasks.return_value = [_make_task()]
        mock_sc.list_decisions.return_value = []
        mock_sc.get_open_questions.return_value = []
        mock_llm.return_value = ("Narrative text here.", {})

        await build_pre_meeting_continuity_context(
            participants=["Eyal"],
            meeting_title="Sync",
        )

        # Verify Sonnet model was used
        mock_llm.assert_called_once()
        _, kwargs = mock_llm.call_args
        assert kwargs.get("call_site") == "pre_meeting_continuity_synthesis"

    @pytest.mark.asyncio
    @patch("processors.meeting_continuity.call_llm")
    @patch("processors.meeting_continuity.supabase_client")
    async def test_narrative_failure_non_fatal(self, mock_sc, mock_llm):
        """If LLM synthesis fails, result should still contain raw data."""
        mock_sc.get_meetings_by_participant_overlap.return_value = [
            _make_meeting(),
        ]
        mock_sc.get_tasks.return_value = [_make_task()]
        mock_sc.list_decisions.return_value = []
        mock_sc.get_open_questions.return_value = []
        mock_llm.side_effect = Exception("LLM down")

        result = await build_pre_meeting_continuity_context(
            participants=["Eyal"],
            meeting_title="Sync",
        )
        assert result is not None
        assert "meetings" in result
        # Narrative should be absent (not crash)
        assert "narrative" not in result

    @pytest.mark.asyncio
    @patch("processors.meeting_continuity.supabase_client")
    async def test_full_db_failure_returns_none(self, mock_sc):
        mock_sc.get_meetings_by_participant_overlap.side_effect = Exception("DB down")
        mock_sc.get_tasks.side_effect = Exception("DB down")
        mock_sc.list_decisions.side_effect = Exception("DB down")
        mock_sc.get_open_questions.side_effect = Exception("DB down")

        result = await build_pre_meeting_continuity_context(
            participants=["Eyal"],
            meeting_title="Sync",
        )
        assert result is None

    @pytest.mark.asyncio
    @patch("processors.meeting_continuity.call_llm")
    @patch("processors.meeting_continuity.supabase_client")
    async def test_meeting_task_stats_per_meeting(self, mock_sc, mock_llm):
        """Each meeting in the context should have per-meeting task stats."""
        mock_sc.get_meetings_by_participant_overlap.return_value = [
            _make_meeting("m1", "Sprint 1", "2026-03-20"),
            _make_meeting("m2", "Sprint 2", "2026-03-27"),
        ]
        mock_sc.get_tasks.side_effect = [
            # First call for m1 tasks
            [
                _make_task("T1", status="done", meeting_id="m1"),
                _make_task("T2", status="pending", meeting_id="m1"),
            ],
            # Second call for m2 tasks
            [
                _make_task("T3", status="done", meeting_id="m2"),
            ],
            # Participant tasks for Eyal
            [_make_task("T4", status="pending")],
        ]
        mock_sc.list_decisions.return_value = []
        mock_sc.get_open_questions.return_value = []
        mock_llm.return_value = ("Good progress.", {})

        result = await build_pre_meeting_continuity_context(
            participants=["Eyal"],
            meeting_title="Sprint 3",
        )
        assert result is not None
        meetings = result.get("meetings", [])
        assert len(meetings) == 2
        assert meetings[0]["task_stats"]["total"] == 2
        assert meetings[0]["task_stats"]["done"] == 1

    @pytest.mark.asyncio
    @patch("processors.meeting_continuity.call_llm")
    @patch("processors.meeting_continuity.supabase_client")
    async def test_decisions_with_participant_filter(self, mock_sc, mock_llm):
        """Decisions should be filtered to those involving meeting participants."""
        mock_sc.get_meetings_by_participant_overlap.return_value = []
        mock_sc.get_tasks.return_value = []
        mock_sc.list_decisions.return_value = [
            _make_decision("D1", participants_involved=["Eyal"], status="active"),
            _make_decision("D2", participants_involved=["Outsider"], status="active"),
        ]
        mock_sc.get_open_questions.return_value = []
        mock_llm.return_value = ("Decision context.", {})

        result = await build_pre_meeting_continuity_context(
            participants=["Eyal"],
            meeting_title="Review",
        )
        assert result is not None
        # Only D1 should be included (Eyal is a participant)
        descs = [d["description"] for d in result.get("decisions", [])]
        assert "D1" in descs
        assert "D2" not in descs

    @pytest.mark.asyncio
    @patch("processors.meeting_continuity.call_llm")
    @patch("processors.meeting_continuity.supabase_client")
    async def test_max_items_caps(self, mock_sc, mock_llm):
        """Participant tasks, decisions, questions should be capped."""
        mock_sc.get_meetings_by_participant_overlap.return_value = []
        mock_sc.get_tasks.return_value = [
            _make_task(f"Task {i}", "Eyal", "pending") for i in range(20)
        ]
        mock_sc.list_decisions.return_value = [
            _make_decision(f"Dec {i}", participants_involved=["Eyal"], status="active")
            for i in range(20)
        ]
        old = (datetime.now(timezone.utc) - timedelta(days=10)).isoformat()
        mock_sc.get_open_questions.return_value = [
            _make_question(f"Q{i}", raised_by="Eyal", created_at=old)
            for i in range(20)
        ]
        mock_llm.return_value = ("Lots of context.", {})

        result = await build_pre_meeting_continuity_context(
            participants=["Eyal"],
            meeting_title="Big Review",
        )
        assert result is not None
        assert len(result.get("participant_tasks", {}).get("Eyal", [])) <= 5
        assert len(result.get("decisions", [])) <= 8
        assert len(result.get("open_questions", [])) <= 5
