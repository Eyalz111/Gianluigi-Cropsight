"""
Tests for processors/weekly_digest.py and schedulers/weekly_digest_scheduler.py

Tests the weekly digest generation pipeline:
1. get_meetings_for_week — date range query
2. get_decisions_for_week — decisions from weekly meetings
3. get_task_summary — task categorisation
4. get_open_questions_summary — open question fetch
5. format_digest_document — Markdown output
6. generate_weekly_digest — full orchestrator
7. WeeklyDigestScheduler._check_and_generate — Sunday trigger logic
8. Duplicate prevention (same week not generated twice)
"""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from datetime import datetime, timedelta


# ============================================================================
# Sample data used across tests
# ============================================================================

SAMPLE_MEETINGS = [
    {
        "id": "meeting-1",
        "title": "MVP Review",
        "date": "2026-02-23T10:00:00",
        "participants": ["Eyal", "Roye"],
        "summary": "Reviewed MVP progress",
        "approval_status": "approved",
    },
    {
        "id": "meeting-2",
        "title": "Sprint Planning",
        "date": "2026-02-25T14:00:00",
        "participants": ["Eyal", "Paolo"],
        "summary": "Planned sprint goals",
        "approval_status": "approved",
    },
]

SAMPLE_DECISIONS = [
    {
        "id": "dec-1",
        "meeting_id": "meeting-1",
        "description": "Use semantic versioning for the API",
        "context": "Discussion about API strategy",
        "meetings": {"title": "MVP Review", "date": "2026-02-23"},
    },
    {
        "id": "dec-2",
        "meeting_id": "meeting-2",
        "description": "Deploy to AWS ECS instead of GKE",
        "context": "Cost analysis discussion",
        "meetings": {"title": "Sprint Planning", "date": "2026-02-25"},
    },
]

SAMPLE_DONE_TASKS = [
    {
        "id": "task-1",
        "title": "Prepare client demo",
        "assignee": "Paolo",
        "status": "done",
        "deadline": "2026-02-25",
        "meetings": {"title": "MVP Review", "date": "2026-02-23"},
    },
]

SAMPLE_OVERDUE_TASKS = [
    {
        "id": "task-2",
        "title": "Update deployment docs",
        "assignee": "Roye",
        "status": "overdue",
        "deadline": "2026-02-20",
        "meetings": {"title": "Sprint Planning", "date": "2026-02-18"},
    },
]

SAMPLE_PENDING_TASKS = [
    {
        "id": "task-3",
        "title": "Review security audit",
        "assignee": "Eyal",
        "status": "pending",
        "priority": "H",
        "deadline": (datetime.now() + timedelta(days=3)).strftime("%Y-%m-%d"),
        "meetings": {"title": "Sprint Planning", "date": "2026-02-25"},
    },
    {
        "id": "task-4",
        "title": "Write user docs",
        "assignee": "Yoram",
        "status": "pending",
        "priority": "M",
        "deadline": (datetime.now() + timedelta(days=30)).strftime("%Y-%m-%d"),
        "meetings": None,
    },
]

SAMPLE_OPEN_QUESTIONS = [
    {
        "id": "q-1",
        "question": "Which satellite data provider should we use?",
        "raised_by": "Yoram",
        "status": "open",
        "meeting_id": "meeting-1",
        "meetings": {"title": "MVP Review", "date": "2026-02-23"},
    },
]

SAMPLE_UPCOMING_EVENTS = [
    {
        "id": "event-1",
        "title": "CropSight Demo",
        "start": "2026-03-02T10:00:00Z",
        "end": "2026-03-02T11:00:00Z",
        "attendees": [
            {"email": "eyal@cropsight.io", "displayName": "Eyal"},
            {"email": "roye@cropsight.io", "displayName": "Roye"},
        ],
        "color_id": "3",
    },
    {
        "id": "event-2",
        "title": "Personal Dentist",
        "start": "2026-03-03T09:00:00Z",
        "end": "2026-03-03T10:00:00Z",
        "attendees": [],
        "color_id": None,
    },
]


# ============================================================================
# 1. get_meetings_for_week
# ============================================================================

