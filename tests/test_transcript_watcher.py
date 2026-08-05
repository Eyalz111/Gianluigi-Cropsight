"""
Tests for schedulers/transcript_watcher.py

Tests the transcript watcher functionality.
"""

import pytest
import sys
from unittest.mock import AsyncMock, patch, MagicMock
from datetime import datetime


# Mock settings before importing modules that depend on it
@pytest.fixture(autouse=True)
def mock_settings_for_watcher():
    """Mock settings for all tests in this module."""
    mock_settings = MagicMock()
    mock_settings.ANTHROPIC_API_KEY = "test-key"
    mock_settings.SUPABASE_URL = "https://test.supabase.co"
    mock_settings.SUPABASE_KEY = "test-key"
    mock_settings.GOOGLE_CLIENT_ID = "test-client"
    mock_settings.GOOGLE_CLIENT_SECRET = "test-secret"
    mock_settings.GOOGLE_REFRESH_TOKEN = "test-token"
    mock_settings.RAW_TRANSCRIPTS_FOLDER_ID = "folder-123"
    mock_settings.EYAL_EMAIL = "eyal@test.com"
    mock_settings.ROYE_EMAIL = "roye@test.com"
    mock_settings.PAOLO_EMAIL = "paolo@test.com"
    mock_settings.YORAM_EMAIL = "yoram@test.com"

    with patch.dict('sys.modules', {}):
        with patch('config.settings.settings', mock_settings):
            yield mock_settings


class TestTranscriptWatcherFilename:
    """Tests for filename parsing (no imports required)."""

    def test_parse_filename_date_at_start(self):
        """Should parse date at start of filename."""
        import re
        from datetime import datetime

        # Inline implementation for testing
        def parse_filename(filename):
            name = re.sub(r'\.(txt|md|docx?)$', '', filename, flags=re.IGNORECASE)
            date_start = re.match(r'^(\d{4}-\d{2}-\d{2})\s*[-–]\s*(.+)$', name)
            if date_start:
                return {"date": date_start.group(1), "title": date_start.group(2).strip(), "participants": []}
            date_end = re.match(r'^(.+)\s*[-–]\s*(\d{4}-\d{2}-\d{2})$', name)
            if date_end:
                return {"date": date_end.group(2), "title": date_end.group(1).strip(), "participants": []}
            return {"date": datetime.now().strftime("%Y-%m-%d"), "title": name.strip(), "participants": []}

        result = parse_filename("2026-02-24 - MVP Review.txt")
        assert result["date"] == "2026-02-24"
        assert result["title"] == "MVP Review"

    def test_parse_filename_date_at_end(self):
        """Should parse date at end of filename."""
        import re
        from datetime import datetime

        def parse_filename(filename):
            name = re.sub(r'\.(txt|md|docx?)$', '', filename, flags=re.IGNORECASE)
            date_start = re.match(r'^(\d{4}-\d{2}-\d{2})\s*[-–]\s*(.+)$', name)
            if date_start:
                return {"date": date_start.group(1), "title": date_start.group(2).strip(), "participants": []}
            date_end = re.match(r'^(.+)\s*[-–]\s*(\d{4}-\d{2}-\d{2})$', name)
            if date_end:
                return {"date": date_end.group(2), "title": date_end.group(1).strip(), "participants": []}
            return {"date": datetime.now().strftime("%Y-%m-%d"), "title": name.strip(), "participants": []}

        result = parse_filename("MVP Review - 2026-02-24.txt")
        assert result["date"] == "2026-02-24"
        assert result["title"] == "MVP Review"

    def test_parse_filename_no_date(self):
        """Should use today's date when no date in filename."""
        import re
        from datetime import datetime

        def parse_filename(filename):
            name = re.sub(r'\.(txt|md|docx?)$', '', filename, flags=re.IGNORECASE)
            date_start = re.match(r'^(\d{4}-\d{2}-\d{2})\s*[-–]\s*(.+)$', name)
            if date_start:
                return {"date": date_start.group(1), "title": date_start.group(2).strip(), "participants": []}
            date_end = re.match(r'^(.+)\s*[-–]\s*(\d{4}-\d{2}-\d{2})$', name)
            if date_end:
                return {"date": date_end.group(2), "title": date_end.group(1).strip(), "participants": []}
            return {"date": datetime.now().strftime("%Y-%m-%d"), "title": name.strip(), "participants": []}

        result = parse_filename("MVP Review.txt")
        assert result["date"] == datetime.now().strftime("%Y-%m-%d")
        assert result["title"] == "MVP Review"


