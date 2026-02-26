"""
Pytest configuration and shared fixtures.

This module provides mock fixtures for all external services,
allowing tests to run without actual API connections.
"""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from datetime import datetime, timezone


# =============================================================================
# Mock Settings
# =============================================================================

@pytest.fixture
def mock_settings():
    """Provide mock settings for testing."""
    settings = MagicMock()
    settings.ANTHROPIC_API_KEY = "test-api-key"
    settings.SUPABASE_URL = "https://test.supabase.co"
    settings.SUPABASE_KEY = "test-supabase-key"
    settings.TELEGRAM_BOT_TOKEN = "test-telegram-token"
    settings.TELEGRAM_EYAL_CHAT_ID = "123456789"
    settings.GOOGLE_CLIENT_ID = "test-client-id"
    settings.GOOGLE_CLIENT_SECRET = "test-client-secret"
    settings.GOOGLE_REFRESH_TOKEN = "test-refresh-token"
    settings.EYAL_EMAIL = "eyal@cropsight.io"
    settings.ROYE_EMAIL = "roye@cropsight.io"
    settings.PAOLO_EMAIL = "paolo@cropsight.io"
    settings.YORAM_EMAIL = "yoram@cropsight.io"
    settings.CROPSIGHT_CALENDAR_COLOR_ID = "9"  # Purple
    settings.RAW_TRANSCRIPTS_FOLDER_ID = "folder-123"
    settings.MEETING_SUMMARIES_FOLDER_ID = "folder-456"
    settings.TASK_TRACKER_SHEET_ID = "sheet-123"
    settings.STAKEHOLDER_TRACKER_SHEET_ID = "sheet-456"
    settings.EYAL_TELEGRAM_ID = 123456789
    settings.ROYE_TELEGRAM_ID = 987654321
    settings.validate_required = MagicMock(return_value=[])
    settings.validate_optional = MagicMock(return_value=[])
    return settings


# =============================================================================
# Mock Services
# =============================================================================

@pytest.fixture
def mock_supabase():
    """Mock Supabase client."""
    client = AsyncMock()
    client.health_check = AsyncMock(return_value=True)
    client.log_action = AsyncMock(return_value=True)
    client.create_meeting = AsyncMock(return_value="meeting-uuid-123")
    client.get_meeting = AsyncMock(return_value={
        "id": "meeting-uuid-123",
        "title": "Test Meeting",
        "date": "2026-02-24",
        "summary": "Test summary",
    })
    client.create_task = AsyncMock(return_value="task-uuid-123")
    client.update_task = AsyncMock(return_value=True)
    client.get_open_questions = AsyncMock(return_value=[])
    return client


@pytest.fixture
def mock_telegram_bot():
    """Mock Telegram bot."""
    bot = AsyncMock()
    bot.send_to_eyal = AsyncMock(return_value=True)
    bot.send_to_group = AsyncMock(return_value=True)
    bot.send_message = AsyncMock(return_value=True)
    bot.send_approval_request = AsyncMock(return_value=True)
    bot.start = AsyncMock()
    bot.stop = AsyncMock()
    return bot


@pytest.fixture
def mock_drive_service():
    """Mock Google Drive service."""
    service = AsyncMock()
    service.authenticate = AsyncMock(return_value=True)
    service.get_new_transcripts = AsyncMock(return_value=[])
    service.download_file = AsyncMock(return_value="[00:00:15] Eyal: Hello\n[00:01:00] Roye: Hi")
    service.save_meeting_summary = AsyncMock(return_value={"id": "file-123", "webViewLink": "https://drive.google.com/file-123"})
    service.save_meeting_prep = AsyncMock(return_value={"id": "file-456", "webViewLink": "https://drive.google.com/file-456"})
    service.mark_file_processed = MagicMock()
    service.get_file_metadata = AsyncMock(return_value={"id": "file-123", "name": "test.txt"})
    return service


