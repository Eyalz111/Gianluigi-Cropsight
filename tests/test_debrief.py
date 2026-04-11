"""
Tests for Phase 3: End-of-Day Debrief and Quick Injection.

Tests cover:
- Quick injection: extraction, approval, dismissal
- Full debrief: session lifecycle, message processing, done detection
- Confirmation & editing
- Opus validation (conditional)
- Injection pipeline
- Calendar & edge cases
- RAG source weight boost
"""

import json
import pytest
from datetime import date, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch, PropertyMock


# =============================================================================
# Quick Injection
# =============================================================================

class TestQuickInjection:
    """Tests for process_quick_injection() in processors/debrief.py."""

    @pytest.mark.asyncio
    async def test_quick_injection_extracts_items(self):
        """Single message should extract items."""
        mock_response = json.dumps({
            "extracted_items": [
                {"type": "task", "title": "Follow up with Orit", "assignee": "Eyal", "priority": "M"},
                {"type": "information", "description": "Wheat data confirmed for next week"},
            ],
            "response_text": "Got it — captured a task and an info item.",
        })

        with patch("processors.debrief.call_llm", return_value=(mock_response, {})):
            from processors.debrief import process_quick_injection

            result = await process_quick_injection(
                user_message="Just spoke with Orit — wheat data confirmed for next week. Need to follow up.",
                user_id="eyal",
            )

            assert result["action"] == "quick_injection_confirm"
            assert len(result["extracted_items"]) == 2
            assert result["extracted_items"][0]["type"] == "task"

    @pytest.mark.asyncio
    async def test_quick_injection_no_items(self):
        """Message with no extractable content returns no items."""
        mock_response = json.dumps({
            "extracted_items": [],
            "response_text": "Nothing to extract.",
        })

        with patch("processors.debrief.call_llm", return_value=(mock_response, {})):
            from processors.debrief import process_quick_injection

            result = await process_quick_injection(
                user_message="Hi, how's it going?",
                user_id="eyal",
            )

            assert result["action"] == "none"
            assert len(result["extracted_items"]) == 0

    @pytest.mark.asyncio
    async def test_quick_injection_no_session_state(self):
        """Quick injection should NOT create a debrief session."""
        mock_response = json.dumps({
            "extracted_items": [{"type": "information", "description": "test"}],
            "response_text": "Got it.",
        })

        with patch("processors.debrief.call_llm", return_value=(mock_response, {})):
            with patch("processors.debrief.supabase_client") as mock_db:
                from processors.debrief import process_quick_injection

                await process_quick_injection(
                    user_message="FYI, deal signed.",
                    user_id="eyal",
                )

                # Should NOT create a debrief session
                mock_db.create_debrief_session.assert_not_called()

    @pytest.mark.asyncio
    async def test_quick_injection_approve_injects(self):
        """Approving quick injection should write items to DB."""
        items = [
            {"type": "task", "title": "Call Orit", "assignee": "Eyal", "priority": "M"},
            {"type": "decision", "description": "Going with AWS"},
        ]

        with patch("processors.debrief.supabase_client") as mock_db:
            mock_db.get_debrief_session.return_value = {
                "id": "session-1",
                "items_captured": items,
                "date": date.today().isoformat(),
                "status": "confirming",
            }
            mock_db.create_meeting.return_value = {"id": "pseudo-meeting-1"}
            mock_db.create_task.return_value = {"id": "task-1"}
            mock_db.create_decision.return_value = {"id": "dec-1"}

            with patch("services.embeddings.embedding_service") as mock_embed:
                mock_embed.chunk_and_embed_document = AsyncMock(return_value=[])

                with patch("processors.cross_reference.deduplicate_tasks") as mock_dedup:
                    mock_dedup.return_value = {
                        "new_tasks": [{"title": "Call Orit", "assignee": "Eyal", "priority": "M"}],
                        "duplicates": [],
                        "updates": [],
                    }

                    from processors.debrief import confirm_debrief

                    result = await confirm_debrief("session-1", approved=True)

                    assert result["action"] == "debrief_approved"
                    mock_db.create_meeting.assert_called_once()
                    mock_db.create_task.assert_called_once()
                    mock_db.create_decision.assert_called_once()

    @pytest.mark.asyncio
    async def test_quick_injection_dismiss_no_side_effects(self):
        """Dismissing quick injection should have no DB writes."""
        with patch("processors.debrief.supabase_client") as mock_db:
            mock_db.get_debrief_session.return_value = {
                "id": "session-1",
                "items_captured": [],
                "status": "confirming",
            }

            from processors.debrief import confirm_debrief

            result = await confirm_debrief("session-1", approved=False)

            assert result["action"] == "debrief_cancelled"
            mock_db.create_meeting.assert_not_called()
            mock_db.create_task.assert_not_called()