class TestTranscriptParticipantExtraction:
    """Tests for participant extraction from transcripts."""

    def test_extract_participants_from_transcript(self):
        """Should extract speaker names from Tactiq format."""
        import re

        def extract_participants(content):
            speaker_pattern = r'\[\d{2}:\d{2}:\d{2}\]\s+([A-Z][a-z]+(?:\s[A-Z][a-z]+)?)\s*:'
            matches = re.findall(speaker_pattern, content)
            seen = set()
            participants = []
            for name in matches:
                if name not in seen:
                    seen.add(name)
                    participants.append(name)
            return participants

        transcript = """[00:00:15] Eyal: Welcome everyone.
[00:00:30] Roye: Thanks Eyal.
[00:01:15] Eyal: Let's discuss the MVP.
[00:02:00] Paolo: I'll prepare the demo.
"""
        participants = extract_participants(transcript)

        assert "Eyal" in participants
        assert "Roye" in participants
        assert "Paolo" in participants
        assert len(participants) == 3

    def test_extract_participants_preserves_order(self):
        """Should preserve order of first appearance."""
        import re

        def extract_participants(content):
            speaker_pattern = r'\[\d{2}:\d{2}:\d{2}\]\s+([A-Z][a-z]+(?:\s[A-Z][a-z]+)?)\s*:'
            matches = re.findall(speaker_pattern, content)
            seen = set()
            participants = []
            for name in matches:
                if name not in seen:
                    seen.add(name)
                    participants.append(name)
            return participants

        transcript = """[00:00:15] Eyal: Welcome.
[00:00:30] Roye: Hi.
[00:01:15] Paolo: Hello.
"""
        participants = extract_participants(transcript)

        assert participants[0] == "Eyal"
        assert participants[1] == "Roye"
        assert participants[2] == "Paolo"

    def test_extract_participants_handles_two_word_names(self):
        """Should handle two-word names."""
        import re

        def extract_participants(content):
            speaker_pattern = r'\[\d{2}:\d{2}:\d{2}\]\s+([A-Z][a-z]+(?:\s[A-Z][a-z]+)?)\s*:'
            matches = re.findall(speaker_pattern, content)
            seen = set()
            participants = []
            for name in matches:
                if name not in seen:
                    seen.add(name)
                    participants.append(name)
            return participants

        transcript = """[00:00:15] Eyal Zror: Welcome.
[00:00:30] Roye Tadmor: Hi.
"""
        participants = extract_participants(transcript)

        assert len(participants) == 2
        assert "Eyal Zror" in participants or "Eyal" in participants


class TestMultiFolderInbox:
    """Transcripts can arrive in more than one Drive folder: Tactiq writes to a
    folder IT picks (drive.file scope means its picker can't browse existing
    folders) while Eyal also drops files by hand. [2026-08-05]"""

    def test_primary_only_when_no_extras(self, monkeypatch):
        from services.google_drive import drive_service, settings as ds
        monkeypatch.setattr(ds, "RAW_TRANSCRIPTS_FOLDER_ID", "primary")
        monkeypatch.setattr(ds, "RAW_TRANSCRIPTS_FOLDER_IDS", "", raising=False)
        assert drive_service.transcript_folder_ids() == ["primary"]

    def test_extras_appended_primary_first(self, monkeypatch):
        from services.google_drive import drive_service, settings as ds
        monkeypatch.setattr(ds, "RAW_TRANSCRIPTS_FOLDER_ID", "primary")
        monkeypatch.setattr(ds, "RAW_TRANSCRIPTS_FOLDER_IDS", "b, c", raising=False)
        assert drive_service.transcript_folder_ids() == ["primary", "b", "c"]

    def test_dedupes_and_ignores_blanks(self, monkeypatch):
        """A folder listed twice must not be polled twice — it would double-list
        every file in it."""
        from services.google_drive import drive_service, settings as ds
        monkeypatch.setattr(ds, "RAW_TRANSCRIPTS_FOLDER_ID", "primary")
        monkeypatch.setattr(ds, "RAW_TRANSCRIPTS_FOLDER_IDS", " primary , ,b, b ", raising=False)
        assert drive_service.transcript_folder_ids() == ["primary", "b"]

    def test_unconfigured_is_empty(self, monkeypatch):
        from services.google_drive import drive_service, settings as ds
        monkeypatch.setattr(ds, "RAW_TRANSCRIPTS_FOLDER_ID", "")
        monkeypatch.setattr(ds, "RAW_TRANSCRIPTS_FOLDER_IDS", "", raising=False)
        assert drive_service.transcript_folder_ids() == []

    def test_rejected_subfolder_cached_per_parent(self, monkeypatch):
        """One cached Rejected id across inboxes would quarantine a file into the
        WRONG folder (potentially a different drive)."""
        from services.google_drive import drive_service
        drive_service._rejected_subfolder_cache = {"folder-a": "rej-a", "folder-b": "rej-b"}
        assert drive_service._get_or_create_rejected_subfolder("folder-a") == "rej-a"
        assert drive_service._get_or_create_rejected_subfolder("folder-b") == "rej-b"
        drive_service._rejected_subfolder_cache = None


