"""
Tests for v0.3 Cross-Reference Intelligence.

Tests cover:
- Task deduplication (deduplicate_tasks)
- Task status inference (infer_task_status_changes)
- Open question resolution (resolve_open_questions)
- Cross-reference orchestrator (run_cross_reference)
- Supabase CRUD for task_mentions
- Approval flow cross-reference integration
- Time-weighted RAG scoring
- Parent chunk retrieval
- Query router classification
"""

import pytest
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch, PropertyMock


# =============================================================================
# Task Deduplication
# =============================================================================

class TestDeduplicateTasks:
    """Tests for deduplicate_tasks() in cross_reference.py."""

    @pytest.mark.asyncio
    async def test_no_existing_tasks_all_new(self):
        """When there are no existing tasks, all should be classified as NEW."""
        with patch("processors.cross_reference.supabase_client") as mock_db:
            mock_db.get_tasks = MagicMock(return_value=[])

            from processors.cross_reference import deduplicate_tasks

            new_tasks = [
                {"title": "Set up AWS infra", "assignee": "Roye", "category": "Product & Tech"},
                {"title": "Draft investor deck", "assignee": "Eyal", "category": "Finance & Fundraising"},
            ]

            result = await deduplicate_tasks(new_tasks, "meeting-1", "transcript text")

            assert len(result["new_tasks"]) == 2
            assert len(result["duplicates"]) == 0
            assert len(result["updates"]) == 0

    @pytest.mark.asyncio
    async def test_empty_new_tasks(self):
        """When no new tasks are provided, result should be empty."""
        from processors.cross_reference import deduplicate_tasks

        result = await deduplicate_tasks([], "meeting-1", "transcript text")

        assert len(result["new_tasks"]) == 0
        assert len(result["duplicates"]) == 0
        assert len(result["updates"]) == 0

    @pytest.mark.asyncio
    async def test_duplicate_classification(self):
        """Tasks classified as DUPLICATE by Claude should go to duplicates list."""
        with (
            patch("processors.cross_reference.supabase_client") as mock_db,
            patch("processors.cross_reference.call_llm") as mock_llm,
        ):
            mock_db.get_tasks = MagicMock(return_value=[
                {"id": "task-1", "title": "Set up dev environment", "assignee": "Roye",
                 "category": "Product & Tech", "status": "pending"},
            ])
            mock_llm.return_value = (
                '{"classifications": [{"new_task_index": "A", "type": "DUPLICATE", "existing_task_id": "task-1", "new_status": null, "evidence": null, "reason": "Same task"}]}',
                {"input_tokens": 100, "output_tokens": 50, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0},
            )

            from processors.cross_reference import deduplicate_tasks

            new_tasks = [
                {"title": "Configure development env", "assignee": "Roye", "category": "Product & Tech"},
            ]

            result = await deduplicate_tasks(new_tasks, "meeting-2", "transcript")

            assert len(result["duplicates"]) == 1
            assert result["duplicates"][0]["existing_task_id"] == "task-1"
            assert len(result["new_tasks"]) == 0

    @pytest.mark.asyncio
    async def test_update_classification(self):
        """Tasks classified as UPDATE should go to updates list with new_status."""
        with (
            patch("processors.cross_reference.supabase_client") as mock_db,
            patch("processors.cross_reference.call_llm") as mock_llm,
        ):
            mock_db.get_tasks = MagicMock(return_value=[
                {"id": "task-1", "title": "Set up dev environment", "assignee": "Roye",
                 "category": "Product & Tech", "status": "in_progress"},
            ])
            mock_llm.return_value = (
                '{"classifications": [{"new_task_index": "A", "type": "UPDATE", "existing_task_id": "task-1", "new_status": "done", "evidence": "We finished the setup", "reason": "Task completed"}]}',
                {"input_tokens": 100, "output_tokens": 50, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0},
            )

            from processors.cross_reference import deduplicate_tasks

            new_tasks = [
                {"title": "Complete dev setup", "assignee": "Roye", "category": "Product & Tech"},
            ]

            result = await deduplicate_tasks(new_tasks, "meeting-2", "transcript")

            assert len(result["updates"]) == 1
            assert result["updates"][0]["existing_task_id"] == "task-1"
            assert result["updates"][0]["new_status"] == "done"
            assert len(result["new_tasks"]) == 0

    @pytest.mark.asyncio
    async def test_llm_error_falls_back_to_all_new(self):
        """On LLM error, all tasks should be treated as new (safe default)."""
        with (
            patch("processors.cross_reference.supabase_client") as mock_db,
            patch("processors.cross_reference.call_llm") as mock_llm,
        ):
            mock_db.get_tasks = MagicMock(return_value=[
                {"id": "task-1", "title": "Existing task", "assignee": "Eyal",
                 "category": "Strategy & Research", "status": "pending"},
            ])
            mock_llm.side_effect = Exception("API Error")

            from processors.cross_reference import deduplicate_tasks

            new_tasks = [{"title": "New task", "assignee": "Eyal", "category": "Strategy & Research"}]
            result = await deduplicate_tasks(new_tasks, "meeting-1", "transcript")

            # Should fall back to treating all as new
            assert len(result["new_tasks"]) == 1
            assert len(result["duplicates"]) == 0


