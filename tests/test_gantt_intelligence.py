"""
Tests for Gantt intelligence — metrics computation and Now-Next-Later view.

Tests verify that compute_gantt_metrics() correctly reads the items/status
structure from get_gantt_status(), and that milestone risk and NNL bucketing
use the proper week calculations.
"""

import pytest
from unittest.mock import AsyncMock, patch


# =============================================================================
# compute_gantt_metrics
# =============================================================================

class TestComputeGanttMetrics:
    """Tests for compute_gantt_metrics()."""

    @pytest.mark.asyncio
    async def test_uses_items_key_not_sections(self):
        """Should read from 'items' key, not 'sections'."""
        mock_status = {
            "week": 13,
            "items": [
                {"section": "Product", "subsection": "V1 SOW", "status": "active", "text": "[R] Build API"},
                {"section": "Product", "subsection": "Testing", "status": "completed", "text": "[R] Unit tests done"},
                {"section": "Sales", "subsection": "Outreach", "status": "blocked", "text": "[P] Waiting on legal"},
            ],
            "count": 3,
        }
        mock_horizon = {"current_week": 13, "milestones": [], "count": 0}

        with patch("services.gantt_manager.gantt_manager", autospec=False) as mock_gm:
            mock_gm.get_gantt_status = AsyncMock(return_value=mock_status)
            mock_gm.get_gantt_horizon = AsyncMock(return_value=mock_horizon)

            from processors.gantt_intelligence import compute_gantt_metrics
            result = await compute_gantt_metrics()

            assert result["velocity"]["total_cells"] == 3
            assert result["velocity"]["active"] == 1
            assert result["velocity"]["completed"] == 1
            assert result["velocity"]["blocked"] == 1

    @pytest.mark.asyncio
    async def test_slippage_ratio(self):
        """Slippage ratio = blocked / total."""
        mock_status = {
            "week": 13,
            "items": [
                {"status": "active", "text": "a"},
                {"status": "active", "text": "b"},
                {"status": "active", "text": "c"},
                {"status": "active", "text": "d"},
                {"status": "blocked", "text": "e"},
            ],
            "count": 5,
        }
        mock_horizon = {"current_week": 13, "milestones": [], "count": 0}

        with patch("services.gantt_manager.gantt_manager", autospec=False) as mock_gm:
            mock_gm.get_gantt_status = AsyncMock(return_value=mock_status)
            mock_gm.get_gantt_horizon = AsyncMock(return_value=mock_horizon)

            from processors.gantt_intelligence import compute_gantt_metrics
            result = await compute_gantt_metrics()

            assert result["slippage_ratio"] == 0.2

    @pytest.mark.asyncio
    async def test_empty_items_returns_zeros(self):
        """Empty Gantt should return zero velocity, not crash."""
        mock_status = {"week": 13, "items": [], "count": 0}
        mock_horizon = {"current_week": 13, "milestones": [], "count": 0}

        with patch("services.gantt_manager.gantt_manager", autospec=False) as mock_gm:
            mock_gm.get_gantt_status = AsyncMock(return_value=mock_status)
            mock_gm.get_gantt_horizon = AsyncMock(return_value=mock_horizon)

            from processors.gantt_intelligence import compute_gantt_metrics
            result = await compute_gantt_metrics()

            assert result["velocity"]["total_cells"] == 0
            assert result["slippage_ratio"] == 0.0

    @pytest.mark.asyncio
    async def test_gantt_error_returns_summary(self):
        """Gantt read error should return summary, not crash."""
        mock_status = {"error": "Broken pipe", "week": 13}
        mock_horizon = {"current_week": 13, "milestones": [], "count": 0}

        with patch("services.gantt_manager.gantt_manager", autospec=False) as mock_gm:
            mock_gm.get_gantt_status = AsyncMock(return_value=mock_status)
            mock_gm.get_gantt_horizon = AsyncMock(return_value=mock_horizon)

            from processors.gantt_intelligence import compute_gantt_metrics
            result = await compute_gantt_metrics()

            assert "unavailable" in result["summary"].lower()

    @pytest.mark.asyncio
    async def test_milestone_risks_computes_weeks_away(self):
        """Milestone risk should compute weeks_away = ms.week - current_week."""
        mock_status = {"week": 13, "items": [{"status": "active", "text": "x"}], "count": 1}
        mock_horizon = {
            "current_week": 13,
            "milestones": [
                {"section": "Product", "subsection": "Launch", "text": "Product launch", "week": 14, "type": "milestone"},
                {"section": "Funding", "subsection": "Close", "text": "Round close", "week": 16, "type": "milestone"},
                {"section": "Sales", "subsection": "Target", "text": "100 clients", "week": 25, "type": "milestone"},
            ],
            "count": 3,
        }

        with patch("services.gantt_manager.gantt_manager", autospec=False) as mock_gm:
            mock_gm.get_gantt_status = AsyncMock(return_value=mock_status)
            mock_gm.get_gantt_horizon = AsyncMock(return_value=mock_horizon)

            from processors.gantt_intelligence import compute_gantt_metrics
            result = await compute_gantt_metrics()

            # Only milestones within 4 weeks should be included
            assert len(result["milestone_risks"]) == 2
            assert result["milestone_risks"][0]["weeks_away"] == 1
            assert result["milestone_risks"][0]["milestone"] == "Product launch"
            assert result["milestone_risks"][1]["weeks_away"] == 3


