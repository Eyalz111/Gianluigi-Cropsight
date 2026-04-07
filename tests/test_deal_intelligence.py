"""
Tests for deal intelligence — Phase 4.

Tests:
- Deal signal detection from transcripts
- Deal pulse generation for morning brief
- Commitments due generation
- Formatting helpers
"""

import pytest
from datetime import date, timedelta
from unittest.mock import patch, MagicMock

from processors.deal_intelligence import (
    detect_deal_signals,
    generate_deal_pulse,
    generate_commitments_due,
    format_deal_pulse_for_brief,
    format_commitments_for_brief,
    auto_create_deal_interaction,
    DEAL_SIGNAL_KEYWORDS,
    COMMITMENT_SIGNAL_KEYWORDS,
)


# =============================================================================
# Deal Signal Detection
# =============================================================================


class TestDetectDealSignals:
    def test_detects_deal_keywords(self):
        transcript = "We discussed the pilot program and sent a proposal for the POC."
        result = detect_deal_signals(
            transcript_text=transcript,
            meeting_title="Meeting with Agri Corp",
            participants=["Eyal", "John Smith"],
            meeting_date="2026-04-07",
        )
        assert result["has_deal_signals"] is True
        assert "pilot" in result["deal_keywords_found"]
        assert "proposal" in result["deal_keywords_found"]

    def test_no_deal_signals_in_internal_meeting(self):
        transcript = "We discussed the sprint backlog and code review process."
        result = detect_deal_signals(
            transcript_text=transcript,
            meeting_title="Internal standup",
            participants=["Eyal", "Roye"],
            meeting_date="2026-04-07",
        )
        assert result["has_deal_signals"] is False
        assert result["deal_keywords_found"] == []

    def test_single_keyword_not_enough(self):
        transcript = "We mentioned the pilot once."
        result = detect_deal_signals(
            transcript_text=transcript,
            meeting_title="Quick chat",
            participants=["Eyal"],
            meeting_date="2026-04-07",
        )
        assert result["has_deal_signals"] is False

    def test_detects_commitment_signals(self):
        transcript = "We'll send the proposal by next week and I'll share the data."
        result = detect_deal_signals(
            transcript_text=transcript,
            meeting_title="BD meeting",
            participants=["Eyal", "Partner"],
            meeting_date="2026-04-07",
        )
        assert result["has_commitment_signals"] is True
        assert len(result["commitment_keywords_found"]) >= 1

    def test_no_commitment_signals(self):
        transcript = "Discussed internal process improvements."
        result = detect_deal_signals(
            transcript_text=transcript,
            meeting_title="Internal",
            participants=["Eyal"],
            meeting_date="2026-04-07",
        )
        assert result["has_commitment_signals"] is False

    @patch("config.team.get_team_member_names", return_value=[
        "Eyal Zror", "Roye Tadmor", "Paolo Boni", "Prof. Yoram Weiss",
    ])
    def test_identifies_external_participants(self, mock_names):
        result = detect_deal_signals(
            transcript_text="pilot and proposal discussion",
            meeting_title="Meeting",
            participants=["Eyal Zror", "John Smith", "Roye Tadmor"],
            meeting_date="2026-04-07",
        )
        assert "John Smith" in result["external_participants"]
        assert "Eyal Zror" not in result["external_participants"]
        assert "Roye Tadmor" not in result["external_participants"]

    def test_empty_participants_handled(self):
        result = detect_deal_signals(
            transcript_text="pilot and proposal discussion",
            meeting_title="Meeting",
            participants=[],
            meeting_date="2026-04-07",
        )
        assert result["external_participants"] == []

    def test_case_insensitive_keywords(self):
        transcript = "PILOT program and PROPOSAL sent"
        result = detect_deal_signals(
            transcript_text=transcript,
            meeting_title="Test",
            participants=[],
            meeting_date="2026-04-07",
        )
        assert result["has_deal_signals"] is True

    def test_meeting_id_passed_through(self):
        result = detect_deal_signals(
            transcript_text="pilot and proposal",
            meeting_title="Test",
            participants=[],
            meeting_date="2026-04-07",
            meeting_id="abc-123",
        )
        assert result["meeting_id"] == "abc-123"

    def test_meeting_metadata_preserved(self):
        result = detect_deal_signals(
            transcript_text="test",
            meeting_title="CropSight Meeting",
            participants=["Eyal"],
            meeting_date="2026-04-10",
        )
        assert result["meeting_title"] == "CropSight Meeting"
        assert result["meeting_date"] == "2026-04-10"