# =============================================================================
# Task Status Inference
# =============================================================================

class TestInferTaskStatusChanges:
    """Tests for infer_task_status_changes() in cross_reference.py."""

    @pytest.mark.asyncio
    async def test_no_open_tasks_returns_empty(self):
        """When there are no open tasks, should return empty list."""
        with patch("processors.cross_reference.supabase_client") as mock_db:
            mock_db.get_tasks = MagicMock(return_value=[])

            from processors.cross_reference import infer_task_status_changes

            result = await infer_task_status_changes("meeting-1", "transcript text")

            assert result == []

    @pytest.mark.asyncio
    async def test_explicit_completion_high_confidence(self):
        """Explicit completion statement should be flagged with high confidence."""
        with (
            patch("processors.cross_reference.supabase_client") as mock_db,
            patch("processors.cross_reference.call_llm") as mock_llm,
        ):
            mock_db.get_tasks = MagicMock(return_value=[
                {"id": "task-1", "title": "Set up dev environment", "assignee": "Roye",
                 "status": "pending", "category": "Product & Tech", "created_at": "2026-02-10"},
            ])
            mock_llm.return_value = (
                '{"status_changes": [{"task_id": "task-1", "new_status": "done", "evidence": "I finished the dev setup", "confidence": "high", "reasoning": "Explicit completion"}]}',
                {"input_tokens": 100, "output_tokens": 50, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0},
            )

            from processors.cross_reference import infer_task_status_changes

            result = await infer_task_status_changes("meeting-1", "transcript")

            assert len(result) == 1
            assert result[0]["task_id"] == "task-1"
            assert result[0]["new_status"] == "done"
            assert result[0]["confidence"] == "high"
            assert result[0]["task_title"] == "Set up dev environment"

    @pytest.mark.asyncio
    async def test_progress_mention_medium_confidence(self):
        """Progress mention should be flagged as in_progress with medium confidence."""
        with (
            patch("processors.cross_reference.supabase_client") as mock_db,
            patch("processors.cross_reference.call_llm") as mock_llm,
        ):
            mock_db.get_tasks = MagicMock(return_value=[
                {"id": "task-2", "title": "Draft Moldova timeline", "assignee": "Paolo",
                 "status": "pending", "category": "BD & Sales", "created_at": "2026-02-15"},
            ])
            mock_llm.return_value = (
                '{"status_changes": [{"task_id": "task-2", "new_status": "in_progress", "evidence": "I am working on the timeline", "confidence": "medium", "reasoning": "Active work mentioned"}]}',
                {"input_tokens": 100, "output_tokens": 50, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0},
            )

            from processors.cross_reference import infer_task_status_changes

            result = await infer_task_status_changes("meeting-1", "transcript")

            assert len(result) == 1
            assert result[0]["new_status"] == "in_progress"
            assert result[0]["confidence"] == "medium"

    @pytest.mark.asyncio
    async def test_unrelated_transcript_no_changes(self):
        """When transcript doesn't mention any tasks, should return empty."""
        with (
            patch("processors.cross_reference.supabase_client") as mock_db,
            patch("processors.cross_reference.call_llm") as mock_llm,
        ):
            mock_db.get_tasks = MagicMock(return_value=[
                {"id": "task-1", "title": "Some task", "assignee": "Eyal",
                 "status": "pending", "category": "Strategy & Research", "created_at": "2026-02-10"},
            ])
            mock_llm.return_value = (
                '{"status_changes": []}',
                {"input_tokens": 100, "output_tokens": 10, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0},
            )

            from processors.cross_reference import infer_task_status_changes

            result = await infer_task_status_changes("meeting-1", "unrelated topic transcript")

            assert result == []


