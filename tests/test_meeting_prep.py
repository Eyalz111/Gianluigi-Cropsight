"""
Tests for processors/meeting_prep.py

Tests all shared meeting-prep functions:
- find_related_meetings
- find_relevant_decisions
- find_participant_tasks
- get_stakeholder_context
- format_prep_document
- generate_meeting_prep (full orchestrator)
"""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from datetime import datetime


# =============================================================================
# Test find_related_meetings
# =============================================================================

class TestFindRelatedMeetings:
    """Tests for find_related_meetings() — semantic search on past meetings."""

    @pytest.mark.asyncio
    async def test_finds_unique_meetings(self):
        """Should embed the topic, search embeddings, then look up unique meetings."""
        mock_embedding = [0.1] * 1536

        # Two chunks from the same meeting + one from another meeting
        mock_search_results = [
            {"source_id": "meeting-1", "similarity": 0.95},
            {"source_id": "meeting-1", "similarity": 0.90},  # duplicate
            {"source_id": "meeting-2", "similarity": 0.85},
        ]

        mock_meeting_1 = {
            "id": "meeting-1",
            "title": "MVP Review",
            "date": "2026-02-20",
            "summary": "Discussed MVP progress and next steps.",
        }
        mock_meeting_2 = {
            "id": "meeting-2",
            "title": "Sprint Planning",
            "date": "2026-02-18",
            "summary": "Planned next sprint tasks.",
        }

        with patch("processors.meeting_prep.embedding_service") as mock_embed, \
             patch("processors.meeting_prep.supabase_client") as mock_db:
            mock_embed.embed_text = AsyncMock(return_value=mock_embedding)
            mock_db.search_embeddings = MagicMock(return_value=mock_search_results)
            mock_db.get_meeting = MagicMock(
                side_effect=lambda mid: {
                    "meeting-1": mock_meeting_1,
                    "meeting-2": mock_meeting_2,
                }.get(mid)
            )

            from processors.meeting_prep import find_related_meetings

            results = await find_related_meetings(
                topic="MVP Review", participants=["Eyal"], limit=5
            )

        # Should have called embed_text with the topic
        mock_embed.embed_text.assert_awaited_once_with("MVP Review")

        # Should have searched embeddings with the right params
        mock_db.search_embeddings.assert_called_once_with(
            query_embedding=mock_embedding,
            limit=10,  # limit * 2
            source_type="meeting",
        )

        # Should return 2 unique meetings (not 3 — one was a duplicate)
        assert len(results) == 2
        assert results[0]["meeting_id"] == "meeting-1"
        assert results[1]["meeting_id"] == "meeting-2"
        assert results[0]["title"] == "MVP Review"

    @pytest.mark.asyncio
    async def test_returns_empty_on_error(self):
        """Should return empty list when embedding service fails."""
        with patch("processors.meeting_prep.embedding_service") as mock_embed:
            mock_embed.embed_text = AsyncMock(side_effect=Exception("API error"))

            from processors.meeting_prep import find_related_meetings

            results = await find_related_meetings(
                topic="Test", participants=[], limit=5
            )

        assert results == []

    @pytest.mark.asyncio
    async def test_respects_limit(self):
        """Should not return more meetings than the limit."""
        mock_embedding = [0.1] * 1536
        # 4 unique meetings
        mock_search_results = [
            {"source_id": f"meeting-{i}", "similarity": 0.9 - i * 0.1}
            for i in range(4)
        ]

        with patch("processors.meeting_prep.embedding_service") as mock_embed, \
             patch("processors.meeting_prep.supabase_client") as mock_db:
            mock_embed.embed_text = AsyncMock(return_value=mock_embedding)
            mock_db.search_embeddings = MagicMock(return_value=mock_search_results)
            mock_db.get_meeting = MagicMock(
                return_value={
                    "id": "m", "title": "Test", "date": "2026-01-01", "summary": "S"
                }
            )

            from processors.meeting_prep import find_related_meetings

            results = await find_related_meetings(
                topic="Test", participants=[], limit=2
            )

        assert len(results) == 2


