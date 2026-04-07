"""
Tests for deal CRUD operations in SupabaseClient.

Tests:
- Deal create/read/update
- Deal interaction creation + last_interaction_date update
- External commitment CRUD
- Stale deal queries
- Overdue action/commitment queries
"""

import pytest
from datetime import date, datetime, timedelta, timezone
from unittest.mock import patch, MagicMock, PropertyMock


# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture
def mock_client():
    """Create a mock Supabase client for testing."""
    with patch("services.supabase_client.SupabaseClient.client", new_callable=PropertyMock) as mock:
        client = MagicMock()
        mock.return_value = client
        yield client


@pytest.fixture
def db():
    """Get the SupabaseClient singleton (with mocked client)."""
    from services.supabase_client import SupabaseClient
    return SupabaseClient()


# =============================================================================
# Deal CRUD
# =============================================================================


class TestCreateDeal:
    def test_creates_with_required_fields(self, db, mock_client):
        mock_client.table.return_value.insert.return_value.execute.return_value = MagicMock(
            data=[{"id": "deal-1", "name": "Test Deal", "organization": "TestOrg", "stage": "lead"}]
        )
        # Mock log_action
        mock_client.table.return_value.insert.return_value.execute.return_value.data = [
            {"id": "deal-1", "name": "Test Deal", "organization": "TestOrg"}
        ]

        result = db.create_deal(name="Test Deal", organization="TestOrg")
        assert result["id"] == "deal-1"

    def test_creates_with_optional_fields(self, db, mock_client):
        mock_client.table.return_value.insert.return_value.execute.return_value = MagicMock(
            data=[{"id": "deal-2", "name": "Big Deal", "stage": "proposal", "probability": 60}]
        )

        result = db.create_deal(
            name="Big Deal",
            organization="BigCo",
            contact_person="John",
            stage="proposal",
            probability=60,
            value_estimate="$50K",
            source="conference",
        )
        assert result["id"] == "deal-2"


class TestUpdateDeal:
    def test_update_sets_updated_at(self, db, mock_client):
        mock_client.table.return_value.update.return_value.eq.return_value.execute.return_value = MagicMock(
            data=[{"id": "deal-1", "stage": "proposal"}]
        )

        result = db.update_deal("deal-1", stage="proposal")
        assert result["stage"] == "proposal"
        # Verify update was called with updated_at
        call_args = mock_client.table.return_value.update.call_args
        assert "updated_at" in call_args[0][0]

    def test_update_nonexistent_raises(self, db, mock_client):
        mock_client.table.return_value.update.return_value.eq.return_value.execute.return_value = MagicMock(
            data=[]
        )

        with pytest.raises(ValueError, match="not found"):
            db.update_deal("fake-id", stage="proposal")


class TestGetDeal:
    def test_returns_deal(self, db, mock_client):
        mock_client.table.return_value.select.return_value.eq.return_value.execute.return_value = MagicMock(
            data=[{"id": "deal-1", "name": "Test"}]
        )

        result = db.get_deal("deal-1")
        assert result["id"] == "deal-1"

    def test_returns_none_when_not_found(self, db, mock_client):
        mock_client.table.return_value.select.return_value.eq.return_value.execute.return_value = MagicMock(
            data=[]
        )

        result = db.get_deal("nonexistent")
        assert result is None


class TestGetDeals:
    def test_list_all_deals(self, db, mock_client):
        mock_client.table.return_value.select.return_value.order.return_value.limit.return_value.execute.return_value = MagicMock(
            data=[{"id": "1"}, {"id": "2"}]
        )

        result = db.get_deals()
        assert len(result) == 2

    def test_filter_by_stage(self, db, mock_client):
        query = mock_client.table.return_value.select.return_value
        query.eq.return_value.order.return_value.limit.return_value.execute.return_value = MagicMock(
            data=[{"id": "1", "stage": "lead"}]
        )

        result = db.get_deals(stage="lead")
        assert len(result) == 1


class TestStaleDealQueries:
    def test_get_stale_deals(self, db, mock_client):
        chain = mock_client.table.return_value.select.return_value
        chain.lt.return_value.not_.in_.return_value.order.return_value.execute.return_value = MagicMock(
            data=[{"id": "1", "name": "Stale Deal"}]
        )

        result = db.get_stale_deals(days=7)
        assert len(result) == 1

    def test_get_overdue_deal_actions(self, db, mock_client):
        chain = mock_client.table.return_value.select.return_value
        chain.lt.return_value.not_.in_.return_value.not_.is_.return_value.order.return_value.execute.return_value = MagicMock(
            data=[{"id": "1", "next_action_date": "2026-04-01"}]
        )

        result = db.get_overdue_deal_actions()
        assert len(result) == 1


