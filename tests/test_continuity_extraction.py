"""Tests for Phase 12 A2: Continuity-aware extraction.

Tests cover:
- extract_task_match_annotations() in transcript_processor.py
- _process_task_match_annotations() in cross_reference.py
- existing_task_match schema in extraction prompt
- Feature flag CONTINUITY_AUTO_APPLY_ENABLED
- Integration: annotations flow from extraction → cross-reference
"""

import pytest
from unittest.mock import MagicMock, patch, AsyncMock


# =========================================================================
# extract_task_match_annotations
# =========================================================================

class TestExtractTaskMatchAnnotations:
    """Tests for transcript_processor.extract_task_match_annotations()."""

    def test_no_annotations(self):
        from processors.transcript_processor import extract_task_match_annotations

        tasks = [
            {"title": "New task", "assignee": "Eyal"},
            {"title": "Another task", "assignee": "Roye"},
        ]
        result = extract_task_match_annotations(tasks)
        assert result == []

    def test_null_match_ignored(self):
        from processors.transcript_processor import extract_task_match_annotations

        tasks = [
            {"title": "New task", "existing_task_match": None},
        ]
        result = extract_task_match_annotations(tasks)
        assert result == []

    def test_empty_dict_match_ignored(self):
        from processors.transcript_processor import extract_task_match_annotations

        tasks = [
            {"title": "New task", "existing_task_match": {}},
        ]
        result = extract_task_match_annotations(tasks)
        assert result == []

    def test_match_without_task_id_ignored(self):
        from processors.transcript_processor import extract_task_match_annotations

        tasks = [
            {"title": "T1", "existing_task_match": {"confidence": "high", "evolution": "completion"}},
        ]
        result = extract_task_match_annotations(tasks)
        assert result == []

    def test_valid_match_extracted(self):
        from processors.transcript_processor import extract_task_match_annotations

        tasks = [
            {
                "title": "UPDATE: Write accuracy abstract",
                "existing_task_match": {
                    "task_id": "abc-123",
                    "confidence": "high",
                    "evolution": "completion",
                },
            },
        ]
        result = extract_task_match_annotations(tasks)
        assert len(result) == 1
        assert result[0]["task_id"] == "abc-123"
        assert result[0]["confidence"] == "high"
        assert result[0]["evolution"] == "completion"
        assert result[0]["task_index"] == 0
        assert "accuracy abstract" in result[0]["title"]

    def test_multiple_tasks_mixed(self):
        from processors.transcript_processor import extract_task_match_annotations

        tasks = [
            {"title": "New task", "existing_task_match": None},
            {
                "title": "UPDATE: Deploy staging",
                "existing_task_match": {
                    "task_id": "task-1",
                    "confidence": "medium",
                    "evolution": "status_update",
                },
            },
            {"title": "Another new task"},
            {
                "title": "UPDATE: Review budget",
                "existing_task_match": {
                    "task_id": "task-2",
                    "confidence": "high",
                    "evolution": "scope_change",
                },
            },
        ]
        result = extract_task_match_annotations(tasks)
        assert len(result) == 2
        assert result[0]["task_index"] == 1
        assert result[0]["task_id"] == "task-1"
        assert result[1]["task_index"] == 3
        assert result[1]["task_id"] == "task-2"

    def test_default_confidence_low(self):
        from processors.transcript_processor import extract_task_match_annotations

        tasks = [
            {
                "title": "T1",
                "existing_task_match": {"task_id": "t-1"},
            },
        ]
        result = extract_task_match_annotations(tasks)
        assert result[0]["confidence"] == "low"

    def test_evolution_can_be_none(self):
        from processors.transcript_processor import extract_task_match_annotations

        tasks = [
            {
                "title": "T1",
                "existing_task_match": {
                    "task_id": "t-1",
                    "confidence": "medium",
                    "evolution": None,
                },
            },
        ]
        result = extract_task_match_annotations(tasks)
        assert result[0]["evolution"] is None

    def test_string_match_value_ignored(self):
        """Non-dict existing_task_match should be ignored."""
        from processors.transcript_processor import extract_task_match_annotations

        tasks = [
            {"title": "T1", "existing_task_match": "not-a-dict"},
        ]
        result = extract_task_match_annotations(tasks)
        assert result == []

    def test_empty_task_list(self):
        from processors.transcript_processor import extract_task_match_annotations

        result = extract_task_match_annotations([])
        assert result == []


