"""
Tests for commitment scorecard in weekly digest.

Note: Commitment extraction and fulfillment functions were removed in Phase 10
(commitments merged into tasks). Only the weekly digest scorecard remains.
"""

import pytest
from unittest.mock import patch

from processors.weekly_digest import (
    get_commitment_scorecard,
    format_digest_document,
)


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
