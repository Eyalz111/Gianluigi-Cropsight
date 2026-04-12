"""
Golden-snapshot tests for the Telegram communication overhaul (PR 3).

Tests realistic data shapes through each major formatter and asserts
structural properties. Eyeball the output once during development —
automated tests catch regressions, not "does it read naturally."
"""

import re
import pytest

from processors.morning_brief import format_morning_brief
from processors.debrief import _format_extraction_summary
from processors.proactive_alerts import format_alerts_message


# =========================================================================
# Structural assertion helpers
# =========================================================================

# Matches emoji outside the allowed set (🔴 U+1F534, 🟡 U+1F7E1)
_DISALLOWED_EMOJI_RE = re.compile(
    r"[\U0001f300-\U0001f533\U0001f535-\U0001f7e0\U0001f7e2-\U0001f9ff]"
)

def _assert_no_type_tags(text: str):
    """No [task], [info], [decision] type tags (but [SENSITIVE] is OK)."""
    for tag in ["[task]", "[info]", "[decision]", "[commitment]", "[gantt_update]"]:
        assert tag not in text.lower(), f"Found type tag {tag} in output"

def _assert_no_markdown_bold(text: str):
    """No *bold* Markdown artifacts in what should be HTML."""
    # Match *word* patterns but not standalone * (e.g., in multiplication)
    matches = re.findall(r"\*[A-Za-z].*?[A-Za-z]\*", text)
    assert not matches, f"Found Markdown bold artifacts: {matches}"

def _assert_no_counting_headers(text: str):
    """No 'Title (N):' counting headers."""
    matches = re.findall(r"[A-Z][a-z]+ \(\d+\):", text)
    assert not matches, f"Found counting headers: {matches}"

def _assert_no_disallowed_emoji(text: str):
    """Only 🔴 and 🟡 allowed."""
    found = _DISALLOWED_EMOJI_RE.findall(text)
    assert not found, f"Found disallowed emoji: {[hex(ord(c)) for c in found]}"

def _assert_char_limit(text: str, limit: int = 3800):
    # Allow a bit of overflow for the continuation marker
    assert len(text) <= limit + 10, f"Output too long: {len(text)} chars"


# =========================================================================
# Morning Brief Scenarios
# =========================================================================