class TestGetMeetingsForWeek:
    """Tests for get_meetings_for_week — verifies date range query."""

    @pytest.mark.asyncio
    async def test_returns_meetings_in_date_range(self):
        """Should call supabase_client.list_meetings with correct date range."""
        with patch("processors.weekly_digest.supabase_client") as mock_db:
            mock_db.list_meetings = MagicMock(return_value=SAMPLE_MEETINGS)

            from processors.weekly_digest import get_meetings_for_week

            week_start = datetime(2026, 2, 23)
            week_end = datetime(2026, 2, 28, 23, 59, 59)
            result = await get_meetings_for_week(week_start, week_end)

            # Verify list_meetings was called with date range
            mock_db.list_meetings.assert_called_once_with(
                date_from=week_start,
                date_to=week_end,
            )
            assert len(result) == 2
            assert result[0]["title"] == "MVP Review"

    @pytest.mark.asyncio
    async def test_returns_empty_on_no_meetings(self):
        """Should return empty list when no meetings in range."""
        with patch("processors.weekly_digest.supabase_client") as mock_db:
            mock_db.list_meetings = MagicMock(return_value=[])

            from processors.weekly_digest import get_meetings_for_week

            week_start = datetime(2026, 3, 1)
            week_end = datetime(2026, 3, 7, 23, 59, 59)
            result = await get_meetings_for_week(week_start, week_end)

            assert result == []

    @pytest.mark.asyncio
    async def test_handles_error_gracefully(self):
        """Should return empty list on database error."""
        with patch("processors.weekly_digest.supabase_client") as mock_db:
            mock_db.list_meetings = MagicMock(side_effect=Exception("DB error"))

            from processors.weekly_digest import get_meetings_for_week

            result = await get_meetings_for_week(datetime.now(), datetime.now())
            assert result == []


# ============================================================================
# 2. get_decisions_for_week
# ============================================================================

class TestGetDecisionsForWeek:
    """Tests for get_decisions_for_week — verifies decisions from meetings."""

    @pytest.mark.asyncio
    async def test_returns_decisions_from_weekly_meetings(self):
        """Should get decisions for each meeting in the week."""
        with patch("processors.weekly_digest.supabase_client") as mock_db:
            mock_db.list_meetings = MagicMock(return_value=SAMPLE_MEETINGS)
            mock_db.list_decisions = MagicMock(side_effect=[
                [SAMPLE_DECISIONS[0]],  # For meeting-1
                [SAMPLE_DECISIONS[1]],  # For meeting-2
            ])

            from processors.weekly_digest import get_decisions_for_week

            week_start = datetime(2026, 2, 23)
            week_end = datetime(2026, 2, 28, 23, 59, 59)
            result = await get_decisions_for_week(week_start, week_end)

            assert len(result) == 2
            assert result[0]["description"] == "Use semantic versioning for the API"
            assert result[1]["description"] == "Deploy to AWS ECS instead of GKE"

    @pytest.mark.asyncio
    async def test_attaches_meeting_title(self):
        """Should attach _meeting_title to each decision."""
        with patch("processors.weekly_digest.supabase_client") as mock_db:
            mock_db.list_meetings = MagicMock(return_value=[SAMPLE_MEETINGS[0]])
            mock_db.list_decisions = MagicMock(return_value=[SAMPLE_DECISIONS[0]])

            from processors.weekly_digest import get_decisions_for_week

            result = await get_decisions_for_week(datetime.now(), datetime.now())

            assert result[0]["_meeting_title"] == "MVP Review"

    @pytest.mark.asyncio
    async def test_returns_empty_when_no_meetings(self):
        """Should return empty list when no meetings exist."""
        with patch("processors.weekly_digest.supabase_client") as mock_db:
            mock_db.list_meetings = MagicMock(return_value=[])

            from processors.weekly_digest import get_decisions_for_week

            result = await get_decisions_for_week(datetime.now(), datetime.now())
            assert result == []


# ============================================================================
# 3. get_task_summary
# ============================================================================