# =============================================================================
# Test find_relevant_decisions
# =============================================================================

class TestFindRelevantDecisions:
    """Tests for find_relevant_decisions() — hybrid semantic + ILIKE search."""

    @pytest.mark.asyncio
    async def test_hybrid_search_combines_results(self):
        """Should combine semantic and keyword search, dedup by ID."""
        mock_embedding = [0.1] * 1536

        # Semantic search returns one decision
        mock_semantic = [
            {
                "source_id": "dec-1",
                "id": "emb-1",
                "chunk_text": "Use semantic versioning",
                "metadata": {"context": "API design", "meeting_title": "Sprint", "date": "2026-02-10"},
            }
        ]

        # ILIKE returns two: one overlapping (dec-1) and one new (dec-2)
        mock_ilike = [
            {
                "id": "dec-1",
                "description": "Use semantic versioning",
                "context": "API design",
                "meetings": {"title": "Sprint", "date": "2026-02-10"},
            },
            {
                "id": "dec-2",
                "description": "Use REST over GraphQL",
                "context": "Architecture",
                "meetings": {"title": "Tech Review", "date": "2026-02-12"},
            },
        ]

        with patch("processors.meeting_prep.embedding_service") as mock_embed, \
             patch("processors.meeting_prep.supabase_client") as mock_db:
            mock_embed.embed_text = AsyncMock(return_value=mock_embedding)
            mock_db.search_embeddings = MagicMock(return_value=mock_semantic)
            mock_db.list_decisions = MagicMock(return_value=mock_ilike)

            from processors.meeting_prep import find_relevant_decisions

            results = await find_relevant_decisions(topic="API versioning", limit=10)

        # Should have 2 unique decisions (dec-1 deduped)
        assert len(results) == 2
        ids = [r["id"] for r in results]
        assert "dec-1" in ids
        assert "dec-2" in ids

    @pytest.mark.asyncio
    async def test_semantic_failure_falls_back_to_ilike(self):
        """If semantic search fails, ILIKE results should still be returned."""
        mock_ilike = [
            {
                "id": "dec-3",
                "description": "Hire data scientist",
                "context": "Team growth",
                "meetings": {"title": "Planning", "date": "2026-02-14"},
            }
        ]

        with patch("processors.meeting_prep.embedding_service") as mock_embed, \
             patch("processors.meeting_prep.supabase_client") as mock_db:
            mock_embed.embed_text = AsyncMock(side_effect=Exception("embed fail"))
            mock_db.list_decisions = MagicMock(return_value=mock_ilike)

            from processors.meeting_prep import find_relevant_decisions

            results = await find_relevant_decisions(topic="data", limit=10)

        assert len(results) == 1
        assert results[0]["id"] == "dec-3"

    @pytest.mark.asyncio
    async def test_both_strategies_fail_returns_empty(self):
        """If both semantic and ILIKE fail, return empty list."""
        with patch("processors.meeting_prep.embedding_service") as mock_embed, \
             patch("processors.meeting_prep.supabase_client") as mock_db:
            mock_embed.embed_text = AsyncMock(side_effect=Exception("fail"))
            mock_db.list_decisions = MagicMock(side_effect=Exception("fail"))

            from processors.meeting_prep import find_relevant_decisions

            results = await find_relevant_decisions(topic="anything", limit=10)

        assert results == []


# =============================================================================
# Test find_participant_tasks
# =============================================================================