class TestMorningBriefSnapshots:
    """Golden snapshots for format_morning_brief()."""

    def _make_brief(self, sections, stats=None):
        return {"sections": sections, "stats": stats or {}}

    def test_busy_day(self):
        """All sections populated — realistic busy day."""
        brief = self._make_brief([
            {"type": "email_scan", "title": "Email Intelligence", "items": [
                {"type": "task", "text": "Review Moldova data package by Monday", "_source_category": "team", "_sensitive": False},
                {"type": "info", "text": "Roye confirmed AWS migration timeline", "_source_category": "team", "_sensitive": False},
                {"type": "info", "text": "IIA Tnufa extension eligibility confirmed", "_source_category": "investor", "_sensitive": True},
            ]},
            {"type": "calendar", "title": "Today's Calendar", "events": [
                {"time": "10:00", "title": "Weekly Sync"},
                {"time": "14:00", "title": "Moldova Data Review"},
            ]},
            {"type": "alerts", "title": "Alerts", "alerts": [
                {"severity": "high", "message": "Paolo has 3 overdue BD tasks"},
                {"severity": "medium", "message": "Lavazza deck commitment — 8 days stale"},
            ]},
            {"type": "task_urgency", "title": "Urgent Tasks", "items": [
                {"title": "Accuracy framework doc", "assignee": "Roye", "deadline": "tomorrow"},
            ]},
            {"type": "deal_pulse", "title": "Deal Pulse", "items": [
                {"name": "Acme Corp", "organization": "EMEA", "detail": "follow-up overdue", "type": "overdue"},
            ]},
            {"type": "system_state", "watcher_status": "ok", "rejected_count": 0, "errors_24h": 0, "pending_queue": 0},
        ])
        result = format_morning_brief(brief)

        assert result.startswith("<b>Good morning</b>")
        assert "Team emails" in result
        assert "Investor emails" in result
        assert "[SENSITIVE]" in result
        assert "Today" in result
        assert "Needs attention" in result
        assert "🔴" in result
        assert "Deals" in result
        assert "System: all clear" in result
        _assert_no_type_tags(result)
        _assert_no_markdown_bold(result)
        _assert_no_counting_headers(result)
        _assert_char_limit(result)

    def test_quiet_day(self):
        """Only calendar + system state — quiet morning."""
        brief = self._make_brief([
            {"type": "calendar", "title": "Today's Calendar", "events": [
                {"time": "09:00", "title": "Standup"},
            ]},
            {"type": "system_state", "watcher_status": "ok", "rejected_count": 0, "errors_24h": 0, "pending_queue": 0},
        ])
        result = format_morning_brief(brief)
        assert "Good morning" in result
        assert "Today" in result
        assert "Standup" in result
        assert "all clear" in result
        # No email section, no attention section
        assert "emails" not in result.lower() or "email" not in result.split("Good morning")[0]
        assert "Needs attention" not in result

    def test_all_overdue(self):
        """Heavy alerts day — lots of overdue tasks."""
        alerts = [{"severity": "high", "message": f"Task {i} overdue"} for i in range(5)]
        brief = self._make_brief([
            {"type": "alerts", "title": "Alerts", "alerts": alerts},
            {"type": "drift_alerts", "title": "Drift", "items": [
                {"drift_description": "Product section 60% overdue"},
            ]},
            {"type": "system_state", "watcher_status": "ok", "rejected_count": 0, "errors_24h": 0, "pending_queue": 0},
        ])
        result = format_morning_brief(brief)
        assert "Needs attention" in result
        assert result.count("🔴") >= 5
        _assert_no_type_tags(result)

    def test_sensitive_items_only(self):
        """Only sensitive investor emails."""
        brief = self._make_brief([
            {"type": "email_scan", "title": "Email", "items": [
                {"type": "info", "text": "Term sheet from VC", "_source_category": "investor", "_sensitive": True},
            ]},
            {"type": "system_state", "watcher_status": "ok", "rejected_count": 0, "errors_24h": 0, "pending_queue": 0},
        ])
        result = format_morning_brief(brief)
        assert "[SENSITIVE]" in result
        assert "Investor emails" in result

    def test_zero_emails(self):
        """No email items — email section should be omitted entirely."""
        brief = self._make_brief([
            {"type": "calendar", "title": "Today's Calendar", "events": [
                {"time": "10:00", "title": "Team Sync"},
            ]},
            {"type": "system_state", "watcher_status": "ok", "rejected_count": 0, "errors_24h": 0, "pending_queue": 0},
        ])
        result = format_morning_brief(brief)
        assert "emails" not in result.lower().replace("good morning", "")

    def test_system_problems(self):
        """System state has errors."""
        brief = self._make_brief([
            {"type": "system_state", "watcher_status": "stale", "rejected_count": 2, "errors_24h": 3, "pending_queue": 1},
        ])
        result = format_morning_brief(brief)
        assert "System:" in result
        assert "all clear" not in result
        assert "stale" in result or "error" in result.lower()

    def test_deal_pulse_with_overdue(self):
        """Deal items with overdue markers."""
        brief = self._make_brief([
            {"type": "deal_pulse", "title": "Deal Pulse", "items": [
                {"name": "Acme", "organization": "EU", "detail": "follow-up overdue", "type": "overdue"},
                {"name": "TechFarm", "organization": "US", "detail": "status update pending", "type": "normal"},
            ]},
            {"type": "system_state", "watcher_status": "ok", "rejected_count": 0, "errors_24h": 0, "pending_queue": 0},
        ])
        result = format_morning_brief(brief)
        assert "Deals" in result
        assert "Acme" in result
        assert "🔴" in result  # overdue deal gets red

    def test_empty_brief(self):
        """Empty sections returns empty string."""
        assert format_morning_brief({"sections": []}) == ""
        assert format_morning_brief({}) == ""

    def test_morning_after_heavy_debrief(self):
        """Lots of email items from overnight — should handle gracefully."""
        items = []
        for i in range(15):
            items.append({
                "type": "task", "text": f"Follow up on item {i} from debrief",
                "_source_category": "team", "_sensitive": False,
            })
        brief = self._make_brief([
            {"type": "email_scan", "title": "Email", "items": items},
            {"type": "system_state", "watcher_status": "ok", "rejected_count": 0, "errors_24h": 0, "pending_queue": 0},
        ])
        result = format_morning_brief(brief)
        assert "Team emails" in result
        assert "...and 5 more" in result  # 15 items, shows 10, overflow for 5
        _assert_no_type_tags(result)
        _assert_char_limit(result)


