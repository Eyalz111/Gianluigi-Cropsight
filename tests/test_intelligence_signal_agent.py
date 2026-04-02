"""Tests for the intelligence signal agent pipeline."""

import asyncio
import pytest
from datetime import datetime, timezone
from unittest.mock import patch, MagicMock, AsyncMock

from processors.intelligence_signal_agent import (
    generate_intelligence_signal,
    distribute_intelligence_signal,
    _extract_flags,
    _truncate_research_results,
    _update_competitor_watchlist,
    _weeks_since,
)


@pytest.fixture
def mock_supabase():
    with patch("processors.intelligence_signal_agent.supabase_client") as mock:
        mock.create_intelligence_signal.return_value = {"signal_id": "signal-w14-2026"}
        mock.update_intelligence_signal.return_value = {"signal_id": "signal-w14-2026"}
        mock.get_intelligence_signal.return_value = None
        mock.get_latest_intelligence_signal.return_value = None
        mock.get_competitor_watchlist.return_value = []
        mock.upsert_competitor.return_value = {}
        mock.deactivate_stale_competitors.return_value = 0
        mock.create_pending_approval.return_value = {"approval_id": "signal-w14-2026"}
        mock.log_action.return_value = {}
        yield mock


@pytest.fixture
def mock_context():
    with patch("processors.intelligence_signal_agent.build_context_packet") as mock:
        mock.return_value = {
            "week_number": 14,
            "year": 2026,
            "signal_id": "signal-w14-2026",
            "active_crops": ["wheat", "coffee"],
            "active_regions": ["EU", "Brazil"],
            "known_competitors": [{"name": "SatYield"}],
            "last_signal_flags": [],
            "active_bd_pipeline": [],
            "technical_focus": [],
            "open_tasks_summary": {},
        }
        yield mock


@pytest.fixture
def mock_research_queries():
    with patch("processors.intelligence_signal_agent.build_research_queries") as mock:
        mock.return_value = [
            {"section": "market_overview", "query": "test", "system_prompt": "test"},
        ]
        yield mock


@pytest.fixture
def mock_exploration_queries():
    with patch("processors.intelligence_signal_agent.build_exploration_queries") as mock:
        mock.return_value = [
            {"section": "exploration_adjacent", "query": "test", "system_prompt": "test"},
        ]
        yield mock


@pytest.fixture
def mock_perplexity():
    with patch("processors.intelligence_signal_agent.perplexity_client") as mock:
        mock.is_available.return_value = True
        result = MagicMock()
        result.success = True
        result.content = "Research results here."
        mock.search_batch = AsyncMock(return_value={
            "market_overview": result,
            "exploration_adjacent": result,
        })
        yield mock


@pytest.fixture
def mock_settings():
    with patch("processors.intelligence_signal_agent.settings") as mock:
        mock.model_extraction = "claude-opus-4-6"
        mock.model_agent = "claude-sonnet-4-6"
        mock.INTELLIGENCE_SIGNAL_AUTO_DISTRIBUTE = False
        mock.INTELLIGENCE_SIGNAL_VIDEO_ENABLED = False
        mock.intelligence_signal_recipients_list = ["eyal@example.com"]
        yield mock


class TestExtractFlags:
    def test_extracts_flags_from_content(self):
        content = """### FLAGS
**[FLAG]** Brazil drought threatens 15% of Arabica supply. (urgency: high)
**[FLAG]** SatYield closes Series A. (urgency: medium)

### The Problem, This Week
Content here."""

        flags = _extract_flags(content)

        assert len(flags) == 2
        assert flags[0]["flag"] == "Brazil drought threatens 15% of Arabica supply"
        assert flags[0]["urgency"] == "high"
        assert flags[1]["urgency"] == "medium"

    def test_no_flags_returns_empty(self):
        content = """### FLAGS
No flags this week.

### Content
Stuff."""

        flags = _extract_flags(content)

        assert flags == []

    def test_max_3_flags(self):
        content = """### FLAGS
**[FLAG]** Flag one. (urgency: high)
**[FLAG]** Flag two. (urgency: medium)
**[FLAG]** Flag three. (urgency: high)
**[FLAG]** Flag four. (urgency: medium)

### Content"""

        flags = _extract_flags(content)

        assert len(flags) == 3

    def test_handles_missing_flags_section(self):
        content = "### The Problem\nNo flags section."

        flags = _extract_flags(content)

        assert flags == []


