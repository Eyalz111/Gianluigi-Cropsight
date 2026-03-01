"""
Tests for commitment tracking (v0.3 Tier 2).

Tests cover:
- Commitment extraction from transcripts
- Fulfillment detection
- CRUD operations
- Weekly digest scorecard
"""

import json
import pytest
from unittest.mock import patch, MagicMock, AsyncMock

from processors.cross_reference import (
    extract_commitments,
    check_commitment_fulfillment,
    run_cross_reference,
)
from processors.weekly_digest import (
    get_commitment_scorecard,
    format_digest_document,
)


# =========================================================================
# Test extract_commitments
# =========================================================================

class TestExtractCommitments:
    """Tests for commitment extraction via LLM."""

    @pytest.mark.asyncio
    async def test_normal_extraction(self):
        """Should extract commitments from transcript."""
        commitments_json = json.dumps({
            "commitments": [
                {"speaker": "Eyal", "commitment_text": "Send the investor deck by Friday", "context": "Discussing fundraising", "implied_deadline": "Friday"},
                {"speaker": "Paolo", "commitment_text": "Set up meeting with Lavazza", "context": "BD discussion", "implied_deadline": "next week"},
            ]
        })

        with patch("processors.cross_reference.call_llm") as mock_llm:
            mock_llm.return_value = (commitments_json, {"input_tokens": 100, "output_tokens": 50, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0})

            result = await extract_commitments("meeting-123", "transcript text", ["Eyal", "Paolo"])
            assert len(result) == 2
            assert result[0]["speaker"] == "Eyal"
            assert result[1]["commitment_text"] == "Set up meeting with Lavazza"

    @pytest.mark.asyncio
    async def test_empty_extraction(self):
        """No commitments found should return empty list."""
        with patch("processors.cross_reference.call_llm") as mock_llm:
            mock_llm.return_value = ('{"commitments": []}', {"input_tokens": 50, "output_tokens": 10, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0})

            result = await extract_commitments("meeting-123", "short chat", [])
            assert result == []

    @pytest.mark.asyncio
    async def test_llm_error_returns_empty(self):
        """LLM error should return empty list."""
        with patch("processors.cross_reference.call_llm") as mock_llm:
            mock_llm.side_effect = Exception("API error")

            result = await extract_commitments("meeting-123", "transcript", [])
            assert result == []

    @pytest.mark.asyncio
    async def test_past_tense_filtered_by_llm(self):
        """Past-tense statements should be filtered by prompt instructions."""
        commitments_json = json.dumps({
            "commitments": [
                {"speaker": "Eyal", "commitment_text": "Will send deck tomorrow", "context": "Planning", "implied_deadline": "tomorrow"},
            ]
        })

        with patch("processors.cross_reference.call_llm") as mock_llm:
            mock_llm.return_value = (commitments_json, {"input_tokens": 100, "output_tokens": 50, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0})

            result = await extract_commitments("meeting-123", "I sent the deck. Will send deck tomorrow.", [])
            assert len(result) == 1
            assert "Will send" in result[0]["commitment_text"]


# =========================================================================
# Test check_commitment_fulfillment
# =========================================================================

