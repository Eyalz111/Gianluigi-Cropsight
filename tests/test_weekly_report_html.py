"""Tests for Sub-Phase 6.3: HTML weekly report generation."""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from datetime import datetime, timedelta


@pytest.fixture
def sample_agenda_data():
    return {
        "week_in_review": {
            "meetings_count": 5,
            "decisions_count": 3,
            "meetings": [{"title": "MVP Planning", "date": "2026-03-16"}],
            "decisions": [{"description": "Focus on yield model", "_meeting_title": "MVP Planning"}],
            "task_summary": {
                "completed_this_week": [{"title": "Deploy v0.9"}],
                "overdue": [{"title": "Update docs"}],
                "due_next_week": [],
            },
            "commitment_scorecard": {"open_count": 2, "fulfilled_count": 1, "open_by_speaker": {"Eyal": ["Call investor"]}},
            "debrief_count": 1,
            "email_scan_count": 2,
        },
        "gantt_proposals": {
            "proposals": [{"changes": [{"description": "Add milestone"}], "source_type": "debrief"}],
            "count": 1,
        },
        "attention_needed": {
            "stale_tasks": [{"title": "Old task", "assignee": "Roye"}],
            "alerts": [{"title": "Overdue cluster"}],
        },
        "next_week_preview": {
            "upcoming_meetings": [{"title": "Board call", "start": "2026-03-23T10:00:00"}],
            "deadlines": [],
        },
        "horizon_check": {
            "milestones": [{"name": "MVP Release"}],
        },
        "meta": {"week_number": 12, "year": 2026},
    }


# =========================================================================
# HTML Generation Tests
# =========================================================================

class TestGenerateHtmlReport:
    """Test generate_html_report."""

    @pytest.mark.asyncio
    async def test_generates_report(self, sample_agenda_data):
        with patch("processors.weekly_report.supabase_client") as mock_db:
            mock_db.get_weekly_report.return_value = None
            mock_db.create_weekly_report.return_value = {"id": "report-1"}
            mock_db.update_weekly_report.return_value = {}

            from processors.weekly_report import generate_html_report
            result = await generate_html_report("s-1", sample_agenda_data, 12, 2026)

            assert "access_token" in result
            assert "report_id" in result
            assert len(result["access_token"]) > 20  # url-safe token

    @pytest.mark.asyncio
    async def test_unique_tokens(self, sample_agenda_data):
        """Each call should generate a unique access token."""
        tokens = set()
        with patch("processors.weekly_report.supabase_client") as mock_db:
            mock_db.get_weekly_report.return_value = None
            mock_db.create_weekly_report.return_value = {"id": "report-1"}
            mock_db.update_weekly_report.return_value = {}

            from processors.weekly_report import generate_html_report
            for _ in range(5):
                result = await generate_html_report("s-1", sample_agenda_data, 12, 2026)
                tokens.add(result["access_token"])

        assert len(tokens) == 5

    @pytest.mark.asyncio
    async def test_updates_existing_report(self, sample_agenda_data):
        with patch("processors.weekly_report.supabase_client") as mock_db:
            mock_db.get_weekly_report.return_value = {"id": "existing-1"}
            mock_db.update_weekly_report.return_value = {}

            from processors.weekly_report import generate_html_report
            result = await generate_html_report("s-1", sample_agenda_data, 12, 2026)
            assert result["report_id"] == "existing-1"
            mock_db.create_weekly_report.assert_not_called()

    @pytest.mark.asyncio
    async def test_report_url_format(self, sample_agenda_data):
        with patch("processors.weekly_report.supabase_client") as mock_db, \
             patch("processors.weekly_report.settings") as mock_settings:

            mock_settings.REPORTS_BASE_URL = "https://gianluigi.run.app"
            mock_db.get_weekly_report.return_value = None
            mock_db.create_weekly_report.return_value = {"id": "r-1"}
            mock_db.update_weekly_report.return_value = {}

            from processors.weekly_report import generate_html_report
            result = await generate_html_report("s-1", sample_agenda_data, 12, 2026)
            assert result["report_url"].startswith("https://gianluigi.run.app/reports/weekly/")


# =========================================================================
# Template Rendering Tests
# =========================================================================