class TestGetTaskSummary:
    """Tests for get_task_summary — verifies task categorisation."""

    @pytest.mark.asyncio
    async def test_categorises_completed_tasks(self):
        """Should include done tasks in completed_this_week."""
        with patch("processors.weekly_digest.supabase_client") as mock_db:
            mock_db.get_tasks = MagicMock(side_effect=lambda **kw: {
                "done": SAMPLE_DONE_TASKS,
                "overdue": [],
                "pending": [],
            }.get(kw.get("status"), []))

            from processors.weekly_digest import get_task_summary

            result = await get_task_summary()
            assert len(result["completed_this_week"]) == 1
            assert result["completed_this_week"][0]["title"] == "Prepare client demo"

    @pytest.mark.asyncio
    async def test_categorises_overdue_tasks(self):
        """Should include overdue tasks."""
        with patch("processors.weekly_digest.supabase_client") as mock_db:
            mock_db.get_tasks = MagicMock(side_effect=lambda **kw: {
                "done": [],
                "overdue": SAMPLE_OVERDUE_TASKS,
                "pending": [],
            }.get(kw.get("status"), []))

            from processors.weekly_digest import get_task_summary

            result = await get_task_summary()
            assert len(result["overdue"]) == 1
            assert result["overdue"][0]["title"] == "Update deployment docs"

    @pytest.mark.asyncio
    async def test_categorises_due_next_week(self):
        """Should include pending tasks with deadlines in the next 7 days."""
        with patch("processors.weekly_digest.supabase_client") as mock_db:
            mock_db.get_tasks = MagicMock(side_effect=lambda **kw: {
                "done": [],
                "overdue": [],
                "pending": SAMPLE_PENDING_TASKS,
            }.get(kw.get("status"), []))

            from processors.weekly_digest import get_task_summary

            result = await get_task_summary()
            # task-3 is due in 3 days (within 7-day window)
            # task-4 is due in 30 days (outside 7-day window)
            assert len(result["due_next_week"]) == 1
            assert result["due_next_week"][0]["title"] == "Review security audit"

    @pytest.mark.asyncio
    async def test_handles_empty_tasks(self):
        """Should return empty lists when no tasks exist."""
        with patch("processors.weekly_digest.supabase_client") as mock_db:
            mock_db.get_tasks = MagicMock(return_value=[])

            from processors.weekly_digest import get_task_summary

            result = await get_task_summary()
            assert result["completed_this_week"] == []
            assert result["overdue"] == []
            assert result["due_next_week"] == []


# ============================================================================
# 4. get_open_questions_summary
# ============================================================================

class TestGetOpenQuestionsSummary:
    """Tests for get_open_questions_summary — verifies open question fetch."""

    @pytest.mark.asyncio
    async def test_returns_open_questions(self):
        """Should return all open questions."""
        with patch("processors.weekly_digest.supabase_client") as mock_db:
            mock_db.get_open_questions = MagicMock(
                return_value=SAMPLE_OPEN_QUESTIONS
            )

            from processors.weekly_digest import get_open_questions_summary

            result = await get_open_questions_summary()
            assert len(result) == 1
            assert result[0]["question"] == "Which satellite data provider should we use?"

    @pytest.mark.asyncio
    async def test_calls_with_open_status(self):
        """Should call get_open_questions with status='open'."""
        with patch("processors.weekly_digest.supabase_client") as mock_db:
            mock_db.get_open_questions = MagicMock(return_value=[])

            from processors.weekly_digest import get_open_questions_summary

            await get_open_questions_summary()
            mock_db.get_open_questions.assert_called_once_with(status="open")

    @pytest.mark.asyncio
    async def test_handles_error_gracefully(self):
        """Should return empty list on error."""
        with patch("processors.weekly_digest.supabase_client") as mock_db:
            mock_db.get_open_questions = MagicMock(
                side_effect=Exception("DB error")
            )

            from processors.weekly_digest import get_open_questions_summary

            result = await get_open_questions_summary()
            assert result == []


# ============================================================================
# 5. format_digest_document
# ============================================================================