# =========================================================================
# _process_task_match_annotations
# =========================================================================

class TestProcessTaskMatchAnnotations:
    """Tests for cross_reference._process_task_match_annotations()."""

    @patch("processors.cross_reference.settings")
    @patch("processors.cross_reference.supabase_client")
    def test_creates_task_mentions(self, mock_sc, mock_settings):
        mock_settings.CONTINUITY_AUTO_APPLY_ENABLED = False

        from processors.cross_reference import _process_task_match_annotations

        annotations = [
            {
                "task_index": 0,
                "task_id": "task-1",
                "confidence": "high",
                "evolution": "completion",
                "title": "Write abstract",
            },
        ]
        _process_task_match_annotations(annotations, "meeting-1")

        mock_sc.create_task_mentions_batch.assert_called_once()
        mentions = mock_sc.create_task_mentions_batch.call_args[0][0]
        assert len(mentions) == 1
        assert mentions[0]["task_id"] == "task-1"
        assert mentions[0]["implied_status"] == "done"
        assert mentions[0]["confidence"] == "high"

    @patch("processors.cross_reference.settings")
    @patch("processors.cross_reference.supabase_client")
    def test_completion_implies_done(self, mock_sc, mock_settings):
        mock_settings.CONTINUITY_AUTO_APPLY_ENABLED = False

        from processors.cross_reference import _process_task_match_annotations

        annotations = [
            {"task_index": 0, "task_id": "t1", "confidence": "high",
             "evolution": "completion", "title": "T1"},
        ]
        _process_task_match_annotations(annotations, "m1")
        mentions = mock_sc.create_task_mentions_batch.call_args[0][0]
        assert mentions[0]["implied_status"] == "done"

    @patch("processors.cross_reference.settings")
    @patch("processors.cross_reference.supabase_client")
    def test_status_update_implies_in_progress(self, mock_sc, mock_settings):
        mock_settings.CONTINUITY_AUTO_APPLY_ENABLED = False

        from processors.cross_reference import _process_task_match_annotations

        annotations = [
            {"task_index": 0, "task_id": "t1", "confidence": "medium",
             "evolution": "status_update", "title": "T1"},
        ]
        _process_task_match_annotations(annotations, "m1")
        mentions = mock_sc.create_task_mentions_batch.call_args[0][0]
        assert mentions[0]["implied_status"] == "in_progress"

    @patch("processors.cross_reference.settings")
    @patch("processors.cross_reference.supabase_client")
    def test_scope_change_no_implied_status(self, mock_sc, mock_settings):
        mock_settings.CONTINUITY_AUTO_APPLY_ENABLED = False

        from processors.cross_reference import _process_task_match_annotations

        annotations = [
            {"task_index": 0, "task_id": "t1", "confidence": "high",
             "evolution": "scope_change", "title": "T1"},
        ]
        _process_task_match_annotations(annotations, "m1")
        mentions = mock_sc.create_task_mentions_batch.call_args[0][0]
        assert mentions[0]["implied_status"] is None

    @patch("processors.cross_reference.settings")
    @patch("processors.cross_reference.supabase_client")
    def test_auto_apply_disabled_no_updates(self, mock_sc, mock_settings):
        """When CONTINUITY_AUTO_APPLY_ENABLED=False, no task updates happen."""
        mock_settings.CONTINUITY_AUTO_APPLY_ENABLED = False

        from processors.cross_reference import _process_task_match_annotations

        annotations = [
            {"task_index": 0, "task_id": "t1", "confidence": "high",
             "evolution": "completion", "title": "Finished task"},
        ]
        _process_task_match_annotations(annotations, "m1")

        # Should create mentions but NOT update task
        mock_sc.create_task_mentions_batch.assert_called_once()
        mock_sc.update_task.assert_not_called()

    @patch("processors.cross_reference.settings")
    @patch("processors.cross_reference.supabase_client")
    def test_auto_apply_enabled_high_confidence_completion(self, mock_sc, mock_settings):
        """When auto-apply enabled, high-confidence completion → update task to done."""
        mock_settings.CONTINUITY_AUTO_APPLY_ENABLED = True

        from processors.cross_reference import _process_task_match_annotations

        annotations = [
            {"task_index": 0, "task_id": "t1", "confidence": "high",
             "evolution": "completion", "title": "Done task"},
        ]
        _process_task_match_annotations(annotations, "m1")

        mock_sc.update_task.assert_called_once_with("t1", status="done")

    @patch("processors.cross_reference.settings")
    @patch("processors.cross_reference.supabase_client")
    def test_auto_apply_enabled_high_confidence_status_update(self, mock_sc, mock_settings):
        mock_settings.CONTINUITY_AUTO_APPLY_ENABLED = True

        from processors.cross_reference import _process_task_match_annotations

        annotations = [
            {"task_index": 0, "task_id": "t1", "confidence": "high",
             "evolution": "status_update", "title": "In progress task"},
        ]
        _process_task_match_annotations(annotations, "m1")

        mock_sc.update_task.assert_called_once_with("t1", status="in_progress")

    @patch("processors.cross_reference.settings")
    @patch("processors.cross_reference.supabase_client")
    def test_auto_apply_skips_medium_confidence(self, mock_sc, mock_settings):
        """Auto-apply should only act on high confidence."""
        mock_settings.CONTINUITY_AUTO_APPLY_ENABLED = True

        from processors.cross_reference import _process_task_match_annotations

        annotations = [
            {"task_index": 0, "task_id": "t1", "confidence": "medium",
             "evolution": "completion", "title": "Maybe done"},
        ]
        _process_task_match_annotations(annotations, "m1")

        mock_sc.update_task.assert_not_called()

    @patch("processors.cross_reference.settings")
    @patch("processors.cross_reference.supabase_client")
    def test_auto_apply_skips_low_confidence(self, mock_sc, mock_settings):
        mock_settings.CONTINUITY_AUTO_APPLY_ENABLED = True

        from processors.cross_reference import _process_task_match_annotations

        annotations = [
            {"task_index": 0, "task_id": "t1", "confidence": "low",
             "evolution": "completion", "title": "Possibly done"},
        ]
        _process_task_match_annotations(annotations, "m1")

        mock_sc.update_task.assert_not_called()

    @patch("processors.cross_reference.settings")
    @patch("processors.cross_reference.supabase_client")
    def test_auto_apply_scope_change_no_action(self, mock_sc, mock_settings):
        """Scope changes don't auto-apply even with high confidence."""
        mock_settings.CONTINUITY_AUTO_APPLY_ENABLED = True

        from processors.cross_reference import _process_task_match_annotations

        annotations = [
            {"task_index": 0, "task_id": "t1", "confidence": "high",
             "evolution": "scope_change", "title": "Scope change"},
        ]
        _process_task_match_annotations(annotations, "m1")

        mock_sc.update_task.assert_not_called()

    @patch("processors.cross_reference.settings")
    @patch("processors.cross_reference.supabase_client")
    def test_empty_annotations_no_action(self, mock_sc, mock_settings):
        from processors.cross_reference import _process_task_match_annotations

        _process_task_match_annotations([], "m1")
        mock_sc.create_task_mentions_batch.assert_not_called()

    @patch("processors.cross_reference.settings")
    @patch("processors.cross_reference.supabase_client")
    def test_mention_creation_failure_non_fatal(self, mock_sc, mock_settings):
        mock_settings.CONTINUITY_AUTO_APPLY_ENABLED = False
        mock_sc.create_task_mentions_batch.side_effect = Exception("DB error")

        from processors.cross_reference import _process_task_match_annotations

        # Should not raise
        annotations = [
            {"task_index": 0, "task_id": "t1", "confidence": "high",
             "evolution": "completion", "title": "T1"},
        ]
        _process_task_match_annotations(annotations, "m1")

    @patch("processors.cross_reference.settings")
    @patch("processors.cross_reference.supabase_client")
    def test_auto_apply_failure_non_fatal(self, mock_sc, mock_settings):
        mock_settings.CONTINUITY_AUTO_APPLY_ENABLED = True
        mock_sc.update_task.side_effect = Exception("DB error")

        from processors.cross_reference import _process_task_match_annotations

        annotations = [
            {"task_index": 0, "task_id": "t1", "confidence": "high",
             "evolution": "completion", "title": "T1"},
        ]
        # Should not raise
        _process_task_match_annotations(annotations, "m1")

    @patch("processors.cross_reference.settings")
    @patch("processors.cross_reference.supabase_client")
    def test_multiple_annotations_mixed(self, mock_sc, mock_settings):
        mock_settings.CONTINUITY_AUTO_APPLY_ENABLED = True

        from processors.cross_reference import _process_task_match_annotations

        annotations = [
            {"task_index": 0, "task_id": "t1", "confidence": "high",
             "evolution": "completion", "title": "Done"},
            {"task_index": 1, "task_id": "t2", "confidence": "medium",
             "evolution": "status_update", "title": "Maybe"},
            {"task_index": 2, "task_id": "t3", "confidence": "high",
             "evolution": "status_update", "title": "Started"},
        ]
        _process_task_match_annotations(annotations, "m1")

        # Mentions for all 3
        mentions = mock_sc.create_task_mentions_batch.call_args[0][0]
        assert len(mentions) == 3

        # Auto-apply only for t1 (high+completion) and t3 (high+status_update)
        assert mock_sc.update_task.call_count == 2