# =============================================================================
# generate_now_next_later
# =============================================================================

class TestGenerateNowNextLater:
    """Tests for generate_now_next_later()."""

    @pytest.mark.asyncio
    async def test_bucketing_by_weeks_away(self):
        """Items should be bucketed: now (<=2), next (3-6), later (7+)."""
        mock_horizon = {
            "current_week": 13,
            "milestones": [
                {"section": "A", "text": "Now item", "week": 14, "owner": "R", "type": "milestone"},
                {"section": "B", "text": "Next item", "week": 17, "owner": "E", "type": "milestone"},
                {"section": "C", "text": "Later item", "week": 22, "owner": "P", "type": "milestone"},
            ],
            "count": 3,
        }

        with patch("services.gantt_manager.gantt_manager", autospec=False) as mock_gm:
            mock_gm.get_gantt_horizon = AsyncMock(return_value=mock_horizon)

            from processors.gantt_intelligence import generate_now_next_later
            result = await generate_now_next_later()

            assert len(result["now"]) == 1
            assert result["now"][0]["label"] == "Now item"
            assert len(result["next"]) == 1
            assert result["next"][0]["label"] == "Next item"
            assert len(result["later"]) == 1
            assert result["later"][0]["label"] == "Later item"

    @pytest.mark.asyncio
    async def test_empty_milestones(self):
        """No milestones should return empty buckets."""
        mock_horizon = {"current_week": 13, "milestones": [], "count": 0}

        with patch("services.gantt_manager.gantt_manager", autospec=False) as mock_gm:
            mock_gm.get_gantt_horizon = AsyncMock(return_value=mock_horizon)

            from processors.gantt_intelligence import generate_now_next_later
            result = await generate_now_next_later()

            assert result["now"] == []
            assert result["next"] == []
            assert result["later"] == []


# =============================================================================
# Retry logic (google_sheets._execute_with_retry)
# =============================================================================

class TestSheetsRetry:
    """Tests for _execute_with_retry in GoogleSheetsService."""

    def test_succeeds_first_try(self):
        """Should return result on first successful call."""
        from services.google_sheets import GoogleSheetsService

        svc = GoogleSheetsService()
        svc._credentials = None  # Skip credential check

        mock_request = type("MockReq", (), {"execute": lambda self: {"values": [["a"]]}})()
        result = svc._execute_with_retry(mock_request)
        assert result == {"values": [["a"]]}

    def test_retries_on_connection_error(self):
        """Should retry on ConnectionError and succeed."""
        from services.google_sheets import GoogleSheetsService

        svc = GoogleSheetsService()
        svc._credentials = None

        call_count = 0

        def mock_execute(self_inner):
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                raise ConnectionError("Broken pipe")
            return {"values": []}

        mock_request = type("MockReq", (), {"execute": mock_execute})()
        result = svc._execute_with_retry(mock_request, base_delay=0.01)
        assert result == {"values": []}
        assert call_count == 3

    def test_does_not_retry_on_value_error(self):
        """Non-transient errors should not be retried."""
        from services.google_sheets import GoogleSheetsService

        svc = GoogleSheetsService()
        svc._credentials = None

        mock_request = type("MockReq", (), {
            "execute": lambda self: (_ for _ in ()).throw(ValueError("bad input"))
        })()

        with pytest.raises(ValueError):
            svc._execute_with_retry(mock_request)