class TestCheckCommitmentFulfillment:
    """Tests for commitment fulfillment detection."""

    @pytest.mark.asyncio
    async def test_no_open_commitments(self):
        """No open commitments should return empty list."""
        with patch("processors.cross_reference.supabase_client") as mock_db:
            mock_db.get_commitments.return_value = []
            result = await check_commitment_fulfillment("meeting-123", "transcript")
            assert result == []

    @pytest.mark.asyncio
    async def test_explicit_fulfillment(self):
        """Should detect explicit fulfillment."""
        fulfilled_json = json.dumps({
            "fulfilled": [
                {"commitment_id": "c1", "evidence": "I sent the deck yesterday", "confidence": "high"},
            ]
        })

        with patch("processors.cross_reference.supabase_client") as mock_db, \
             patch("processors.cross_reference.call_llm") as mock_llm:
            mock_db.get_commitments.return_value = [
                {"id": "c1", "speaker": "Eyal", "commitment_text": "Send investor deck", "implied_deadline": "Friday"},
            ]
            mock_llm.return_value = (fulfilled_json, {"input_tokens": 100, "output_tokens": 50, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0})

            result = await check_commitment_fulfillment("meeting-456", "I sent the deck yesterday")
            assert len(result) == 1
            assert result[0]["commitment_id"] == "c1"
            assert result[0]["confidence"] == "high"

    @pytest.mark.asyncio
    async def test_no_fulfillment_found(self):
        """Should return empty when nothing fulfilled."""
        with patch("processors.cross_reference.supabase_client") as mock_db, \
             patch("processors.cross_reference.call_llm") as mock_llm:
            mock_db.get_commitments.return_value = [
                {"id": "c1", "speaker": "Eyal", "commitment_text": "Send deck", "implied_deadline": "Friday"},
            ]
            mock_llm.return_value = ('{"fulfilled": []}', {"input_tokens": 50, "output_tokens": 10, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0})

            result = await check_commitment_fulfillment("meeting-456", "unrelated chat")
            assert result == []

    @pytest.mark.asyncio
    async def test_invalid_commitment_id_filtered(self):
        """Fulfillment with unknown commitment_id should be filtered out."""
        fulfilled_json = json.dumps({
            "fulfilled": [
                {"commitment_id": "bad-id", "evidence": "done", "confidence": "high"},
            ]
        })

        with patch("processors.cross_reference.supabase_client") as mock_db, \
             patch("processors.cross_reference.call_llm") as mock_llm:
            mock_db.get_commitments.return_value = [
                {"id": "c1", "speaker": "Eyal", "commitment_text": "Send deck", "implied_deadline": "Friday"},
            ]
            mock_llm.return_value = (fulfilled_json, {"input_tokens": 100, "output_tokens": 50, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0})

            result = await check_commitment_fulfillment("meeting-456", "transcript")
            assert result == []


# =========================================================================
# Test Commitment CRUD
# =========================================================================

class TestCommitmentCRUD:
    """Test supabase_client commitment methods with mocked client."""

    def _make_client(self):
        from services.supabase_client import SupabaseClient
        client = SupabaseClient()
        mock = MagicMock()
        object.__setattr__(client, "_client", mock)
        return client, mock

    def test_create_commitment(self):
        """Should insert commitment record."""
        db, mock = self._make_client()
        mock.table.return_value.insert.return_value.execute.return_value = MagicMock(
            data=[{"id": "c1", "speaker": "Eyal", "commitment_text": "Send deck", "status": "open"}]
        )
        result = db.create_commitment("m1", "Eyal", "Send deck")
        assert result["status"] == "open"
        mock.table.assert_called_with("commitments")

    def test_create_commitments_batch(self):
        """Should batch-insert commitments."""
        db, mock = self._make_client()
        mock.table.return_value.insert.return_value.execute.return_value = MagicMock(
            data=[{"id": "c1"}]
        )
        result = db.create_commitments_batch("m1", [
            {"speaker": "Eyal", "commitment_text": "Send deck"},
            {"speaker": "Paolo", "commitment_text": "Call Lavazza"},
        ])
        assert len(result) == 2

    def test_get_commitments_filter_by_speaker(self):
        """Should filter by speaker."""
        db, mock = self._make_client()
        mock.table.return_value.select.return_value.ilike.return_value.order.return_value.limit.return_value.execute.return_value = MagicMock(
            data=[{"id": "c1", "speaker": "Eyal", "commitment_text": "Send deck"}]
        )
        result = db.get_commitments(speaker="Eyal")
        assert len(result) == 1

    def test_get_commitments_filter_by_status(self):
        """Should filter by status."""
        db, mock = self._make_client()
        mock.table.return_value.select.return_value.eq.return_value.order.return_value.limit.return_value.execute.return_value = MagicMock(
            data=[{"id": "c1", "status": "open"}]
        )
        result = db.get_commitments(status="open")
        assert len(result) == 1

    def test_fulfill_commitment(self):
        """Should update status to fulfilled."""
        db, mock = self._make_client()
        mock.table.return_value.update.return_value.eq.return_value.execute.return_value = MagicMock(
            data=[{"id": "c1", "status": "fulfilled", "evidence": "sent it"}]
        )
        result = db.fulfill_commitment("c1", evidence="sent it")
        assert result["status"] == "fulfilled"