# =========================================================================
# Integration: annotations in run_cross_reference
# =========================================================================

class TestRunCrossReferenceAnnotations:
    """Test that task_match_annotations flow through run_cross_reference."""

    @pytest.mark.asyncio
    @patch("processors.cross_reference.settings")
    @patch("processors.cross_reference.supabase_client")
    @patch("processors.cross_reference.call_llm")
    async def test_annotations_passed_through(self, mock_llm, mock_sc, mock_settings):
        mock_settings.CONTINUITY_AUTO_APPLY_ENABLED = False
        mock_settings.model_simple = "haiku"
        mock_settings.model_agent = "sonnet"
        mock_sc.get_tasks.return_value = []
        mock_sc.get_open_questions.return_value = []
        mock_sc.list_decisions.return_value = []

        from processors.cross_reference import run_cross_reference

        annotations = [
            {"task_index": 0, "task_id": "t1", "confidence": "high",
             "evolution": "completion", "title": "Done task"},
        ]

        result = await run_cross_reference(
            meeting_id="m1",
            transcript="test transcript",
            new_tasks=[{"title": "Done task"}],
            task_match_annotations=annotations,
        )

        assert result["task_match_annotations"] == annotations
        # Mentions should have been created
        mock_sc.create_task_mentions_batch.assert_called()

    @pytest.mark.asyncio
    @patch("processors.cross_reference.settings")
    @patch("processors.cross_reference.supabase_client")
    @patch("processors.cross_reference.call_llm")
    async def test_no_annotations_still_works(self, mock_llm, mock_sc, mock_settings):
        mock_settings.CONTINUITY_AUTO_APPLY_ENABLED = False
        mock_settings.model_simple = "haiku"
        mock_settings.model_agent = "sonnet"
        mock_sc.get_tasks.return_value = []
        mock_sc.get_open_questions.return_value = []
        mock_sc.list_decisions.return_value = []

        from processors.cross_reference import run_cross_reference

        result = await run_cross_reference(
            meeting_id="m1",
            transcript="test transcript",
            new_tasks=[],
        )

        assert result["task_match_annotations"] == []