class TestFindParticipantTasks:
    """Tests for find_participant_tasks() — per-participant task queries."""

    @pytest.mark.asyncio
    async def test_queries_each_participant(self):
        """Should call get_tasks for each participant and map results."""
        eyal_tasks = [
            {"id": "t-1", "title": "Review proposal", "status": "pending", "priority": "H"},
        ]
        roye_tasks = [
            {"id": "t-2", "title": "Fix bug", "status": "pending", "priority": "M"},
            {"id": "t-3", "title": "Write tests", "status": "pending", "priority": "L"},
        ]

        with patch("processors.meeting_prep.supabase_client") as mock_db:
            mock_db.get_tasks = MagicMock(
                side_effect=lambda assignee, status: {
                    "Eyal": eyal_tasks,
                    "Roye": roye_tasks,
                }.get(assignee, [])
            )

            from processors.meeting_prep import find_participant_tasks

            results = await find_participant_tasks(["Eyal", "Roye"])

        assert "Eyal" in results
        assert "Roye" in results
        assert len(results["Eyal"]) == 1
        assert len(results["Roye"]) == 2

    @pytest.mark.asyncio
    async def test_skips_participants_with_no_tasks(self):
        """Participants with no tasks should not appear in the result."""
        with patch("processors.meeting_prep.supabase_client") as mock_db:
            mock_db.get_tasks = MagicMock(return_value=[])

            from processors.meeting_prep import find_participant_tasks

            results = await find_participant_tasks(["Paolo"])

        assert "Paolo" not in results
        assert results == {}

    @pytest.mark.asyncio
    async def test_handles_error_gracefully(self):
        """Should not crash if get_tasks raises for one participant."""
        with patch("processors.meeting_prep.supabase_client") as mock_db:
            mock_db.get_tasks = MagicMock(
                side_effect=[
                    [{"id": "t-1", "title": "Task 1", "status": "pending"}],
                    Exception("DB error"),
                ]
            )

            from processors.meeting_prep import find_participant_tasks

            results = await find_participant_tasks(["Eyal", "Roye"])

        # First participant succeeded, second failed silently
        assert "Eyal" in results
        assert "Roye" not in results


# =============================================================================
# Test get_stakeholder_context
# =============================================================================

class TestGetStakeholderContext:
    """Tests for get_stakeholder_context() — sheets lookup."""

    @pytest.mark.asyncio
    async def test_looks_up_each_participant(self):
        """Should call sheets_service.get_stakeholder_info for each name."""
        mock_info = [
            {
                "organization_name": "AgriTech Inc",
                "type": "Customer",
                "description": "Large farming operation",
                "desired_outcome": "Partnership",
                "status": "Active",
            }
        ]

        with patch("processors.meeting_prep.sheets_service") as mock_sheets:
            mock_sheets.get_stakeholder_info = AsyncMock(
                side_effect=lambda name: mock_info if name == "Rita" else []
            )

            from processors.meeting_prep import get_stakeholder_context

            results = await get_stakeholder_context(
                participant_names=["Eyal", "Rita"],
                meeting_title="Partnership Discussion",
            )

        assert len(results) == 1
        assert results[0]["organization_name"] == "AgriTech Inc"

    @pytest.mark.asyncio
    async def test_returns_empty_when_no_matches(self):
        """Should return empty list when no stakeholders are found."""
        with patch("processors.meeting_prep.sheets_service") as mock_sheets:
            mock_sheets.get_stakeholder_info = AsyncMock(return_value=[])

            from processors.meeting_prep import get_stakeholder_context

            results = await get_stakeholder_context(
                participant_names=["Unknown Person"],
                meeting_title="Test",
            )

        assert results == []


# =============================================================================
# Test format_prep_document
# =============================================================================