# =========================================================================
# Test Weekly Digest Scorecard
# =========================================================================

class TestCommitmentScorecard:
    """Test commitment scorecard in weekly digest."""

    @pytest.mark.asyncio
    async def test_scorecard_with_commitments(self):
        """Should count open and fulfilled commitments by speaker."""
        with patch("processors.weekly_digest.supabase_client") as mock_db:
            mock_db.get_commitments.side_effect = lambda status=None: (
                [
                    {"speaker": "Eyal", "commitment_text": "Send deck"},
                    {"speaker": "Eyal", "commitment_text": "Call investors"},
                    {"speaker": "Paolo", "commitment_text": "Meet Lavazza"},
                ] if status == "open" else
                [
                    {"speaker": "Eyal", "commitment_text": "Finished legal review"},
                ] if status == "fulfilled" else []
            )

            result = await get_commitment_scorecard()
            assert result["open_count"] == 3
            assert result["fulfilled_count"] == 1
            assert len(result["open_by_speaker"]["Eyal"]) == 2
            assert len(result["open_by_speaker"]["Paolo"]) == 1

    @pytest.mark.asyncio
    async def test_scorecard_empty(self):
        """Empty commitments should return zero counts."""
        with patch("processors.weekly_digest.supabase_client") as mock_db:
            mock_db.get_commitments.return_value = []

            result = await get_commitment_scorecard()
            assert result["open_count"] == 0
            assert result["fulfilled_count"] == 0
            assert result["open_by_speaker"] == {}

    def test_digest_includes_commitment_section(self):
        """Digest document should include commitment scorecard section."""
        scorecard = {
            "open_count": 3,
            "fulfilled_count": 1,
            "open_by_speaker": {
                "Eyal": ["Send deck", "Call investors"],
                "Paolo": ["Meet Lavazza"],
            },
        }

        digest = format_digest_document(
            week_of="2026-02-23",
            meetings=[],
            decisions=[],
            tasks_completed=[],
            tasks_overdue=[],
            tasks_upcoming=[],
            open_questions=[],
            upcoming_meetings=[],
            commitment_scorecard=scorecard,
        )

        assert "Commitment Scorecard" in digest
        assert "3** open" in digest
        assert "1** fulfilled" in digest
        assert "Eyal" in digest
        assert "Send deck" in digest


# =========================================================================
# Test Opus piggyback (v0.4 — pre-extracted commitments)
# =========================================================================