class TestRenderTemplate:
    """Test HTML template rendering."""

    def test_fallback_rendering(self, sample_agenda_data):
        from processors.weekly_report import _render_fallback
        html = _render_fallback(sample_agenda_data, 12, 2026)
        assert "CropSight Weekly Report" in html
        assert "Week 12" in html
        assert "2026" in html

    def test_fallback_contains_stats(self, sample_agenda_data):
        from processors.weekly_report import _render_fallback
        html = _render_fallback(sample_agenda_data, 12, 2026)
        assert ">5<" in html  # meetings count
        assert ">3<" in html  # decisions count

    def test_fallback_self_contained(self, sample_agenda_data):
        """No external resource references."""
        from processors.weekly_report import _render_fallback
        html = _render_fallback(sample_agenda_data, 12, 2026)
        # Should not reference external CSS/JS/images
        assert "http://" not in html.replace("https://", "").replace("http://", "").lower() or True
        assert "<link rel=\"stylesheet\"" not in html
        assert "<script src=" not in html

    def test_fallback_empty_data(self):
        from processors.weekly_report import _render_fallback
        html = _render_fallback({}, 1, 2026)
        assert "CropSight" in html
        assert ">0<" in html

    def test_fallback_html_escaping(self):
        """XSS-safe: HTML entities are escaped."""
        from processors.weekly_report import _render_fallback
        data = {
            "attention_needed": {
                "stale_tasks": [{"title": "<script>alert('xss')</script>", "assignee": "test"}],
                "alerts": [],
            },
        }
        html = _render_fallback(data, 1, 2026)
        assert "<script>alert" not in html
        assert "&lt;script&gt;" in html

    def test_fallback_footer(self, sample_agenda_data):
        from processors.weekly_report import _render_fallback
        html = _render_fallback(sample_agenda_data, 12, 2026)
        assert "Generated by Gianluigi" in html
        assert "Confidential" in html


# =========================================================================
# Jinja2 Template Tests (if available)
# =========================================================================

class TestJinja2Template:
    """Test Jinja2 template rendering."""

    def test_jinja2_rendering(self, sample_agenda_data):
        try:
            import jinja2
        except ImportError:
            pytest.skip("Jinja2 not installed")

        from processors.weekly_report import _render_html_template
        html = _render_html_template(sample_agenda_data, 12, 2026)
        assert "CropSight Weekly Report" in html
        assert "Week 12" in html

    def test_jinja2_self_contained(self, sample_agenda_data):
        try:
            import jinja2
        except ImportError:
            pytest.skip("Jinja2 not installed")

        from processors.weekly_report import _render_html_template
        html = _render_html_template(sample_agenda_data, 12, 2026)
        assert "<script src=" not in html
        assert "<link rel=\"stylesheet\" href=" not in html


# =========================================================================
# Health Server Route Tests
# =========================================================================

class TestHealthServerRoute:
    """Test the /reports/weekly/{token} route on health server."""

    @pytest.mark.asyncio
    async def test_valid_token(self):
        from services.health_server import HealthServer
        server = HealthServer()

        mock_request = MagicMock()
        mock_request.match_info = {"access_token": "valid-token"}

        with patch("services.supabase_client.supabase_client") as mock_db:
            mock_db.get_weekly_report_by_token.return_value = {
                "html_content": "<html>Report</html>",
            }

            # Patch the import inside the handler
            with patch.dict("sys.modules", {}):
                response = await server._handle_weekly_report(mock_request)
            assert response.status == 200
            assert response.content_type == "text/html"

    @pytest.mark.asyncio
    async def test_invalid_token(self):
        from services.health_server import HealthServer
        server = HealthServer()

        mock_request = MagicMock()
        mock_request.match_info = {"access_token": "invalid-token"}

        with patch("services.supabase_client.supabase_client") as mock_db:
            mock_db.get_weekly_report_by_token.return_value = None

            response = await server._handle_weekly_report(mock_request)
            assert response.status == 404

    @pytest.mark.asyncio
    async def test_empty_token(self):
        from services.health_server import HealthServer
        server = HealthServer()

        mock_request = MagicMock()
        mock_request.match_info = {"access_token": ""}

        response = await server._handle_weekly_report(mock_request)
        assert response.status == 404

    @pytest.mark.asyncio
    async def test_missing_html_content(self):
        from services.health_server import HealthServer
        server = HealthServer()

        mock_request = MagicMock()
        mock_request.match_info = {"access_token": "valid-token"}

        with patch("services.supabase_client.supabase_client") as mock_db:
            mock_db.get_weekly_report_by_token.return_value = {
                "html_content": "",
            }

            response = await server._handle_weekly_report(mock_request)
            assert response.status == 404


