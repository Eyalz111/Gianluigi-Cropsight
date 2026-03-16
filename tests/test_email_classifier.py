"""
Tests for processors/email_classifier.py

Tests email classification, intelligence extraction, and keyword building:
- classify_email: relevant/borderline/false_positive routing
- extract_email_intelligence: structured item parsing from emails
- build_filter_keywords: live keyword list with caching
"""

import json
import pytest
from unittest.mock import MagicMock, patch


@pytest.fixture(autouse=True)
def mock_settings():
    mock = MagicMock()
    mock.model_simple = "claude-haiku"
    mock.model_agent = "claude-sonnet"
    mock.personal_contacts_blocklist_list = []
    with patch("config.settings.settings", mock):
        yield mock


@pytest.fixture(autouse=True)
def reset_keyword_cache():
    """Clear keyword cache before each test."""
    from processors.email_classifier import clear_keyword_cache
    clear_keyword_cache()
    yield
    clear_keyword_cache()


class TestClassifyEmail:
    """Tests for classify_email()."""

    @pytest.mark.asyncio
    async def test_relevant_classification(self):
        from processors.email_classifier import classify_email

        with patch("processors.email_classifier.call_llm", return_value=("relevant", {"input_tokens": 10, "output_tokens": 1})):
            result = await classify_email("eyal@cropsight.io", "Moldova pilot update", "Progress on the pilot...")
        assert result == "relevant"

    @pytest.mark.asyncio
    async def test_borderline_classification(self):
        from processors.email_classifier import classify_email

        with patch("processors.email_classifier.call_llm", return_value=("borderline", {})):
            result = await classify_email("unknown@corp.com", "Meeting follow-up", "Hi team...")
        assert result == "borderline"

    @pytest.mark.asyncio
    async def test_false_positive_classification(self):
        from processors.email_classifier import classify_email

        with patch("processors.email_classifier.call_llm", return_value=("false_positive", {})):
            result = await classify_email("news@newsletter.com", "Weekly digest", "Top stories...")
        assert result == "false_positive"

    @pytest.mark.asyncio
    async def test_unexpected_output_defaults_to_borderline(self):
        from processors.email_classifier import classify_email

        with patch("processors.email_classifier.call_llm", return_value=("maybe_relevant", {})):
            result = await classify_email("test@test.com", "Subject", "Body")
        assert result == "borderline"

    @pytest.mark.asyncio
    async def test_whitespace_and_casing_stripped(self):
        from processors.email_classifier import classify_email

        with patch("processors.email_classifier.call_llm", return_value=("  Relevant \n", {})):
            result = await classify_email("test@test.com", "Subject", "Body")
        assert result == "relevant"

    @pytest.mark.asyncio
    async def test_llm_error_defaults_to_borderline(self):
        from processors.email_classifier import classify_email

        with patch("processors.email_classifier.call_llm", side_effect=Exception("API timeout")):
            result = await classify_email("test@test.com", "Subject", "Body")
        assert result == "borderline"

    @pytest.mark.asyncio
    async def test_filter_keywords_passed_to_prompt(self):
        from processors.email_classifier import classify_email

        with patch("processors.email_classifier.call_llm", return_value=("relevant", {})) as mock_llm:
            await classify_email("a@b.com", "Sub", "Body", filter_keywords=["wheat", "gagauzia"])
            call_kwargs = mock_llm.call_args
            system_prompt = call_kwargs.kwargs.get("system") or call_kwargs[1].get("system", "")
            if not system_prompt:
                # positional args: prompt, model, max_tokens, system
                system_prompt = call_kwargs[0][3] if len(call_kwargs[0]) > 3 else ""
            assert "wheat" in system_prompt

    @pytest.mark.asyncio
    async def test_no_filter_keywords_uses_defaults(self):
        from processors.email_classifier import classify_email

        with patch("processors.email_classifier.call_llm", return_value=("relevant", {})) as mock_llm:
            await classify_email("a@b.com", "Sub", "Body", filter_keywords=None)
            args, kwargs = mock_llm.call_args
            # system is passed as kwarg
            system_prompt = kwargs.get("system", "")
            assert "cropsight" in system_prompt