# =============================================================================
# Full Debrief — Session Lifecycle
# =============================================================================

class TestDebriefSessionLifecycle:
    """Tests for debrief session start/resume/cancel."""

    @pytest.mark.asyncio
    async def test_start_debrief_creates_session(self):
        """Starting a debrief should create a new session."""
        with patch("processors.debrief.supabase_client") as mock_db:
            mock_db.get_active_debrief_session.return_value = None
            mock_db.create_debrief_session.return_value = {
                "id": "new-session-1",
                "date": date.today().isoformat(),
            }

            # Mock calendar (locally imported in start_debrief)
            with patch("services.google_calendar.calendar_service") as mock_cal:
                mock_cal.get_todays_events = AsyncMock(return_value=[])

                from processors.debrief import start_debrief

                result = await start_debrief(user_id="eyal")

                assert result["action"] == "debrief_started"
                assert result["session_id"] == "new-session-1"
                mock_db.create_debrief_session.assert_called_once()

    @pytest.mark.asyncio
    async def test_start_debrief_resumes_existing(self):
        """Same-date session should be resumed, not recreated."""
        today_str = date.today().isoformat()

        with patch("processors.debrief.supabase_client") as mock_db:
            mock_db.get_active_debrief_session.return_value = {
                "id": "existing-session",
                "date": today_str,
                "items_captured": [{"type": "task", "title": "Existing"}],
                "calendar_events_remaining": ["Board Meeting"],
                "status": "in_progress",
            }

            from processors.debrief import start_debrief

            result = await start_debrief(user_id="eyal")

            assert result["action"] == "debrief_resumed"
            assert result["session_id"] == "existing-session"
            mock_db.create_debrief_session.assert_not_called()

    @pytest.mark.asyncio
    async def test_start_debrief_cancels_stale(self):
        """Old-date session should be cancelled before creating new."""
        yesterday = (date.today() - timedelta(days=1)).isoformat()

        with patch("processors.debrief.supabase_client") as mock_db:
            mock_db.get_active_debrief_session.return_value = {
                "id": "stale-session",
                "date": yesterday,
                "status": "in_progress",
            }
            mock_db.create_debrief_session.return_value = {
                "id": "new-session",
                "date": date.today().isoformat(),
            }

            with patch("services.google_calendar.calendar_service") as mock_cal:
                mock_cal.get_todays_events = AsyncMock(return_value=[])

                from processors.debrief import start_debrief

                result = await start_debrief(user_id="eyal")

                assert result["action"] == "debrief_started"
                # Stale session should be cancelled
                mock_db.update_debrief_session.assert_any_call(
                    "stale-session", status="cancelled"
                )
                mock_db.create_debrief_session.assert_called_once()

    @pytest.mark.asyncio
    async def test_active_session_survives_restart(self):
        """Supabase should find session even after bot restart."""
        with patch("processors.debrief.supabase_client") as mock_db:
            mock_db.get_active_debrief_session.return_value = {
                "id": "surviving-session",
                "date": date.today().isoformat(),
                "status": "in_progress",
                "items_captured": [],
                "calendar_events_remaining": [],
            }

            from processors.debrief import start_debrief

            result = await start_debrief(user_id="eyal")

            # Should resume, not create
            assert result["action"] == "debrief_resumed"
            assert result["session_id"] == "surviving-session"


# =============================================================================
# Full Debrief — Message Processing
# =============================================================================