# =============================================================================
# Open Question Resolution
# =============================================================================

class TestResolveOpenQuestions:
    """Tests for resolve_open_questions() in cross_reference.py."""

    @pytest.mark.asyncio
    async def test_no_open_questions_returns_empty(self):
        """When there are no open questions, should return empty list."""
        with patch("processors.cross_reference.supabase_client") as mock_db:
            mock_db.get_open_questions = MagicMock(return_value=[])

            from processors.cross_reference import resolve_open_questions

            result = await resolve_open_questions("meeting-1", "transcript text")

            assert result == []

    @pytest.mark.asyncio
    async def test_question_answered_in_transcript(self):
        """When a question is clearly answered, should be in resolved list."""
        with (
            patch("processors.cross_reference.supabase_client") as mock_db,
            patch("processors.cross_reference.call_llm") as mock_llm,
        ):
            mock_db.get_open_questions = MagicMock(return_value=[
                {"id": "q-1", "question": "What is our pricing model?",
                 "raised_by": "Eyal", "status": "open"},
            ])
            mock_llm.return_value = (
                '{"resolved_questions": [{"question_id": "q-1", "answer": "Freemium with enterprise tier", "evidence": "We decided on freemium model", "confidence": "high"}]}',
                {"input_tokens": 100, "output_tokens": 50, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0},
            )

            from processors.cross_reference import resolve_open_questions

            result = await resolve_open_questions("meeting-1", "transcript")

            assert len(result) == 1
            assert result[0]["question_id"] == "q-1"
            assert result[0]["answer"] == "Freemium with enterprise tier"
            assert result[0]["question"] == "What is our pricing model?"


# =============================================================================
# Cross-Reference Orchestrator
# =============================================================================

class TestRunCrossReference:
    """Tests for run_cross_reference() orchestrator."""

    @pytest.mark.asyncio
    async def test_orchestrates_all_three_analyses(self):
        """run_cross_reference should call all three analysis functions."""
        with (
            patch("processors.cross_reference.deduplicate_tasks", new_callable=AsyncMock) as mock_dedup,
            patch("processors.cross_reference.infer_task_status_changes", new_callable=AsyncMock) as mock_status,
            patch("processors.cross_reference.resolve_open_questions", new_callable=AsyncMock) as mock_resolve,
            patch("processors.cross_reference.supabase_client") as mock_db,
        ):
            mock_dedup.return_value = {"new_tasks": [], "duplicates": [], "updates": []}
            mock_status.return_value = []
            mock_resolve.return_value = []
            mock_db.create_task_mentions_batch = MagicMock(return_value=[])

            from processors.cross_reference import run_cross_reference

            result = await run_cross_reference(
                meeting_id="meeting-1",
                transcript="transcript text",
                new_tasks=[{"title": "Task A"}],
            )

            mock_dedup.assert_called_once()
            mock_status.assert_called_once()
            mock_resolve.assert_called_once()

            assert "dedup" in result
            assert "status_changes" in result
            assert "resolved_questions" in result

    @pytest.mark.asyncio
    async def test_creates_task_mentions_for_duplicates(self):
        """Should create task_mention records for duplicates and status changes."""
        with (
            patch("processors.cross_reference.deduplicate_tasks", new_callable=AsyncMock) as mock_dedup,
            patch("processors.cross_reference.infer_task_status_changes", new_callable=AsyncMock) as mock_status,
            patch("processors.cross_reference.resolve_open_questions", new_callable=AsyncMock) as mock_resolve,
            patch("processors.cross_reference.supabase_client") as mock_db,
        ):
            mock_dedup.return_value = {
                "new_tasks": [],
                "duplicates": [{"task": {"title": "Dup task"}, "existing_task_id": "task-1", "reason": "same"}],
                "updates": [],
            }
            mock_status.return_value = [
                {"task_id": "task-2", "task_title": "Status task", "new_status": "done",
                 "evidence": "done", "confidence": "high"},
            ]
            mock_resolve.return_value = []
            mock_db.create_task_mentions_batch = MagicMock(return_value=[{}, {}])

            from processors.cross_reference import run_cross_reference

            await run_cross_reference("meeting-1", "transcript", [])

            # Should have created 2 mentions (1 duplicate + 1 status change)
            mock_db.create_task_mentions_batch.assert_called_once()
            mentions = mock_db.create_task_mentions_batch.call_args[0][0]
            assert len(mentions) == 2


