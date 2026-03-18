"""Tests for processors/morning_brief.py."""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch


@pytest.fixture(autouse=True)
def mock_settings():
    mock = MagicMock()
    mock.EMAIL_DAILY_SCAN_ENABLED = True
    mock.MORNING_BRIEF_ENABLED = True
    mock.EYAL_EMAIL = "eyal@cropsight.io"
    mock.ROYE_EMAIL = "roye@cropsight.io"
    mock.PAOLO_EMAIL = "paolo@cropsight.io"
    mock.YORAM_EMAIL = "yoram@cropsight.io"
    with patch("config.settings.settings", mock):
        yield mock


# ── helpers ──────────────────────────────────────────────────────────────

def _make_scan(scan_id, sender="unknown@test.com", subject="Hello",
               items=None, scan_type="daily"):
    return {
        "id": scan_id,
        "sender": sender,
        "subject": subject,
        "scan_type": scan_type,
        "extracted_items": items or [{"type": "info", "text": "some item"}],
    }


# Compile helper patches calendar + alerts (both use local imports inside
# compile_morning_brief so we patch at their origin modules).
def _compile_patches():
    """Return a dict of context-manager patches shared by compile tests."""
    return {
        "sc": patch("processors.morning_brief.supabase_client"),
        "cal": patch("services.google_calendar.calendar_service"),
        "cal_filter": patch("guardrails.calendar_filter.is_cropsight_meeting", return_value=True),
    }


# =========================================================================
# TestCategorizeSource
# =========================================================================

class TestCategorizeSource:

    def test_team_email(self):
        with patch("config.team.is_team_email", return_value=True):
            from processors.morning_brief import _categorize_source
            assert _categorize_source("roye@cropsight.io", "standup notes") == "team"

    def test_investor_keyword(self):
        with patch("config.team.is_team_email", return_value=False):
            from processors.morning_brief import _categorize_source
            assert _categorize_source("john@vc.com", "Re: investor update Q1") == "investor"

    def test_legal_keyword(self):
        with patch("config.team.is_team_email", return_value=False):
            from processors.morning_brief import _categorize_source
            # "legal" is in SENSITIVE_KEYWORDS and matched to "legal" category
            assert _categorize_source("attorney@law.com", "Legal review") == "legal"

    def test_other_default(self):
        with patch("config.team.is_team_email", return_value=False), \
             patch("processors.morning_brief.supabase_client") as mock_sc:
            mock_sc.list_entities.return_value = []
            from processors.morning_brief import _categorize_source
            assert _categorize_source("random@gmail.com", "weekend plans") == "other"


# =========================================================================
# TestCompileMorningBrief
# =========================================================================

class TestCompileMorningBrief:

    @pytest.mark.asyncio
    async def test_empty_when_no_items(self):
        with patch("processors.morning_brief.supabase_client") as mock_sc, \
             patch("services.google_calendar.calendar_service") as mock_cal:
            mock_sc.get_unapproved_email_scans.return_value = []
            mock_sc.get_pending_prep_outlines.return_value = []
            mock_sc.get_active_weekly_review_session.return_value = None
            mock_cal.get_todays_events = AsyncMock(return_value=[])
            from processors.morning_brief import compile_morning_brief
            brief = await compile_morning_brief()
        assert brief["sections"] == []
        assert brief["scan_ids"] == []

    @pytest.mark.asyncio
    async def test_merges_daily_and_constant(self):
        daily = [_make_scan("d1", sender="someone@vc.com", subject="funding update",
                            items=[{"type": "action", "text": "Follow up"}])]
        constant = [_make_scan("c1", sender="roye@cropsight.io", subject="standup",
                               items=[{"type": "info", "text": "All good"}])]

        def side_effect(scan_type, date_from):
            if scan_type == "daily":
                return daily
            return constant

        with patch("processors.morning_brief.supabase_client") as mock_sc, \
             patch("processors.morning_brief._categorize_source", return_value="other"), \
             patch("services.google_calendar.calendar_service") as mock_cal:
            mock_sc.get_unapproved_email_scans.side_effect = side_effect
            mock_cal.get_todays_events = AsyncMock(return_value=[])
            from processors.morning_brief import compile_morning_brief
            brief = await compile_morning_brief()
        types = [s["type"] for s in brief["sections"]]
        assert "email_scan" in types
        assert "constant_layer" in types

    @pytest.mark.asyncio
    async def test_includes_calendar_events(self):
        events = [{"title": "Team Sync", "start": "2026-03-16T09:00:00"}]
        with patch("processors.morning_brief.supabase_client") as mock_sc, \
             patch("services.google_calendar.calendar_service") as mock_cal, \
             patch("guardrails.calendar_filter.is_cropsight_meeting", return_value=True):
            mock_sc.get_unapproved_email_scans.return_value = []
            mock_cal.get_todays_events = AsyncMock(return_value=events)
            from processors.morning_brief import compile_morning_brief
            brief = await compile_morning_brief()
        cal_sections = [s for s in brief["sections"] if s["type"] == "calendar"]
        assert len(cal_sections) == 1
        assert cal_sections[0]["events"][0]["title"] == "Team Sync"
        assert brief["stats"]["calendar_events"] == 1

    @pytest.mark.asyncio
    async def test_collects_scan_ids(self):
        scans = [_make_scan("id-1"), _make_scan("id-2")]
        with patch("processors.morning_brief.supabase_client") as mock_sc, \
             patch("processors.morning_brief._categorize_source", return_value="other"), \
             patch("services.google_calendar.calendar_service") as mock_cal:
            mock_sc.get_unapproved_email_scans.side_effect = \
                lambda **kw: scans if kw.get("scan_type") == "daily" else []
            mock_cal.get_todays_events = AsyncMock(return_value=[])
            from processors.morning_brief import compile_morning_brief
            brief = await compile_morning_brief()
        assert "id-1" in brief["scan_ids"]
        assert "id-2" in brief["scan_ids"]