class TestTruncateResearchResults:
    def test_truncates_long_content(self):
        results = {"section1": "x" * 5000}
        truncated = _truncate_research_results(results)

        assert len(truncated["section1"]) == 3000 + len("... [truncated]")
        assert truncated["section1"].endswith("... [truncated]")

    def test_preserves_short_content(self):
        results = {"section1": "Short content"}
        truncated = _truncate_research_results(results)

        assert truncated["section1"] == "Short content"

    def test_handles_empty_results(self):
        truncated = _truncate_research_results({})

        assert truncated == {}


class TestWeeksSince:
    def test_same_year(self):
        assert _weeks_since(10, 2026, 14, 2026) == 4

    def test_cross_year(self):
        assert _weeks_since(50, 2025, 2, 2026) == 4

    def test_same_week(self):
        assert _weeks_since(14, 2026, 14, 2026) == 0


class TestUpdateCompetitorWatchlist:
    def test_discovers_new_competitor(self):
        with patch("processors.intelligence_signal_agent.supabase_client") as mock_sc:
            mock_sc.get_competitor_watchlist.return_value = [
                {"name": "SatYield", "category": "known", "appearance_count": 5}
            ]
            mock_sc.upsert_competitor.return_value = {}
            mock_sc.deactivate_stale_competitors.return_value = 0

            research = {
                "competitor_landscape": "SatYield raised funding. NewCo launched a platform."
            }

            changes = _update_competitor_watchlist(research, 14, 2026)

            # SatYield was already known, NewCo is new
            assert "SatYield" not in changes["discovered"]

    def test_returns_empty_when_no_competitor_data(self):
        with patch("processors.intelligence_signal_agent.supabase_client") as mock_sc:
            mock_sc.get_competitor_watchlist.return_value = []
            mock_sc.deactivate_stale_competitors.return_value = 0

            changes = _update_competitor_watchlist({}, 14, 2026)

            assert changes["promoted"] == []
            assert changes["deactivated"] == []
            assert changes["discovered"] == []

    def test_handles_errors_gracefully(self):
        with patch("processors.intelligence_signal_agent.supabase_client") as mock_sc:
            mock_sc.get_competitor_watchlist.side_effect = Exception("DB error")

            changes = _update_competitor_watchlist(
                {"competitor_landscape": "test"}, 14, 2026
            )

            # Should not crash — returns empty changes
            assert changes["promoted"] == []


