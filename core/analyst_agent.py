"""
Analyst Agent — Accuracy-critical extraction and analysis.

Uses Claude Opus (model_extraction) for tasks requiring high accuracy:
- Transcript extraction (Phase 1: delegates to transcript_processor)
- Email extraction (Phase 3+)
- Debrief extraction (Phase 3+)
- Extraction validation (Phase 3+)

Phase 1: Thin wrapper around existing transcript_processor.
"""

import logging

from config.settings import settings
from core.llm import call_llm, get_client

logger = logging.getLogger(__name__)


class AnalystAgent:
    """
    Handles accuracy-critical extraction and analysis tasks.

    Uses Opus for maximum extraction quality. Currently delegates
    to existing processors; will gain direct extraction capabilities
    in later phases.
    """

    def __init__(self):
        self.model = settings.model_extraction

    async def extract_from_debrief(
        self,
        raw_messages: list[str],
        items_captured: list[dict],
    ) -> list[dict]:
        """
        Validate debrief extraction with Opus.

        Compares Sonnet's extracted items against the raw messages
        for accuracy. Only called when items > DEBRIEF_OPUS_THRESHOLD.

        Args:
            raw_messages: Raw user messages from the debrief.
            items_captured: Items extracted by Sonnet during the session.

        Returns:
            Validated/corrected items list, or original if validation fails.
        """
        import json
        from core.debrief_prompt import get_debrief_extraction_prompt

        prompt = get_debrief_extraction_prompt(raw_messages, items_captured)

        try:
            text, _usage = call_llm(
                prompt=prompt,
                model=self.model,
                max_tokens=4096,
                call_site="debrief_opus_validation",
            )

            # Parse response — strip markdown fences if present
            text = text.strip()
            if text.startswith("```"):
                # Remove only the opening and closing fence lines
                lines = text.split("\n")
                # Remove first line (```json or ```)
                if lines and lines[0].strip().startswith("```"):
                    lines = lines[1:]
                # Remove last line (```) only if it is purely a fence
                if lines and lines[-1].strip() == "```":
                    lines = lines[:-1]
                text = "\n".join(lines)

            parsed = json.loads(text)
            validated = parsed.get("validated_items", [])
            changes = parsed.get("changes_made", [])

            if changes:
                logger.info(f"Opus validation made {len(changes)} changes to debrief items")
                for change in changes:
                    logger.debug(f"  - {change}")

            return validated if validated else items_captured

        except Exception as e:
            logger.warning(f"Opus debrief validation failed: {e}")
            return items_captured

    async def extract_from_transcript(
        self,
        transcript_content: str,
        meeting_title: str,
        meeting_date: str,
        participants: list[str],
    ) -> dict:
        """
        Extract structured data from a meeting transcript.

        Delegates to the existing transcript_processor pipeline.

        Args:
            transcript_content: Raw transcript text.
            meeting_title: Title of the meeting.
            meeting_date: Date in ISO format.
            participants: List of participant names.

        Returns:
            Extraction result dict from transcript_processor.
        """
        from processors.transcript_processor import process_transcript

        return await process_transcript(
            file_content=transcript_content,
            meeting_title=meeting_title,
            meeting_date=meeting_date,
            participants=participants,
        )


# Singleton instance for easy import
analyst_agent = AnalystAgent()