class TestFormatDigestDocument:
    """Tests for format_digest_document — verifies Markdown output."""

    def test_contains_header(self):
        """Should include the weekly digest header."""
        from processors.weekly_digest import format_digest_document

        result = format_digest_document(
            week_of="2026-02-23",
            meetings=[],
            decisions=[],
            tasks_completed=[],
            tasks_overdue=[],
            tasks_upcoming=[],
            open_questions=[],
            upcoming_meetings=[],
        )
        assert "# CropSight Weekly Digest — Week of 2026-02-23" in result

    def test_contains_all_sections(self):
        """Should include all required sections."""
        from processors.weekly_digest import format_digest_document

        result = format_digest_document(
            week_of="2026-02-23",
            meetings=SAMPLE_MEETINGS,
            decisions=SAMPLE_DECISIONS,
            tasks_completed=SAMPLE_DONE_TASKS,
            tasks_overdue=SAMPLE_OVERDUE_TASKS,
            tasks_upcoming=SAMPLE_PENDING_TASKS,
            open_questions=SAMPLE_OPEN_QUESTIONS,
            upcoming_meetings=SAMPLE_UPCOMING_EVENTS[:1],
        )

        assert "## Meetings This Week" in result
        assert "## Key Decisions Made" in result
        assert "## Task Status" in result
        assert "### Completed" in result
        assert "### Overdue" in result
        assert "### Due Next Week" in result
        assert "## Open Questions" in result
        assert "## Upcoming Meetings Next Week" in result

    def test_includes_meeting_info(self):
        """Should list meetings with title and date."""
        from processors.weekly_digest import format_digest_document

        result = format_digest_document(
            week_of="2026-02-23",
            meetings=SAMPLE_MEETINGS,
            decisions=SAMPLE_DECISIONS,
            tasks_completed=[],
            tasks_overdue=[],
            tasks_upcoming=[],
            open_questions=[],
            upcoming_meetings=[],
        )

        assert "MVP Review" in result
        assert "Sprint Planning" in result

    def test_includes_decisions(self):
        """Should list decisions with descriptions."""
        from processors.weekly_digest import format_digest_document

        result = format_digest_document(
            week_of="2026-02-23",
            meetings=[],
            decisions=SAMPLE_DECISIONS,
            tasks_completed=[],
            tasks_overdue=[],
            tasks_upcoming=[],
            open_questions=[],
            upcoming_meetings=[],
        )

        assert "Use semantic versioning for the API" in result
        assert "Deploy to AWS ECS instead of GKE" in result

    def test_includes_task_tables(self):
        """Should include task tables with assignee info."""
        from processors.weekly_digest import format_digest_document

        result = format_digest_document(
            week_of="2026-02-23",
            meetings=[],
            decisions=[],
            tasks_completed=SAMPLE_DONE_TASKS,
            tasks_overdue=SAMPLE_OVERDUE_TASKS,
            tasks_upcoming=SAMPLE_PENDING_TASKS,
            open_questions=[],
            upcoming_meetings=[],
        )

        assert "Prepare client demo" in result
        assert "Paolo" in result
        assert "Update deployment docs" in result
        assert "Roye" in result

    def test_includes_open_questions(self):
        """Should list open questions with raised_by."""
        from processors.weekly_digest import format_digest_document

        result = format_digest_document(
            week_of="2026-02-23",
            meetings=[],
            decisions=[],
            tasks_completed=[],
            tasks_overdue=[],
            tasks_upcoming=[],
            open_questions=SAMPLE_OPEN_QUESTIONS,
            upcoming_meetings=[],
        )

        assert "satellite data provider" in result
        assert "Yoram" in result

    def test_includes_footer(self):
        """Should include the Gianluigi footer."""
        from processors.weekly_digest import format_digest_document

        result = format_digest_document(
            week_of="2026-02-23",
            meetings=[],
            decisions=[],
            tasks_completed=[],
            tasks_overdue=[],
            tasks_upcoming=[],
            open_questions=[],
            upcoming_meetings=[],
        )

        assert "Gianluigi" in result
        assert "CropSight" in result

    def test_shows_empty_state_messages(self):
        """Should show 'no data' messages when sections are empty."""
        from processors.weekly_digest import format_digest_document

        result = format_digest_document(
            week_of="2026-02-23",
            meetings=[],
            decisions=[],
            tasks_completed=[],
            tasks_overdue=[],
            tasks_upcoming=[],
            open_questions=[],
            upcoming_meetings=[],
        )

        assert "No meetings this week" in result
        assert "No decisions recorded this week" in result
        assert "No tasks completed this week" in result
        assert "No overdue tasks" in result
        assert "No open questions" in result


# ============================================================================
# 6. generate_weekly_digest (full orchestrator)
# ============================================================================