class TestDebriefMessageProcessing:
    """Tests for process_debrief_message()."""

    def _mock_session(self, **overrides):
        session = {
            "id": "session-1",
            "date": date.today().isoformat(),
            "status": "in_progress",
            "raw_messages": [],
            "items_captured": [],
            "calendar_events_remaining": [],
            "calendar_events_covered": [],
            "created_at": datetime.now().isoformat(),
        }
        session.update(overrides)
        return session

    @pytest.mark.asyncio
    async def test_process_message_extracts_items(self):
        """Paragraph input should extract items."""
        mock_response = json.dumps({
            "extracted_items": [
                {"type": "task", "title": "Draft LOI for Jason", "assignee": "Eyal", "priority": "H"},
                {"type": "commitment", "speaker": "Jason", "commitment_text": "Will send term sheet"},
            ],
            "follow_up_question": None,
            "response_text": "Captured 2 items from the Jason call.",
        })

        with patch("processors.debrief.supabase_client") as mock_db:
            mock_db.get_debrief_session.return_value = self._mock_session()
            mock_db.get_tasks.return_value = []

            with patch("processors.debrief.call_llm", return_value=(mock_response, {})):
                from processors.debrief import process_debrief_message

                result = await process_debrief_message(
                    session_id="session-1",
                    user_message="Called Jason. He's in. Sending LOI by Thursday. He'll send term sheet by next week.",
                    user_id="eyal",
                )

                assert result["action"] == "debrief_message"
                assert result["items_count"] == 2
                assert result["show_finish_button"] is True

    @pytest.mark.asyncio
    async def test_process_message_accumulates_items(self):
        """Items should accumulate across multiple messages."""
        existing_items = [
            {"type": "task", "title": "Existing task", "assignee": "Eyal"},
        ]

        mock_response = json.dumps({
            "extracted_items": [
                {"type": "decision", "description": "Go with AWS"},
            ],
            "follow_up_question": None,
            "response_text": "Noted the AWS decision.",
        })

        with patch("processors.debrief.supabase_client") as mock_db:
            mock_db.get_debrief_session.return_value = self._mock_session(
                items_captured=existing_items,
            )
            mock_db.get_tasks.return_value = []

            with patch("processors.debrief.call_llm", return_value=(mock_response, {})):
                from processors.debrief import process_debrief_message

                result = await process_debrief_message(
                    session_id="session-1",
                    user_message="Decision: we're going with AWS.",
                    user_id="eyal",
                )

                # 1 existing + 1 new = 2
                assert result["items_count"] == 2

    @pytest.mark.asyncio
    async def test_process_message_asks_followup(self):
        """Follow-up question should be included in response."""
        mock_response = json.dumps({
            "extracted_items": [
                {"type": "information", "description": "Call with investor"},
            ],
            "follow_up_question": "Was this the first call with that investor?",
            "response_text": "Got it.",
        })

        with patch("processors.debrief.supabase_client") as mock_db:
            mock_db.get_debrief_session.return_value = self._mock_session()
            mock_db.get_tasks.return_value = []

            with patch("processors.debrief.call_llm", return_value=(mock_response, {})):
                from processors.debrief import process_debrief_message

                result = await process_debrief_message(
                    session_id="session-1",
                    user_message="Had a call with an investor today.",
                    user_id="eyal",
                )

                assert "Was this the first call" in result["response"]


# =============================================================================
# Done Detection
# =============================================================================

class TestDoneDetection:
    """Tests for is_done_signal()."""

    def test_short_done_signal_detected(self):
        """Short done-like messages should be detected."""
        from processors.debrief import is_done_signal

        assert is_done_signal("done") is True
        assert is_done_signal("Done") is True
        assert is_done_signal("that's it") is True
        assert is_done_signal("That's all") is True
        assert is_done_signal("finished") is True
        assert is_done_signal("nothing else") is True
        assert is_done_signal("I'm done") is True
        assert is_done_signal("done.") is True

    def test_long_message_not_done_signal(self):
        """Longer messages with business content should NOT be done signals."""
        from processors.debrief import is_done_signal

        assert is_done_signal("Done with the Moldova call") is False
        assert is_done_signal("Finished the investor deck draft") is False
        assert is_done_signal("That's all from the board meeting today") is False

    def test_non_done_messages(self):
        """Regular messages should not be done signals."""
        from processors.debrief import is_done_signal

        assert is_done_signal("Called Jason about the deal") is False
        assert is_done_signal("We need to update the Gantt") is False
        assert is_done_signal("What tasks do I have?") is False


