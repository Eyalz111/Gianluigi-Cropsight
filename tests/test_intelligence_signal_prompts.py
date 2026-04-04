"""Tests for intelligence signal prompts and formatters."""

import pytest

from processors.intelligence_signal_prompts import (
    system_prompt_synthesis,
    user_prompt_synthesis,
    system_prompt_script,
    user_prompt_script,
    format_telegram_notification,
    format_email_html,
    format_email_plain,
    BANNED_PHRASES,
)


class TestSystemPromptSynthesis:
    def test_contains_character_traits(self):
        prompt = system_prompt_synthesis()

        assert "journalist" in prompt.lower()
        assert "no opinions" in prompt.lower()
        assert "no recommendations" in prompt.lower()

    def test_contains_banned_phrases(self):
        prompt = system_prompt_synthesis()

        for phrase in BANNED_PHRASES[:3]:
            assert phrase in prompt

    def test_mentions_exploration_corner(self):
        prompt = system_prompt_synthesis()

        assert "exploration corner" in prompt.lower()

    def test_mentions_empty_sections_allowed(self):
        prompt = system_prompt_synthesis()

        assert "empty section" in prompt.lower() or "padding" in prompt.lower()


class TestUserPromptSynthesis:
    def test_includes_context_data(self):
        context = {
            "week_number": 14,
            "year": 2026,
            "active_crops": ["wheat", "coffee"],
            "active_regions": ["EU", "Brazil"],
            "known_competitors": [{"name": "SatYield"}],
            "last_signal_flags": [],
        }
        research = {"market_overview": "Wheat prices rose 4%."}

        prompt = user_prompt_synthesis(context, research)

        assert "W14/2026" in prompt
        assert "wheat" in prompt
        assert "SatYield" in prompt

    def test_includes_research_data(self):
        context = {
            "week_number": 14,
            "year": 2026,
            "active_crops": [],
            "active_regions": [],
            "known_competitors": [],
            "last_signal_flags": [],
        }
        research = {"science_tech": "New SAR satellite launched."}

        prompt = user_prompt_synthesis(context, research)

        assert "New SAR satellite" in prompt

    def test_includes_section_structure(self):
        context = {
            "week_number": 14,
            "year": 2026,
            "active_crops": [],
            "active_regions": [],
            "known_competitors": [],
            "last_signal_flags": [],
        }

        prompt = user_prompt_synthesis(context, {})

        assert "FLAGS" in prompt
        assert "Commodity Pulse" in prompt
        assert "Exploration Corner" in prompt
        assert "This Week's Angle" in prompt

    def test_includes_continuity_flags(self):
        context = {
            "week_number": 14,
            "year": 2026,
            "active_crops": [],
            "active_regions": [],
            "known_competitors": [],
            "last_signal_flags": [
                {"flag": "Brazil drought", "urgency": "high"},
            ],
        }

        prompt = user_prompt_synthesis(context, {})

        assert "Brazil drought" in prompt
        assert "Last Week" in prompt


class TestScriptPrompts:
    def test_system_prompt_mentions_narrator(self):
        prompt = system_prompt_script()

        assert "narrator" in prompt.lower() or "anchor" in prompt.lower()

    def test_user_prompt_includes_content(self):
        prompt = user_prompt_script("Some signal content here.")

        assert "Some signal content here" in prompt
        assert "full Intelligence Signal is attached" in prompt


class TestFormatTelegramNotification:
    def test_basic_notification(self):
        msg = format_telegram_notification(
            signal_id="signal-w14-2026",
            drive_link="https://docs.google.com/doc/123",
            week_number=14,
        )

        assert "W14" in msg
        assert "https://docs.google.com/doc/123" in msg
        assert "Approve via CropSight Ops" in msg

    def test_includes_flags(self):
        msg = format_telegram_notification(
            signal_id="signal-w14-2026",
            drive_link="https://example.com",
            week_number=14,
            flags=[
                {"flag": "Brazil drought critical", "urgency": "high"},
                {"flag": "New competitor funding", "urgency": "medium"},
            ],
        )

        assert "Brazil drought critical" in msg
        assert "New competitor funding" in msg

    def test_no_flags_message(self):
        msg = format_telegram_notification(
            signal_id="signal-w14-2026",
            drive_link="https://example.com",
            week_number=14,
            flags=[],
        )

        assert "No flags this week" in msg

    def test_research_source_warning(self):
        msg = format_telegram_notification(
            signal_id="signal-w14-2026",
            drive_link="https://example.com",
            week_number=14,
            research_source="claude_search",
        )

        assert "backup research" in msg
        assert "Perplexity unavailable" in msg

    def test_no_warning_for_perplexity(self):
        msg = format_telegram_notification(
            signal_id="signal-w14-2026",
            drive_link="https://example.com",
            week_number=14,
            research_source="perplexity",
        )

        assert "backup research" not in msg

    def test_watchlist_changes(self):
        msg = format_telegram_notification(
            signal_id="signal-w14-2026",
            drive_link="https://example.com",
            week_number=14,
            watchlist_changes={
                "promoted": ["Aydi"],
                "deactivated": ["Gro Intelligence"],
                "discovered": [],
            },
        )

        assert "promoted" in msg.lower()
        assert "Aydi" in msg
        assert "deactivated" in msg.lower()
        assert "Gro Intelligence" in msg


class TestFormatEmailHtml:
    def test_contains_html_structure(self):
        html = format_email_html(
            signal_content="## Test Section\nSome content.",
            drive_link="https://example.com",
            week_number=14,
            year=2026,
        )

        assert "<!DOCTYPE html>" in html
        assert "CropSight Intelligence Signal" in html
        assert "Week 14" in html
        assert "https://example.com" in html

    def test_uses_teaser_not_full_content(self):
        html = format_email_html(
            signal_content="## FLAGS\nSome flag\n\n## The Problem\nFirst sentence. Second sentence. Third sentence.\n\n## More\nOther content.",
            drive_link="https://example.com",
            week_number=14,
            year=2026,
        )

        assert "Full report attached" in html
        assert "First sentence" in html
        # Should NOT have the full report inline
        assert "Other content" not in html

    def test_includes_drive_links(self):
        html = format_email_html(
            signal_content="## Topic\nContent here.",
            drive_link="https://example.com",
            week_number=14,
            year=2026,
            video_link="https://video.example.com",
            audio_link="https://audio.example.com",
        )

        assert "https://example.com" in html
        assert "https://video.example.com" in html
        assert "https://audio.example.com" in html


class TestFormatEmailPlain:
    def test_includes_teaser_and_link(self):
        plain = format_email_plain(
            signal_content="## Topic\nThe market moved 5%. Second sentence here.",
            drive_link="https://example.com/doc",
        )

        assert "market moved 5%" in plain
        assert "https://example.com/doc" in plain
        assert "Full report attached" in plain
        assert "Gianluigi" in plain