# =============================================================================
# Supabase Task Mentions CRUD
# =============================================================================

class TestTaskMentionsCRUD:
    """Tests for task_mentions Supabase client methods."""

    def test_create_task_mention(self):
        """create_task_mention should insert a record into task_mentions."""
        with patch("services.supabase_client.SupabaseClient.client", new_callable=PropertyMock) as mock_client_prop:
            mock_client = MagicMock()
            mock_client.table.return_value.insert.return_value.execute.return_value = MagicMock(
                data=[{"id": "mention-1", "task_id": "task-1", "meeting_id": "meeting-1"}]
            )
            mock_client_prop.return_value = mock_client

            from services.supabase_client import SupabaseClient
            client = SupabaseClient()
            object.__setattr__(client, '_client', mock_client)

            result = client.create_task_mention(
                task_id="task-1",
                meeting_id="meeting-1",
                mention_text="Referenced the dev setup",
                implied_status="done",
                confidence="high",
                evidence="It's all set up now",
            )

            mock_client.table.assert_called_with("task_mentions")
            assert result["id"] == "mention-1"

    def test_create_task_mentions_batch_empty(self):
        """create_task_mentions_batch with empty list should return empty."""
        from services.supabase_client import SupabaseClient
        client = SupabaseClient()
        result = client.create_task_mentions_batch([])
        assert result == []

    def test_create_task_mentions_batch(self):
        """create_task_mentions_batch should batch insert mentions."""
        with patch("services.supabase_client.SupabaseClient.client", new_callable=PropertyMock) as mock_client_prop:
            mock_client = MagicMock()
            mock_client.table.return_value.insert.return_value.execute.return_value = MagicMock(
                data=[{"id": "m-1"}, {"id": "m-2"}]
            )
            mock_client_prop.return_value = mock_client

            from services.supabase_client import SupabaseClient
            client = SupabaseClient()
            object.__setattr__(client, '_client', mock_client)

            mentions = [
                {"task_id": "t-1", "meeting_id": "m-1", "mention_text": "text1"},
                {"task_id": "t-2", "meeting_id": "m-1", "mention_text": "text2"},
            ]
            result = client.create_task_mentions_batch(mentions)

            assert len(result) == 2

    def test_get_task_mentions_filters(self):
        """get_task_mentions should apply task_id and meeting_id filters."""
        with patch("services.supabase_client.SupabaseClient.client", new_callable=PropertyMock) as mock_client_prop:
            mock_client = MagicMock()
            mock_query = MagicMock()
            mock_client.table.return_value.select.return_value = mock_query
            mock_query.eq.return_value = mock_query
            mock_query.order.return_value = mock_query
            mock_query.limit.return_value = mock_query
            mock_query.execute.return_value = MagicMock(data=[{"id": "m-1"}])
            mock_client_prop.return_value = mock_client

            from services.supabase_client import SupabaseClient
            client = SupabaseClient()
            object.__setattr__(client, '_client', mock_client)

            result = client.get_task_mentions(task_id="task-1")

            mock_query.eq.assert_called_with("task_id", "task-1")
            assert len(result) == 1


# =============================================================================
# Approval Flow Cross-Reference Integration
# =============================================================================