# =============================================================================
# Confirmation & Editing
# =============================================================================

class TestConfirmationAndEditing:
    """Tests for finalize_debrief, edit_debrief_items, confirm_debrief."""

    @pytest.mark.asyncio
    async def test_finalize_shows_summary(self):
        """Finalize should return extraction summary."""
        items = [
            {"type": "task", "title": "Draft LOI", "assignee": "Eyal"},
            {"type": "decision", "description": "Go with AWS"},
        ]

        with patch("processors.debrief.supabase_client") as mock_db:
            mock_db.get_debrief_session.return_value = {
                "id": "session-1",
                "items_captured": items,
                "raw_messages": [],
                "status": "in_progress",
            }

            from processors.debrief import finalize_debrief

            result = await finalize_debrief("session-1")

            assert result["action"] == "debrief_confirm"
            assert "Tasks" in result["response"]
            assert "Decisions" in result["response"]
            assert len(result["items"]) == 2

    @pytest.mark.asyncio
    async def test_finalize_empty_cancels(self):
        """Finalize with no items should cancel session."""
        with patch("processors.debrief.supabase_client") as mock_db:
            mock_db.get_debrief_session.return_value = {
                "id": "session-1",
                "items_captured": [],
                "status": "in_progress",
            }

            from processors.debrief import finalize_debrief

            result = await finalize_debrief("session-1")

            assert result["action"] == "debrief_cancelled"
            mock_db.update_debrief_session.assert_called_with(
                "session-1", status="cancelled"
            )

    @pytest.mark.asyncio
    async def test_edit_updates_items(self):
        """Edit instruction should modify items."""
        original_items = [
            {"type": "task", "title": "Draft LOI", "assignee": "Eyal"},
        ]
        updated_items = [
            {"type": "task", "title": "Draft LOI", "assignee": "Roye"},
        ]

        mock_response = json.dumps({
            "updated_items": updated_items,
            "response_text": "Changed assignee to Roye.",
        })

        with patch("processors.debrief.supabase_client") as mock_db:
            mock_db.get_debrief_session.return_value = {
                "id": "session-1",
                "items_captured": original_items,
            }

            with patch("processors.debrief.call_llm", return_value=(mock_response, {})):
                from processors.debrief import edit_debrief_items

                result = await edit_debrief_items(
                    session_id="session-1",
                    edit_instruction="Change task 1 assignee to Roye",
                    user_id="eyal",
                )

                assert result["action"] == "debrief_confirm"
                assert result["items"][0]["assignee"] == "Roye"

    @pytest.mark.asyncio
    async def test_confirm_approve_injects(self):
        """Approving should inject tasks and decisions."""
        items = [
            {"type": "task", "title": "Call Orit", "assignee": "Eyal", "priority": "M"},
            {"type": "decision", "description": "Go with AWS", "participants_involved": ["Eyal", "Roye"]},
        ]

        with patch("processors.debrief.supabase_client") as mock_db:
            mock_db.get_debrief_session.return_value = {
                "id": "session-1",
                "items_captured": items,
                "date": date.today().isoformat(),
                "status": "confirming",
            }
            mock_db.create_meeting.return_value = {"id": "pseudo-meeting-1"}
            mock_db.create_task.return_value = {"id": "t1"}
            mock_db.create_decision.return_value = {"id": "d1"}

            with patch("services.embeddings.embedding_service") as mock_embed:
                mock_embed.chunk_and_embed_document = AsyncMock(return_value=[])

                with patch("processors.cross_reference.deduplicate_tasks") as mock_dedup:
                    mock_dedup.return_value = {
                        "new_tasks": [{"title": "Call Orit", "assignee": "Eyal", "priority": "M"}],
                        "duplicates": [],
                        "updates": [],
                    }

                    from processors.debrief import confirm_debrief

                    result = await confirm_debrief("session-1", approved=True)

                    assert result["action"] == "debrief_approved"
                    mock_db.create_task.assert_called_once()
                    mock_db.create_decision.assert_called_once()

    @pytest.mark.asyncio
    async def test_confirm_reject_cancels(self):
        """Rejecting should cancel with no DB writes."""
        with patch("processors.debrief.supabase_client") as mock_db:
            mock_db.get_debrief_session.return_value = {
                "id": "session-1",
                "items_captured": [{"type": "task", "title": "X"}],
                "status": "confirming",
            }

            from processors.debrief import confirm_debrief

            result = await confirm_debrief("session-1", approved=False)

            assert result["action"] == "debrief_cancelled"
            mock_db.update_debrief_session.assert_called_with(
                "session-1", status="cancelled"
            )
            mock_db.create_meeting.assert_not_called()


