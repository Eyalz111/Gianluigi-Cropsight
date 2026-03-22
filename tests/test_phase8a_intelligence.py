"""
Tests for Phase 8a: Extraction Intelligence, Escalation, and Review Hygiene.

Covers:
- A1: Task continuity in extraction (existing tasks as context, participant-first sort)
- A2: Team role context in extraction
- A3: Priority-aware escalation rules
- A4: Weekly review hygiene items (unassigned/no-deadline tasks)
- D2: Hebrew extraction instruction
"""

import pytest
from datetime import datetime, timedelta
from unittest.mock import MagicMock, AsyncMock, patch


# =============================================================================
# A1: Task Continuity in Extraction
# =============================================================================


class TestExtractionTaskContext:
    """Tests for existing task context in extraction prompt."""

    def test_prompt_includes_existing_tasks_when_provided(self):
        """get_summary_extraction_prompt should include EXISTING OPEN TASKS section."""
        from core.system_prompt import get_summary_extraction_prompt

        tasks = [
            {"title": "Write accuracy abstract", "assignee": "Roye", "status": "pending"},
            {"title": "Schedule investor call", "assignee": "Eyal", "status": "in_progress"},
        ]
        prompt = get_summary_extraction_prompt(
            transcript="test transcript",
            meeting_title="Test Meeting",
            meeting_date="2026-03-22",
            participants=["Eyal", "Roye"],
            existing_tasks=tasks,
        )
        assert "EXISTING OPEN TASKS" in prompt
        assert "Write accuracy abstract" in prompt
        assert "Schedule investor call" in prompt
        assert "do NOT duplicate" in prompt.lower() or "NOT duplicate" in prompt

    def test_prompt_omits_tasks_section_when_empty(self):
        """get_summary_extraction_prompt should not include tasks section when None/empty."""
        from core.system_prompt import get_summary_extraction_prompt

        prompt_none = get_summary_extraction_prompt(
            transcript="test",
            meeting_title="Test",
            meeting_date="2026-03-22",
            participants=["Eyal"],
            existing_tasks=None,
        )
        prompt_empty = get_summary_extraction_prompt(
            transcript="test",
            meeting_title="Test",
            meeting_date="2026-03-22",
            participants=["Eyal"],
            existing_tasks=[],
        )
        assert "EXISTING OPEN TASKS" not in prompt_none
        assert "EXISTING OPEN TASKS" not in prompt_empty

    def test_prompt_caps_at_30_tasks(self):
        """Should include at most 30 tasks in the prompt."""
        from core.system_prompt import get_summary_extraction_prompt

        tasks = [
            {"title": f"Task {i}", "assignee": "Roye", "status": "pending"}
            for i in range(50)
        ]
        prompt = get_summary_extraction_prompt(
            transcript="test",
            meeting_title="Test",
            meeting_date="2026-03-22",
            participants=["Eyal"],
            existing_tasks=tasks,
        )
        assert "Task 29" in prompt  # 0-indexed, 30th item
        assert "Task 30" not in prompt  # 31st should be cut

    @pytest.mark.asyncio
    async def test_extract_structured_data_fetches_tasks(self):
        """extract_structured_data should call get_tasks for context."""
        with patch("processors.transcript_processor.supabase_client") as mock_sc, \
             patch("processors.transcript_processor.get_summary_extraction_prompt") as mock_prompt, \
             patch("processors.transcript_processor.call_llm") as mock_llm:
            mock_sc.get_tasks.return_value = [
                {"title": "Existing task", "assignee": "Roye", "status": "pending", "priority": "M"}
            ]
            mock_prompt.return_value = "test prompt"
            mock_llm.return_value = MagicMock(
                content=[MagicMock(text='{"decisions":[],"tasks":[],"follow_ups":[],"open_questions":[],"stakeholders":[],"discussion_summary":"test","executive_summary":"test"}')]
            )

            from processors.transcript_processor import extract_structured_data
            await extract_structured_data(
                transcript="test transcript",
                meeting_title="Test",
                participants=["Eyal", "Roye"],
                meeting_date="2026-03-22",
            )

            # Verify get_tasks was called (at least twice: pending + in_progress)
            assert mock_sc.get_tasks.call_count >= 2
            # Verify existing_tasks was passed to prompt builder
            call_kwargs = mock_prompt.call_args.kwargs
            assert "existing_tasks" in call_kwargs

    @pytest.mark.asyncio
    async def test_extract_structured_data_graceful_degradation(self):
        """If get_tasks fails, extraction should proceed without context."""
        with patch("processors.transcript_processor.supabase_client") as mock_sc, \
             patch("processors.transcript_processor.get_summary_extraction_prompt") as mock_prompt, \
             patch("processors.transcript_processor.call_llm") as mock_llm:
            mock_sc.get_tasks.side_effect = Exception("DB timeout")
            mock_prompt.return_value = "test prompt"
            mock_llm.return_value = MagicMock(
                content=[MagicMock(text='{"decisions":[],"tasks":[],"follow_ups":[],"open_questions":[],"stakeholders":[],"discussion_summary":"test","executive_summary":"test"}')]
            )

            from processors.transcript_processor import extract_structured_data
            # Should not raise
            result = await extract_structured_data(
                transcript="test",
                meeting_title="Test",
                participants=["Eyal"],
                meeting_date="2026-03-22",
            )
            # Extraction should still work
            assert isinstance(result, dict)
            # existing_tasks should be None (not passed or empty)
            call_kwargs = mock_prompt.call_args.kwargs
            assert call_kwargs.get("existing_tasks") is None

    def test_participant_first_sorting(self):
        """Tasks assigned to meeting participants should sort first."""
        tasks = [
            {"title": "Paolo's task", "assignee": "Paolo", "status": "pending", "priority": "M"},
            {"title": "Roye's task", "assignee": "Roye", "status": "pending", "priority": "H"},
            {"title": "Random task", "assignee": "External", "status": "pending", "priority": "H"},
        ]
        participants = ["Eyal", "Roye"]
        participant_names_lower = {p.lower() for p in participants}

        def task_sort_key(t):
            assignee = (t.get("assignee") or "").lower()
            is_participant = 1 if any(
                name in assignee for name in participant_names_lower if name
            ) else 0
            priority_rank = {"H": 0, "M": 1, "L": 2}.get(t.get("priority", "M"), 1)
            return (-is_participant, priority_rank)

        tasks.sort(key=task_sort_key)
        # Roye's task should be first (participant + H priority)
        assert tasks[0]["assignee"] == "Roye"
        # External H priority should come after participants
        assert tasks[-1]["assignee"] == "External" or tasks[-1]["assignee"] == "Paolo"