class TestGenerateWeeklyDigest:
    """Tests for generate_weekly_digest — full orchestrator test."""

    @pytest.mark.asyncio
    async def test_orchestrates_all_sub_functions(self):
        """Should call all sub-functions and return a complete result."""
        with patch("processors.weekly_digest.supabase_client") as mock_db, \
             patch("processors.weekly_digest.calendar_service") as mock_cal, \
             patch("processors.weekly_digest.is_cropsight_meeting", return_value=True):

            # Configure mock returns
            mock_db.list_meetings = MagicMock(return_value=SAMPLE_MEETINGS)
            mock_db.list_decisions = MagicMock(return_value=SAMPLE_DECISIONS)
            mock_db.get_tasks = MagicMock(side_effect=lambda **kw: {
                "done": SAMPLE_DONE_TASKS,
                "overdue": SAMPLE_OVERDUE_TASKS,
                "pending": SAMPLE_PENDING_TASKS,
            }.get(kw.get("status"), []))
            mock_db.get_open_questions = MagicMock(
                return_value=SAMPLE_OPEN_QUESTIONS
            )
            mock_cal.get_upcoming_events = AsyncMock(
                return_value=SAMPLE_UPCOMING_EVENTS
            )

            from processors.weekly_digest import generate_weekly_digest

            week_start = datetime(2026, 2, 23)
            result = await generate_weekly_digest(week_start=week_start)

            # Verify result structure
            assert result["week_of"] == "2026-02-23"
            assert result["meetings_count"] == 2
            assert result["decisions_count"] >= 1
            assert "digest_document" in result
            assert "CropSight Weekly Digest" in result["digest_document"]

    @pytest.mark.asyncio
    async def test_defaults_to_current_week_monday(self):
        """Should default to this week's Monday when no week_start given."""
        with patch("processors.weekly_digest.supabase_client") as mock_db, \
             patch("processors.weekly_digest.calendar_service") as mock_cal, \
             patch("processors.weekly_digest.is_cropsight_meeting", return_value=True):

            mock_db.list_meetings = MagicMock(return_value=[])
            mock_db.list_decisions = MagicMock(return_value=[])
            mock_db.get_tasks = MagicMock(return_value=[])
            mock_db.get_open_questions = MagicMock(return_value=[])
            mock_cal.get_upcoming_events = AsyncMock(return_value=[])

            from processors.weekly_digest import generate_weekly_digest

            result = await generate_weekly_digest()

            # week_of should be a Monday
            week_of_date = datetime.strptime(result["week_of"], "%Y-%m-%d")
            assert week_of_date.weekday() == 0  # Monday

    @pytest.mark.asyncio
    async def test_returns_counts(self):
        """Should return correct counts in the result dict."""
        with patch("processors.weekly_digest.supabase_client") as mock_db, \
             patch("processors.weekly_digest.calendar_service") as mock_cal, \
             patch("processors.weekly_digest.is_cropsight_meeting", return_value=True):

            mock_db.list_meetings = MagicMock(return_value=SAMPLE_MEETINGS)
            mock_db.list_decisions = MagicMock(return_value=[SAMPLE_DECISIONS[0]])
            mock_db.get_tasks = MagicMock(side_effect=lambda **kw: {
                "done": SAMPLE_DONE_TASKS,
                "overdue": SAMPLE_OVERDUE_TASKS,
                "pending": [],
            }.get(kw.get("status"), []))
            mock_db.get_open_questions = MagicMock(return_value=[])
            mock_cal.get_upcoming_events = AsyncMock(return_value=[])

            from processors.weekly_digest import generate_weekly_digest

            result = await generate_weekly_digest(
                week_start=datetime(2026, 2, 23)
            )

            assert result["meetings_count"] == 2
            assert result["tasks_completed"] == 1
            assert result["tasks_overdue"] == 1


# ============================================================================
# 7. WeeklyDigestScheduler._check_and_generate — Sunday trigger logic
# ============================================================================