class TestFormatPrepDocument:
    """Tests for format_prep_document() — Markdown output."""

    def test_includes_all_sections(self):
        """Markdown document should contain all major sections."""
        from processors.meeting_prep import format_prep_document

        event = {
            "title": "CropSight MVP Review",
            "start": "2026-02-25T10:00:00Z",
            "location": "Zoom",
            "attendees": [
                {"displayName": "Eyal Zror", "email": "eyal@cropsight.io"},
                {"displayName": "Roye Tadmor", "email": "roye@cropsight.io"},
            ],
            "description": "Review MVP progress and set next sprint goals.",
        }

        related_meetings = [
            {"title": "Sprint Planning", "date": "2026-02-20", "summary": "Planned sprint 5."},
        ]

        relevant_decisions = [
            {
                "description": "Use semantic versioning for API",
                "source_meeting": "Sprint Planning",
                "date": "2026-02-20",
            },
        ]

        open_questions = [
            {"question": "What data sources should we use?", "raised_by": "Yoram"},
        ]

        participant_tasks = {
            "Eyal Zror": [
                {"title": "Review proposal", "priority": "H", "deadline": "2026-02-25", "status": "pending"},
            ],
            "Roye Tadmor": [
                {"title": "Fix model bug", "priority": "M", "deadline": "2026-02-26", "status": "pending"},
            ],
        }

        stakeholder_info = [
            {
                "organization_name": "AgriTech Corp",
                "type": "Investor",
                "description": "Seed investor",
                "desired_outcome": "ROI",
                "status": "Active",
                "notes": "Follow up next quarter",
            },
        ]

        result = format_prep_document(
            event=event,
            related_meetings=related_meetings,
            relevant_decisions=relevant_decisions,
            open_questions=open_questions,
            participant_tasks=participant_tasks,
            stakeholder_info=stakeholder_info,
        )

        # Check all sections are present
        assert "# Meeting Prep: CropSight MVP Review" in result
        assert "**When:** 2026-02-25T10:00:00Z" in result
        assert "**Where:** Zoom" in result
        assert "Eyal Zror" in result
        assert "Roye Tadmor" in result

        # Agenda section
        assert "## Agenda" in result
        assert "Review MVP progress" in result

        # Stakeholder section
        assert "## Stakeholder Context" in result
        assert "AgriTech Corp" in result
        assert "Seed investor" in result

        # Related meetings section
        assert "## Related Past Meetings" in result
        assert "Sprint Planning" in result

        # Decisions section
        assert "## Relevant Past Decisions" in result
        assert "semantic versioning" in result

        # Open questions section
        assert "## Open Questions" in result
        assert "data sources" in result
        assert "Yoram" in result

        # Participant tasks section
        assert "## Participant Tasks" in result
        assert "Review proposal" in result
        assert "Fix model bug" in result

        # Footer
        assert "Generated by Gianluigi" in result

    def test_minimal_document_no_optional_sections(self):
        """Document with no optional data should still render cleanly."""
        from processors.meeting_prep import format_prep_document

        event = {
            "title": "Quick Sync",
            "start": "2026-02-25T14:00:00Z",
            "location": "",
            "attendees": [],
            "description": "",
        }

        result = format_prep_document(
            event=event,
            related_meetings=[],
            relevant_decisions=[],
            open_questions=[],
            participant_tasks={},
            stakeholder_info=[],
        )

        assert "# Meeting Prep: Quick Sync" in result
        assert "**Where:** Not specified" in result
        assert "**Attendees:** Not specified" in result
        assert "Generated by Gianluigi" in result

        # Optional sections should NOT be present
        assert "## Agenda" not in result
        assert "## Stakeholder Context" not in result
        assert "## Related Past Meetings" not in result
        assert "## Relevant Past Decisions" not in result
        assert "## Open Questions" not in result
        assert "## Participant Tasks" not in result


# =============================================================================
# Test generate_meeting_prep (full orchestrator)
# =============================================================================

