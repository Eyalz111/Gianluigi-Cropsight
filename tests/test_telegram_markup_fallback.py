"""The plain-text fallback must stay READABLE.

Telegram rejects a whole message when its markup is malformed, and the culprit
is almost always a TITLE carrying a stray * _ ` or [ — extraction happily
produces "Send *final* deck" or "Review budget_v2". The fallback resent the
text verbatim, so the recipient got a wall of visible asterisks.
"""

import pytest

from services.telegram_bot import _strip_markup


class TestMarkdown:
    def test_bold_and_italic_markers_go_the_words_stay(self):
        assert _strip_markup("*Bold* and _italic_ text", "Markdown") == "Bold and italic text"

    def test_a_stray_marker_in_a_title_is_cleaned(self):
        """The actual failure: one unmatched asterisk breaks the whole send."""
        assert _strip_markup("Task: Send *final deck to Lavazza", "Markdown") == \
            "Task: Send final deck to Lavazza"

    def test_a_link_keeps_its_label(self):
        assert _strip_markup("See [the sheet](https://x.test/a_b)", "Markdown") == \
            "See the sheet"

    def test_code_fences_and_backticks_go(self):
        assert _strip_markup("Run ```python\nx=1``` now", "Markdown") == "Run x=1 now"

    def test_markdownv2_escapes_are_unescaped(self):
        assert _strip_markup(r"Budget \- v2 \(final\)", "MarkdownV2") == "Budget - v2 (final)"

    def test_underscores_inside_a_word_are_kept(self):
        """A filename is not italics. Stripping every underscore turned
        "budget_v2_final" into "budgetv2final" — worse than the markup."""
        assert _strip_markup("Review budget_v2_final", "Markdown") ==             "Review budget_v2_final"

    def test_delimiting_underscores_still_go(self):
        assert _strip_markup("the _deck_ is ready", "Markdown") == "the deck is ready"


class TestHtml:
    def test_tags_are_removed(self):
        assert _strip_markup("<b>Projects</b> — <i>3 open</i>", "HTML") == \
            "Projects — 3 open"

    def test_br_becomes_a_newline(self):
        assert _strip_markup("one<br>two", "HTML") == "one\ntwo"

    def test_entities_are_decoded(self):
        assert _strip_markup("Q&amp;A &lt;now&gt;", "HTML") == "Q&A <now>"


class TestPassthrough:
    def test_plain_text_is_untouched(self):
        assert _strip_markup("nothing to do here", None) == "nothing to do here"

    def test_empty_is_safe(self):
        assert _strip_markup("", "Markdown") == ""

    def test_no_word_characters_are_ever_lost(self):
        """The one property that matters: formatting goes, content stays."""
        text = "*Chase* NCPB _follow-up_ — [deck](http://x.test) `now`"
        cleaned = _strip_markup(text, "Markdown")
        for word in ("Chase", "NCPB", "follow-up", "deck", "now"):
            assert word in cleaned


class TestStrayAngleBracketsKeepTheirText:
    """`<[^>]+>` deleted everything between a stray "<" and the next ">" —
    precisely the content that caused the parse failure being recovered from.
    A message would arrive looking complete with words cut out of the middle."""

    def test_a_less_than_in_prose_does_not_eat_the_sentence(self):
        text = "Margin is <5% on the Kenya deal, so we should <re-price> it"
        cleaned = _strip_markup(text, "HTML")
        assert "5% on the Kenya deal" in cleaned
        assert "re-price" in cleaned

    def test_real_tags_are_still_removed(self):
        assert _strip_markup("<b>Projects</b> \u2014 <i>3 open</i>", "HTML") == \
            "Projects \u2014 3 open"

    def test_a_tag_with_attributes_is_removed(self):
        assert _strip_markup('<a href="http://x.test">the sheet</a>', "HTML") == \
            "the sheet"

    def test_a_maths_comparison_survives_intact(self):
        assert _strip_markup("if x < y and y > z then ship", "HTML") == \
            "if x < y and y > z then ship"