# =============================================================================
# A2: Team Role Context
# =============================================================================


class TestExtractionTeamRoles:
    """Tests for team role context in extraction prompt."""

    def test_prompt_includes_team_roles(self):
        """get_summary_extraction_prompt should include team roles section."""
        from core.system_prompt import get_summary_extraction_prompt

        team_roles = (
            "- Eyal Zror (CEO): Strategy, fundraising\n"
            "- Roye Tadmor (CTO): Technical execution"
        )
        prompt = get_summary_extraction_prompt(
            transcript="test",
            meeting_title="Test",
            meeting_date="2026-03-22",
            participants=["Eyal"],
            team_roles=team_roles,
        )
        assert "CROPSIGHT TEAM ROLES" in prompt
        assert "Eyal Zror (CEO)" in prompt
        assert "Roye Tadmor (CTO)" in prompt

    def test_prompt_omits_team_roles_when_none(self):
        """Team roles section should be omitted when not provided."""
        from core.system_prompt import get_summary_extraction_prompt

        prompt = get_summary_extraction_prompt(
            transcript="test",
            meeting_title="Test",
            meeting_date="2026-03-22",
            participants=["Eyal"],
            team_roles=None,
        )
        assert "CROPSIGHT TEAM ROLES" not in prompt

    def test_team_members_have_role_descriptions(self):
        """All team members should have role_description field."""
        from config.team import TEAM_MEMBERS

        for key, member in TEAM_MEMBERS.items():
            assert "role_description" in member, f"Missing role_description for {key}"
            assert len(member["role_description"]) > 20, (
                f"Role description too short for {key}: {member['role_description']}"
            )

    def test_extraction_system_prompt_has_assignee_rules(self):
        """Extraction system prompt should contain ASSIGNEE rules."""
        import inspect
        from processors.transcript_processor import extract_structured_data
        source = inspect.getsource(extract_structured_data)
        assert "ASSIGNEE" in source
        assert 'empty string' in source.lower() or '""' in source