class TestWeeklyDigestSchedulerTrigger:
    """Tests for scheduler trigger logic."""

    @pytest.mark.asyncio
    async def test_fires_on_sunday_1800(self):
        """Should fire _generate_and_distribute on Sunday at 18:00."""
        with patch("schedulers.weekly_digest_scheduler.generate_weekly_digest") as mock_gen, \
             patch("schedulers.weekly_digest_scheduler.drive_service") as mock_drive, \
             patch("guardrails.approval_flow.telegram_bot") as mock_tg, \
             patch("guardrails.approval_flow.gmail_service") as mock_gmail, \
             patch("schedulers.weekly_digest_scheduler.supabase_client") as mock_db, \
             patch("guardrails.approval_flow.supabase_client") as mock_af_db, \
             patch("schedulers.weekly_digest_scheduler.settings") as mock_settings:

            mock_gen.return_value = {
                "week_of": "2026-02-23",
                "digest_document": "# Test Digest",
                "meetings_count": 2,
                "decisions_count": 3,
                "tasks_completed": 1,
                "tasks_overdue": 0,
            }
            mock_drive.save_weekly_digest = AsyncMock(
                return_value={"webViewLink": "https://drive.google.com/test"}
            )
            mock_tg.send_approval_request = AsyncMock(return_value=True)
            mock_gmail.send_approval_request = AsyncMock(return_value=True)
            mock_db.log_action = MagicMock()
            mock_af_db.log_action = MagicMock()
            mock_settings.team_emails = ["eyal@test.com"]

            from schedulers.weekly_digest_scheduler import WeeklyDigestScheduler

            scheduler = WeeklyDigestScheduler()

            # Simulate Sunday 18:30
            sunday = datetime(2026, 3, 1, 18, 30)  # 2026-03-01 is a Sunday
            with patch(
                "schedulers.weekly_digest_scheduler.datetime"
            ) as mock_dt:
                mock_dt.now.return_value = sunday
                mock_dt.side_effect = lambda *args, **kw: datetime(*args, **kw)

                await scheduler._check_and_generate()

            # Should have triggered generation
            mock_gen.assert_called_once()

    @pytest.mark.asyncio
    async def test_does_not_fire_on_weekday(self):
        """Should NOT fire on a weekday."""
        from schedulers.weekly_digest_scheduler import WeeklyDigestScheduler

        scheduler = WeeklyDigestScheduler()

        # Simulate Wednesday 18:30
        wednesday = datetime(2026, 2, 25, 18, 30)
        with patch(
            "schedulers.weekly_digest_scheduler.datetime"
        ) as mock_dt:
            mock_dt.now.return_value = wednesday
            mock_dt.side_effect = lambda *args, **kw: datetime(*args, **kw)

            # Patch _generate_and_distribute to track calls
            scheduler._generate_and_distribute = AsyncMock()
            await scheduler._check_and_generate()

            scheduler._generate_and_distribute.assert_not_called()

    @pytest.mark.asyncio
    async def test_does_not_fire_outside_window(self):
        """Should NOT fire outside 18:00-20:00 window."""
        from schedulers.weekly_digest_scheduler import WeeklyDigestScheduler

        scheduler = WeeklyDigestScheduler()

        # Simulate Sunday 10:00 (outside window)
        sunday_morning = datetime(2026, 3, 1, 10, 0)  # Sunday
        with patch(
            "schedulers.weekly_digest_scheduler.datetime"
        ) as mock_dt:
            mock_dt.now.return_value = sunday_morning
            mock_dt.side_effect = lambda *args, **kw: datetime(*args, **kw)

            scheduler._generate_and_distribute = AsyncMock()
            await scheduler._check_and_generate()

            scheduler._generate_and_distribute.assert_not_called()


# ============================================================================
# 8. Duplicate prevention
# ============================================================================