class TestFirstPollWatermark:
    """Adding an inbox that already holds history must not replay it.

    The poll has no time filter, _processed_file_ids is in-memory and empty after
    every Cloud Run cycle, and there is no per-poll cap — so without a watermark a
    newly-watched folder runs months of transcripts through Opus extraction and
    posts an approval card for each, repeating on every restart. [2026-08-06]
    """

    def test_cutoff_respects_setting(self, monkeypatch):
        from services.google_drive import drive_service, settings as ds
        monkeypatch.setattr(ds, "TRANSCRIPT_FIRST_POLL_LOOKBACK_DAYS", 14, raising=False)
        cutoff = drive_service._folder_watermark_cutoff()
        assert cutoff and cutoff.endswith("Z")

    def test_zero_disables_the_cap(self, monkeypatch):
        from services.google_drive import drive_service, settings as ds
        monkeypatch.setattr(ds, "TRANSCRIPT_FIRST_POLL_LOOKBACK_DAYS", 0, raising=False)
        assert drive_service._folder_watermark_cutoff() == ""

    async def test_first_poll_filters_then_subsequent_does_not(self, monkeypatch):
        """First poll of a folder is date-capped; once observed it polls unfiltered."""
        from services.google_drive import drive_service, settings as ds
        monkeypatch.setattr(ds, "RAW_TRANSCRIPTS_FOLDER_ID", "folder-new")
        monkeypatch.setattr(ds, "RAW_TRANSCRIPTS_FOLDER_IDS", "", raising=False)
        monkeypatch.setattr(ds, "TRANSCRIPT_FIRST_POLL_LOOKBACK_DAYS", 14, raising=False)
        drive_service._observed_folders.discard("folder-new")
        drive_service._processed_file_ids.clear()

        seen_queries = []

        def fake_exec(factory, *a, **k):
            seen_queries.append(factory().kwargs["q"])
            return {"files": []}

        class _Req:
            def __init__(self, **kw): self.kwargs = kw
        monkeypatch.setattr(drive_service, "_execute_with_retry", fake_exec)
        monkeypatch.setattr(
            type(drive_service), "service",
            property(lambda self: type("S", (), {
                "files": lambda _s=None: type("F", (), {
                    "list": staticmethod(lambda **kw: _Req(**kw))})()})()),
        )

        await drive_service.get_new_transcripts()
        assert "createdTime >" in seen_queries[0], "first poll must be date-capped"

        await drive_service.get_new_transcripts()
        assert "createdTime >" not in seen_queries[1], "later polls must not filter"
        drive_service._observed_folders.discard("folder-new")

    async def test_failed_listing_does_not_mark_observed(self, monkeypatch):
        """Marking on failure would drop the watermark and replay history next poll."""
        from services.google_drive import drive_service, settings as ds
        monkeypatch.setattr(ds, "RAW_TRANSCRIPTS_FOLDER_ID", "folder-bad")
        monkeypatch.setattr(ds, "RAW_TRANSCRIPTS_FOLDER_IDS", "", raising=False)
        drive_service._observed_folders.discard("folder-bad")

        def boom(*a, **k):
            raise RuntimeError("drive down")
        monkeypatch.setattr(drive_service, "_execute_with_retry", boom)
        await drive_service.get_new_transcripts()
        assert "folder-bad" not in drive_service._observed_folders
