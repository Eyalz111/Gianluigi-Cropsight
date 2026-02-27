"""
Transcript processing pipeline.

This module handles the end-to-end processing of Tactiq transcript exports:
1. Parse raw transcript (speaker labels, timestamps)
2. Send to Claude for structured extraction
3. Extract decisions, tasks, follow-ups, open questions
4. Apply tone and content guardrails
5. Store in Supabase
6. Trigger approval flow

Tactiq export format (expected):
    [00:00:15] Eyal: Welcome everyone...
    [00:01:30] Roye: Thanks, so about the MVP...

Usage:
    from processors.transcript_processor import process_transcript

    result = await process_transcript(
        file_content="...",
        meeting_title="MVP Focus",
        meeting_date="2026-02-22",
        participants=["Eyal", "Roye", "Paolo", "Yoram"]
    )
"""

import asyncio
import json
import logging
import re
from datetime import datetime
from typing import Any

from anthropic import Anthropic, APIStatusError

from config.settings import settings
from core.system_prompt import (
    get_summary_extraction_prompt,
    format_summary as format_summary_template,
)
from services.supabase_client import supabase_client
from services.embeddings import embedding_service
from guardrails.sensitivity_classifier import (
    classify_sensitivity,
    classify_sensitivity_from_content,
)
from guardrails.content_filter import (
    filter_personal_content,
    validate_summary_tone,
)

logger = logging.getLogger(__name__)


async def process_transcript(
    file_content: str,
    meeting_title: str,
    meeting_date: str,
    participants: list[str],
    source_file_path: str | None = None
) -> dict:
    """
    Process a raw transcript through the full pipeline.

    Args:
        file_content: Raw transcript text from Tactiq export.
        meeting_title: Title of the meeting.
        meeting_date: Date in ISO format (YYYY-MM-DD).
        participants: List of participant names.
        source_file_path: Google Drive path to the original file.

    Returns:
        Dict containing:
        - meeting_id: UUID of the created meeting
        - summary: The formatted summary
        - decisions: List of extracted decisions
        - tasks: List of extracted tasks
        - follow_ups: List of follow-up meetings
        - open_questions: List of open questions
        - sensitivity: 'normal' or 'sensitive'
        - approval_status: 'pending'
    """
    logger.info(f"Processing transcript: {meeting_title}")

    # Step 1: Parse transcript structure
    parsed = parse_transcript(file_content)
    duration_minutes = parsed["duration_minutes"]

    # Use speakers from transcript if no participants provided
    if not participants and parsed["speakers"]:
        participants = sorted(parsed["speakers"])
        logger.info(f"Auto-detected participants: {participants}")

    # Step 2: Classify sensitivity (from title)
    sensitivity = classify_sensitivity({"title": meeting_title})

    # Step 3: Send to Claude for structured extraction
    extracted = await extract_structured_data(
        transcript=file_content,
        meeting_title=meeting_title,
        participants=participants,
        meeting_date=meeting_date,
        duration_minutes=duration_minutes,
    )

    # Step 4: Secondary sensitivity check (from content)
    content_sensitivity = classify_sensitivity_from_content(file_content)
    if content_sensitivity == "sensitive":
        sensitivity = "sensitive"

    # Step 5: Validate tone of discussion summary
    tone_issues = validate_summary_tone(extracted.get("discussion_summary", ""))
    if tone_issues:
        logger.warning(f"Tone issues detected: {len(tone_issues)} issues")
        for issue in tone_issues:
            logger.warning(f"  - {issue['type']}: {issue.get('text', '')[:50]}...")

    # Step 6: Format the summary
    summary = format_summary_template(
        meeting_title=meeting_title,
        meeting_date=meeting_date,
        participants=participants,
        duration_minutes=duration_minutes,
        sensitivity=sensitivity,
        decisions=extracted.get("decisions", []),
        tasks=extracted.get("tasks", []),
        follow_ups=extracted.get("follow_ups", []),
        open_questions=extracted.get("open_questions", []),
        discussion_summary=extracted.get("discussion_summary", ""),
        stakeholders_mentioned=extracted.get("stakeholders", []),
    )

    # Step 7: Store in Supabase
    # Create the meeting record first
    from datetime import datetime as dt
    meeting_datetime = dt.fromisoformat(f"{meeting_date}T00:00:00")

    meeting = supabase_client.create_meeting(
        date=meeting_datetime,
        title=meeting_title,
        participants=participants,
        raw_transcript=file_content,
        summary=summary,
        sensitivity=sensitivity,
        source_file_path=source_file_path,
        duration_minutes=duration_minutes,
    )
    meeting_id = meeting["id"]

    logger.info(f"Created meeting record: {meeting_id}")

    # Store extracted data
    await store_meeting_data(
        meeting_id=meeting_id,
        decisions=extracted.get("decisions", []),
        tasks=extracted.get("tasks", []),
        follow_ups=extracted.get("follow_ups", []),
        open_questions=extracted.get("open_questions", []),
    )

    # Step 8: Generate and store embeddings
    await generate_and_store_embeddings(meeting_id, file_content)

    # Step 9: Log the action
    supabase_client.log_action(
        action="meeting_processed",
        details={
            "meeting_id": meeting_id,
            "title": meeting_title,
            "sensitivity": sensitivity,
            "decisions_count": len(extracted.get("decisions", [])),
            "tasks_count": len(extracted.get("tasks", [])),
        },
        triggered_by="auto",
    )

    logger.info(
        f"Transcript processing complete: {meeting_id} "
        f"({len(extracted.get('decisions', []))} decisions, "
        f"{len(extracted.get('tasks', []))} tasks)"
    )

    return {
        "meeting_id": meeting_id,
        "summary": summary,
        "decisions": extracted.get("decisions", []),
        "tasks": extracted.get("tasks", []),
        "follow_ups": extracted.get("follow_ups", []),
        "open_questions": extracted.get("open_questions", []),
        "stakeholders": extracted.get("stakeholders", []),
        "discussion_summary": extracted.get("discussion_summary", ""),
        "sensitivity": sensitivity,
        "approval_status": "pending",
    }


