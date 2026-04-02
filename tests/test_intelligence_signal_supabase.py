"""Tests for intelligence signal Supabase client methods."""

import pytest
from unittest.mock import patch, MagicMock, PropertyMock

from services.supabase_client import SupabaseClient


@pytest.fixture
def mock_client():
    """Create a SupabaseClient with a mocked inner client."""
    with patch.object(SupabaseClient, "client", new_callable=PropertyMock) as mock_prop:
        mock_inner = MagicMock()
        mock_prop.return_value = mock_inner
        sc = SupabaseClient()
        yield sc, mock_inner


class TestCreateIntelligenceSignal:
    def test_creates_record(self, mock_client):
        sc, mock_inner = mock_client
        mock_inner.table.return_value.insert.return_value.execute.return_value = MagicMock(
            data=[{"signal_id": "signal-w14-2026", "status": "generating"}]
        )

        result = sc.create_intelligence_signal({
            "signal_id": "signal-w14-2026",
            "week_number": 14,
            "year": 2026,
        })

        assert result["signal_id"] == "signal-w14-2026"
        mock_inner.table.assert_called_with("intelligence_signals")


class TestUpdateIntelligenceSignal:
    def test_updates_record(self, mock_client):
        sc, mock_inner = mock_client
        mock_inner.table.return_value.update.return_value.eq.return_value.execute.return_value = MagicMock(
            data=[{"signal_id": "signal-w14-2026", "status": "pending_approval"}]
        )

        result = sc.update_intelligence_signal(
            "signal-w14-2026",
            {"status": "pending_approval"},
        )

        assert result["status"] == "pending_approval"

    def test_returns_empty_when_not_found(self, mock_client):
        sc, mock_inner = mock_client
        mock_inner.table.return_value.update.return_value.eq.return_value.execute.return_value = MagicMock(
            data=[]
        )

        result = sc.update_intelligence_signal("nonexistent", {"status": "error"})

        assert result == {}


class TestGetIntelligenceSignal:
    def test_returns_signal(self, mock_client):
        sc, mock_inner = mock_client
        mock_inner.table.return_value.select.return_value.eq.return_value.execute.return_value = MagicMock(
            data=[{"signal_id": "signal-w14-2026"}]
        )

        result = sc.get_intelligence_signal("signal-w14-2026")

        assert result["signal_id"] == "signal-w14-2026"

    def test_returns_none_when_not_found(self, mock_client):
        sc, mock_inner = mock_client
        mock_inner.table.return_value.select.return_value.eq.return_value.execute.return_value = MagicMock(
            data=[]
        )

        result = sc.get_intelligence_signal("nonexistent")

        assert result is None


class TestGetLatestIntelligenceSignal:
    def test_returns_latest(self, mock_client):
        sc, mock_inner = mock_client
        mock_inner.table.return_value.select.return_value.order.return_value.limit.return_value.execute.return_value = MagicMock(
            data=[{"signal_id": "signal-w14-2026", "status": "distributed"}]
        )

        result = sc.get_latest_intelligence_signal()

        assert result["signal_id"] == "signal-w14-2026"

    def test_returns_none_when_empty(self, mock_client):
        sc, mock_inner = mock_client
        mock_inner.table.return_value.select.return_value.order.return_value.limit.return_value.execute.return_value = MagicMock(
            data=[]
        )

        result = sc.get_latest_intelligence_signal()

        assert result is None


class TestGetIntelligenceSignals:
    def test_returns_list(self, mock_client):
        sc, mock_inner = mock_client
        mock_inner.table.return_value.select.return_value.order.return_value.limit.return_value.execute.return_value = MagicMock(
            data=[
                {"signal_id": "signal-w14-2026"},
                {"signal_id": "signal-w13-2026"},
            ]
        )

        result = sc.get_intelligence_signals(limit=4)

        assert len(result) == 2


class TestGetCompetitorWatchlist:
    def test_returns_active_only(self, mock_client):
        sc, mock_inner = mock_client
        mock_inner.table.return_value.select.return_value.eq.return_value.order.return_value.execute.return_value = MagicMock(
            data=[{"name": "SatYield", "is_active": True}]
        )

        result = sc.get_competitor_watchlist(include_deactivated=False)

        assert len(result) == 1

    def test_includes_deactivated(self, mock_client):
        sc, mock_inner = mock_client
        mock_inner.table.return_value.select.return_value.order.return_value.execute.return_value = MagicMock(
            data=[
                {"name": "SatYield", "is_active": True},
                {"name": "OldCo", "is_active": False},
            ]
        )

        result = sc.get_competitor_watchlist(include_deactivated=True)

        assert len(result) == 2


class TestUpsertCompetitor:
    def test_upserts_competitor(self, mock_client):
        sc, mock_inner = mock_client
        mock_inner.table.return_value.upsert.return_value.execute.return_value = MagicMock(
            data=[{"name": "NewCo", "category": "discovered"}]
        )

        result = sc.upsert_competitor({"name": "NewCo", "category": "discovered"})

        assert result["name"] == "NewCo"


class TestDeactivateStaleCompetitors:
    def test_deactivates_stale(self, mock_client):
        sc, mock_inner = mock_client

        # Return competitors that are stale
        mock_inner.table.return_value.select.return_value.eq.return_value.not_.is_.return_value.execute.return_value = MagicMock(
            data=[
                {"id": "123", "name": "OldCo", "last_seen_week": 5, "last_seen_year": 2026},
            ]
        )
        mock_inner.table.return_value.update.return_value.eq.return_value.execute.return_value = MagicMock(data=[])

        result = sc.deactivate_stale_competitors(weeks_threshold=4)

        assert result == 1

    def test_no_stale_competitors(self, mock_client):
        sc, mock_inner = mock_client
        mock_inner.table.return_value.select.return_value.eq.return_value.not_.is_.return_value.execute.return_value = MagicMock(
            data=[]
        )

        result = sc.deactivate_stale_competitors()

        assert result == 0