# =============================================================================
# Opus Validation
# =============================================================================

class TestOpusValidation:
    """Tests for conditional Opus validation."""

    @pytest.mark.asyncio
    async def test_opus_validation_skipped_for_small_debriefs(self):
        """Debriefs with ≤5 items should NOT trigger Opus."""
        items = [
            {"type": "task", "title": f"Task {i}", "assignee": "Eyal"}
            for i in range(3)
        ]

        with patch("processors.debrief.supabase_client") as mock_db:
            mock_db.get_debrief_session.return_value = {
                "id": "session-1",
                "items_captured": items,
                "raw_messages": [],
                "status": "in_progress",
            }

            with patch("processors.debrief.settings") as mock_settings:
                mock_settings.DEBRIEF_OPUS_THRESHOLD = 5

                with patch("core.analyst_agent.analyst_agent") as mock_analyst:
                    from processors.debrief import finalize_debrief

                    result = await finalize_debrief("session-1")

                    assert result["action"] == "debrief_confirm"
                    # Opus should NOT be called
                    mock_analyst.extract_from_debrief.assert_not_called()

    @pytest.mark.asyncio
    async def test_opus_validation_runs_for_large_debriefs(self):
        """Debriefs with >5 items should trigger Opus validation."""
        items = [
            {"type": "task", "title": f"Task {i}", "assignee": "Eyal"}
            for i in range(8)
        ]
        validated_items = items[:7]  # Opus removed one duplicate

        with patch("processors.debrief.supabase_client") as mock_db:
            mock_db.get_debrief_session.return_value = {
                "id": "session-1",
                "items_captured": items,
                "raw_messages": [
                    {"role": "user", "text": "lots of stuff happened"},
                ],
                "status": "in_progress",
            }

            with patch("processors.debrief.settings") as mock_settings:
                mock_settings.DEBRIEF_OPUS_THRESHOLD = 5

                with patch("core.analyst_agent.analyst_agent") as mock_analyst:
                    mock_analyst.extract_from_debrief = AsyncMock(
                        return_value=validated_items
                    )

                    from processors.debrief import finalize_debrief

                    result = await finalize_debrief("session-1")

                    assert result["action"] == "debrief_confirm"
                    mock_analyst.extract_from_debrief.assert_called_once()
                    assert len(result["items"]) == 7


# =============================================================================
# Injection Pipeline
# =============================================================================