# =========================================================================
# Extraction prompt: existing_task_match in schema
# =========================================================================

class TestExtractionPromptSchema:
    """Verify the extraction prompt includes existing_task_match."""

    def test_prompt_includes_existing_task_match_field(self):
        """The extraction system prompt should mention existing_task_match."""
        from processors.transcript_processor import extract_structured_data
        # We can't easily test the full system prompt without calling the function,
        # but we can verify the schema text is in the source module
        import processors.transcript_processor as module
        import inspect
        source = inspect.getsource(module.extract_structured_data)
        assert "existing_task_match" in source

    def test_prompt_includes_evolution_examples(self):
        """The extraction system prompt should include task evolution examples."""
        import processors.transcript_processor as module
        import inspect
        source = inspect.getsource(module.extract_structured_data)
        assert "TASK EVOLUTION EXAMPLES" in source
        assert "scope_change" in source
        assert "completion" in source
        assert "status_update" in source


# =========================================================================
# Feature flag
# =========================================================================

class TestContinuityFeatureFlag:
    """Verify the feature flag exists and defaults to False."""

    def test_flag_defaults_false(self):
        from config.settings import Settings
        s = Settings(
            SUPABASE_URL="https://test.supabase.co",
            SUPABASE_KEY="test-key",
            ANTHROPIC_API_KEY="test-key",
            OPENAI_API_KEY="test-key",
            TELEGRAM_BOT_TOKEN="test-token",
        )
        assert s.CONTINUITY_AUTO_APPLY_ENABLED is False

    def test_flag_can_be_enabled(self):
        from config.settings import Settings
        s = Settings(
            SUPABASE_URL="https://test.supabase.co",
            SUPABASE_KEY="test-key",
            ANTHROPIC_API_KEY="test-key",
            OPENAI_API_KEY="test-key",
            TELEGRAM_BOT_TOKEN="test-token",
            CONTINUITY_AUTO_APPLY_ENABLED=True,
        )
        assert s.CONTINUITY_AUTO_APPLY_ENABLED is True