class TestExtractEmailIntelligence:
    """Tests for extract_email_intelligence()."""

    @pytest.mark.asyncio
    async def test_extracts_task_items(self):
        from processors.email_classifier import extract_email_intelligence

        items = [{"type": "task", "text": "Send proposal to IIA", "assignee": "Eyal", "sensitive": False}]
        with patch("processors.email_classifier.call_llm", return_value=(json.dumps(items), {})):
            result = await extract_email_intelligence("paolo@cropsight.io", "IIA proposal", "Please send...")
        assert len(result) == 1
        assert result[0]["type"] == "task"
        assert result[0]["assignee"] == "Eyal"

    @pytest.mark.asyncio
    async def test_extracts_multiple_types(self):
        from processors.email_classifier import extract_email_intelligence

        items = [
            {"type": "commitment", "text": "Paolo committed to deliver deck by Friday", "speaker": "Paolo"},
            {"type": "deadline_change", "text": "Pilot delayed to Q3", "entity": "Moldova Pilot"},
        ]
        with patch("processors.email_classifier.call_llm", return_value=(json.dumps(items), {})):
            result = await extract_email_intelligence("paolo@x.com", "Update", "body")
        assert len(result) == 2
        assert result[0]["type"] == "commitment"
        assert result[1]["type"] == "deadline_change"

    @pytest.mark.asyncio
    async def test_parses_json_with_code_fences(self):
        from processors.email_classifier import extract_email_intelligence

        raw = '```json\n[{"type": "information", "text": "Budget approved"}]\n```'
        with patch("processors.email_classifier.call_llm", return_value=(raw, {})):
            result = await extract_email_intelligence("a@b.com", "Sub", "Body")
        assert len(result) == 1
        assert result[0]["type"] == "information"

    @pytest.mark.asyncio
    async def test_empty_array_response(self):
        from processors.email_classifier import extract_email_intelligence

        with patch("processors.email_classifier.call_llm", return_value=("[]", {})):
            result = await extract_email_intelligence("a@b.com", "Sub", "Body")
        assert result == []

    @pytest.mark.asyncio
    async def test_dict_with_items_key(self):
        from processors.email_classifier import extract_email_intelligence

        data = {"items": [{"type": "task", "text": "Follow up"}]}
        with patch("processors.email_classifier.call_llm", return_value=(json.dumps(data), {})):
            result = await extract_email_intelligence("a@b.com", "Sub", "Body")
        assert len(result) == 1

    @pytest.mark.asyncio
    async def test_invalid_json_with_embedded_array(self):
        from processors.email_classifier import extract_email_intelligence

        raw = 'Here are the items: [{"type": "task", "text": "Do thing"}] end.'
        with patch("processors.email_classifier.call_llm", return_value=(raw, {})):
            result = await extract_email_intelligence("a@b.com", "Sub", "Body")
        assert len(result) == 1

    @pytest.mark.asyncio
    async def test_completely_unparseable_returns_empty(self):
        from processors.email_classifier import extract_email_intelligence

        with patch("processors.email_classifier.call_llm", return_value=("No items found.", {})):
            result = await extract_email_intelligence("a@b.com", "Sub", "Body")
        assert result == []

    @pytest.mark.asyncio
    async def test_llm_exception_returns_empty(self):
        from processors.email_classifier import extract_email_intelligence

        with patch("processors.email_classifier.call_llm", side_effect=RuntimeError("boom")):
            result = await extract_email_intelligence("a@b.com", "Sub", "Body")
        assert result == []