class TestApprovalFlowCrossReference:
    """Tests for cross-reference integration in approval flow."""

    @pytest.mark.asyncio
    async def test_submit_for_approval_includes_cross_ref(self):
        """submit_for_approval should pass cross_reference to send_approval_request."""
        with (
            patch("guardrails.approval_flow.telegram_bot") as mock_tg,
            patch("guardrails.approval_flow.gmail_service") as mock_gmail,
            patch("guardrails.approval_flow.supabase_client") as mock_db,
            patch("guardrails.approval_flow.settings") as mock_settings,
            patch("guardrails.approval_flow.conversation_memory") as mock_conv,
        ):
            mock_tg.send_approval_request = AsyncMock(return_value=True)
            mock_gmail.send_approval_request = AsyncMock(return_value=True)
            mock_db.log_action = MagicMock(return_value={"id": "log-1"})
            mock_db.upsert_pending_approval = MagicMock(return_value={})
            mock_settings.APPROVAL_MODE = "manual"
            mock_settings.TELEGRAM_EYAL_CHAT_ID = None
            mock_settings.EYAL_EMAIL = None

            from guardrails.approval_flow import submit_for_approval

            cross_ref = {
                "status_changes": [{"task_id": "t-1", "new_status": "done"}],
                "dedup": {"duplicates": [], "updates": []},
                "resolved_questions": [],
            }
            content = {
                "title": "Team Meeting",
                "summary": "We discussed things.",
                "cross_reference": cross_ref,
            }

            await submit_for_approval("meeting_summary", content, "meeting-1")

            # Verify cross_reference was passed to Telegram
            tg_kwargs = mock_tg.send_approval_request.call_args.kwargs
            assert tg_kwargs.get("cross_reference") == cross_ref

    @pytest.mark.asyncio
    async def test_apply_cross_reference_changes(self):
        """_apply_cross_reference_changes should update tasks and resolve questions."""
        with patch("guardrails.approval_flow.supabase_client") as mock_db:
            mock_db.update_task = MagicMock(return_value={"id": "t-1", "status": "done"})
            mock_db.resolve_question = MagicMock(return_value={"id": "q-1", "status": "resolved"})

            from guardrails.approval_flow import _apply_cross_reference_changes

            cross_ref = {
                "status_changes": [
                    {"task_id": "t-1", "new_status": "done", "evidence": "done"},
                ],
                "dedup": {
                    "duplicates": [],
                    "updates": [{"existing_task_id": "t-2", "new_status": "in_progress"}],
                },
                "resolved_questions": [
                    {"question_id": "q-1"},
                ],
            }

            result = await _apply_cross_reference_changes(
                meeting_id="meeting-1",
                cross_ref=cross_ref,
                meeting_title="Team Meeting",
                meeting_date="2026-02-28",
            )

            # Should have applied 2 status changes (1 from status_changes + 1 from dedup updates)
            assert result["status_changes_applied"] == 2
            # Should have resolved 1 question
            assert result["questions_resolved"] == 1
            # Verify the calls
            assert mock_db.update_task.call_count == 2
            mock_db.resolve_question.assert_called_once_with(
                question_id="q-1",
                resolved_in_meeting_id="meeting-1",
            )


# =============================================================================
# Time-Weighted RAG
# =============================================================================

class TestTimeWeightedRAG:
    """Tests for _apply_time_weighting in supabase_client."""

    def test_recent_results_boosted(self):
        """Recent results should get a higher score than older ones."""
        from services.supabase_client import SupabaseClient

        now = datetime.now()
        results = [
            {
                "id": "old",
                "rrf_score": 0.5,
                "metadata": {"meeting_date": (now - timedelta(days=90)).isoformat()},
            },
            {
                "id": "recent",
                "rrf_score": 0.5,
                "metadata": {"meeting_date": (now - timedelta(days=1)).isoformat()},
            },
        ]

        weighted = SupabaseClient._apply_time_weighting(results)

        # Recent result should now rank higher
        assert weighted[0]["id"] == "recent"
        assert weighted[0]["rrf_score"] > weighted[1]["rrf_score"]

    def test_no_metadata_no_crash(self):
        """Results without metadata should not crash."""
        from services.supabase_client import SupabaseClient

        results = [
            {"id": "no-meta", "rrf_score": 0.5, "metadata": None},
            {"id": "empty-meta", "rrf_score": 0.4, "metadata": {}},
        ]

        weighted = SupabaseClient._apply_time_weighting(results)

        # Should still return results sorted by rrf_score
        assert len(weighted) == 2
        assert weighted[0]["id"] == "no-meta"

    def test_half_life_decay(self):
        """Score should decay by approximately half at half_life_days."""
        from services.supabase_client import SupabaseClient

        now = datetime.now()
        results = [
            {
                "id": "at-half-life",
                "rrf_score": 0.0,  # Zero base score to isolate recency
                "metadata": {"meeting_date": (now - timedelta(days=30)).isoformat()},
            },
            {
                "id": "today",
                "rrf_score": 0.0,
                "metadata": {"meeting_date": now.isoformat()},
            },
        ]

        weighted = SupabaseClient._apply_time_weighting(results, half_life_days=30)

        today_score = next(r for r in weighted if r["id"] == "today")["rrf_score"]
        half_score = next(r for r in weighted if r["id"] == "at-half-life")["rrf_score"]

        # Today should get ~0.3 recency boost, half-life should get ~0.15
        assert today_score > half_score
        assert half_score > 0