# =========================================================================
# Quality gate: extraction prompt structure
# =========================================================================

class TestExtractionPromptStructure:
    """Verify the extraction prompt is well-formed for continuity-aware extraction."""

    @patch("processors.transcript_processor.supabase_client")
    def test_extraction_prompt_with_existing_tasks(self, mock_sc):
        """When existing tasks are provided, prompt should include them."""
        from core.system_prompt import get_summary_extraction_prompt

        existing_tasks = [
            {"title": "Write abstract", "assignee": "Roye", "status": "pending"},
            {"title": "Send deck", "assignee": "Paolo", "status": "in_progress"},
        ]

        prompt = get_summary_extraction_prompt(
            transcript="[00:00] Eyal: Hi everyone",
            meeting_title="Test Meeting",
            meeting_date="2026-04-01",
            participants=["Eyal", "Roye"],
            existing_tasks=existing_tasks,
        )

        assert "EXISTING OPEN TASKS" in prompt
        assert "Write abstract" in prompt
        assert "Send deck" in prompt

    @patch("processors.transcript_processor.supabase_client")
    def test_extraction_prompt_with_history_context(self, mock_sc):
        from core.system_prompt import get_summary_extraction_prompt

        prompt = get_summary_extraction_prompt(
            transcript="[00:00] Eyal: Hi",
            meeting_title="Test",
            meeting_date="2026-04-01",
            participants=["Eyal"],
            meeting_history_context="Meeting: Sprint 1 (2026-03-28)\n  Tasks: 2/3 done",
        )

        assert "PREVIOUS MEETING CONTEXT" in prompt
        assert "Sprint 1" in prompt


# =========================================================================
# Quality gate: simulated extraction response parsing
# =========================================================================