class TestGenerateMeetingPrep:
    """Tests for generate_meeting_prep() — the full orchestrator."""

    @pytest.mark.asyncio
    async def test_full_orchestrator_success(self):
        """Full orchestrator should call all sub-functions and return prep data."""
        mock_event = {
            "id": "event-abc",
            "title": "CropSight Product Review",
            "start": "2026-02-25T10:00:00Z",
            "end": "2026-02-25T11:00:00Z",
            "attendees": [
                {"displayName": "Eyal Zror", "email": "eyal@cropsight.io"},
                {"displayName": "Roye Tadmor", "email": "roye@cropsight.io"},
            ],
            "location": "Zoom",
            "description": "Review the product roadmap.",
        }

        mock_embedding = [0.1] * 1536
        mock_search_results = [
            {"source_id": "meeting-1", "similarity": 0.9},
        ]
        mock_meeting = {
            "id": "meeting-1", "title": "Previous Review",
            "date": "2026-02-20", "summary": "Good progress.",
        }
        mock_decisions_ilike = [
            {
                "id": "dec-1",
                "description": "Focus on MVP features",
                "context": "Roadmap",
                "meetings": {"title": "Previous Review", "date": "2026-02-20"},
            }
        ]
        mock_open_questions = [
            {"question": "What about CropSight satellite data?", "raised_by": "Yoram", "meeting_id": "m-1"},
        ]
        mock_tasks = [
            {"id": "t-1", "title": "Review roadmap", "status": "pending", "priority": "H"},
        ]

        with patch("processors.meeting_prep.calendar_service") as mock_cal, \
             patch("processors.meeting_prep.embedding_service") as mock_embed, \
             patch("processors.meeting_prep.supabase_client") as mock_db, \
             patch("processors.meeting_prep.sheets_service") as mock_sheets:

            # Calendar returns our event
            mock_cal.get_event = AsyncMock(return_value=mock_event)

            # Embedding service
            mock_embed.embed_text = AsyncMock(return_value=mock_embedding)

            # Supabase: search_embeddings, get_meeting, list_decisions, get_tasks, get_open_questions
            mock_db.search_embeddings = MagicMock(return_value=mock_search_results)
            mock_db.get_meeting = MagicMock(return_value=mock_meeting)
            mock_db.list_decisions = MagicMock(return_value=mock_decisions_ilike)
            mock_db.get_open_questions = MagicMock(return_value=mock_open_questions)
            mock_db.get_tasks = MagicMock(
                side_effect=lambda assignee, status: mock_tasks if assignee == "Eyal Zror" else []
            )

            # Sheets: no stakeholder matches
            mock_sheets.get_stakeholder_info = AsyncMock(return_value=[])

            from processors.meeting_prep import generate_meeting_prep

            result = await generate_meeting_prep("event-abc")

        # Should have called calendar to get the event
        mock_cal.get_event.assert_awaited_once_with("event-abc")

        # Should return all the expected keys
        assert "event" in result
        assert "prep_document" in result
        assert "related_meetings" in result
        assert "relevant_decisions" in result
        assert "open_questions" in result
        assert "participant_tasks" in result
        assert "stakeholder_info" in result

        # The prep document should be non-empty Markdown
        assert "# Meeting Prep: CropSight Product Review" in result["prep_document"]
        assert "Related Past Meetings" in result["prep_document"]

        # Verify data was collected
        assert len(result["related_meetings"]) == 1
        assert result["related_meetings"][0]["title"] == "Previous Review"

    @pytest.mark.asyncio
    async def test_returns_error_when_event_not_found(self):
        """Should return error dict when the calendar event is not found."""
        with patch("processors.meeting_prep.calendar_service") as mock_cal:
            mock_cal.get_event = AsyncMock(return_value=None)

            from processors.meeting_prep import generate_meeting_prep

            result = await generate_meeting_prep("nonexistent-id")

        assert "error" in result
        assert result["error"] == "Event not found"


# =============================================================================
# Test _find_open_questions (internal helper)
# =============================================================================

class TestFindOpenQuestions:
    """Tests for the internal _find_open_questions helper."""

    def test_filters_by_keyword_overlap(self):
        """Should return questions that share words with the topic."""
        mock_questions = [
            {"question": "What about CropSight data sources?", "raised_by": "Yoram", "meeting_id": "m-1"},
            {"question": "When is the launch date?", "raised_by": "Eyal", "meeting_id": "m-2"},
        ]

        with patch("processors.meeting_prep.supabase_client") as mock_db:
            mock_db.get_open_questions = MagicMock(return_value=mock_questions)

            from processors.meeting_prep import _find_open_questions

            # "CropSight" overlaps with the first question
            results = _find_open_questions("CropSight Review")

        assert len(results) == 1
        assert "data sources" in results[0]["question"]

    def test_returns_empty_on_no_overlap(self):
        """Should return empty list when no questions match."""
        mock_questions = [
            {"question": "Unrelated stuff?", "raised_by": "Someone", "meeting_id": "m-1"},
        ]

        with patch("processors.meeting_prep.supabase_client") as mock_db:
            mock_db.get_open_questions = MagicMock(return_value=mock_questions)

            from processors.meeting_prep import _find_open_questions

            results = _find_open_questions("Completely Different Topic")

        assert results == []


