"""
Intelligence Signal prompts and formatters.

Contains all prompts (synthesis character, section structure, video script)
and output formatters (Telegram notification, HTML email, plain text email).

The news anchor character is the editorial voice of the signal — an engaged,
energetic journalist who reports facts without opinions or recommendations.
"""

import logging
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

# ── Banned phrases ─────────────────────────────────────────────────────
# These phrases are signals of lazy AI writing. The synthesis prompt
# explicitly bans them to maintain authentic journalist voice.

BANNED_PHRASES = [
    "underscores",
    "highlights the need",
    "is poised to",
    "in an increasingly",
    "it remains to be seen",
    "the landscape is evolving",
    "in today's rapidly changing",
    "this is a game-changer",
    "only time will tell",
    "a paradigm shift",
]


def system_prompt_synthesis() -> str:
    """
    System prompt for the Opus synthesis call.

    Establishes the news anchor / editor-in-chief character.
    """
    banned_list = ", ".join(f'"{p}"' for p in BANNED_PHRASES)

    return f"""You are the editor-in-chief and lead correspondent of the CropSight Intelligence Signal — a weekly market intelligence publication for a 4-person AgTech startup that builds ML-powered crop yield forecasting.

Your voice: engaged, energetic journalist covering AgTech and commodity markets. You are genuinely curious about this space. You write like a senior Bloomberg or Reuters correspondent who also reads Nature and TechCrunch.

ABSOLUTE RULES:
1. NO opinions. NO recommendations. NO "CropSight should..." or "the team might consider..."
2. NO agenda. You report what's happening, not what you think the team should do about it.
3. Penalize false confidence. If a source is weak or a claim is unverified, say so explicitly.
4. Reward honest uncertainty. "Data is thin here" is better than padding with filler.
5. Allow empty sections. If nothing notable happened in a category this week, write ONE sentence acknowledging that and move on. Do NOT pad.
6. Vary section lengths naturally. A big week for commodities gets more space than a quiet one.
7. Use specific names, numbers, and dates. Never write "a major AgTech company" when you can name it.
8. Write flags ONLY for genuinely decision-relevant developments. Not every week needs 3 flags. Zero flags is fine.
9. The Exploration Corner is for out-of-scope topics. If the research came back empty, say so in one sentence. Honest empty is better than forced content.

BANNED PHRASES (never use these): {banned_list}

FORMAT: Write in clean markdown. Use ## for section headers. Use bullet points for lists. Keep paragraphs short (2-3 sentences max)."""


def user_prompt_synthesis(context: dict, research_results: dict) -> str:
    """
    User prompt for the Opus synthesis call.

    Structures the research results into the signal's section format
    and provides the context packet for continuity awareness.

    Args:
        context: Output of build_context_packet().
        research_results: Dict mapping section labels to research content.

    Returns:
        Formatted user prompt string.
    """
    # Format research results
    research_text = ""
    for section, content in research_results.items():
        research_text += f"\n### Research: {section}\n{content}\n"

    # Format context
    crops = ", ".join(context.get("active_crops", []))
    regions = ", ".join(context.get("active_regions", []))
    competitors = [c.get("name", "") for c in context.get("known_competitors", [])]
    competitor_text = ", ".join(competitors) if competitors else "none tracked"

    # Format last flags for continuity
    last_flags = context.get("last_signal_flags", [])
    continuity_text = ""
    if last_flags:
        flag_items = []
        for f in last_flags:
            if isinstance(f, dict):
                flag_items.append(f"- {f.get('flag', 'unknown')}")
        if flag_items:
            continuity_text = (
                "\n\n## Last Week's Flags (for continuity — follow up if relevant)\n"
                + "\n".join(flag_items)
            )

    week = context.get("week_number", "?")
    year = context.get("year", "?")

    return f"""Write CropSight Intelligence Signal W{week}/{year}.

## CropSight Context
- Active crops: {crops}
- Active regions: {regions}
- Tracked competitors: {competitor_text}
- Team size: 4 (CEO, CTO, BD, Advisor)
- Stage: Pre-revenue, PoC
{continuity_text}

## Research Data
{research_text}

## Required Output Structure

Write the signal using EXACTLY these sections in this order:

### FLAGS
0-3 flags maximum. Each flag must be decision-relevant to CropSight specifically.
Format each as: **[FLAG]** One sentence. (urgency: high or medium)
If nothing is genuinely flag-worthy this week, write "No flags this week."

### The Problem, This Week
The single most important development. Named region, named crop, named consequence. 2-3 paragraphs.

### New Horizons
Only if there is a genuine "why now" signal — a new technology, dataset, or market opening that didn't exist last month. If nothing qualifies, skip this section entirely.

### Commodity Pulse
Price movements, supply/demand shifts, weather impacts on the crops CropSight tracks.

### Regional Watch
Region-specific developments in CropSight's active regions.

### Regulatory Radar
Policy, regulation, trade agreements affecting AgTech or commodity markets. If quiet this week, one sentence.

### Competitive Landscape
What CropSight's competitors are doing. Funding, launches, hires, partnerships.

### AgTech Funding
Deals, acquisitions, partnerships in the AgTech space this week.

### Science & Tech Signals
Academic papers, new datasets, model improvements, satellite launches relevant to crop forecasting.

### Exploration Corner
1-2 items from the out-of-scope rotating queries (adjacent markets, wild card crops, unexplored geographies).
If nothing interesting came back this week, say so in one sentence and move on.
Do not pad. Honest empty is better than forced content.

### This Week's Angle
A creative, non-obvious synthesis. Connect dots across sections. What story would a smart reader miss if they only read the headlines? 2-3 paragraphs."""