# =========================================================================
# TestFormatMorningBrief
# =========================================================================

class TestFormatMorningBrief:

    def test_empty_brief_returns_empty(self):
        from processors.morning_brief import format_morning_brief
        assert format_morning_brief({"sections": [], "stats": {}}) == ""

    def test_source_categorization_no_raw_metadata(self):
        from processors.morning_brief import format_morning_brief
        brief = {
            "sections": [{
                "type": "email_scan",
                "title": "Email Intelligence",
                "items": [{"type": "info", "text": "Something happened",
                           "_source_category": "investor", "_sensitive": True}],
            }],
            "stats": {"email_scans": 1, "constant_items": 0, "calendar_events": 0},
        }
        result = format_morning_brief(brief)
        assert "investor correspondence" in result
        # Must not contain any raw email address or subject
        assert "@" not in result
        assert "Subject" not in result

    def test_sensitive_flagged(self):
        from processors.morning_brief import format_morning_brief
        brief = {
            "sections": [{
                "type": "email_scan",
                "title": "Email Intelligence",
                "items": [{"type": "info", "text": "Funding term sheet",
                           "_source_category": "investor", "_sensitive": True}],
            }],
            "stats": {"email_scans": 1, "constant_items": 0},
        }
        result = format_morning_brief(brief)
        assert "[SENSITIVE]" in result

    def test_truncation(self):
        from processors.morning_brief import format_morning_brief
        # Need enough unique categories each with 10 items of long text to
        # exceed 4000 chars.  The formatter caps at 10 items per category,
        # so use many categories.
        categories = ["team", "investor", "legal", "partner", "other"]
        items = []
        for cat in categories:
            for _ in range(10):
                items.append({
                    "type": "info",
                    "text": "X" * 120,
                    "_source_category": cat,
                    "_sensitive": False,
                })
        brief = {
            "sections": [{
                "type": "email_scan",
                "title": "Email Intelligence",
                "items": items,
            }],
            "stats": {"email_scans": len(items), "constant_items": 0},
        }
        result = format_morning_brief(brief)
        assert len(result) <= 4100  # 4000 + truncation suffix
        assert "truncated" in result


# =========================================================================
# TestTriggerMorningBrief
# =========================================================================

class TestTriggerMorningBrief:

    @pytest.mark.asyncio
    async def test_runs_scan_then_compiles(self):
        call_order = []

        async def mock_scan():
            call_order.append("scan")
            return {"scanned": 5}

        async def mock_compile():
            call_order.append("compile")
            return {"sections": [], "stats": {}, "scan_ids": []}

        with patch("schedulers.personal_email_scanner.personal_email_scanner") as mock_scanner, \
             patch("processors.morning_brief.compile_morning_brief", side_effect=mock_compile):
            mock_scanner.run_daily_scan = mock_scan
            from processors.morning_brief import trigger_morning_brief
            await trigger_morning_brief()
        assert call_order == ["scan", "compile"]

    @pytest.mark.asyncio
    async def test_silent_when_nothing(self):
        with patch("schedulers.personal_email_scanner.personal_email_scanner") as mock_scanner, \
             patch("processors.morning_brief.compile_morning_brief", new_callable=AsyncMock,
                   return_value={"sections": [], "stats": {}, "scan_ids": []}):
            mock_scanner.run_daily_scan = AsyncMock(return_value={})
            from processors.morning_brief import trigger_morning_brief
            result = await trigger_morning_brief()
        assert result is None

    @pytest.mark.asyncio
    async def test_submits_for_approval(self):
        brief = {
            "sections": [{"type": "email_scan", "title": "t",
                          "items": [{"type": "info", "text": "x",
                                     "_source_category": "other",
                                     "_sensitive": False}]}],
            "stats": {"email_scans": 1},
            "scan_ids": ["s1"],
        }
        mock_submit = AsyncMock()
        with patch("schedulers.personal_email_scanner.personal_email_scanner") as mock_scanner, \
             patch("processors.morning_brief.compile_morning_brief",
                   new_callable=AsyncMock, return_value=brief), \
             patch("guardrails.approval_flow.submit_for_approval", mock_submit):
            mock_scanner.run_daily_scan = AsyncMock(return_value={})
            from processors.morning_brief import trigger_morning_brief
            result = await trigger_morning_brief()
        assert result is not None
        mock_submit.assert_called_once()
        call_kwargs = mock_submit.call_args
        assert call_kwargs.kwargs.get("content_type") == "morning_brief"