def parse_transcript(raw_content: str) -> dict:
    """
    Parse a raw Tactiq transcript into structured format.

    Extracts:
    - Speaker labels
    - Timestamps
    - Utterances

    Args:
        raw_content: Raw transcript text.

    Returns:
        Dict with:
        - utterances: List of {speaker, timestamp, text}
        - speakers: Set of unique speakers
        - duration_minutes: Estimated duration based on timestamps
    """
    # Pattern for Tactiq format — supports both:
    #   [HH:MM:SS] Speaker: text   (bracketed)
    #   MM:SS Speaker: text         (unbracketed, actual Tactiq Google Meet export)
    pattern_bracketed = r'\[(\d{1,2}:\d{2}(?::\d{2})?)\]\s*([^:\[\]]+):\s*(.+?)(?=\n\[|\Z)'
    pattern_unbracketed = r'^(\d{1,2}:\d{2}(?::\d{2})?)\s+([^:\d][^:]+):\s*(.+?)(?=\n\d{1,2}:\d{2}|\Z)'

    # Try bracketed first
    matches = re.findall(pattern_bracketed, raw_content, re.DOTALL)
    if not matches:
        # Fall back to unbracketed Tactiq format
        matches = re.findall(pattern_unbracketed, raw_content, re.DOTALL | re.MULTILINE)

    utterances = []
    speakers = set()

    for timestamp, speaker, text in matches:
        # Normalize speaker name to Title Case (Tactiq sometimes exports lowercase)
        speaker_clean = speaker.strip().title()
        utterances.append({
            "timestamp": timestamp.strip(),
            "speaker": speaker_clean,
            "text": text.strip()
        })
        speakers.add(speaker_clean)

    # Estimate duration
    duration_minutes = estimate_duration(utterances)

    return {
        "utterances": utterances,
        "speakers": speakers,
        "duration_minutes": duration_minutes,
    }