# =============================================================================
# Test scheduler sensitivity distribution (integration-level)
# =============================================================================

class TestSchedulerSensitivityDistribution:
    """Tests for sensitivity-aware distribution via approval flow."""

    @pytest.mark.asyncio
    async def test_sensitive_meeting_submits_for_approval(self):
        """Sensitive meetings should submit for approval with sensitivity=sensitive."""
        event = {
            "id": "event-sensitive",
            "title": "Investor Meeting Prep",  # contains "investor"
            "start": "2026-02-25T10:00:00Z",
            "attendees": [],
            "location": "",
            "description": "",
        }

        with patch("schedulers.meeting_prep_scheduler.find_related_meetings", new_callable=AsyncMock, return_value=[]), \
             patch("schedulers.meeting_prep_scheduler.find_relevant_decisions", new_callable=AsyncMock, return_value=[]), \
             patch("schedulers.meeting_prep_scheduler._find_open_questions", return_value=[]), \
             patch("schedulers.meeting_prep_scheduler.get_stakeholder_context", new_callable=AsyncMock, return_value=[]), \
             patch("schedulers.meeting_prep_scheduler.find_participant_tasks", new_callable=AsyncMock, return_value={}), \
             patch("schedulers.meeting_prep_scheduler.format_prep_document", return_value="# Prep"), \
             patch("schedulers.meeting_prep_scheduler.drive_service") as mock_drive, \
             patch("schedulers.meeting_prep_scheduler.submit_for_approval", new_callable=AsyncMock) as mock_submit, \
             patch("schedulers.meeting_prep_scheduler.supabase_client") as mock_db:

            mock_drive.save_meeting_prep = AsyncMock(
                return_value={"webViewLink": "https://drive.google.com/test"}
            )
            mock_submit.return_value = {"status": "pending"}
            mock_db.log_action = MagicMock()

            from schedulers.meeting_prep_scheduler import MeetingPrepScheduler

            scheduler = MeetingPrepScheduler()
            result = await scheduler._generate_prep_for_meeting(event)

        assert result["status"] == "success"
        assert result["sensitivity"] == "sensitive"

        # Should have submitted for approval with correct content type
        mock_submit.assert_awaited_once()
        call_kwargs = mock_submit.call_args[1]
        assert call_kwargs["content_type"] == "meeting_prep"
        assert call_kwargs["content"]["sensitivity"] == "sensitive"

    @pytest.mark.asyncio
    async def test_normal_meeting_submits_for_approval(self):
        """Normal meetings should submit for approval with sensitivity=normal."""
        event = {
            "id": "event-normal",
            "title": "CropSight Sprint Planning",
            "start": "2026-02-25T10:00:00Z",
            "attendees": [],
            "location": "",
            "description": "",
        }

        with patch("schedulers.meeting_prep_scheduler.find_related_meetings", new_callable=AsyncMock, return_value=[]), \
             patch("schedulers.meeting_prep_scheduler.find_relevant_decisions", new_callable=AsyncMock, return_value=[]), \
             patch("schedulers.meeting_prep_scheduler._find_open_questions", return_value=[]), \
             patch("schedulers.meeting_prep_scheduler.get_stakeholder_context", new_callable=AsyncMock, return_value=[]), \
             patch("schedulers.meeting_prep_scheduler.find_participant_tasks", new_callable=AsyncMock, return_value={}), \
             patch("schedulers.meeting_prep_scheduler.format_prep_document", return_value="# Prep"), \
             patch("schedulers.meeting_prep_scheduler.drive_service") as mock_drive, \
             patch("schedulers.meeting_prep_scheduler.submit_for_approval", new_callable=AsyncMock) as mock_submit, \
             patch("schedulers.meeting_prep_scheduler.supabase_client") as mock_db:

            mock_drive.save_meeting_prep = AsyncMock(
                return_value={"webViewLink": "https://drive.google.com/test"}
            )
            mock_submit.return_value = {"status": "pending"}
            mock_db.log_action = MagicMock()

            from schedulers.meeting_prep_scheduler import MeetingPrepScheduler

            scheduler = MeetingPrepScheduler()
            result = await scheduler._generate_prep_for_meeting(event)

        assert result["status"] == "success"
        assert result["sensitivity"] == "normal"

        # Should have submitted for approval with correct content type
        mock_submit.assert_awaited_once()
        call_kwargs = mock_submit.call_args[1]
        assert call_kwargs["content_type"] == "meeting_prep"
        assert call_kwargs["content"]["sensitivity"] == "normal"