class TestExtractionResponseParsing:
    """Quality gate: verify extraction response with existing_task_match parses correctly."""

    def test_parse_response_with_task_match(self):
        """Simulated LLM response with existing_task_match fields parses correctly."""
        import json
        from processors.transcript_processor import (
            _parse_extraction_response,
            extract_task_match_annotations,
        )

        simulated_response = json.dumps({
            "executive_summary": "Team discussed Moldova pilot progress and fundraising.",
            "decisions": [
                {
                    "label": "Moldova Pilot",
                    "description": "Revise business plan with conservative scenario",
                    "rationale": "Investor feedback on aggressive projections",
                    "options_considered": ["Single optimistic", "Two scenarios"],
                    "confidence": 4,
                    "context": "Tnufa feedback",
                    "participants_involved": ["Eyal"],
                    "transcript_timestamp": "03:30",
                }
            ],
            "tasks": [
                {
                    "label": "SatYield Model",
                    "title": "Write 1-page accuracy abstract documenting benchmarks",
                    "assignee": "Roye",
                    "deadline": "2026-03-28",
                    "priority": "H",
                    "category": "Product & Tech",
                    "transcript_timestamp": "01:15",
                    "existing_task_match": None,
                },
                {
                    "label": "Lavazza",
                    "title": "UPDATE: Send capability deck to Lavazza with Moldova results",
                    "assignee": "Paolo",
                    "deadline": "2026-03-27",
                    "priority": "H",
                    "category": "BD & Sales",
                    "transcript_timestamp": "01:50",
                    "existing_task_match": {
                        "task_id": "existing-task-uuid-1",
                        "confidence": "high",
                        "evolution": "scope_change",
                    },
                },
                {
                    "label": "Pre-Seed Fundraising",
                    "title": "Provide AWS cost estimates for 50/100/200 farm sites",
                    "assignee": "Roye",
                    "deadline": "2026-03-31",
                    "priority": "M",
                    "category": "Finance & Fundraising",
                    "transcript_timestamp": "04:00",
                    "existing_task_match": None,
                },
            ],
            "follow_ups": [],
            "open_questions": [
                {
                    "label": "Product Roadmap",
                    "question": "Should we start grape yield models now or after wheat?",
                    "raised_by": "Roye",
                    "transcript_timestamp": "05:50",
                }
            ],
            "stakeholders": [
                {
                    "name": "Dr. Rita Ferraro",
                    "type": "person",
                    "context": "Head of sustainability at Lavazza",
                    "speaker": "Paolo",
                    "relationship": "client",
                }
            ],
            "discussion_summary": "The team reviewed progress on SatYield accuracy.",
        })

        # Parse
        parsed = _parse_extraction_response(simulated_response)

        # Verify structure
        assert len(parsed["tasks"]) == 3
        assert parsed["tasks"][0]["existing_task_match"] is None
        assert parsed["tasks"][1]["existing_task_match"]["task_id"] == "existing-task-uuid-1"
        assert parsed["tasks"][1]["existing_task_match"]["evolution"] == "scope_change"

        # Extract annotations
        annotations = extract_task_match_annotations(parsed["tasks"])
        assert len(annotations) == 1
        assert annotations[0]["task_id"] == "existing-task-uuid-1"
        assert annotations[0]["confidence"] == "high"
        assert annotations[0]["task_index"] == 1

    def test_parse_response_backward_compatible(self):
        """Old-style response without existing_task_match still parses fine."""
        import json
        from processors.transcript_processor import (
            _parse_extraction_response,
            extract_task_match_annotations,
        )

        simulated_response = json.dumps({
            "executive_summary": "Test meeting.",
            "decisions": [],
            "tasks": [
                {
                    "label": "Test",
                    "title": "Test task",
                    "assignee": "Eyal",
                    "deadline": None,
                    "priority": "M",
                    "category": "Operations & HR",
                    "transcript_timestamp": "00:00",
                    # No existing_task_match field at all
                },
            ],
            "follow_ups": [],
            "open_questions": [],
            "stakeholders": [],
            "discussion_summary": "Nothing happened.",
        })

        parsed = _parse_extraction_response(simulated_response)
        annotations = extract_task_match_annotations(parsed["tasks"])
        assert len(annotations) == 0