def estimate_duration(utterances: list[dict]) -> int:
    """
    Estimate meeting duration from transcript timestamps.

    Args:
        utterances: List of parsed utterances with timestamps.

    Returns:
        Duration in minutes.
    """
    if not utterances:
        return 0

    def parse_timestamp(ts: str) -> int:
        """Convert timestamp to total seconds."""
        parts = ts.split(":")
        if len(parts) == 2:
            # MM:SS format
            return int(parts[0]) * 60 + int(parts[1])
        elif len(parts) == 3:
            # HH:MM:SS format
            return int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])
        return 0

    # Get first and last timestamps
    first_ts = utterances[0].get("timestamp", "0:00")
    last_ts = utterances[-1].get("timestamp", "0:00")

    first_seconds = parse_timestamp(first_ts)
    last_seconds = parse_timestamp(last_ts)

    duration_seconds = last_seconds - first_seconds
    duration_minutes = max(1, duration_seconds // 60)

    return duration_minutes


async def extract_structured_data(
    transcript: str,
    meeting_title: str,
    participants: list[str],
    meeting_date: str,
    duration_minutes: int | None = None,
) -> dict:
    """
    Use Claude to extract structured data from transcript.

    Extracts:
    - Key decisions (with context, participants, timestamp)
    - Action items (with assignee, deadline, priority, timestamp)
    - Follow-up meetings (with leader, agenda, prep needed)
    - Open questions (with who raised them)
    - New stakeholders/contacts mentioned
    - Discussion summary

    Args:
        transcript: The full transcript text.
        meeting_title: Meeting title for context.
        participants: Known participants for context.
        meeting_date: Date of the meeting.
        duration_minutes: Estimated duration.

    Returns:
        Dict with all extracted elements.
    """
    # Build the extraction prompt
    prompt = get_summary_extraction_prompt(
        transcript=transcript,
        meeting_title=meeting_title,
        meeting_date=meeting_date,
        participants=participants,
        duration_minutes=duration_minutes,
    )

    # Call Claude for extraction
    client = Anthropic(api_key=settings.ANTHROPIC_API_KEY)

    # Use a structured extraction approach with JSON output
    extraction_system = """You are an expert meeting analyst. Extract structured information from meeting transcripts.

IMPORTANT: Your response must be valid JSON with this exact structure:
{
    "decisions": [
        {
            "description": "The decision made",
            "context": "Surrounding context",
            "participants_involved": ["Name1", "Name2"],
            "transcript_timestamp": "MM:SS"
        }
    ],
    "tasks": [
        {
            "title": "Task description",
            "assignee": "Name",
            "deadline": "YYYY-MM-DD or null",
            "priority": "H/M/L",
            "category": "Product & Tech / BD & Sales / Legal & Compliance / Finance & Fundraising / Operations & HR / Strategy & Research",
            "transcript_timestamp": "MM:SS"
        }
    ],
    "follow_ups": [
        {
            "title": "Meeting title",
            "proposed_date": "Description or null",
            "led_by": "Name",
            "participants": ["Name1", "Name2"],
            "agenda_items": ["Item 1", "Item 2"],
            "prep_needed": "What needs to happen before"
        }
    ],
    "open_questions": [
        {
            "question": "The question",
            "raised_by": "Name",
            "transcript_timestamp": "MM:SS"
        }
    ],
    "stakeholders": [
        {
            "name": "Person or org name",
            "context": "How they were mentioned"
        }
    ],
    "discussion_summary": "2-4 paragraphs summarizing the key discussion topics. Professional tone only. No emotional characterizations."
}

Apply all tone guardrails: no emotional characterizations, professional language only, cite timestamps."""

    # Retry with exponential backoff for transient errors (529 overloaded, 500, etc.)
    max_retries = 4
    base_delay = 5  # seconds

    for attempt in range(max_retries):
        try:
            response = client.messages.create(
                model=settings.model_extraction,
                max_tokens=4096,
                system=extraction_system,
                messages=[
                    {"role": "user", "content": prompt}
                ],
            )

            # Parse the response
            response_text = response.content[0].text

            # Try to extract JSON from the response
            extracted = _parse_extraction_response(response_text)

            logger.info(f"Extracted: {len(extracted.get('decisions', []))} decisions, "
                        f"{len(extracted.get('tasks', []))} tasks")

            return extracted

        except APIStatusError as e:
            if e.status_code in (529, 500, 502, 503) and attempt < max_retries - 1:
                delay = base_delay * (2 ** attempt)  # 5s, 10s, 20s, 40s
                logger.warning(
                    f"Claude API error {e.status_code} (attempt {attempt + 1}/{max_retries}), "
                    f"retrying in {delay}s..."
                )
                await asyncio.sleep(delay)
                continue
            logger.error(f"Claude API error after {attempt + 1} attempts: {e}")
            return {
                "decisions": [],
                "tasks": [],
                "follow_ups": [],
                "open_questions": [],
                "stakeholders": [],
                "discussion_summary": f"Error during extraction: {str(e)}",
            }

        except Exception as e:
            logger.error(f"Error calling Claude for extraction: {e}")
            return {
                "decisions": [],
                "tasks": [],
                "follow_ups": [],
                "open_questions": [],
                "stakeholders": [],
                "discussion_summary": f"Error during extraction: {str(e)}",
            }

    # Should not reach here, but just in case
    return {
        "decisions": [],
        "tasks": [],
        "follow_ups": [],
        "open_questions": [],
        "stakeholders": [],
        "discussion_summary": "Extraction failed after all retries",
    }


def _parse_extraction_response(response_text: str) -> dict:
    """
    Parse Claude's extraction response into structured data.

    Handles both clean JSON responses and JSON embedded in text.

    Args:
        response_text: Raw response from Claude.

    Returns:
        Parsed dict with extracted data.
    """
    # Try direct JSON parse first
    try:
        return json.loads(response_text)
    except json.JSONDecodeError:
        pass

    # Try to find JSON in the response (common with markdown code blocks)
    json_match = re.search(r'```(?:json)?\s*([\s\S]*?)```', response_text)
    if json_match:
        try:
            return json.loads(json_match.group(1))
        except json.JSONDecodeError:
            pass

    # Try to find JSON object anywhere in response
    json_match = re.search(r'\{[\s\S]*\}', response_text)
    if json_match:
        try:
            return json.loads(json_match.group(0))
        except json.JSONDecodeError:
            pass

    logger.warning("Could not parse extraction response as JSON")

    # Return default structure
    return {
        "decisions": [],
        "tasks": [],
        "follow_ups": [],
        "open_questions": [],
        "stakeholders": [],
        "discussion_summary": response_text[:500] if response_text else "",
    }


async def store_meeting_data(
    meeting_id: str,
    decisions: list[dict],
    tasks: list[dict],
    follow_ups: list[dict],
    open_questions: list[dict]
) -> None:
    """
    Store extracted data in Supabase tables.

    Args:
        meeting_id: UUID of the parent meeting.
        decisions: List of decision dicts to store.
        tasks: List of task dicts to store.
        follow_ups: List of follow-up meeting dicts to store.
        open_questions: List of open question dicts to store.
    """
    # Store decisions
    if decisions:
        supabase_client.create_decisions_batch(meeting_id, decisions)
        logger.info(f"Stored {len(decisions)} decisions")

    # Store tasks
    if tasks:
        supabase_client.create_tasks_batch(meeting_id, tasks)
        logger.info(f"Stored {len(tasks)} tasks")

    # Store follow-up meetings
    if follow_ups:
        supabase_client.create_follow_ups_batch(meeting_id, follow_ups)
        logger.info(f"Stored {len(follow_ups)} follow-up meetings")

    # Store open questions
    if open_questions:
        supabase_client.create_open_questions_batch(meeting_id, open_questions)
        logger.info(f"Stored {len(open_questions)} open questions")


async def generate_and_store_embeddings(
    meeting_id: str,
    transcript: str
) -> None:
    """
    Chunk transcript and store embeddings in Supabase.

    v0.2 upgrade: When meeting metadata (title, date, participants) is
    available, uses context-enriched embeddings so the vectors capture
    who/when/what — improving search recall for questions like
    "What did Roye say in the MVP meeting?".

    Falls back to the original non-contextual method if the meeting
    record cannot be retrieved.

    Args:
        meeting_id: UUID of the meeting.
        transcript: Full transcript text.
    """
    try:
        # Try to get meeting metadata for contextual embeddings
        meeting = None
        try:
            meeting = supabase_client.get_meeting(meeting_id)
        except Exception as e:
            logger.warning(f"Could not fetch meeting {meeting_id} for context: {e}")

        # Use contextual method if we have meeting info, otherwise fall back
        if meeting and meeting.get("title"):
            # v0.2: Context-enriched embeddings — vectors include meeting context
            embedded_chunks = await embedding_service.chunk_and_embed_transcript_with_context(
                transcript=transcript,
                meeting_id=meeting_id,
                meeting_title=meeting.get("title", ""),
                meeting_date=meeting.get("date", ""),
                participants=meeting.get("participants", []),
            )
            logger.info(f"Using context-enriched embeddings for meeting {meeting_id}")
        else:
            # Fallback: original method without context prefix
            embedded_chunks = await embedding_service.chunk_and_embed_transcript(
                transcript=transcript,
                meeting_id=meeting_id,
            )
            logger.info(f"Using standard embeddings for meeting {meeting_id} (no metadata)")

        if not embedded_chunks:
            logger.warning(f"No embeddings generated for meeting {meeting_id}")
            return

        # Prepare records for storage
        embedding_records = [
            {
                "source_type": "meeting",
                "source_id": meeting_id,
                "chunk_text": chunk["text"],
                "chunk_index": chunk["chunk_index"],
                "speaker": chunk.get("speaker"),
                "timestamp_range": chunk.get("timestamp_range"),
                "embedding": chunk["embedding"],
                "metadata": chunk.get("metadata", {}),
            }
            for chunk in embedded_chunks
        ]

        # Store in Supabase
        supabase_client.store_embeddings_batch(embedding_records)
        logger.info(f"Stored {len(embedding_records)} embeddings for meeting {meeting_id}")

    except Exception as e:
        logger.error(f"Error generating/storing embeddings: {e}")
        # Don't fail the whole process if embeddings fail
        # The meeting data is still valuable without embeddings