def system_prompt_script() -> str:
    """System prompt for video narration script generation."""
    return """You are a news flash narrator for the CropSight Intelligence Signal video.
Your tone is professional but energetic — like a Bloomberg TV anchor doing a 90-second market segment.
Write for spoken delivery: short sentences, natural rhythm, no jargon that sounds awkward when read aloud.
The script will be converted to speech via text-to-speech, so avoid symbols and abbreviations."""


def user_prompt_script(signal_content: str) -> str:
    """
    User prompt for generating the video narration script.

    Args:
        signal_content: The full written signal report.

    Returns:
        Formatted prompt for script generation.
    """
    return f"""Based on this Intelligence Signal report, write a 60-90 second narration script for a news flash video.

Rules:
1. Pick the 3-4 most important developments from the report
2. Open with a punchy one-liner that hooks the viewer
3. Keep each item to 2-3 sentences
4. Close with: "The full Intelligence Signal is attached — dig in."
5. Total length: 150-250 words
6. Write for speech — no bullet points, no headers, no markdown

Report:
{signal_content}"""


def system_prompt_structured_script() -> str:
    """System prompt for structured video script with visual metadata."""
    return """You are a news flash narrator for the CropSight Intelligence Signal video.
Your tone is professional but energetic — like a Bloomberg TV anchor doing a 90-second market segment.
Write for spoken delivery: short sentences, natural rhythm.

You output a structured JSON array where each segment specifies narration text AND what visual to show.
Each segment will be rendered as a separate video slide with its own audio clip."""


def user_prompt_structured_script(signal_content: str) -> str:
    """
    User prompt for generating a structured video script with visual hints.

    The LLM outputs a JSON array of segments, each with narration and
    metadata for visual rendering (charts, crop photos, flags, text cards).

    Args:
        signal_content: The full written signal report.

    Returns:
        Formatted prompt requesting JSON output.
    """
    return f"""Based on this Intelligence Signal, write a structured video script.

Output ONLY valid JSON — an array of segment objects. No markdown, no explanation, just the JSON array.

Segment schema:
{{
  "segment_type": "headline|stat|crop|country|competitor|closing",
  "narration": "1-3 sentences for this segment (20-40 words)",
  "visual_hint": "title|chart|crop_photo|flag|text_card|closing",
  "data": {{"label": "Wheat Futures", "value": "+4%", "direction": "up"}},
  "country_code": "BR",
  "crop": "coffee"
}}

Rules:
1. Output 5-7 segments total
2. Each segment narration is 1-3 sentences (20-40 words)
3. Total narration across all segments: 150-250 words
4. First segment must be type "headline" with visual_hint "title"
5. Last segment must be type "closing" — narration ends with "The full Intelligence Signal is attached — dig in."
6. Use "stat" for price movements, production numbers, trade volumes — include data.label, data.value, data.direction
7. Use "crop" for crop-specific news — include crop field (wheat, coffee, corn, soybeans, cocoa, grapes)
8. Use "country" for country-specific news — include country_code (ISO 2-letter: BR, US, EU, IL, IN, UA, AR, CO, ET, ID)
9. Use "competitor" for company news — keep visual_hint as "text_card"
10. Write for speech — spell out percentages ("four percent" not "4%") and single-digit numbers ("three regions" not "3 regions"). Keep years, dollar amounts, and multi-digit quantities as digits ("2024", "$23M", "8.7 million tonnes", "23 countries")
11. The data field is ONLY for stat segments. Omit it for other types.
12. country_code and crop fields are optional — only include when relevant to the segment

Report:
{signal_content}"""


