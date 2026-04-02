"""Tests for the intelligence signal context builder."""

import pytest
from datetime import datetime, timezone
from unittest.mock import patch, MagicMock

from processors.intelligence_signal_context import (
    build_context_packet,
    build_research_queries,
    build_exploration_queries,
    DEFAULT_ACTIVE_CROPS,
    DEFAULT_ACTIVE_REGIONS,
)


@pytest.fixture
def mock_supabase():
    with patch("processors.intelligence_signal_context.supabase_client") as mock:
        # Default: return empty for all calls
        mock.client.table.return_value.select.return_value.eq.return_value.order.return_value.execute.return_value = MagicMock(data=[])
        mock.client.table.return_value.select.return_value.eq.return_value.order.return_value.limit.return_value.execute.return_value = MagicMock(data=[])
        mock.client.table.return_value.select.return_value.gte.return_value.order.return_value.limit.return_value.execute.return_value = MagicMock(data=[])
        mock.client.table.return_value.select.return_value.gte.return_value.limit.return_value.execute.return_value = MagicMock(data=[])
        mock.client.table.return_value.select.return_value.not_.is_.return_value.order.return_value.limit.return_value.execute.return_value = MagicMock(data=[])
        mock.get_tasks.return_value = []
        yield mock


class TestBuildContextPacket:
    def test_returns_required_keys(self, mock_supabase):
        context = build_context_packet()

        assert "week_number" in context
        assert "year" in context
        assert "signal_id" in context
        assert "active_crops" in context
        assert "active_regions" in context
        assert "known_competitors" in context
        assert "last_signal_flags" in context
        assert "open_tasks_summary" in context

    def test_signal_id_format(self, mock_supabase):
        context = build_context_packet()

        assert context["signal_id"].startswith("signal-w")
        assert str(context["year"]) in context["signal_id"]

    def test_uses_default_crops_when_empty(self, mock_supabase):
        context = build_context_packet()

        assert context["active_crops"] == DEFAULT_ACTIVE_CROPS

    def test_uses_default_regions_when_empty(self, mock_supabase):
        context = build_context_packet()

        assert context["active_regions"] == DEFAULT_ACTIVE_REGIONS

    def test_populates_competitors_from_watchlist(self, mock_supabase):
        mock_supabase.client.table.return_value.select.return_value.eq.return_value.order.return_value.execute.return_value = MagicMock(
            data=[
                {"name": "SatYield", "category": "known", "funding": "$6M", "target_customer": "Traders", "key_limitation": "Pre-rev"},
                {"name": "EOSDA", "category": "known", "funding": "$355K", "target_customer": "Farmers", "key_limitation": "Self-reported"},
            ]
        )

        context = build_context_packet()

        assert len(context["known_competitors"]) == 2
        names = [c["name"] for c in context["known_competitors"]]
        assert "SatYield" in names

    def test_populates_bd_pipeline(self, mock_supabase):
        mock_supabase.get_tasks.return_value = [
            {"title": "Follow up with Cargill", "priority": "H"},
        ]

        context = build_context_packet()

        assert len(context["active_bd_pipeline"]) == 1
        assert context["active_bd_pipeline"][0]["title"] == "Follow up with Cargill"

    def test_handles_supabase_errors_gracefully(self, mock_supabase):
        mock_supabase.client.table.side_effect = Exception("DB unavailable")
        mock_supabase.get_tasks.side_effect = Exception("DB unavailable")

        context = build_context_packet()

        # Should fall back to defaults, not crash
        assert context["known_competitors"] == []
        assert context["active_crops"] == DEFAULT_ACTIVE_CROPS


class TestBuildResearchQueries:
    def test_returns_core_sections(self, mock_supabase):
        context = build_context_packet()
        queries = build_research_queries(context)

        sections = [q["section"] for q in queries]
        assert "market_overview" in sections
        assert "competitor_landscape" in sections
        assert "science_tech" in sections
        assert "regulation_policy" in sections
        assert "customer_segment" in sections

    def test_includes_competitor_names(self, mock_supabase):
        context = build_context_packet()
        context["known_competitors"] = [
            {"name": "SatYield"},
            {"name": "EOSDA"},
        ]
        queries = build_research_queries(context)

        comp_query = next(q for q in queries if q["section"] == "competitor_landscape")
        assert "SatYield" in comp_query["query"]
        assert "EOSDA" in comp_query["query"]

    def test_each_query_has_system_prompt(self, mock_supabase):
        context = build_context_packet()
        queries = build_research_queries(context)

        for q in queries:
            assert "system_prompt" in q
            assert len(q["system_prompt"]) > 10

    def test_continuity_query_when_last_flags(self, mock_supabase):
        context = build_context_packet()
        context["last_signal_flags"] = [
            {"flag": "Brazil drought affecting coffee", "urgency": "high"},
        ]
        queries = build_research_queries(context)

        sections = [q["section"] for q in queries]
        assert "continuity" in sections

        cont_query = next(q for q in queries if q["section"] == "continuity")
        assert "Brazil drought" in cont_query["query"]

    def test_no_continuity_query_when_no_flags(self, mock_supabase):
        context = build_context_packet()
        context["last_signal_flags"] = []
        queries = build_research_queries(context)

        sections = [q["section"] for q in queries]
        assert "continuity" not in sections


class TestBuildExplorationQueries:
    def test_returns_2_queries_by_default(self):
        queries = build_exploration_queries(week_number=14)

        assert len(queries) == 2

    def test_returns_3_queries_every_3rd_week(self):
        queries = build_exploration_queries(week_number=12)  # 12 % 3 == 0

        assert len(queries) == 3
        sections = [q["section"] for q in queries]
        assert "exploration_geography" in sections

    def test_queries_rotate_weekly(self):
        q1 = build_exploration_queries(week_number=1)
        q2 = build_exploration_queries(week_number=2)

        # Different weeks should produce different queries
        assert q1[0]["query"] != q2[0]["query"]

    def test_each_query_has_required_keys(self):
        queries = build_exploration_queries(week_number=14)

        for q in queries:
            assert "section" in q
            assert "query" in q
            assert "system_prompt" in q