class TestInjectionPipeline:
    """Tests for _inject_debrief_items()."""

    @pytest.mark.asyncio
    async def test_inject_pseudo_meeting_created(self):
        """Injection should create a pseudo-meeting for FK constraints."""
        items = [{"type": "information", "description": "Test info"}]

        with patch("processors.debrief.supabase_client") as mock_db:
            mock_db.create_meeting.return_value = {"id": "pseudo-meeting-1"}

            with patch("services.embeddings.embedding_service") as mock_embed:
                mock_embed.chunk_and_embed_document = AsyncMock(return_value=[])

                from processors.debrief import _inject_debrief_items

                result = await _inject_debrief_items(
                    session_id="session-1",
                    items=items,
                    source_date=date.today().isoformat(),
                )

                # Pseudo-meeting should be created with source_file_path="debrief"
                call_kwargs = mock_db.create_meeting.call_args
                assert call_kwargs.kwargs.get("source_file_path") == "debrief"
                assert "Debrief:" in call_kwargs.kwargs.get("title", "")

    @pytest.mark.asyncio
    async def test_inject_tasks_direct_no_dedup(self):
        """
        Debrief task injection must bypass cross-meeting dedup.

        Dedup was previously used here but the Haiku classifier silently
        dropped genuinely-new tasks when it false-positive-matched them to
        existing work (data-loss incident 2026-04-10). Debrief is CEO-authored
        free text — trust the input and create directly.
        """
        items = [
            {"type": "task", "title": "Follow up with Orit", "assignee": "Eyal", "priority": "H"},
        ]

        with patch("processors.debrief.supabase_client") as mock_db:
            mock_db.create_meeting.return_value = {"id": "pseudo-meeting-1"}
            mock_db.create_task.return_value = {"id": "task-1"}

            with patch("services.embeddings.embedding_service") as mock_embed:
                mock_embed.chunk_and_embed_document = AsyncMock(return_value=[])

                with patch("processors.cross_reference.deduplicate_tasks") as mock_dedup:
                    from processors.debrief import _inject_debrief_items

                    result = await _inject_debrief_items(
                        session_id=None,
                        items=items,
                        source_date=date.today().isoformat(),
                    )

                    # Dedup must NOT be called — we bypass it for debrief.
                    mock_dedup.assert_not_called()
                    # Task must be created directly.
                    mock_db.create_task.assert_called_once()
                    assert result["counts"]["tasks"] == 1

    @pytest.mark.asyncio
    async def test_inject_promotes_approval_status(self):
        """
        Pseudo-meeting and child tasks must be promoted to approval_status='approved'.

        Debrief bypasses the normal meeting approval flow because Eyal already
        confirmed via the Inject button. Without this promote, T3.1's default
        'pending' gate would hide debrief tasks from the central read helpers.
        """
        items = [
            {"type": "task", "title": "Call U Bank", "assignee": "Eyal", "priority": "M"},
        ]

        with patch("processors.debrief.supabase_client") as mock_db:
            mock_db.create_meeting.return_value = {"id": "pseudo-meeting-42"}
            mock_db.create_task.return_value = {"id": "task-42"}

            with patch("services.embeddings.embedding_service") as mock_embed:
                mock_embed.chunk_and_embed_document = AsyncMock(return_value=[])

                from processors.debrief import _inject_debrief_items

                await _inject_debrief_items(
                    session_id=None,
                    items=items,
                    source_date=date.today().isoformat(),
                )

                # Collect every .table(...) call on the client chain
                table_calls = [
                    c.args[0]
                    for c in mock_db.client.table.call_args_list
                ]
                # Promote must hit meetings + at least tasks
                assert "meetings" in table_calls
                assert "tasks" in table_calls

    @pytest.mark.asyncio
    async def test_inject_gantt_updates_proposes(self):
        """Gantt updates should create proposals."""
        items = [
            {"type": "gantt_update", "section": "Product & Tech", "description": "Delay MVP to W14"},
        ]

        with patch("processors.debrief.supabase_client") as mock_db:
            mock_db.create_meeting.return_value = {"id": "pseudo-meeting-1"}

            with patch("services.embeddings.embedding_service") as mock_embed:
                mock_embed.chunk_and_embed_document = AsyncMock(return_value=[])

                with patch("services.gantt_manager.gantt_manager") as mock_gantt:
                    mock_gantt.propose_gantt_update = AsyncMock(
                        return_value={"proposal_id": "prop-1"}
                    )

                    from processors.debrief import _inject_debrief_items

                    result = await _inject_debrief_items(
                        session_id=None,
                        items=items,
                        source_date=date.today().isoformat(),
                    )

                    mock_gantt.propose_gantt_update.assert_called_once()
                    assert result["counts"]["gantt_proposals"] == 1

    @pytest.mark.asyncio
    async def test_inject_embeddings_created(self):
        """Injection should create embeddings with source_type='debrief'."""
        items = [
            {"type": "information", "description": "Important fact about Moldova"},
        ]

        with patch("processors.debrief.supabase_client") as mock_db:
            mock_db.create_meeting.return_value = {"id": "pseudo-meeting-1"}

            with patch("services.embeddings.embedding_service") as mock_embed:
                mock_embed.chunk_and_embed_document = AsyncMock(return_value=[
                    {
                        "text": "Important fact about Moldova",
                        "embedding": [0.1] * 1536,
                        "chunk_index": 0,
                    }
                ])

                from processors.debrief import _inject_debrief_items

                await _inject_debrief_items(
                    session_id=None,
                    items=items,
                    source_date=date.today().isoformat(),
                )

                # Check embeddings were stored
                mock_db.store_embeddings_batch.assert_called_once()
                stored = mock_db.store_embeddings_batch.call_args[0][0]
                assert stored[0]["source_type"] == "debrief"
                assert stored[0]["metadata"]["source_type"] == "debrief"