# =============================================================================
# Test Pre-Meeting Reminders (Phase 5)
# =============================================================================

class TestPreMeetingReminder:
    """Tests for _send_pre_meeting_reminder() in the scheduler."""

    @pytest.mark.asyncio
    async def test_reminder_fires_when_meeting_2_to_3_hours_away(self):
        """Should send reminder when meeting is 2-3 hours away."""
        from datetime import timezone, timedelta

        now = datetime.now(timezone.utc)
        # Meeting 2.5 hours from now
        meeting_start = (now + timedelta(hours=2, minutes=30)).isoformat()

        event = {
            "id": "event-reminder-1",
            "title": "CropSight Standup",
            "start": meeting_start,
            "attendees": [
                {"email": "eyal@cropsight.io", "displayName": "Eyal Zror"},
                {"email": "roye@cropsight.io", "displayName": "Roye Tadmor"},
            ],
        }

        with patch("schedulers.meeting_prep_scheduler.find_related_meetings", new_callable=AsyncMock, return_value=[]), \
             patch("schedulers.meeting_prep_scheduler.find_participant_tasks", new_callable=AsyncMock, return_value={}), \
             patch("schedulers.meeting_prep_scheduler.telegram_bot") as mock_tg, \
             patch("schedulers.meeting_prep_scheduler.supabase_client") as mock_db:

            mock_tg.send_to_eyal = AsyncMock(return_value=True)
            mock_db.log_action = MagicMock()

            from schedulers.meeting_prep_scheduler import MeetingPrepScheduler

            scheduler = MeetingPrepScheduler()
            result = await scheduler._send_pre_meeting_reminder(event)

        assert result is True
        mock_tg.send_to_eyal.assert_awaited_once()
        # Message should contain the meeting title
        sent_msg = mock_tg.send_to_eyal.call_args[0][0]
        assert "CropSight Standup" in sent_msg

    @pytest.mark.asyncio
    async def test_reminder_skipped_when_too_far(self):
        """Should not send reminder when meeting is more than 3 hours away."""
        from datetime import timezone, timedelta

        now = datetime.now(timezone.utc)
        # Meeting 5 hours from now
        meeting_start = (now + timedelta(hours=5)).isoformat()

        event = {
            "id": "event-too-far",
            "title": "Future Meeting",
            "start": meeting_start,
            "attendees": [],
        }

        with patch("schedulers.meeting_prep_scheduler.telegram_bot") as mock_tg, \
             patch("schedulers.meeting_prep_scheduler.supabase_client") as mock_db:

            mock_tg.send_to_eyal = AsyncMock(return_value=True)
            mock_db.log_action = MagicMock()

            from schedulers.meeting_prep_scheduler import MeetingPrepScheduler

            scheduler = MeetingPrepScheduler()
            result = await scheduler._send_pre_meeting_reminder(event)

        assert result is False
        mock_tg.send_to_eyal.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_reminder_skipped_when_too_close(self):
        """Should not send reminder when meeting is less than 2 hours away."""
        from datetime import timezone, timedelta

        now = datetime.now(timezone.utc)
        # Meeting 1 hour from now
        meeting_start = (now + timedelta(hours=1)).isoformat()

        event = {
            "id": "event-too-close",
            "title": "Imminent Meeting",
            "start": meeting_start,
            "attendees": [],
        }

        with patch("schedulers.meeting_prep_scheduler.telegram_bot") as mock_tg, \
             patch("schedulers.meeting_prep_scheduler.supabase_client") as mock_db:

            mock_tg.send_to_eyal = AsyncMock(return_value=True)
            mock_db.log_action = MagicMock()

            from schedulers.meeting_prep_scheduler import MeetingPrepScheduler

            scheduler = MeetingPrepScheduler()
            result = await scheduler._send_pre_meeting_reminder(event)

        assert result is False
        mock_tg.send_to_eyal.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_duplicate_reminder_prevention(self):
        """Should not send the same reminder twice."""
        from datetime import timezone, timedelta

        now = datetime.now(timezone.utc)
        meeting_start = (now + timedelta(hours=2, minutes=30)).isoformat()

        event = {
            "id": "event-dup-test",
            "title": "Dup Test Meeting",
            "start": meeting_start,
            "attendees": [],
        }

        with patch("schedulers.meeting_prep_scheduler.find_related_meetings", new_callable=AsyncMock, return_value=[]), \
             patch("schedulers.meeting_prep_scheduler.find_participant_tasks", new_callable=AsyncMock, return_value={}), \
             patch("schedulers.meeting_prep_scheduler.telegram_bot") as mock_tg, \
             patch("schedulers.meeting_prep_scheduler.supabase_client") as mock_db:

            mock_tg.send_to_eyal = AsyncMock(return_value=True)
            mock_db.log_action = MagicMock()

            from schedulers.meeting_prep_scheduler import MeetingPrepScheduler

            scheduler = MeetingPrepScheduler()

            # First call should send
            result1 = await scheduler._send_pre_meeting_reminder(event)
            # Second call should skip (duplicate)
            result2 = await scheduler._send_pre_meeting_reminder(event)

        assert result1 is True
        assert result2 is False
        # Only one message sent
        assert mock_tg.send_to_eyal.await_count == 1

    @pytest.mark.asyncio
    async def test_reminder_includes_related_meeting_context(self):
        """Should include related meeting info in the reminder message."""
        from datetime import timezone, timedelta

        now = datetime.now(timezone.utc)
        meeting_start = (now + timedelta(hours=2, minutes=30)).isoformat()

        event = {
            "id": "event-context-test",
            "title": "CropSight Sprint",
            "start": meeting_start,
            "attendees": [
                {"email": "eyal@cropsight.io", "displayName": "Eyal Zror"},
            ],
        }

        mock_related = [
            {
                "title": "Previous Sprint",
                "date": "2026-02-20",
                "summary": "Planned next steps.",
            }
        ]
        mock_tasks = {
            "Eyal Zror": [
                {"id": "t-1", "title": "Review proposal", "status": "pending"},
                {"id": "t-2", "title": "Write spec", "status": "pending"},
            ]
        }

        with patch("schedulers.meeting_prep_scheduler.find_related_meetings", new_callable=AsyncMock, return_value=mock_related), \
             patch("schedulers.meeting_prep_scheduler.find_participant_tasks", new_callable=AsyncMock, return_value=mock_tasks), \
             patch("schedulers.meeting_prep_scheduler.telegram_bot") as mock_tg, \
             patch("schedulers.meeting_prep_scheduler.supabase_client") as mock_db:

            mock_tg.send_to_eyal = AsyncMock(return_value=True)
            mock_db.log_action = MagicMock()

            from schedulers.meeting_prep_scheduler import MeetingPrepScheduler

            scheduler = MeetingPrepScheduler()
            result = await scheduler._send_pre_meeting_reminder(event)

        assert result is True
        sent_msg = mock_tg.send_to_eyal.call_args[0][0]
        assert "Previous Sprint" in sent_msg
        assert "2026-02-20" in sent_msg
        assert "Open tasks for attendees: 2" in sent_msg
        assert "Eyal Zror" in sent_msg