def format_telegram_notification(
    signal_id: str,
    drive_link: str,
    week_number: int,
    flags: list[dict] | None = None,
    research_source: str | None = None,
    watchlist_changes: dict | None = None,
    video_link: str | None = None,
) -> str:
    """
    Format Telegram notification for Eyal (notification-only, not full content).

    Args:
        signal_id: e.g. "signal-w14-2026"
        drive_link: Google Doc webViewLink
        week_number: ISO week number
        flags: List of flag dicts [{flag: str, urgency: str}]
        research_source: "perplexity", "perplexity_retry", or "claude_search"
        watchlist_changes: Dict with promoted/deactivated/discovered lists

    Returns:
        Formatted Telegram message string (HTML parse mode).
    """
    lines = [f"<b>Intelligence Signal W{week_number} ready for review.</b>"]

    # Top flags preview
    if flags:
        for f in flags[:3]:
            if isinstance(f, dict):
                urgency = f.get("urgency", "medium")
                icon = "\U0001f534" if urgency == "high" else "\U0001f7e1"  # red/yellow circle
                lines.append(f"{icon} {f.get('flag', '')}")
    else:
        lines.append("No flags this week.")

    # Research source warning (Enhancement 2)
    if research_source and research_source != "perplexity":
        lines.append("")
        lines.append(
            "\u26a0\ufe0f Generated via backup research "
            "(Perplexity unavailable). Quality may vary."
        )

    # Watchlist changes (Fix 3)
    if watchlist_changes:
        changes = []
        promoted = watchlist_changes.get("promoted", [])
        deactivated = watchlist_changes.get("deactivated", [])
        discovered = watchlist_changes.get("discovered", [])

        if promoted:
            changes.append(f"{len(promoted)} promoted ({', '.join(promoted)})")
        if deactivated:
            changes.append(f"{len(deactivated)} deactivated ({', '.join(deactivated)})")
        if discovered:
            changes.append(f"{len(discovered)} new ({', '.join(discovered)})")

        if changes:
            lines.append("")
            lines.append(f"Watchlist: {'; '.join(changes)}.")

    lines.append("")
    lines.append(f'<a href="{drive_link}">Open in Google Docs</a>')
    if video_link:
        lines.append(f'<a href="{video_link}">Watch video</a>')
    lines.append("Approve via CropSight Ops when ready.")

    return "\n".join(lines)


def _extract_teaser(signal_content: str) -> str:
    """
    Extract a 2-3 sentence teaser from the first real content section.

    Skips the title, metadata, and FLAGS section. Grabs the first
    paragraph of actual content (typically "The Problem, This Week").
    """
    import re

    # Split into sections on ## headers
    sections = re.split(r'^##\s+(.+)$', signal_content, flags=re.MULTILINE)

    # sections alternates: [preamble, header1, body1, header2, body2, ...]
    # Find the first non-FLAGS section body
    teaser = ""
    i = 1  # Skip preamble (index 0)
    while i + 1 < len(sections):
        header = sections[i].strip()
        body = sections[i + 1].strip()
        i += 2

        # Skip FLAGS section
        if header.upper().startswith("FLAG"):
            continue

        # Found real content — grab first 2-3 sentences
        # Strip markdown formatting
        body = re.sub(r'\*\*(.+?)\*\*', r'\1', body)  # Bold → plain
        body = re.sub(r'#{1,3}\s+.*$', '', body, flags=re.MULTILINE)
        body = re.sub(r'---+', '', body)
        body = body.strip()

        # Get first 2-3 sentences
        sentences = re.split(r'(?<=[.!?])\s+', body)
        teaser = " ".join(sentences[:3])

        if len(teaser) > 400:
            cut = teaser[:400].rfind(".")
            if cut > 100:
                teaser = teaser[:cut + 1]

        break

    if len(teaser) < 20:
        teaser = "This week's intelligence signal is ready for your review."

    return teaser