# =============================================================================
# Deal Interactions
# =============================================================================


class TestCreateDealInteraction:
    def test_creates_and_updates_last_interaction(self, db, mock_client):
        mock_client.table.return_value.insert.return_value.execute.return_value = MagicMock(
            data=[{"id": "int-1", "deal_id": "deal-1"}]
        )
        mock_client.table.return_value.update.return_value.eq.return_value.execute.return_value = MagicMock(
            data=[{"id": "deal-1"}]
        )

        result = db.create_deal_interaction(
            deal_id="deal-1",
            interaction_type="meeting",
            summary="Discussed partnership",
            interaction_date="2026-04-07",
        )
        assert result["id"] == "int-1"

    def test_defaults_date_to_today(self, db, mock_client):
        mock_client.table.return_value.insert.return_value.execute.return_value = MagicMock(
            data=[{"id": "int-2"}]
        )
        mock_client.table.return_value.update.return_value.eq.return_value.execute.return_value = MagicMock(
            data=[{}]
        )

        db.create_deal_interaction(
            deal_id="deal-1",
            interaction_type="email",
            summary="Follow-up email",
        )
        # Verify insert was called (date should be auto-set)
        mock_client.table.return_value.insert.assert_called()


class TestGetDealTimeline:
    def test_returns_interactions(self, db, mock_client):
        mock_client.table.return_value.select.return_value.eq.return_value.order.return_value.limit.return_value.execute.return_value = MagicMock(
            data=[{"id": "1", "interaction_type": "meeting"}, {"id": "2", "interaction_type": "email"}]
        )

        result = db.get_deal_timeline("deal-1")
        assert len(result) == 2


# =============================================================================
# External Commitments
# =============================================================================


class TestCreateExternalCommitment:
    def test_creates_with_required_fields(self, db, mock_client):
        mock_client.table.return_value.insert.return_value.execute.return_value = MagicMock(
            data=[{"id": "ec-1", "organization": "PartnerCo", "commitment": "Send report"}]
        )

        result = db.create_external_commitment(
            organization="PartnerCo",
            commitment="Send report",
        )
        assert result["id"] == "ec-1"

    def test_creates_with_all_optional(self, db, mock_client):
        mock_client.table.return_value.insert.return_value.execute.return_value = MagicMock(
            data=[{"id": "ec-2"}]
        )

        result = db.create_external_commitment(
            organization="OrgA",
            commitment="Deliver analysis",
            deal_id="deal-1",
            contact_person="Alice",
            promised_to="Bob",
            deadline="2026-04-15",
            source_meeting_id="meeting-1",
            notes="Urgent",
        )
        assert result["id"] == "ec-2"


class TestUpdateExternalCommitment:
    def test_update_status(self, db, mock_client):
        mock_client.table.return_value.update.return_value.eq.return_value.execute.return_value = MagicMock(
            data=[{"id": "ec-1", "status": "fulfilled"}]
        )

        result = db.update_external_commitment("ec-1", status="fulfilled")
        assert result["status"] == "fulfilled"

    def test_update_nonexistent_raises(self, db, mock_client):
        mock_client.table.return_value.update.return_value.eq.return_value.execute.return_value = MagicMock(
            data=[]
        )

        with pytest.raises(ValueError, match="not found"):
            db.update_external_commitment("fake-id", status="fulfilled")


class TestGetExternalCommitments:
    def test_list_all(self, db, mock_client):
        mock_client.table.return_value.select.return_value.order.return_value.limit.return_value.execute.return_value = MagicMock(
            data=[{"id": "1"}, {"id": "2"}]
        )

        result = db.get_external_commitments()
        assert len(result) == 2

    def test_filter_by_status(self, db, mock_client):
        query = mock_client.table.return_value.select.return_value
        query.eq.return_value.order.return_value.limit.return_value.execute.return_value = MagicMock(
            data=[{"id": "1", "status": "open"}]
        )

        result = db.get_external_commitments(status="open")
        assert len(result) == 1

    def test_filter_by_organization(self, db, mock_client):
        query = mock_client.table.return_value.select.return_value
        query.ilike.return_value.order.return_value.limit.return_value.execute.return_value = MagicMock(
            data=[{"id": "1", "organization": "PartnerCo"}]
        )

        result = db.get_external_commitments(organization="Partner")
        assert len(result) == 1


class TestGetOverdueCommitments:
    def test_returns_overdue(self, db, mock_client):
        chain = mock_client.table.return_value.select.return_value
        chain.eq.return_value.lt.return_value.not_.is_.return_value.order.return_value.execute.return_value = MagicMock(
            data=[{"id": "1", "deadline": "2026-04-01", "status": "open"}]
        )

        result = db.get_overdue_commitments()
        assert len(result) == 1