class TestBuildFilterKeywords:
    """Tests for build_filter_keywords() and clear_keyword_cache()."""

    def _patch_supabase(self, entities=None, tasks=None, decisions=None):
        mock_client = MagicMock()
        mock_client.list_entities.return_value = entities or []
        mock_client.get_tasks.return_value = tasks or []
        mock_client.list_decisions.return_value = decisions or []
        return patch("services.supabase_client.supabase_client", mock_client)

    def test_baseline_keywords_always_included(self):
        from processors.email_classifier import build_filter_keywords

        with self._patch_supabase():
            result = build_filter_keywords("scan-1")
        assert "cropsight" in result
        assert "moldova" in result
        assert "agtech" in result

    def test_entity_names_added(self):
        from processors.email_classifier import build_filter_keywords

        entities = [
            {"canonical_name": "Lavazza", "aliases": ["lavazza group"]},
            {"canonical_name": "IIA", "aliases": ["Israel Innovation Authority"]},
        ]
        with self._patch_supabase(entities=entities):
            result = build_filter_keywords("scan-2")
        assert "lavazza" in result
        assert "lavazza group" in result
        assert "israel innovation authority" in result

    def test_short_entity_names_excluded(self):
        from processors.email_classifier import build_filter_keywords

        entities = [{"canonical_name": "AB", "aliases": ["X"]}]
        with self._patch_supabase(entities=entities):
            result = build_filter_keywords("scan-3")
        assert "ab" not in result
        assert "x" not in result

    def test_task_keywords_added(self):
        from processors.email_classifier import build_filter_keywords

        tasks = [{"title": "Prepare Moldova pilot proposal deck"}]
        with self._patch_supabase(tasks=tasks):
            result = build_filter_keywords("scan-4")
        # Words >3 chars extracted
        assert "prepare" in result
        assert "moldova" in result
        assert "pilot" in result
        assert "proposal" in result
        assert "deck" in result

    def test_decision_keywords_added(self):
        from processors.email_classifier import build_filter_keywords

        decisions = [{"description": "Decided to pursue Gagauzia regional expansion"}]
        with self._patch_supabase(decisions=decisions):
            result = build_filter_keywords("scan-5")
        # Words >4 chars extracted for decisions
        assert "decided" in result
        assert "pursue" in result
        assert "gagauzia" in result

    def test_caching_same_scan_id(self):
        from processors.email_classifier import build_filter_keywords

        with self._patch_supabase() as mock_patch:
            mock_client = mock_patch.start() if hasattr(mock_patch, 'start') else None
        # Use context manager properly
        with self._patch_supabase() as mock_sb:
            result1 = build_filter_keywords("scan-same")
            result2 = build_filter_keywords("scan-same")
            # Second call should use cache, not query again
            # list_entities called only once
            from processors.email_classifier import _keyword_cache
            assert result1 is result2

    def test_different_scan_id_invalidates_cache(self):
        from processors.email_classifier import build_filter_keywords

        with self._patch_supabase():
            result1 = build_filter_keywords("scan-a")
        with self._patch_supabase(entities=[{"canonical_name": "NewEntity", "aliases": []}]):
            result2 = build_filter_keywords("scan-b")
        assert "newentity" in result2

    def test_clear_keyword_cache(self):
        from processors.email_classifier import build_filter_keywords, clear_keyword_cache

        with self._patch_supabase():
            build_filter_keywords("scan-x")
        clear_keyword_cache()
        from processors import email_classifier
        assert email_classifier._keyword_cache is None
        assert email_classifier._keyword_cache_scan_id is None

    def test_entity_error_does_not_crash(self):
        from processors.email_classifier import build_filter_keywords

        mock_client = MagicMock()
        mock_client.list_entities.side_effect = Exception("DB down")
        mock_client.get_tasks.return_value = []
        mock_client.list_decisions.return_value = []
        with patch("services.supabase_client.supabase_client", mock_client):
            result = build_filter_keywords("scan-err")
        # Should still have baseline keywords
        assert "cropsight" in result

    def test_results_are_sorted_and_deduplicated(self):
        from processors.email_classifier import build_filter_keywords

        with self._patch_supabase():
            result = build_filter_keywords("scan-sort")
        assert result == sorted(result)
        assert len(result) == len(set(result))