def format_email_html(
    signal_content: str,
    drive_link: str,
    week_number: int,
    year: int,
    flags: list[dict] | None = None,
    video_link: str | None = None,
    audio_link: str | None = None,
) -> str:
    """
    Format HTML email body — concise teaser with links.

    Follows the meeting summary email pattern: short preview paragraph,
    not the full report. Full report is in the attached .docx.
    """
    teaser = _extract_teaser(signal_content)

    # Build links section
    links_parts = [f'<a href="{drive_link}" style="color:#00D4AA;">View full report</a>']
    if video_link:
        links_parts.append(f'<a href="{video_link}" style="color:#00D4AA;">Watch video</a>')
    if audio_link:
        links_parts.append(f'<a href="{audio_link}" style="color:#00D4AA;">Listen to podcast</a>')
    links_html = " &middot; ".join(links_parts)

    return f"""<!DOCTYPE html>
<html>
<head><meta charset="utf-8"></head>
<body style="font-family:Georgia,serif;max-width:680px;margin:0 auto;padding:20px;color:#1a1a1a;line-height:1.6;">
    <div style="border-bottom:3px solid #0A1628;padding-bottom:12px;margin-bottom:24px;">
        <h1 style="margin:0;color:#0A1628;font-size:24px;">
            CropSight Intelligence Signal
        </h1>
        <p style="margin:4px 0 0;color:#666;font-size:14px;">
            Week {week_number}, {year}
        </p>
    </div>

    <p style="font-size:16px;line-height:1.7;">{teaser}</p>

    <p style="font-size:14px;color:#444;">
        <strong>Full report attached.</strong>
    </p>

    <p style="font-size:13px;">{links_html}</p>

    <hr style="border:none;border-top:1px solid #ddd;margin:24px 0;">
    <p style="font-size:12px;color:#999;">
        CropSight Intelligence Signal is generated weekly by Gianluigi.
    </p>
</body>
</html>"""


def format_email_plain(
    signal_content: str,
    drive_link: str,
    video_link: str | None = None,
    audio_link: str | None = None,
) -> str:
    """
    Format plain text email body — concise teaser with links.
    """
    teaser = _extract_teaser(signal_content)

    links = [f"Full report: {drive_link}"]
    if video_link:
        links.append(f"Watch video: {video_link}")
    if audio_link:
        links.append(f"Listen to podcast: {audio_link}")

    return (
        f"{teaser}\n\n"
        f"Full report attached.\n\n"
        f"---\n"
        + "\n".join(links)
        + "\n\nGenerated by Gianluigi, CropSight's AI operations assistant."
    )


def _markdown_to_html(text: str) -> str:
    """
    Basic markdown to HTML conversion for email.

    Handles headers, bold, bullet points, and paragraphs.
    Not a full markdown parser — just enough for signal output.
    """
    import re

    lines = text.split("\n")
    html_lines = []
    in_list = False

    for line in lines:
        stripped = line.strip()

        # Headers
        if stripped.startswith("### "):
            if in_list:
                html_lines.append("</ul>")
                in_list = False
            html_lines.append(
                f'<h3 style="color:#0A1628;margin:20px 0 8px;font-size:16px;">'
                f'{stripped[4:]}</h3>'
            )
        elif stripped.startswith("## "):
            if in_list:
                html_lines.append("</ul>")
                in_list = False
            html_lines.append(
                f'<h2 style="color:#0A1628;margin:24px 0 10px;font-size:18px;">'
                f'{stripped[3:]}</h2>'
            )
        # Bullet points
        elif stripped.startswith("- ") or stripped.startswith("* "):
            if not in_list:
                html_lines.append('<ul style="padding-left:20px;">')
                in_list = True
            content = stripped[2:]
            # Bold
            content = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", content)
            html_lines.append(f"<li>{content}</li>")
        # Empty line
        elif not stripped:
            if in_list:
                html_lines.append("</ul>")
                in_list = False
        # Regular paragraph
        else:
            if in_list:
                html_lines.append("</ul>")
                in_list = False
            # Bold
            content = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", stripped)
            html_lines.append(f"<p>{content}</p>")

    if in_list:
        html_lines.append("</ul>")

    return "\n".join(html_lines)
