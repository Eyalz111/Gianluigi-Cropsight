"""Tests for Phase 13 B4: Full email body storage.

Tests cover:
- body_text parameter in create_email_scan
- Body stored only for relevant/borderline emails
- Body truncated to 50K chars
- email_watcher stores body for relevant emails
- personal_email_scanner stores body for relevant emails
"""

import pytest
from datetime import date
from unittest.mock import MagicMock, patch, AsyncMock


# =========================================================================
# create_email_scan: body_text parameter
# =========================================================================

class TestCreateEmailScanBody:

    @patch("services.supabase_client.supabase_client")
    def test_body_text_included_in_insert(self, mock_sc):
        mock_sc.client.table.return_value.insert.return_value.execute.return_value = MagicMock(
            data=[{"id": "scan-1"}]
        )

        from services.supabase_client import SupabaseClient
        result = SupabaseClient.create_email_scan(
            mock_sc,
            scan_type="constant",
            email_id="msg-1",
            date="2026-04-02",
            sender="test@test.com",
            subject="Hello",
            classification="relevant",
            body_text="This is the full email body text.",
        )

        insert_arg = mock_sc.client.table.return_value.insert.call_args[0][0]
        assert "body_text" in insert_arg
        assert insert_arg["body_text"] == "This is the full email body text."

    @patch("services.supabase_client.supabase_client")
    def test_body_text_none_not_included(self, mock_sc):
        mock_sc.client.table.return_value.insert.return_value.execute.return_value = MagicMock(
            data=[{"id": "scan-1"}]
        )

        from services.supabase_client import SupabaseClient
        SupabaseClient.create_email_scan(
            mock_sc,
            scan_type="constant",
            email_id="msg-1",
            date="2026-04-02",
            body_text=None,
        )

        insert_arg = mock_sc.client.table.return_value.insert.call_args[0][0]
        assert "body_text" not in insert_arg

    @patch("services.supabase_client.supabase_client")
    def test_body_text_empty_not_included(self, mock_sc):
        mock_sc.client.table.return_value.insert.return_value.execute.return_value = MagicMock(
            data=[{"id": "scan-1"}]
        )

        from services.supabase_client import SupabaseClient
        SupabaseClient.create_email_scan(
            mock_sc,
            scan_type="constant",
            email_id="msg-1",
            date="2026-04-02",
            body_text="",
        )

        insert_arg = mock_sc.client.table.return_value.insert.call_args[0][0]
        assert "body_text" not in insert_arg

    @patch("services.supabase_client.supabase_client")
    def test_body_text_truncated_at_50k(self, mock_sc):
        mock_sc.client.table.return_value.insert.return_value.execute.return_value = MagicMock(
            data=[{"id": "scan-1"}]
        )

        long_body = "x" * 60000

        from services.supabase_client import SupabaseClient
        SupabaseClient.create_email_scan(
            mock_sc,
            scan_type="constant",
            email_id="msg-1",
            date="2026-04-02",
            body_text=long_body,
        )

        insert_arg = mock_sc.client.table.return_value.insert.call_args[0][0]
        assert len(insert_arg["body_text"]) == 50000


# =========================================================================
# email_watcher: body stored for relevant/borderline
# =========================================================================

class TestEmailWatcherBodyStorage:

    def test_source_passes_body_for_relevant(self):
        """Verify email_watcher source code passes body_text conditionally."""
        import inspect
        import schedulers.email_watcher as module
        source = inspect.getsource(module)
        assert 'body_text=body if classification in ("relevant", "borderline")' in source

    def test_source_does_not_store_body_unconditionally(self):
        """Body should NOT be stored for all classifications."""
        import inspect
        import schedulers.email_watcher as module
        source = inspect.getsource(module)
        # Should not have a plain body_text=body without condition
        lines = source.split("\n")
        for line in lines:
            if "body_text=body," in line and "if classification" not in line:
                pytest.fail(f"Found unconditional body storage: {line.strip()}")


# =========================================================================
# personal_email_scanner: body stored for relevant/borderline
# =========================================================================

class TestPersonalEmailScannerBodyStorage:

    def test_source_passes_body_for_relevant(self):
        """Verify personal_email_scanner passes body_text conditionally."""
        import inspect
        import schedulers.personal_email_scanner as module
        source = inspect.getsource(module)
        assert 'body_text=full_body if classification in ("relevant", "borderline")' in source

    def test_full_body_initialized_before_use(self):
        """full_body should be initialized to None before the conditional block."""
        import inspect
        import schedulers.personal_email_scanner as module
        source = inspect.getsource(module)
        assert "full_body = None" in source