# =============================================================================
# A3: Priority-Aware Escalation
# =============================================================================


class TestEscalationTierClassification:
    """Tests for config/escalation.py classification logic."""

    def test_high_priority_escalates_faster(self):
        """H priority tasks should reach higher tiers at fewer days."""
        from config.escalation import classify_overdue_tier

        # H at 2 days = low, M at 2 days = None
        assert classify_overdue_tier(2, "H") == "low"
        assert classify_overdue_tier(2, "M") is None

        # H at 5 days = medium, M at 5 days = low
        assert classify_overdue_tier(5, "H") == "medium"
        assert classify_overdue_tier(5, "M") == "low"

        # H at 11 days = critical, M at 11 days = medium
        assert classify_overdue_tier(11, "H") == "critical"
        assert classify_overdue_tier(11, "M") == "medium"

    def test_low_priority_escalates_slower(self):
        """L priority tasks should have higher thresholds."""
        from config.escalation import classify_overdue_tier

        assert classify_overdue_tier(5, "L") is None
        assert classify_overdue_tier(7, "L") == "low"
        assert classify_overdue_tier(14, "L") == "medium"
        assert classify_overdue_tier(22, "L") == "critical"

    def test_zero_or_negative_days_returns_none(self):
        """Tasks not overdue should return None."""
        from config.escalation import classify_overdue_tier

        assert classify_overdue_tier(0, "H") is None
        assert classify_overdue_tier(-1, "M") is None

    def test_unknown_priority_defaults_to_medium(self):
        """Unknown priority should use M thresholds."""
        from config.escalation import classify_overdue_tier

        assert classify_overdue_tier(3, "X") == "low"  # M threshold
        assert classify_overdue_tier(7, "X") == "medium"

    def test_all_tiers_reachable(self):
        """Every tier should be reachable for every priority."""
        from config.escalation import classify_overdue_tier, ESCALATION_TIERS

        for priority in ("H", "M", "L"):
            tiers_hit = set()
            for days in range(1, 30):
                tier = classify_overdue_tier(days, priority)
                if tier:
                    tiers_hit.add(tier)
            assert tiers_hit == {"low", "medium", "high", "critical"}, (
                f"Not all tiers reachable for priority {priority}: {tiers_hit}"
            )


class TestOverdueEscalationAlerts:
    """Tests for _check_overdue_escalation in proactive_alerts.py."""

    def test_generates_alert_for_critical_tasks(self):
        """Should generate alerts for tasks at high/critical escalation tier."""
        today = datetime.now().date()
        overdue_20d = (today - timedelta(days=20)).isoformat()

        with patch("processors.proactive_alerts.supabase_client") as mock_sc:
            mock_sc.get_tasks.return_value = [
                {"id": "t1", "title": "Old task", "assignee": "Roye",
                 "priority": "H", "status": "overdue", "deadline": overdue_20d},
            ]

            from processors.proactive_alerts import _check_overdue_escalation
            alerts = _check_overdue_escalation()

            assert len(alerts) >= 1
            assert alerts[0]["type"] == "overdue_escalation"
            assert alerts[0]["severity"] in ("high", "critical")

    def test_skips_low_tier_tasks(self):
        """Should NOT generate alerts for low/medium tier tasks."""
        today = datetime.now().date()
        overdue_2d = (today - timedelta(days=2)).isoformat()

        with patch("processors.proactive_alerts.supabase_client") as mock_sc:
            mock_sc.get_tasks.return_value = [
                {"id": "t1", "title": "Recent task", "assignee": "Roye",
                 "priority": "M", "status": "overdue", "deadline": overdue_2d},
            ]

            from processors.proactive_alerts import _check_overdue_escalation
            alerts = _check_overdue_escalation()

            assert len(alerts) == 0

    def test_empty_overdue_returns_empty(self):
        """No overdue tasks → no alerts."""
        with patch("processors.proactive_alerts.supabase_client") as mock_sc:
            mock_sc.get_tasks.return_value = []

            from processors.proactive_alerts import _check_overdue_escalation
            alerts = _check_overdue_escalation()

            assert alerts == []