# =============================================================================
# Deal Pulse Generation
# =============================================================================


class TestGenerateDealPulse:
    @patch("processors.deal_intelligence.supabase_client")
    def test_returns_overdue_actions(self, mock_sb):
        yesterday = (date.today() - timedelta(days=2)).isoformat()
        mock_sb.get_overdue_deal_actions.return_value = [
            {
                "name": "AgriCorp Deal",
                "organization": "AgriCorp",
                "next_action": "Send proposal",
                "next_action_date": yesterday,
            },
        ]
        mock_sb.get_stale_deals.return_value = []

        result = generate_deal_pulse()
        assert len(result) == 1
        assert result[0]["type"] == "overdue"
        assert result[0]["name"] == "AgriCorp Deal"
        assert "2d overdue" in result[0]["detail"]

    @patch("processors.deal_intelligence.supabase_client")
    def test_returns_stale_deals(self, mock_sb):
        mock_sb.get_overdue_deal_actions.return_value = []
        ten_days_ago = (date.today() - timedelta(days=10)).isoformat()
        mock_sb.get_stale_deals.return_value = [
            {
                "name": "Old Lead",
                "organization": "StaleCo",
                "last_interaction_date": ten_days_ago,
            },
        ]

        result = generate_deal_pulse()
        assert len(result) == 1
        assert result[0]["type"] == "stale"
        assert "10 days" in result[0]["detail"]

    @patch("processors.deal_intelligence.supabase_client")
    def test_max_items_capped(self, mock_sb):
        yesterday = (date.today() - timedelta(days=1)).isoformat()
        mock_sb.get_overdue_deal_actions.return_value = [
            {"name": f"Deal {i}", "organization": f"Org {i}", "next_action": "Follow up", "next_action_date": yesterday}
            for i in range(5)
        ]
        mock_sb.get_stale_deals.return_value = []

        result = generate_deal_pulse(max_items=3)
        assert len(result) == 3

    @patch("processors.deal_intelligence.supabase_client")
    def test_empty_when_no_deals(self, mock_sb):
        mock_sb.get_overdue_deal_actions.return_value = []
        mock_sb.get_stale_deals.return_value = []

        result = generate_deal_pulse()
        assert result == []

    @patch("processors.deal_intelligence.supabase_client")
    def test_overdue_prioritized_over_stale(self, mock_sb):
        yesterday = (date.today() - timedelta(days=1)).isoformat()
        mock_sb.get_overdue_deal_actions.return_value = [
            {"name": f"Urgent {i}", "organization": f"Org {i}", "next_action": "Call", "next_action_date": yesterday}
            for i in range(3)
        ]
        ten_days_ago = (date.today() - timedelta(days=10)).isoformat()
        mock_sb.get_stale_deals.return_value = [
            {"name": "Stale Deal", "organization": "StaleCo", "last_interaction_date": ten_days_ago},
        ]

        result = generate_deal_pulse(max_items=3)
        assert all(r["type"] == "overdue" for r in result)

    @patch("processors.deal_intelligence.supabase_client")
    def test_stale_deal_no_last_interaction(self, mock_sb):
        mock_sb.get_overdue_deal_actions.return_value = []
        mock_sb.get_stale_deals.return_value = [
            {"name": "New Lead", "organization": "NewCo", "last_interaction_date": None},
        ]

        result = generate_deal_pulse()
        assert len(result) == 1
        assert "No recorded interactions" in result[0]["detail"]


# =============================================================================
# Commitments Due Generation
# =============================================================================