@pytest.fixture
def mock_calendar_service():
    """Mock Google Calendar service."""
    service = AsyncMock()
    service.authenticate = AsyncMock(return_value=True)
    service.get_upcoming_events = AsyncMock(return_value=[])
    service.get_event = AsyncMock(return_value={
        "id": "event-123",
        "title": "Test Meeting",
        "start": "2026-02-24T10:00:00Z",
        "end": "2026-02-24T11:00:00Z",
        "attendees": [
            {"email": "eyal@cropsight.io", "displayName": "Eyal Zror"},
            {"email": "roye@cropsight.io", "displayName": "Roye Tadmor"},
        ],
    })
    service.get_events_needing_prep = AsyncMock(return_value=[])
    return service


@pytest.fixture
def mock_sheets_service():
    """Mock Google Sheets service."""
    service = AsyncMock()
    service.authenticate = AsyncMock(return_value=True)
    service.add_task = AsyncMock(return_value=True)
    service.get_all_tasks = AsyncMock(return_value=[
        {
            "row_number": 2,
            "task": "Review proposal",
            "assignee": "Eyal",
            "deadline": "2026-02-25",
            "status": "pending",
            "priority": "H",
        },
        {
            "row_number": 3,
            "task": "Prepare demo",
            "assignee": "Roye",
            "deadline": "2026-02-23",
            "status": "pending",
            "priority": "M",
        },
    ])
    service.update_task_status = AsyncMock(return_value=True)
    service.get_stakeholder_info = AsyncMock(return_value=[])
    return service


@pytest.fixture
def mock_gmail_service():
    """Mock Gmail service."""
    service = AsyncMock()
    service.authenticate = AsyncMock(return_value=True)
    service.send_meeting_summary = AsyncMock(return_value=True)
    service.send_approval_request = AsyncMock(return_value=True)
    return service


@pytest.fixture
def mock_embedding_service():
    """Mock embedding service."""
    service = AsyncMock()
    service.health_check = AsyncMock(return_value=True)
    service.create_embedding = AsyncMock(return_value=[0.1] * 1536)
    service.search_similar = AsyncMock(return_value=[])
    return service


# =============================================================================
# Sample Data Fixtures
# =============================================================================

@pytest.fixture
def sample_calendar_event():
    """Sample calendar event for testing."""
    return {
        "id": "event-123",
        "title": "CropSight MVP Review",
        "start": "2026-02-24T10:00:00Z",
        "end": "2026-02-24T11:00:00Z",
        "attendees": [
            {"email": "eyal@cropsight.io", "displayName": "Eyal Zror"},
            {"email": "roye@cropsight.io", "displayName": "Roye Tadmor"},
        ],
        "color_id": "9",
    }


@pytest.fixture
def sample_transcript():
    """Sample Tactiq transcript for testing."""
    return """[00:00:15] Eyal: Welcome everyone to the MVP review meeting.
[00:00:30] Roye: Thanks Eyal. Let's discuss the model accuracy.
[00:01:15] Eyal: Great. Paolo, can you prepare the client demo for next week?
[00:01:45] Paolo: Sure, I'll have it ready by Friday.
[00:02:30] Roye: We need to decide on the API versioning strategy.
[00:03:00] Eyal: Let's go with semantic versioning. That's decided.
[00:04:15] Yoram: I have a question about the satellite data sources.
[00:05:00] Eyal: Good question, let's follow up on that next meeting.
"""


@pytest.fixture
def sample_summary():
    """Sample meeting summary for testing."""
    return """# MVP Review Meeting - 2026-02-24

## Attendees
- Eyal Zror (CEO)
- Roye Tadmor (CTO)
- Paolo Vailetti (BD)
- Yoram Weiss (Advisor)

## Summary
The team met to review MVP progress. Discussed model accuracy and client demo preparation.

## Decisions
1. Use semantic versioning for the API (ref: ~03:00)

## Action Items
1. Paolo: Prepare client demo by Friday

## Open Questions
1. Satellite data sources - to be discussed next meeting
"""


@pytest.fixture
def sample_task():
    """Sample task for testing."""
    return {
        "task": "Prepare client demo",
        "category": "Product & Tech",
        "assignee": "Paolo",
        "source_meeting": "MVP Review",
        "deadline": "2026-02-28",
        "status": "pending",
        "priority": "H",
        "created_date": "2026-02-24",
    }
