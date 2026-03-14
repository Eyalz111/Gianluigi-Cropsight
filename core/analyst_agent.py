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
from core.llm import get_client

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