# =============================================================================
# Calendar & Edge Cases
# =============================================================================

class TestEdgeCases:
    """Tests for TTL, max items, and calendar detection."""

    @pytest.mark.asyncio
    async def test_session_ttl_expiry(self):
        """Expired session should be auto-closed."""
        old_time = (datetime.utcnow() - timedelta(minutes=120)).isoformat()

        with patch("processors.debrief.supabase_client") as mock_db:
            mock_db.get_debrief_session.return_value = {
                "id": "session-1",
                "created_at": old_time,
                "status": "in_progress",
                "raw_messages": [],
                "items_captured": [],
                "calendar_events_remaining": [],
            }

            with patch("processors.debrief.settings") as mock_settings:
                mock_settings.DEBRIEF_TTL_MINUTES = 60
                mock_settings.DEBRIEF_MAX_ITEMS = 30

                from processors.debrief import process_debrief_message

                result = await process_debrief_message(
                    session_id="session-1",
                    user_message="Test message",
                    user_id="eyal",
                )

                assert result["action"] == "session_expired"
                mock_db.update_debrief_session.assert_called_with(
                    "session-1", status="cancelled"
                )

    @pytest.mark.asyncio
    async def test_max_items_safety_cap(self):
        """Should reject messages beyond DEBRIEF_MAX_ITEMS."""
        items = [{"type": "task", "title": f"Task {i}"} for i in range(30)]

        with patch("processors.debrief.supabase_client") as mock_db:
            mock_db.get_debrief_session.return_value = {
                "id": "session-1",
                "created_at": datetime.utcnow().isoformat(),
                "status": "in_progress",
                "raw_messages": [],
                "items_captured": items,
                "calendar_events_remaining": [],
            }

            with patch("processors.debrief.settings") as mock_settings:
                mock_settings.DEBRIEF_MAX_ITEMS = 30
                mock_settings.DEBRIEF_TTL_MINUTES = 60

                from processors.debrief import process_debrief_message

                result = await process_debrief_message(
                    session_id="session-1",
                    user_message="One more thing...",
                    user_id="eyal",
                )

                assert result["action"] == "debrief_max_items"

    def test_calendar_gap_detection(self):
        """Un-transcribed meetings should be detected."""
        from processors.debrief import _check_transcript_exists

        with patch("processors.debrief.supabase_client") as mock_db:
            mock_db.client.table.return_value.select.return_value.gte.return_value.lt.return_value.neq.return_value.execute.return_value = MagicMock(
                data=[
                    {"id": "m1", "title": "CropSight: Weekly Sync"},
                ]
            )

            assert _check_transcript_exists("Weekly Sync", date.today()) is True
            assert _check_transcript_exists("Board Meeting", date.today()) is False


# =============================================================================
# RAG Source Weight
# =============================================================================