class TestGetEscalationItems:
    """Tests for get_escalation_items (used by weekly review)."""

    def test_returns_all_tiers(self):
        """Should return items across all tiers, sorted critical-first."""
        today = datetime.now().date()

        with patch("processors.proactive_alerts.supabase_client") as mock_sc:
            mock_sc.get_tasks.return_value = [
                {"id": "t1", "title": "Critical", "assignee": "Roye",
                 "priority": "H", "deadline": (today - timedelta(days=15)).isoformat(),
                 "status": "overdue"},
                {"id": "t2", "title": "Low", "assignee": "Paolo",
                 "priority": "M", "deadline": (today - timedelta(days=3)).isoformat(),
                 "status": "overdue"},
            ]

            from processors.proactive_alerts import get_escalation_items
            items = get_escalation_items()

            assert len(items) == 2
            # Critical should be first
            assert items[0]["tier"] == "critical"
            assert items[1]["tier"] == "low"


# =============================================================================
# A4: Weekly Review Hygiene Items
# =============================================================================


class TestWeeklyReviewHygiene:
    """Tests for task hygiene items in weekly review compilation."""

    @pytest.mark.asyncio
    async def test_attention_needed_includes_hygiene_fields(self):
        """_compile_attention_needed should include unassigned and no-deadline fields."""
        with patch("processors.weekly_review.supabase_client") as mock_sc, \
             patch("processors.proactive_alerts.generate_alerts", return_value=[]), \
             patch("processors.proactive_alerts.supabase_client") as mock_pa_sc:

            mock_sc.get_stale_tasks.return_value = []
            mock_sc.get_tasks_without_assignee.return_value = [
                {"id": "t1", "title": "Unassigned task", "assignee": ""}
            ]
            mock_sc.get_tasks_without_deadline.return_value = [
                {"id": "t2", "title": "No deadline task", "deadline": None}
            ]
            # Mock for escalation items (called inside get_escalation_items)
            mock_pa_sc.get_tasks.return_value = []

            from processors.weekly_review import _compile_attention_needed
            result = await _compile_attention_needed()

            assert "tasks_no_assignee" in result
            assert "tasks_no_deadline" in result
            assert "escalation_items" in result
            assert len(result["tasks_no_assignee"]) == 1
            assert len(result["tasks_no_deadline"]) == 1


# =============================================================================
# D2: Hebrew Extraction Instruction
# =============================================================================


class TestHebrewExtractionInstruction:
    """Tests for Hebrew language handling in extraction prompt."""

    def test_extraction_prompt_has_language_handling(self):
        """Extraction system prompt should contain LANGUAGE HANDLING section."""
        import inspect
        from processors.transcript_processor import extract_structured_data
        source = inspect.getsource(extract_structured_data)
        assert "LANGUAGE HANDLING" in source
        assert "Hebrew" in source
        assert "proper nouns" in source.lower() or "proper noun" in source.lower()

    def test_extraction_prompt_keeps_person_names(self):
        """Hebrew instruction should specify keeping person names as-is."""
        import inspect
        from processors.transcript_processor import extract_structured_data
        source = inspect.getsource(extract_structured_data)
        assert "Eyal" in source and "Roye" in source and "Paolo" in source