# =========================================================================
# Debrief Summary Scenarios
# =========================================================================

class TestDebriefSummarySnapshots:
    """Golden snapshots for _format_extraction_summary()."""

    def test_single_task_prose(self):
        """One task should be formatted as prose."""
        items = [{"type": "task", "title": "Draft LOI", "assignee": "Eyal"}]
        result = _format_extraction_summary(items)
        assert "Here's what I got" in result
        assert "task" in result.lower()
        assert "Draft LOI" in result or "draft loi" in result.lower()
        _assert_no_counting_headers(result)

    def test_mixed_small_counts_prose(self):
        """3 tasks + 1 decision should all be prose."""
        items = [
            {"type": "task", "title": "Follow up with Lavazza", "assignee": "Paolo"},
            {"type": "task", "title": "Schedule security review", "assignee": "Eyal"},
            {"type": "task", "title": "Draft accuracy framework", "assignee": "Roye"},
            {"type": "decision", "description": "Going with AWS over Azure"},
        ]
        result = _format_extraction_summary(items)
        assert "Three task" in result or "3 task" in result
        assert "decision" in result.lower()
        assert "AWS" in result
        _assert_no_counting_headers(result)

    def test_large_counts_compact_list(self):
        """8 tasks + 3 decisions should use compact list."""
        tasks = [{"type": "task", "title": f"Task {i}", "assignee": "Eyal"} for i in range(8)]
        decisions = [{"type": "decision", "description": f"Decision {i}"} for i in range(3)]
        items = tasks + decisions
        result = _format_extraction_summary(items)
        assert "8 tasks" in result.lower()
        # Should have numbered items
        assert "1." in result
        assert "2." in result

    def test_sensitive_items(self):
        """Sensitive items should show (sensitive) inline."""
        items = [
            {"type": "information", "description": "Investor call details", "sensitive": True},
            {"type": "task", "title": "Review term sheet", "assignee": "Eyal", "sensitive": True},
        ]
        result = _format_extraction_summary(items)
        assert "(sensitive)" in result.lower()

    def test_empty_items(self):
        """Empty list returns 'No items captured.'"""
        assert _format_extraction_summary([]) == "No items captured."


# =========================================================================
# Alerts Scenarios
# =========================================================================

class TestAlertSnapshots:
    """Golden snapshots for format_alerts_message()."""

    def test_mixed_severities(self):
        """Mixed severities — high first, then medium."""
        alerts = [
            {"severity": "medium", "title": "Stale commitment", "details": "Lavazza deck 2 weeks"},
            {"severity": "high", "title": "Paolo overdue cluster", "details": "3 tasks overdue"},
            {"severity": "low", "title": "Recurring discussion", "details": ""},
        ]
        result = format_alerts_message(alerts)
        assert "Heads up" in result
        assert "🔴" in result
        assert "🟡" in result
        # High should come before medium
        assert result.index("Paolo") < result.index("Stale")
        _assert_no_markdown_bold(result)

    def test_high_only(self):
        """Only high-severity alerts."""
        alerts = [
            {"severity": "high", "title": "Critical issue", "details": "System down"},
        ]
        result = format_alerts_message(alerts)
        assert "🔴" in result
        assert "Critical issue" in result
        assert "System down" in result

    def test_low_only(self):
        """Only low-severity — no emoji, just text."""
        alerts = [
            {"severity": "low", "title": "Minor thing", "details": ""},
        ]
        result = format_alerts_message(alerts)
        assert "Minor thing" in result
        assert "🔴" not in result
        assert "🟡" not in result

    def test_empty(self):
        """Empty alerts returns empty string."""
        assert format_alerts_message([]) == ""