class TestGenerateCommitmentsDue:
    @patch("processors.deal_intelligence.supabase_client")
    def test_returns_overdue_commitments(self, mock_sb):
        three_days_ago = (date.today() - timedelta(days=3)).isoformat()
        mock_sb.get_overdue_commitments.return_value = [
            {
                "organization": "PartnerCo",
                "commitment": "Send data analysis report",
                "deadline": three_days_ago,
                "promised_to": "John",
            },
        ]

        result = generate_commitments_due()
        assert len(result) == 1
        assert result[0]["organization"] == "PartnerCo"
        assert result[0]["days_overdue"] == 3
        assert result[0]["promised_to"] == "John"

    @patch("processors.deal_intelligence.supabase_client")
    def test_capped_at_max_items(self, mock_sb):
        yesterday = (date.today() - timedelta(days=1)).isoformat()
        mock_sb.get_overdue_commitments.return_value = [
            {"organization": f"Org {i}", "commitment": f"Promise {i}", "deadline": yesterday, "promised_to": ""}
            for i in range(10)
        ]

        result = generate_commitments_due(max_items=3)
        assert len(result) == 3

    @patch("processors.deal_intelligence.supabase_client")
    def test_empty_when_no_overdue(self, mock_sb):
        mock_sb.get_overdue_commitments.return_value = []

        result = generate_commitments_due()
        assert result == []

    @patch("processors.deal_intelligence.supabase_client")
    def test_commitment_text_truncated(self, mock_sb):
        yesterday = (date.today() - timedelta(days=1)).isoformat()
        long_text = "A" * 200
        mock_sb.get_overdue_commitments.return_value = [
            {"organization": "Org", "commitment": long_text, "deadline": yesterday, "promised_to": ""},
        ]

        result = generate_commitments_due()
        assert len(result[0]["commitment"]) == 80


# =============================================================================
# Formatting
# =============================================================================


class TestFormatDealPulse:
    def test_format_overdue(self):
        items = [{"type": "overdue", "name": "Deal A", "organization": "OrgA", "detail": "Follow up — 2d overdue"}]
        result = format_deal_pulse_for_brief(items)
        assert "!" in result
        assert "Deal A" in result
        assert "OrgA" in result

    def test_format_stale(self):
        items = [{"type": "stale", "name": "Deal B", "organization": "OrgB", "detail": "No contact in 10 days"}]
        result = format_deal_pulse_for_brief(items)
        assert "~" in result
        assert "Deal B" in result

    def test_empty_returns_empty_string(self):
        assert format_deal_pulse_for_brief([]) == ""


class TestFormatCommitments:
    def test_format_with_promised_to(self):
        items = [{"commitment": "Send report", "promised_to": "John", "days_overdue": 3}]
        result = format_commitments_for_brief(items)
        assert "Send report" in result
        assert "to John" in result
        assert "3d overdue" in result

    def test_format_without_promised_to(self):
        items = [{"commitment": "Send docs", "promised_to": "", "days_overdue": 1}]
        result = format_commitments_for_brief(items)
        assert "to " not in result or "to (" not in result

    def test_empty_returns_empty_string(self):
        assert format_commitments_for_brief([]) == ""


# =============================================================================
# Auto-create Interaction
# =============================================================================


class TestAutoCreateDealInteraction:
    @patch("processors.deal_intelligence.supabase_client")
    def test_creates_interaction_for_existing_deal(self, mock_sb):
        mock_sb.get_deal.return_value = {"id": "deal-1", "name": "Test Deal"}
        mock_sb.create_deal_interaction.return_value = {"id": "int-1", "deal_id": "deal-1"}

        result = auto_create_deal_interaction(
            deal_id="deal-1",
            meeting_id="meeting-1",
            meeting_title="Strategy Meeting",
            meeting_date="2026-04-07",
        )
        assert result is not None
        mock_sb.create_deal_interaction.assert_called_once()

    @patch("processors.deal_intelligence.supabase_client")
    def test_returns_none_for_nonexistent_deal(self, mock_sb):
        mock_sb.get_deal.return_value = None

        result = auto_create_deal_interaction(
            deal_id="fake-id",
            meeting_id="meeting-1",
            meeting_title="Test",
            meeting_date="2026-04-07",
        )
        assert result is None
        mock_sb.create_deal_interaction.assert_not_called()