# =========================================================================
# Upcoming Review Calendar Check Tests (Item 11)
# =========================================================================

class TestUpcomingReviewInBrief:
    """Test section 7: calendar check for upcoming weekly review."""

    @pytest.mark.asyncio
    async def test_review_event_found_no_session(self):
        """Review event on calendar, no active session → shows in brief."""
        mock_cal = MagicMock()
        mock_cal.get_todays_events = AsyncMock(return_value=[
            {"title": "CropSight: Weekly Review with Gianluigi", "start": "2026-03-18T14:00:00Z"},
        ])

        mock_scheduler = MagicMock()
        mock_scheduler._is_review_event = MagicMock(return_value=True)

        with patch("processors.morning_brief.supabase_client") as mock_db, \
             patch.dict("sys.modules", {
                 "services.google_calendar": MagicMock(calendar_service=mock_cal),
                 "schedulers.weekly_review_scheduler": MagicMock(weekly_review_scheduler=mock_scheduler),
             }):
            mock_db.get_unapproved_email_scans.return_value = []
            mock_db.get_active_weekly_review_session.return_value = None
            mock_db.get_pending_prep_outlines.return_value = []

            from processors.morning_brief import compile_morning_brief
            brief = await compile_morning_brief()
            types = [s["type"] for s in brief["sections"]]
            assert "upcoming_review" in types

    @pytest.mark.asyncio
    async def test_review_event_found_session_exists(self):
        """Review event on calendar, active session exists → not shown (avoid duplicate)."""
        mock_cal = MagicMock()
        mock_cal.get_todays_events = AsyncMock(return_value=[
            {"title": "CropSight: Weekly Review with Gianluigi", "start": "2026-03-18T14:00:00Z"},
        ])

        mock_scheduler = MagicMock()
        mock_scheduler._is_review_event = MagicMock(return_value=True)

        with patch("processors.morning_brief.supabase_client") as mock_db, \
             patch.dict("sys.modules", {
                 "services.google_calendar": MagicMock(calendar_service=mock_cal),
                 "schedulers.weekly_review_scheduler": MagicMock(weekly_review_scheduler=mock_scheduler),
             }):
            mock_db.get_unapproved_email_scans.return_value = []
            # First call for section 6, second for section 7
            mock_db.get_active_weekly_review_session.return_value = {"id": "s-1", "status": "ready", "week_number": 12}
            mock_db.get_pending_prep_outlines.return_value = []

            from processors.morning_brief import compile_morning_brief
            brief = await compile_morning_brief()
            types = [s["type"] for s in brief["sections"]]
            assert "upcoming_review" not in types

    @pytest.mark.asyncio
    async def test_no_review_event(self):
        """No review event on calendar → no upcoming_review section."""
        mock_cal = MagicMock()
        mock_cal.get_todays_events = AsyncMock(return_value=[
            {"title": "Team standup", "start": "2026-03-18T09:00:00Z"},
        ])

        mock_scheduler = MagicMock()
        mock_scheduler._is_review_event = MagicMock(return_value=False)

        with patch("processors.morning_brief.supabase_client") as mock_db, \
             patch.dict("sys.modules", {
                 "services.google_calendar": MagicMock(calendar_service=mock_cal),
                 "schedulers.weekly_review_scheduler": MagicMock(weekly_review_scheduler=mock_scheduler),
             }):
            mock_db.get_unapproved_email_scans.return_value = []
            mock_db.get_active_weekly_review_session.return_value = None
            mock_db.get_pending_prep_outlines.return_value = []

            from processors.morning_brief import compile_morning_brief
            brief = await compile_morning_brief()
            types = [s["type"] for s in brief["sections"]]
            assert "upcoming_review" not in types


class TestUpcomingReviewFormatting:
    """Test formatting of upcoming_review section."""

    def test_format_upcoming_review_with_time(self):
        from processors.morning_brief import format_morning_brief
        brief = {
            "sections": [{"type": "upcoming_review", "title": "Weekly Review", "time": "14:00"}],
            "stats": {},
        }
        result = format_morning_brief(brief)
        assert "at 14:00" in result
        assert "prep starts 3h before" in result

    def test_format_upcoming_review_no_time(self):
        from processors.morning_brief import format_morning_brief
        brief = {
            "sections": [{"type": "upcoming_review", "title": "Weekly Review", "time": ""}],
            "stats": {},
        }
        result = format_morning_brief(brief)
        assert "today" in result
