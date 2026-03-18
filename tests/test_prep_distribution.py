"""
Tests for Phase 5.5: Prep Distribution + Word Doc.

Tests cover:
- Word doc generation with various template types
- Word doc with focus areas and gantt snapshot
- Distribution: sensitive meeting → Eyal-only
- Distribution: normal meeting → team-wide
- Distribution generates and uploads .docx
"""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch


# =============================================================================
# Test generate_prep_docx
# =============================================================================

class TestGeneratePrepDocx:

    def test_basic_document(self):
        """Should produce valid .docx bytes."""
        from services.word_generator import generate_prep_docx

        result = generate_prep_docx(
            title="Tech Review",
            date="2026-03-17",
            meeting_type="Founders Technical Review",
            participants=["Eyal", "Roye"],
            sections=[
                {"name": "Open Tasks", "status": "ok", "data": [{"title": "Fix bug"}], "item_count": 1},
                {"name": "Decisions", "status": "ok", "data": [{"description": "Use PyTorch"}], "item_count": 1},
            ],
        )

        assert isinstance(result, bytes)
        assert len(result) > 0
        # Check it's a valid ZIP (docx is ZIP-based)
        assert result[:2] == b"PK"

    def test_with_focus_areas(self):
        """Focus areas should be included in the doc."""
        from services.word_generator import generate_prep_docx

        result = generate_prep_docx(
            title="Strategy Meeting",
            date="2026-03-18",
            meeting_type="Monthly Strategic Review",
            participants=["Eyal", "Roye", "Paolo"],
            sections=[],
            focus_areas=["Focus on MVP timeline", "Check Paolo's BD pipeline"],
        )

        assert isinstance(result, bytes)
        assert len(result) > 0

    def test_with_gantt_snapshot(self):
        """Gantt snapshot should add a table."""
        from services.word_generator import generate_prep_docx

        result = generate_prep_docx(
            title="Tech Review",
            date="2026-03-17",
            meeting_type="Founders Technical Review",
            participants=["Eyal", "Roye"],
            sections=[],
            gantt_snapshot=[
                ["Product & Technology", "ML Pipeline", "On Track", "Roye", "12"],
                ["Product & Technology", "API", "Delayed", "Eyal", "12"],
            ],
        )

        assert isinstance(result, bytes)
        assert len(result) > 0

    def test_unavailable_sections(self):
        """Unavailable sections should not crash."""
        from services.word_generator import generate_prep_docx

        result = generate_prep_docx(
            title="Meeting",
            date="2026-03-17",
            meeting_type="Generic",
            participants=[],
            sections=[
                {"name": "Decisions", "status": "unavailable: timeout", "data": None, "item_count": 0},
            ],
        )

        assert isinstance(result, bytes)
        assert len(result) > 0

    def test_dict_data_sections(self):
        """Sections with dict data (e.g., tasks by person) should work."""
        from services.word_generator import generate_prep_docx

        result = generate_prep_docx(
            title="Review",
            date="2026-03-17",
            meeting_type="Test",
            participants=["Eyal"],
            sections=[
                {
                    "name": "Tasks by Person",
                    "status": "ok",
                    "data": {"Roye": [{"title": "Fix ML"}, {"title": "Deploy API"}]},
                    "item_count": 2,
                },
            ],
        )

        assert isinstance(result, bytes)
        assert len(result) > 0

    def test_empty_sections(self):
        """Empty sections list should produce valid doc."""
        from services.word_generator import generate_prep_docx

        result = generate_prep_docx(
            title="Quick Sync",
            date="2026-03-17",
            meeting_type="Generic",
            participants=[],
            sections=[],
        )

        assert isinstance(result, bytes)
        assert len(result) > 0


# =============================================================================
# Test distribute_approved_prep
# =============================================================================