class TestSourceWeight:
    """Test debrief content gets 1.5x priority in search."""

    def test_source_weight_boost(self):
        """Debrief items should get 1.5x boost in time weighting."""
        from services.supabase_client import SupabaseClient

        results = [
            {
                "rrf_score": 0.5,
                "metadata": {
                    "meeting_date": datetime.now().isoformat(),
                    "source_type": "debrief",
                },
            },
            {
                "rrf_score": 0.5,
                "metadata": {
                    "meeting_date": datetime.now().isoformat(),
                    "source_type": "meeting",
                },
            },
        ]

        weighted = SupabaseClient._apply_time_weighting(results, half_life_days=30)

        # Debrief item should have higher score
        debrief_item = next(
            r for r in weighted if r["metadata"]["source_type"] == "debrief"
        )
        meeting_item = next(
            r for r in weighted if r["metadata"]["source_type"] == "meeting"
        )

        assert debrief_item["rrf_score"] > meeting_item["rrf_score"]


# =============================================================================
# Debrief Prompts
# =============================================================================

class TestDebriefPrompts:
    """Tests for prompt generation functions."""

    def test_get_debrief_system_prompt(self):
        """Debrief system prompt should contain key instructions."""
        from core.debrief_prompt import get_debrief_system_prompt
        prompt = get_debrief_system_prompt()
        assert "extracted_items" in prompt
        assert "follow_up_question" in prompt
        assert "JSON" in prompt

    def test_get_quick_injection_prompt(self):
        """Quick injection prompt should be simpler (no follow-ups)."""
        from core.debrief_prompt import get_quick_injection_prompt
        prompt = get_quick_injection_prompt()
        assert "extracted_items" in prompt
        assert "follow_up_question" not in prompt

    def test_get_debrief_extraction_prompt(self):
        """Opus extraction prompt should include raw messages and items."""
        from core.debrief_prompt import get_debrief_extraction_prompt
        prompt = get_debrief_extraction_prompt(
            raw_messages=["Message 1", "Message 2"],
            items_captured=[{"type": "task", "title": "Test"}],
        )
        assert "Message 1" in prompt
        assert "Message 2" in prompt
        assert "validated_items" in prompt


# =============================================================================
# JSON Parsing
# =============================================================================

class TestJsonParsing:
    """Tests for _parse_llm_json helper."""

    def test_parse_clean_json(self):
        """Clean JSON should parse correctly."""
        from processors.debrief import _parse_llm_json
        result = _parse_llm_json('{"key": "value"}')
        assert result == {"key": "value"}

    def test_parse_json_with_code_fences(self):
        """JSON wrapped in code fences should parse."""
        from processors.debrief import _parse_llm_json
        result = _parse_llm_json('```json\n{"key": "value"}\n```')
        assert result == {"key": "value"}

    def test_parse_json_with_surrounding_text(self):
        """JSON embedded in text should be extracted."""
        from processors.debrief import _parse_llm_json
        result = _parse_llm_json('Here is the result: {"key": "value"} end')
        assert result == {"key": "value"}

    def test_parse_invalid_json(self):
        """Invalid JSON should return empty dict."""
        from processors.debrief import _parse_llm_json
        result = _parse_llm_json("not json at all")
        assert result == {}


# =============================================================================
# Format Summary
# =============================================================================

class TestFormatSummary:
    """Tests for _format_extraction_summary."""

    def test_format_groups_by_type(self):
        """Summary should group items by type."""
        from processors.debrief import _format_extraction_summary

        items = [
            {"type": "task", "title": "Task 1", "assignee": "Eyal"},
            {"type": "task", "title": "Task 2", "assignee": "Roye"},
            {"type": "decision", "description": "Decision 1"},
        ]

        summary = _format_extraction_summary(items)
        assert "Tasks (2)" in summary
        assert "Decisions (1)" in summary
        assert "Task 1" in summary

    def test_format_empty_items(self):
        """Empty items should return 'No items' message."""
        from processors.debrief import _format_extraction_summary
        assert "No items" in _format_extraction_summary([])

    def test_format_sensitive_flag(self):
        """Sensitive items should be flagged."""
        from processors.debrief import _format_extraction_summary

        items = [
            {"type": "information", "description": "Investor call details", "sensitive": True},
        ]

        summary = _format_extraction_summary(items)
        assert "[SENSITIVE]" in summary
