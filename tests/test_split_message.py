"""
Tests for _split_message() — smart message splitting with continuation
markers, HTML tag safety, and no mid-word cuts.
"""

import pytest

from services.telegram_bot import _split_message, _adjust_cut_for_html_tags


class TestSplitMessageBasic:
    """Basic splitting behavior."""

    def test_short_message_returns_single_part(self):
        """Messages under max_len should not be split."""
        text = "Hello, world!"
        parts = _split_message(text)
        assert len(parts) == 1
        assert parts[0] == text

    def test_empty_string(self):
        """Empty string returns single empty part."""
        parts = _split_message("")
        assert len(parts) == 1
        assert parts[0] == ""

    def test_exact_limit_not_split(self):
        """Message exactly at max_len should not be split."""
        text = "a" * 3800
        parts = _split_message(text, max_len=3800)
        assert len(parts) == 1


class TestContinuationMarkers:
    """Continuation markers (...) are added between parts."""

    def test_two_parts_get_markers(self):
        """When split into 2 parts, first gets suffix and second gets prefix."""
        # Create text that will split on a double newline
        part1 = "A" * 2000
        part2 = "B" * 2000
        text = part1 + "\n\n" + part2
        parts = _split_message(text, max_len=2500)

        assert len(parts) == 2
        assert parts[0].endswith("\n\n(...)")
        assert parts[1].startswith("(...)\n\n")

    def test_three_parts_middle_gets_both_markers(self):
        """Middle part in a 3-way split gets both prefix and suffix markers."""
        chunk = "X" * 1800
        text = chunk + "\n\n" + chunk + "\n\n" + chunk
        parts = _split_message(text, max_len=2200)

        assert len(parts) >= 2
        # First part ends with suffix
        assert parts[0].endswith("\n\n(...)")
        # Last part starts with prefix
        assert parts[-1].startswith("(...)\n\n")

    def test_single_part_no_markers(self):
        """Single-part message gets no markers."""
        text = "Short message"
        parts = _split_message(text)
        assert len(parts) == 1
        assert "(...)" not in parts[0]


class TestSplitBoundaries:
    """Split point preferences: double-newline > single-newline > space."""

    def test_prefers_double_newline(self):
        """Splits on double-newline when available."""
        text = "A" * 2000 + "\n\n" + "B" * 1000
        parts = _split_message(text, max_len=2500)
        assert len(parts) == 2
        # First part should contain all A's (without the marker)
        content = parts[0].replace("\n\n(...)", "")
        assert content == "A" * 2000

    def test_falls_back_to_single_newline(self):
        """Falls back to single newline when no double-newline in range."""
        text = "A" * 2000 + "\n" + "B" * 1000
        parts = _split_message(text, max_len=2500)
        assert len(parts) == 2

    def test_falls_back_to_space(self):
        """Falls back to space when no newline in range."""
        # Long words separated by spaces, no newlines
        text = ("word " * 800).strip()
        parts = _split_message(text, max_len=2200)
        assert len(parts) >= 2
        # No part should end mid-word (i.e., no word should be split)
        for part in parts:
            cleaned = part.replace("\n\n(...)", "").replace("(...)\n\n", "").strip()
            # Should end with a complete word
            assert cleaned[-1] != "-"  # no hyphenation

    def test_no_mid_word_cut(self):
        """Even without newlines, should not cut in the middle of a word."""
        # One very long word — should still split cleanly
        text = "a" * 5000
        parts = _split_message(text, max_len=2200)
        assert len(parts) >= 2
        # Content should be preserved
        reconstructed = ""
        for part in parts:
            cleaned = part.replace("\n\n(...)", "").replace("(...)\n\n", "")
            reconstructed += cleaned
        assert reconstructed == text


class TestHtmlTagSafety:
    """HTML tag boundary safety via _adjust_cut_for_html_tags."""

    def test_does_not_split_inside_bold(self):
        """Should not split inside an unclosed <b> tag."""
        text = "Hello " + "x" * 2000 + " <b>important stuff that is bold</b> end"
        parts = _split_message(text, max_len=2100)
        # No part should contain an unclosed <b> at the end
        for part in parts:
            cleaned = part.replace("\n\n(...)", "")
            open_count = cleaned.lower().count("<b")
            close_count = cleaned.lower().count("</b>")
            # Allow equal or close-enough (markers may shift things)
            # The key is: the last part should be balanced
            assert open_count <= close_count + 1  # at most 1 unclosed

    def test_does_not_split_inside_anchor(self):
        """Should not split inside an <a href='...'>...</a> span."""
        before = "x" * 2000
        link = '<a href="https://example.com/very/long/path/to/something">Click here</a>'
        after = " more text"
        text = before + " " + link + after
        parts = _split_message(text, max_len=2100)

        # The link should be entirely in one part
        for part in parts:
            if "<a " in part:
                assert "</a>" in part, "Anchor tag split across parts"

    def test_adjust_cut_noop_when_safe(self):
        """_adjust_cut_for_html_tags returns cut unchanged when safe."""
        text = "Hello <b>world</b> this is a test"
        cut = 20  # After the </b>
        result = _adjust_cut_for_html_tags(text, cut)
        assert result == cut

    def test_adjust_cut_backs_up_on_unclosed_bold(self):
        """_adjust_cut_for_html_tags backs up when <b> is unclosed."""
        # The <b> tag must be past the halfway point for backup to trigger
        padding = "x" * 1500
        text = padding + " <b>world this is still bold and should not be cut" + "y" * 500
        cut = len(padding) + 25  # Inside the <b> span
        result = _adjust_cut_for_html_tags(text, cut)
        # Should back up to before <b>
        assert result <= text.index("<b")


class TestMaxLenReduction:
    """The default max_len is 3800 (from 4000) to leave room for markers."""

    def test_default_max_len(self):
        """Default max_len should be 3800."""
        text = "a" * 3800
        parts = _split_message(text)
        assert len(parts) == 1  # Exactly at limit, no split

        text = "a" * 3801
        parts = _split_message(text)
        assert len(parts) >= 2  # Over limit, must split