class TestDistributeApprovedPrep:

    @pytest.mark.asyncio
    async def test_sensitive_eyal_only(self):
        """Sensitive meeting prep should only go to Eyal."""
        with patch("guardrails.approval_flow.telegram_bot") as mock_tg, \
             patch("guardrails.approval_flow.supabase_client") as mock_db, \
             patch("guardrails.approval_flow.settings") as mock_settings, \
             patch("services.word_generator.generate_prep_docx") as mock_docx, \
             patch("services.google_drive.drive_service") as mock_drive:

            mock_tg.send_to_eyal = AsyncMock(return_value=True)
            mock_tg.send_to_group = AsyncMock(return_value=True)
            mock_db.log_action.return_value = None
            mock_settings.ENVIRONMENT = "production"
            mock_docx.return_value = b"PK..."
            mock_drive.save_meeting_prep = AsyncMock(return_value={"webViewLink": "https://drive/gdoc"})
            mock_drive.upload_file = AsyncMock(return_value={"webViewLink": "https://drive/docx"})

            from guardrails.approval_flow import distribute_approved_prep

            result = await distribute_approved_prep(
                meeting_id="prep-evt1",
                content={
                    "title": "Sensitive Board Meeting",
                    "sensitivity": "sensitive",
                    "summary": "Prep doc content here.",
                    "start_time": "2026-03-17T10:00:00",
                },
            )

            assert result["telegram_sent"] is True
            assert result["distribution"] == "eyal_only"
            mock_tg.send_to_eyal.assert_called_once()
            mock_tg.send_to_group.assert_not_called()

    @pytest.mark.asyncio
    async def test_normal_team_distribution(self):
        """Normal meeting should distribute to team."""
        with patch("guardrails.approval_flow.telegram_bot") as mock_tg, \
             patch("guardrails.approval_flow.supabase_client") as mock_db, \
             patch("guardrails.approval_flow.settings") as mock_settings, \
             patch("guardrails.approval_flow.gmail_service") as mock_gmail, \
             patch("services.word_generator.generate_prep_docx") as mock_docx, \
             patch("services.google_drive.drive_service") as mock_drive:

            mock_tg.send_to_eyal = AsyncMock(return_value=True)
            mock_tg.send_to_group = AsyncMock(return_value=True)
            mock_gmail.send_email = AsyncMock(return_value=True)
            mock_db.log_action.return_value = None
            mock_settings.ENVIRONMENT = "production"
            mock_docx.return_value = b"PK..."
            mock_drive.save_meeting_prep = AsyncMock(return_value={"webViewLink": "https://drive/gdoc"})
            mock_drive.upload_file = AsyncMock(return_value={"webViewLink": "https://drive/docx"})

            from guardrails.approval_flow import distribute_approved_prep

            result = await distribute_approved_prep(
                meeting_id="prep-evt1",
                content={
                    "title": "Tech Review",
                    "sensitivity": "normal",
                    "summary": "Prep doc content here.",
                    "start_time": "2026-03-17T10:00:00",
                    "attendees": [
                        {"displayName": "Eyal", "email": "eyal@cropsight.com"},
                        {"displayName": "Roye", "email": "roye@cropsight.com"},
                    ],
                },
            )

            assert result["telegram_sent"] is True
            assert result["distribution"] == "team"
            mock_tg.send_to_eyal.assert_called_once()
            mock_tg.send_to_group.assert_called_once()
            mock_gmail.send_email.assert_called_once()

    @pytest.mark.asyncio
    async def test_docx_generation_failure_non_fatal(self):
        """Docx generation failure should not block distribution."""
        with patch("guardrails.approval_flow.telegram_bot") as mock_tg, \
             patch("guardrails.approval_flow.supabase_client") as mock_db, \
             patch("guardrails.approval_flow.settings") as mock_settings, \
             patch("services.google_drive.drive_service") as mock_drive, \
             patch("services.word_generator.generate_prep_docx", side_effect=Exception("docx error")):

            mock_tg.send_to_eyal = AsyncMock(return_value=True)
            mock_db.log_action.return_value = None
            mock_settings.ENVIRONMENT = "development"
            mock_drive.save_meeting_prep = AsyncMock(return_value={"webViewLink": "https://drive/gdoc"})

            from guardrails.approval_flow import distribute_approved_prep

            result = await distribute_approved_prep(
                meeting_id="prep-evt1",
                content={
                    "title": "Tech Review",
                    "sensitivity": "normal",
                    "summary": "Prep doc content here.",
                    "start_time": "2026-03-17T10:00:00",
                },
            )

            assert result["telegram_sent"] is True
            assert result["docx_uploaded"] is False

    @pytest.mark.asyncio
    async def test_non_production_eyal_only(self):
        """Non-production environments should only send to Eyal."""
        with patch("guardrails.approval_flow.telegram_bot") as mock_tg, \
             patch("guardrails.approval_flow.supabase_client") as mock_db, \
             patch("guardrails.approval_flow.settings") as mock_settings, \
             patch("services.word_generator.generate_prep_docx") as mock_docx, \
             patch("services.google_drive.drive_service") as mock_drive:

            mock_tg.send_to_eyal = AsyncMock(return_value=True)
            mock_tg.send_to_group = AsyncMock(return_value=True)
            mock_db.log_action.return_value = None
            mock_settings.ENVIRONMENT = "development"
            mock_docx.return_value = b"PK..."
            mock_drive.save_meeting_prep = AsyncMock(return_value={"webViewLink": "https://drive/gdoc"})
            mock_drive.upload_file = AsyncMock(return_value={})

            from guardrails.approval_flow import distribute_approved_prep

            result = await distribute_approved_prep(
                meeting_id="prep-evt1",
                content={
                    "title": "Tech Review",
                    "sensitivity": "normal",
                    "summary": "Prep doc content here.",
                    "start_time": "2026-03-17T10:00:00",
                },
            )

            assert result["telegram_sent"] is True
            assert result["distribution"] == "eyal_only"
            mock_tg.send_to_group.assert_not_called()