# =============================================================================
# Parent Chunk Retrieval
# =============================================================================

class TestParentChunkRetrieval:
    """Tests for expanded context in enrich_chunks_with_context."""

    def test_fetches_neighbor_chunks(self):
        """Should fetch chunk_index - 1 and + 1 as expanded context."""
        with patch("services.supabase_client.SupabaseClient.client", new_callable=PropertyMock) as mock_client_prop:
            mock_client = MagicMock()
            mock_client_prop.return_value = mock_client

            # Mock get_meeting
            mock_meeting_response = MagicMock(data=[{
                "id": "meeting-1", "title": "Test Meeting",
                "date": "2026-02-28", "participants": ["Eyal"],
            }])
            # Mock decisions and tasks queries
            mock_empty_response = MagicMock(data=[])
            # Mock neighbors query
            mock_neighbors_response = MagicMock(data=[
                {"chunk_text": "Previous chunk text", "chunk_index": 2},
                {"chunk_text": "Next chunk text", "chunk_index": 4},
            ])

            def table_router(table_name):
                mock_table = MagicMock()
                if table_name == "meetings":
                    mock_table.select.return_value.eq.return_value.execute.return_value = mock_meeting_response
                elif table_name == "embeddings":
                    # For the neighbors query
                    mock_table.select.return_value.eq.return_value.in_.return_value.execute.return_value = mock_neighbors_response
                else:
                    mock_table.select.return_value.eq.return_value.execute.return_value = mock_empty_response
                    mock_table.select.return_value.eq.return_value.order.return_value.limit.return_value.execute.return_value = mock_empty_response
                    mock_table.select.return_value.eq.return_value.ilike.return_value.limit.return_value.execute.return_value = mock_empty_response
                return mock_table

            mock_client.table.side_effect = table_router

            from services.supabase_client import SupabaseClient
            client = SupabaseClient()
            object.__setattr__(client, '_client', mock_client)

            chunks = [{
                "id": "chunk-1",
                "source_id": "meeting-1",
                "source_type": "meeting",
                "chunk_index": 3,
                "chunk_text": "Current chunk text",
            }]

            enriched = client.enrich_chunks_with_context(chunks)

            assert len(enriched) == 1
            assert "expanded_context" in enriched[0]
            assert "Previous chunk text" in enriched[0]["expanded_context"]
            assert "Next chunk text" in enriched[0]["expanded_context"]


# =============================================================================
# Query Router
# =============================================================================

class TestQueryRouter:
    """Tests for _classify_query in agent.py."""

    def _get_agent(self):
        """Create agent instance with mocked dependencies."""
        with (
            patch("core.agent.Anthropic"),
            patch("core.agent.settings") as mock_settings,
            patch("core.agent.get_system_prompt", return_value="system prompt"),
            patch("core.agent.TOOL_DEFINITIONS", []),
        ):
            mock_settings.ANTHROPIC_API_KEY = "test-key"
            mock_settings.model_agent = "claude-haiku-4-5-20251001"
            from core.agent import GianluigiAgent
            return GianluigiAgent()

    def test_task_status_query(self):
        """'Status of' queries should be classified as task_status."""
        agent = self._get_agent()
        assert agent._classify_query("What's the status of the Moldova deal?") == "task_status"

    def test_progress_query(self):
        """'Progress on' queries should be classified as task_status."""
        agent = self._get_agent()
        assert agent._classify_query("What progress on the investor deck?") == "task_status"

    def test_where_are_we_query(self):
        """'Where are we' queries should be classified as task_status."""
        agent = self._get_agent()
        assert agent._classify_query("Where are we with the AWS setup?") == "task_status"

    def test_entity_lookup_query(self):
        """'Tell me about' queries should be classified as entity_lookup."""
        agent = self._get_agent()
        assert agent._classify_query("Tell me about AgriTech Partners") == "entity_lookup"

    def test_decision_history_query(self):
        """'When did we decide' queries should be classified as decision_history."""
        agent = self._get_agent()
        assert agent._classify_query("When did we decide on the pricing model?") == "decision_history"

    def test_general_query(self):
        """Generic queries should be classified as general."""
        agent = self._get_agent()
        assert agent._classify_query("Hello, how are you?") == "general"

    def test_what_was_decided_query(self):
        """'What was decided' queries should be classified as decision_history."""
        agent = self._get_agent()
        assert agent._classify_query("What was decided about cloud providers?") == "decision_history"