class TestDuplicatePrevention:
    """Tests for duplicate digest prevention."""

    @pytest.mark.asyncio
    async def test_does_not_generate_twice_for_same_week(self):
        """Should skip generation if already generated for this week."""
        from schedulers.weekly_digest_scheduler import WeeklyDigestScheduler

        scheduler = WeeklyDigestScheduler()
        # Mark as already generated for this week
        scheduler._last_digest_week = "2026-02-23"

        # Simulate Sunday 18:30 of the same week
        sunday = datetime(2026, 3, 1, 18, 30)  # Sunday of week starting 2026-02-23
        with patch(
            "schedulers.weekly_digest_scheduler.datetime"
        ) as mock_dt:
            mock_dt.now.return_value = sunday
            mock_dt.side_effect = lambda *args, **kw: datetime(*args, **kw)

            scheduler._generate_and_distribute = AsyncMock()
            await scheduler._check_and_generate()

            # Should NOT have called generate since _last_digest_week matches
            scheduler._generate_and_distribute.assert_not_called()

    @pytest.mark.asyncio
    async def test_generates_for_new_week(self):
        """Should generate when a new week starts."""
        with patch("schedulers.weekly_digest_scheduler.generate_weekly_digest") as mock_gen, \
             patch("schedulers.weekly_digest_scheduler.drive_service") as mock_drive, \
             patch("guardrails.approval_flow.telegram_bot") as mock_tg, \
             patch("guardrails.approval_flow.gmail_service") as mock_gmail, \
             patch("schedulers.weekly_digest_scheduler.supabase_client") as mock_db, \
             patch("guardrails.approval_flow.supabase_client") as mock_af_db, \
             patch("schedulers.weekly_digest_scheduler.settings") as mock_settings:

            mock_gen.return_value = {
                "week_of": "2026-03-02",
                "digest_document": "# Test",
                "meetings_count": 1,
                "decisions_count": 0,
                "tasks_completed": 0,
                "tasks_overdue": 0,
            }
            mock_drive.save_weekly_digest = AsyncMock(return_value={"webViewLink": ""})
            mock_tg.send_approval_request = AsyncMock(return_value=True)
            mock_gmail.send_approval_request = AsyncMock(return_value=True)
            mock_db.log_action = MagicMock()
            mock_af_db.log_action = MagicMock()
            mock_settings.team_emails = []

            from schedulers.weekly_digest_scheduler import WeeklyDigestScheduler

            scheduler = WeeklyDigestScheduler()
            # Last digest was for previous week
            scheduler._last_digest_week = "2026-02-23"

            # Simulate the next Sunday (different week)
            next_sunday = datetime(2026, 3, 8, 18, 30)  # Sunday of new week
            with patch(
                "schedulers.weekly_digest_scheduler.datetime"
            ) as mock_dt:
                mock_dt.now.return_value = next_sunday
                mock_dt.side_effect = lambda *args, **kw: datetime(*args, **kw)

                await scheduler._check_and_generate()

            # Should have triggered generation for the new week
            mock_gen.assert_called_once()

    @pytest.mark.asyncio
    async def test_updates_last_digest_week_after_generation(self):
        """Should update _last_digest_week after successful generation."""
        with patch("schedulers.weekly_digest_scheduler.generate_weekly_digest") as mock_gen, \
             patch("schedulers.weekly_digest_scheduler.drive_service") as mock_drive, \
             patch("guardrails.approval_flow.telegram_bot") as mock_tg, \
             patch("guardrails.approval_flow.gmail_service") as mock_gmail, \
             patch("schedulers.weekly_digest_scheduler.supabase_client") as mock_db, \
             patch("guardrails.approval_flow.supabase_client") as mock_af_db, \
             patch("schedulers.weekly_digest_scheduler.settings") as mock_settings:

            mock_gen.return_value = {
                "week_of": "2026-03-02",
                "digest_document": "# Test",
                "meetings_count": 0,
                "decisions_count": 0,
                "tasks_completed": 0,
                "tasks_overdue": 0,
            }
            mock_drive.save_weekly_digest = AsyncMock(return_value={"webViewLink": ""})
            mock_tg.send_approval_request = AsyncMock(return_value=True)
            mock_gmail.send_approval_request = AsyncMock(return_value=True)
            mock_db.log_action = MagicMock()
            mock_af_db.log_action = MagicMock()
            mock_settings.team_emails = []

            from schedulers.weekly_digest_scheduler import WeeklyDigestScheduler

            scheduler = WeeklyDigestScheduler()
            assert scheduler._last_digest_week is None

            # Simulate Sunday 18:30
            sunday = datetime(2026, 3, 8, 18, 30)
            with patch(
                "schedulers.weekly_digest_scheduler.datetime"
            ) as mock_dt:
                mock_dt.now.return_value = sunday
                mock_dt.side_effect = lambda *args, **kw: datetime(*args, **kw)

                await scheduler._check_and_generate()

            assert scheduler._last_digest_week == "2026-03-02"