class TestPreExtractedCommitments:
    """Tests for commitment extraction via Opus piggyback."""

    @pytest.mark.asyncio
    async def test_pre_extracted_commitments_skip_haiku_call(self):
        """When pre_extracted_commitments are provided, call_llm should not be called for extraction."""
        pre_extracted = [
            {"speaker": "Eyal", "commitment_text": "Send deck by Friday", "context": "Fundraising", "implied_deadline": "Friday"},
            {"speaker": "Paolo", "commitment_text": "Set up Lavazza meeting", "context": "BD", "implied_deadline": "next week"},
        ]

        with (
            patch("processors.cross_reference.call_llm") as mock_llm,
            patch("processors.cross_reference.supabase_client") as mock_db,
            patch("processors.cross_reference.deduplicate_tasks", new_callable=AsyncMock) as mock_dedup,
            patch("processors.cross_reference.infer_task_status_changes", new_callable=AsyncMock) as mock_status,
            patch("processors.cross_reference.resolve_open_questions", new_callable=AsyncMock) as mock_resolve,
            patch("processors.cross_reference.check_commitment_fulfillment", new_callable=AsyncMock) as mock_fulfill,
        ):
            mock_dedup.return_value = {"new_tasks": [], "duplicates": [], "updates": []}
            mock_status.return_value = []
            mock_resolve.return_value = []
            mock_fulfill.return_value = []
            mock_db.create_commitments_batch = MagicMock(return_value=[])
            mock_db.create_task_mentions_batch = MagicMock(return_value=[])

            result = await run_cross_reference(
                meeting_id="opus-001",
                transcript="Full transcript text",
                new_tasks=[],
                pre_extracted_commitments=pre_extracted,
            )

            # call_llm should NOT have been called (pre-extracted skips extraction)
            mock_llm.assert_not_called()

            # Commitments batch should have been called with the pre-extracted ones
            mock_db.create_commitments_batch.assert_called_once_with("opus-001", pre_extracted)

    @pytest.mark.asyncio
    async def test_fallback_when_pre_extracted_is_none(self):
        """When pre_extracted_commitments is None, should fall back to Haiku extraction."""
        commitments_json = json.dumps({
            "commitments": [
                {"speaker": "Eyal", "commitment_text": "Review draft", "context": "Editing", "implied_deadline": "none"},
            ]
        })

        with (
            patch("processors.cross_reference.call_llm") as mock_llm,
            patch("processors.cross_reference.supabase_client") as mock_db,
            patch("processors.cross_reference.deduplicate_tasks", new_callable=AsyncMock) as mock_dedup,
            patch("processors.cross_reference.infer_task_status_changes", new_callable=AsyncMock) as mock_status,
            patch("processors.cross_reference.resolve_open_questions", new_callable=AsyncMock) as mock_resolve,
            patch("processors.cross_reference.check_commitment_fulfillment", new_callable=AsyncMock) as mock_fulfill,
        ):
            mock_dedup.return_value = {"new_tasks": [], "duplicates": [], "updates": []}
            mock_status.return_value = []
            mock_resolve.return_value = []
            mock_fulfill.return_value = []
            mock_db.create_commitments_batch = MagicMock(return_value=[])
            mock_db.create_task_mentions_batch = MagicMock(return_value=[])

            mock_llm.return_value = (commitments_json, {"input_tokens": 100, "output_tokens": 50, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0})

            result = await run_cross_reference(
                meeting_id="fallback-001",
                transcript="Some transcript",
                new_tasks=[],
                pre_extracted_commitments=None,
            )

            # call_llm SHOULD have been called (Haiku fallback for commitments)
            mock_llm.assert_called()

    @pytest.mark.asyncio
    async def test_empty_pre_extracted_list_uses_fallback(self):
        """Empty list [] should still use fallback (only None skips extraction)."""
        commitments_json = json.dumps({"commitments": []})

        with (
            patch("processors.cross_reference.call_llm") as mock_llm,
            patch("processors.cross_reference.supabase_client") as mock_db,
            patch("processors.cross_reference.deduplicate_tasks", new_callable=AsyncMock) as mock_dedup,
            patch("processors.cross_reference.infer_task_status_changes", new_callable=AsyncMock) as mock_status,
            patch("processors.cross_reference.resolve_open_questions", new_callable=AsyncMock) as mock_resolve,
            patch("processors.cross_reference.check_commitment_fulfillment", new_callable=AsyncMock) as mock_fulfill,
        ):
            mock_dedup.return_value = {"new_tasks": [], "duplicates": [], "updates": []}
            mock_status.return_value = []
            mock_resolve.return_value = []
            mock_fulfill.return_value = []
            mock_db.create_commitments_batch = MagicMock(return_value=[])
            mock_db.create_task_mentions_batch = MagicMock(return_value=[])

            mock_llm.return_value = (commitments_json, {"input_tokens": 50, "output_tokens": 10, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0})

            result = await run_cross_reference(
                meeting_id="empty-001",
                transcript="Some transcript",
                new_tasks=[],
                pre_extracted_commitments=[],  # Empty list — truthy check is falsy
            )

            # Empty list is falsy, so fallback to Haiku should occur
            mock_llm.assert_called()