# =============================================================================
# Telegram Cross-Reference Formatting
# =============================================================================

class TestTelegramCrossRefFormatting:
    """Tests for _format_cross_reference_section in telegram_bot.py."""

    def test_format_status_changes(self):
        """Should format status changes with confidence and evidence."""
        from services.telegram_bot import _format_cross_reference_section

        cross_ref = {
            "status_changes": [{
                "task_title": "Set up dev env",
                "assignee": "Roye",
                "new_status": "done",
                "confidence": "high",
                "evidence": "I've got the dev environment running",
            }],
            "dedup": {"duplicates": [], "updates": []},
            "resolved_questions": [],
        }

        lines = _format_cross_reference_section(cross_ref)

        text = "\n".join(lines)
        assert "Cross-Meeting Intelligence" in text
        assert "Task Status Changes (1)" in text
        assert "HIGH" in text
        assert "Set up dev env" in text
        assert "DONE" in text

    def test_format_empty_cross_ref(self):
        """Empty cross-reference should return empty list."""
        from services.telegram_bot import _format_cross_reference_section

        cross_ref = {
            "status_changes": [],
            "dedup": {"duplicates": [], "updates": []},
            "resolved_questions": [],
        }

        lines = _format_cross_reference_section(cross_ref)
        assert lines == []

    def test_format_resolved_questions(self):
        """Should format resolved questions with Q&A."""
        from services.telegram_bot import _format_cross_reference_section

        cross_ref = {
            "status_changes": [],
            "dedup": {"duplicates": [], "updates": []},
            "resolved_questions": [{
                "question": "What's our pricing model?",
                "answer": "Freemium with enterprise tier",
            }],
        }

        lines = _format_cross_reference_section(cross_ref)

        text = "\n".join(lines)
        assert "Questions Resolved (1)" in text
        assert "pricing model" in text


# =============================================================================
# Weekly Digest Cross-Reference Section
# =============================================================================

class TestWeeklyDigestCrossRef:
    """Tests for cross-reference section in weekly digest."""

    def test_format_with_cross_ref_summary(self):
        """format_digest_document should include cross-reference section."""
        from processors.weekly_digest import format_digest_document

        cross_ref_summary = {
            "total_mentions": 5,
            "duplicates_prevented": 2,
            "status_changes": [
                {"task_title": "Set up dev env", "assignee": "Roye", "new_status": "done"},
            ],
            "questions_resolved": 1,
        }

        doc = format_digest_document(
            week_of="2026-02-24",
            meetings=[],
            decisions=[],
            tasks_completed=[],
            tasks_overdue=[],
            tasks_upcoming=[],
            open_questions=[],
            upcoming_meetings=[],
            cross_ref_summary=cross_ref_summary,
        )

        assert "Cross-Meeting Intelligence This Week" in doc
        assert "2 task(s)" in doc
        assert "1 task status change(s)" in doc
        assert "1 open question(s)" in doc

    def test_format_without_cross_ref_summary(self):
        """format_digest_document without cross_ref should not include section."""
        from processors.weekly_digest import format_digest_document

        doc = format_digest_document(
            week_of="2026-02-24",
            meetings=[],
            decisions=[],
            tasks_completed=[],
            tasks_overdue=[],
            tasks_upcoming=[],
            open_questions=[],
            upcoming_meetings=[],
        )

        assert "Cross-Meeting Intelligence" not in doc