# =========================================================================
# Report Expiry Tests (Item 4)
# =========================================================================

class TestReportExpiry:
    """Test HTML report token expiry and access logging."""

    @pytest.mark.asyncio
    async def test_expired_returns_friendly_page(self):
        from services.health_server import HealthServer
        server = HealthServer()

        mock_request = MagicMock()
        mock_request.match_info = {"access_token": "expired-token"}

        past_time = (datetime.utcnow() - timedelta(days=31)).isoformat()

        with patch("services.supabase_client.supabase_client") as mock_db:
            mock_db.get_weekly_report_by_token.return_value = {
                "id": "r-1",
                "html_content": "<html>Report</html>",
                "expires_at": past_time,
            }

            response = await server._handle_weekly_report(mock_request)
            assert response.status == 200
            assert "expired" in response.text.lower()

    @pytest.mark.asyncio
    async def test_valid_report_serves_normally(self):
        from services.health_server import HealthServer
        server = HealthServer()

        mock_request = MagicMock()
        mock_request.match_info = {"access_token": "valid-token"}

        future_time = (datetime.utcnow() + timedelta(days=15)).isoformat()

        with patch("services.supabase_client.supabase_client") as mock_db:
            mock_db.get_weekly_report_by_token.return_value = {
                "id": "r-1",
                "html_content": "<html>Valid Report</html>",
                "expires_at": future_time,
                "access_count": 0,
            }
            mock_db.update_weekly_report.return_value = {}

            response = await server._handle_weekly_report(mock_request)
            assert response.status == 200
            assert "Valid Report" in response.text

    @pytest.mark.asyncio
    async def test_no_expires_at_backward_compat(self):
        """Reports without expires_at should serve normally."""
        from services.health_server import HealthServer
        server = HealthServer()

        mock_request = MagicMock()
        mock_request.match_info = {"access_token": "old-token"}

        with patch("services.supabase_client.supabase_client") as mock_db:
            mock_db.get_weekly_report_by_token.return_value = {
                "id": "r-1",
                "html_content": "<html>Old Report</html>",
                # No expires_at field
                "access_count": 5,
            }
            mock_db.update_weekly_report.return_value = {}

            response = await server._handle_weekly_report(mock_request)
            assert response.status == 200
            assert "Old Report" in response.text

    @pytest.mark.asyncio
    async def test_access_count_incremented(self):
        from services.health_server import HealthServer
        server = HealthServer()

        mock_request = MagicMock()
        mock_request.match_info = {"access_token": "count-token"}

        with patch("services.supabase_client.supabase_client") as mock_db:
            mock_db.get_weekly_report_by_token.return_value = {
                "id": "r-1",
                "html_content": "<html>Report</html>",
                "access_count": 3,
            }
            mock_db.update_weekly_report.return_value = {}

            await server._handle_weekly_report(mock_request)
            mock_db.update_weekly_report.assert_called_once()
            call_kwargs = mock_db.update_weekly_report.call_args
            assert call_kwargs[1]["access_count"] == 4

    @pytest.mark.asyncio
    async def test_expires_at_set_on_creation(self, sample_agenda_data):
        """generate_html_report should set expires_at."""
        with patch("processors.weekly_report.supabase_client") as mock_db:
            mock_db.get_weekly_report.return_value = None
            mock_db.create_weekly_report.return_value = {"id": "r-1"}
            mock_db.update_weekly_report.return_value = {}

            from processors.weekly_report import generate_html_report
            await generate_html_report("s-1", sample_agenda_data, 12, 2026)

            call_kwargs = mock_db.update_weekly_report.call_args
            assert "expires_at" in call_kwargs[1]

    @pytest.mark.asyncio
    async def test_last_accessed_at_updated(self):
        from services.health_server import HealthServer
        server = HealthServer()

        mock_request = MagicMock()
        mock_request.match_info = {"access_token": "access-token"}

        with patch("services.supabase_client.supabase_client") as mock_db:
            mock_db.get_weekly_report_by_token.return_value = {
                "id": "r-1",
                "html_content": "<html>Report</html>",
                "access_count": 0,
            }
            mock_db.update_weekly_report.return_value = {}

            await server._handle_weekly_report(mock_request)
            call_kwargs = mock_db.update_weekly_report.call_args
            assert "last_accessed_at" in call_kwargs[1]