class TestGenerateIntelligenceSignal:
    @pytest.mark.asyncio
    async def test_full_pipeline_success(
        self,
        mock_supabase,
        mock_context,
        mock_research_queries,
        mock_exploration_queries,
        mock_perplexity,
        mock_settings,
    ):
        with patch("processors.intelligence_signal_agent.call_llm") as mock_llm:
            mock_llm.return_value = (
                "### FLAGS\n**[FLAG]** Test flag. (urgency: high)\n\n### Content\nBody.",
                {"input_tokens": 100, "output_tokens": 200},
            )

            mock_drive = AsyncMock()
            mock_drive.save_intelligence_signal = AsyncMock(return_value={
                "id": "doc-123",
                "webViewLink": "https://docs.google.com/doc/123",
            })

            mock_tg = AsyncMock()
            mock_tg.send_to_eyal = AsyncMock(return_value=True)

            with patch("services.google_drive.drive_service", mock_drive):
                with patch("services.telegram_bot.telegram_bot", mock_tg):
                    result = await generate_intelligence_signal()

        assert result["signal_id"] == "signal-w14-2026"
        assert result["status"] == "pending_approval"
        # Content should be saved before Drive upload
        update_calls = mock_supabase.update_intelligence_signal.call_args_list
        content_saved = any(
            "signal_content" in str(call) for call in update_calls
        )
        assert content_saved

    @pytest.mark.asyncio
    async def test_research_failure_sets_error(
        self,
        mock_supabase,
        mock_context,
        mock_research_queries,
        mock_exploration_queries,
        mock_settings,
    ):
        with patch("processors.intelligence_signal_agent.perplexity_client") as mock_pp:
            mock_pp.is_available.return_value = False

            with patch("processors.intelligence_signal_agent._claude_search_fallback", new_callable=AsyncMock) as mock_fallback:
                mock_fallback.return_value = {}

                result = await generate_intelligence_signal()

        assert result["status"] == "error"
        assert "research_failed" in result.get("error", "")

    @pytest.mark.asyncio
    async def test_synthesis_timeout_sets_error(
        self,
        mock_supabase,
        mock_context,
        mock_research_queries,
        mock_exploration_queries,
        mock_perplexity,
        mock_settings,
    ):
        # Patch _synthesize_report to raise timeout
        with patch(
            "processors.intelligence_signal_agent._synthesize_report",
            new_callable=AsyncMock,
        ) as mock_synth:
            mock_synth.side_effect = RuntimeError("Opus synthesis timed out after 120s")

            mock_tg = AsyncMock()
            mock_tg.send_to_eyal = AsyncMock(return_value=True)

            with patch("services.telegram_bot.telegram_bot", mock_tg):
                result = await generate_intelligence_signal()

        assert result["status"] == "error"


class TestDistributeIntelligenceSignal:
    @pytest.mark.asyncio
    async def test_distributes_via_email(self):
        with patch("processors.intelligence_signal_agent.supabase_client") as mock_sc:
            mock_sc.get_intelligence_signal.return_value = {
                "signal_id": "signal-w14-2026",
                "signal_content": "Full report here.",
                "flags": [{"flag": "Test", "urgency": "high"}],
                "drive_doc_url": "https://example.com/doc",
                "week_number": 14,
                "year": 2026,
            }
            mock_sc.update_intelligence_signal.return_value = {}
            mock_sc.log_action.return_value = {}

            with patch("processors.intelligence_signal_agent.settings") as mock_s:
                mock_s.intelligence_signal_recipients_list = [
                    "eyal@cropsight.com"
                ]

                mock_gmail = AsyncMock()
                mock_gmail.send_email = AsyncMock(return_value=True)

                mock_tg = AsyncMock()
                mock_tg.send_to_eyal = AsyncMock(return_value=True)

                with patch("services.gmail.gmail_service", mock_gmail):
                    with patch("services.telegram_bot.telegram_bot", mock_tg):
                        result = await distribute_intelligence_signal(
                            "signal-w14-2026"
                        )

        assert result["status"] == "distributed"
        assert "eyal@cropsight.com" in result["recipients"]

    @pytest.mark.asyncio
    async def test_missing_signal_returns_error(self):
        with patch("processors.intelligence_signal_agent.supabase_client") as mock_sc:
            mock_sc.get_intelligence_signal.return_value = None

            result = await distribute_intelligence_signal("nonexistent")

        assert result["status"] == "error"

    @pytest.mark.asyncio
    async def test_no_recipients_returns_error(self):
        with patch("processors.intelligence_signal_agent.supabase_client") as mock_sc:
            mock_sc.get_intelligence_signal.return_value = {
                "signal_id": "signal-w14-2026",
                "signal_content": "Content.",
                "flags": [],
                "drive_doc_url": "",
                "week_number": 14,
                "year": 2026,
            }

            with patch("processors.intelligence_signal_agent.settings") as mock_s:
                mock_s.intelligence_signal_recipients_list = []

                result = await distribute_intelligence_signal("signal-w14-2026")

        assert result["status"] == "error"
        assert "recipients" in result["error"].lower()
