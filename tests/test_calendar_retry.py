"""
Tests for Google Calendar idle-wake retry coverage (audit P3-03).

Calendar was the only Google service with no _execute_with_retry wrapper, so a
Cloud Run idle-then-wake cycle (stale httplib2 socket) made every event-read
throw → caught → `[]`, indistinguishable from a genuinely empty calendar. These
tests verify:
  - _execute_with_retry mirrors the Drive factory pattern (retry + null _service)
  - the public read methods set/clear the `last_fetch_failed` signal so callers
    can distinguish "fetch failed" from "no meetings"
"""

from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

from services.google_calendar import GoogleCalendarService


def _make_service():
    """A calendar service with mocked transport (no real OAuth)."""
    svc = GoogleCalendarService()
    svc._service = MagicMock()
    svc._credentials = MagicMock()
    return svc


class TestCalendarExecuteWithRetry:
    """The _execute_with_retry helper (factory form, mirrors Drive)."""

    def test_nulls_service_on_broken_pipe_then_succeeds(self):
        svc = _make_service()
        first = MagicMock()
        first.execute.side_effect = BrokenPipeError(32, "Broken pipe")
        second = MagicMock()
        second.execute.return_value = {"items": []}

        calls = {"n": 0}

        def factory():
            calls["n"] += 1
            return first if calls["n"] == 1 else second

        with patch("time.sleep"):
            result = svc._execute_with_retry(factory, max_retries=3, base_delay=0.001)

        assert result == {"items": []}
        assert calls["n"] == 2
        assert svc._service is None  # nulled so the next .service access rebuilds

    def test_propagates_after_max_attempts(self):
        svc = _make_service()
        req = MagicMock()
        req.execute.side_effect = ConnectionError("refused")
        with patch("time.sleep"), pytest.raises(ConnectionError):
            svc._execute_with_retry(lambda: req, max_retries=3, base_delay=0.001)
        assert req.execute.call_count == 3

    def test_does_not_retry_non_transient(self):
        svc = _make_service()
        req = MagicMock()
        req.execute.side_effect = ValueError("bad argument")
        with patch("time.sleep"), pytest.raises(ValueError):
            svc._execute_with_retry(lambda: req, max_retries=3, base_delay=0.001)
        assert req.execute.call_count == 1

    def test_retries_transient_string_error(self):
        """5xx/rate-limit errors whose type isn't OSError are still retried."""
        svc = _make_service()
        failing = MagicMock()
        failing.execute.side_effect = Exception("HttpError 503: backend unavailable")
        ok = MagicMock()
        ok.execute.return_value = {"items": []}
        n = {"i": 0}

        def factory():
            n["i"] += 1
            return failing if n["i"] == 1 else ok

        with patch("time.sleep"):
            result = svc._execute_with_retry(factory, max_retries=3, base_delay=0.001)
        assert result == {"items": []}


class TestCalendarReadSignals:
    """Public reads set last_fetch_failed correctly and survive an idle-wake pipe."""

    @pytest.mark.asyncio
    async def test_get_events_for_date_sets_failed_on_sustained_outage(self):
        svc = _make_service()
        # Every attempt fails — a genuine sustained outage, not a blip.
        svc._service.events.return_value.list.return_value.execute.side_effect = (
            BrokenPipeError(32, "Broken pipe")
        )
        svc._build_service = MagicMock(return_value=svc._service)

        with patch("time.sleep"):
            result = await svc.get_events_for_date(datetime.now(timezone.utc))

        assert result == []
        assert svc.last_fetch_failed is True

    @pytest.mark.asyncio
    async def test_get_events_clears_failed_on_success(self):
        svc = _make_service()
        svc.last_fetch_failed = True  # pre-set to verify it gets cleared
        svc._service.events.return_value.list.return_value.execute.return_value = {
            "items": [{
                "id": "e1",
                "summary": "Standup",
                "start": {"dateTime": "2026-06-12T09:00:00Z"},
                "end": {"dateTime": "2026-06-12T09:30:00Z"},
            }]
        }

        result = await svc.get_events_for_date(datetime.now(timezone.utc))

        assert len(result) == 1
        assert svc.last_fetch_failed is False

    @pytest.mark.asyncio
    async def test_get_upcoming_events_retries_then_succeeds(self):
        svc = _make_service()
        execs = {"n": 0}

        def make_list(**kwargs):
            req = MagicMock()

            def _execute():
                execs["n"] += 1
                if execs["n"] == 1:
                    raise BrokenPipeError(32, "Broken pipe")
                return {"items": []}

            req.execute = _execute
            return req

        svc._service.events.return_value.list.side_effect = make_list
        # _execute_with_retry nulls _service between attempts; the property
        # rebuilds via _build_service — redirect that to our mock.
        svc._build_service = MagicMock(return_value=svc._service)

        with patch("time.sleep"):
            result = await svc.get_upcoming_events(days=7)

        assert result == []
        assert execs["n"] == 2  # 1 fail + 1 retry success
        assert svc.last_fetch_failed is False
