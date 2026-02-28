"""
Entity extraction and linking for v0.3 Tier 2.

Extracts people, organizations, projects, technologies, and locations
from meeting transcripts and links them to the entity registry.

Entity resolution is local (name matching), not LLM-based, to keep
it fast and free. Upgrade to LLM-based resolution later if quality
is poor.

Usage:
    from processors.entity_extraction import extract_and_link_entities

    results = await extract_and_link_entities(
        meeting_id="uuid",
        transcript="...",
        participants=["Eyal", "Roye"],
    )
"""

import json
import logging
import re
from typing import Any

from anthropic import Anthropic

from config.settings import settings
from config.team import get_team_member_names, TEAM_MEMBERS
from services.supabase_client import supabase_client

logger = logging.getLogger(__name__)

# Generic names that should never be registered as entities.
# These slip through when the LLM over-extracts from casual conversation.
_ENTITY_BLOCKLIST = {
    # Meeting tools / platforms
    "tactiq", "tactiq.io", "google meet", "zoom", "teams", "slack",
    "google", "google docs", "google drive", "google sheets",
    # Generic tech
    "internet", "ai", "machine learning", "cloud", "api", "gps",
    "whatsapp", "telegram", "email",
    # Generic geography (only block when not business-relevant)
    "israel", "italy", "usa", "united states", "europe", "asia",
    "tel aviv", "jerusalem", "rome", "new york",
    # Common nouns that LLMs sometimes extract
    "computer", "phone", "hospital", "university", "government",
    "amazon", "facebook", "meta", "microsoft", "apple",
    # Transcript artifacts and mis-transcriptions of CropSight
    "crop site", "cropsite", "crop sight",
    "jerusalem hospital",
    "weather model",
}


async def extract_and_link_entities(
    meeting_id: str,
    transcript: str,
    participants: list[str],
) -> dict:
    """
    Extract entities from a transcript and link to the entity registry.

    Flow:
    1. Use Claude Haiku to extract raw entities from transcript.
    2. Resolve each entity against the existing registry (local matching).
    3. Create new entity records for unmatched entities.
    4. Batch-create entity_mention records for all entities.

    Args:
        meeting_id: UUID of the meeting.
        transcript: Full transcript text.
        participants: List of meeting participant names.

    Returns:
        Dict with:
        - new_entities: List of newly created entity records.
        - existing_mentions: List of mentions linked to existing entities.
        - total_mentions: Total number of entity mentions created.
    """
    result = {
        "new_entities": [],
        "existing_mentions": [],
        "total_mentions": 0,
    }

    # Step 1: Extract raw entities via LLM
    raw_entities = await _extract_raw_entities(transcript, participants)
    if not raw_entities:
        logger.info(f"No external entities found in meeting {meeting_id}")
        return result

    # Step 2: Fetch existing entities for resolution
    existing_entities = supabase_client.list_entities(limit=500)

    # Step 3: Resolve and create mentions
    mentions_to_create = []

    for raw in raw_entities:
        name = raw.get("name", "").strip()
        entity_type = raw.get("type", "person").strip().lower()
        if not name:
            continue

        # Try to match against existing entities
        matched = _resolve_entity(name, entity_type, existing_entities)

        if matched:
            entity_id = matched["id"]
            result["existing_mentions"].append({
                "entity_name": matched["canonical_name"],
                "entity_id": entity_id,
                "mention": raw,
            })
        else:
            # Create new entity
            try:
                new_entity = supabase_client.create_entity(
                    canonical_name=name,
                    entity_type=entity_type,
                    aliases=[name],
                    metadata=raw.get("metadata", {}),
                    first_seen_meeting_id=meeting_id,
                )
                entity_id = new_entity["id"]
                result["new_entities"].append(new_entity)
                # Add to existing list so subsequent mentions can match
                existing_entities.append(new_entity)
            except Exception as e:
                logger.warning(f"Failed to create entity '{name}': {e}")
                continue

        # Build mention record
        mentions_to_create.append({
            "entity_id": entity_id,
            "meeting_id": meeting_id,
            "mention_text": name,
            "context": raw.get("context", ""),
            "speaker": raw.get("speaker"),
            "sentiment": raw.get("sentiment", "neutral"),
            "transcript_timestamp": raw.get("timestamp"),
        })

    # Step 4: Batch create mentions
    if mentions_to_create:
        created = supabase_client.create_entity_mentions_batch(mentions_to_create)
        result["total_mentions"] = len(created)

    logger.info(
        f"Entity extraction complete for {meeting_id}: "
        f"{len(result['new_entities'])} new, "
        f"{len(result['existing_mentions'])} existing, "
        f"{result['total_mentions']} mentions"
    )
    return result


async def _extract_raw_entities(
    transcript: str,
    participants: list[str],
) -> list[dict]:
    """
    Use Claude Haiku to extract entities from a transcript.

    Excludes meeting participants and known CropSight team members.

    Args:
        transcript: Full transcript text.
        participants: Meeting participant names to exclude.

    Returns:
        List of entity dicts: [{name, type, context, speaker, sentiment, timestamp}]
    """
    # Build exclusion list
    team_names = get_team_member_names()
    exclude_names = set(
        n.lower() for n in (participants + team_names)
    )
    # Also exclude first names of team members
    for name in team_names:
        first = name.split()[0].lower()
        exclude_names.add(first)
    for p in participants:
        first = p.split()[0].lower()
        exclude_names.add(first)

    # Truncate transcript for Haiku
    truncated = transcript[:8000] if len(transcript) > 8000 else transcript

    prompt = f"""Extract BUSINESS-RELEVANT external entities from this startup meeting transcript.

We want: specific people (partners, investors, advisors), specific companies/organizations, named projects, and specific locations relevant to business operations.

EXCLUDE these meeting participants and team members: {', '.join(sorted(exclude_names))}

Also EXCLUDE:
- Generic/common nouns (internet, computer, phone, email, etc.)
- Countries or cities mentioned only in passing or small talk (not business context)
- Tools/platforms used for the meeting itself (Zoom, Google Meet, Tactiq, etc.)
- Generic technology terms (AI, machine learning, cloud, etc.)
- Geopolitical discussion not directly tied to business operations

ONLY include entities that are directly relevant to the startup's business: partners, clients, investors, grant bodies, pilot locations, named projects, specific advisors/contacts.

For each entity, provide:
- name: The canonical name
- type: person / organization / project / technology / location
- context: Brief context of how they were mentioned (1 sentence)
- speaker: Who mentioned them
- sentiment: positive / neutral / negative / mixed
- timestamp: Approximate transcript timestamp if visible

TRANSCRIPT:
{truncated}

Return JSON:
{{"entities": [{{"name": "...", "type": "...", "context": "...", "speaker": "...", "sentiment": "...", "timestamp": "..."}}]}}

Be selective — only include entities that matter for business tracking. Quality over quantity."""

    try:
        client = Anthropic(api_key=settings.ANTHROPIC_API_KEY)
        response = client.messages.create(
            model=settings.model_simple,
            max_tokens=2048,
            messages=[{"role": "user", "content": prompt}],
        )
        response_text = response.content[0].text
        parsed = _parse_json_response(response_text)
        entities = parsed.get("entities", [])

        # Filter out any entities that match excluded names or blocklist
        filtered = []
        for e in entities:
            name_lower = e.get("name", "").lower().strip()
            if not name_lower or name_lower in exclude_names:
                continue
            if name_lower in _ENTITY_BLOCKLIST:
                continue
            if len(name_lower) < 2:
                continue
            filtered.append(e)

        logger.info(f"Extracted {len(filtered)} raw entities")
        return filtered

    except Exception as e:
        logger.error(f"Error extracting entities: {e}")
        return []


def _resolve_entity(
    name: str,
    entity_type: str,
    existing_entities: list[dict],
) -> dict | None:
    """
    Try to match a name against the existing entity registry.

    Resolution order:
    1. Exact canonical_name match (case-insensitive)
    2. Alias match (case-insensitive)
    3. Partial match for persons (last name matching)

    Args:
        name: Name to resolve.
        entity_type: Expected entity type.
        existing_entities: List of existing entity records.

    Returns:
        Matched entity record, or None if no match found.
    """
    name_lower = name.lower().strip()

    # 1. Exact canonical name match
    for entity in existing_entities:
        if entity.get("canonical_name", "").lower() == name_lower:
            return entity

    # 2. Alias match
    for entity in existing_entities:
        aliases = entity.get("aliases", []) or []
        for alias in aliases:
            if alias.lower() == name_lower:
                return entity

    # 3. Partial match for persons (e.g., "Jason" matches "Jason Adelman")
    if entity_type == "person":
        for entity in existing_entities:
            if entity.get("entity_type") != "person":
                continue
            canonical = entity.get("canonical_name", "")
            # Check if search name is a substring of canonical
            if name_lower in canonical.lower():
                return entity
            # Check if canonical is a substring of search name
            if canonical.lower() in name_lower:
                return entity

    return None


def _parse_json_response(response_text: str) -> dict:
    """Parse a JSON response from Claude, handling markdown code blocks."""
    try:
        return json.loads(response_text)
    except json.JSONDecodeError:
        pass

    json_match = re.search(r'```(?:json)?\s*([\s\S]*?)```', response_text)
    if json_match:
        try:
            return json.loads(json_match.group(1))
        except json.JSONDecodeError:
            pass

    json_match = re.search(r'\{[\s\S]*\}', response_text)
    if json_match:
        try:
            return json.loads(json_match.group(0))
        except json.JSONDecodeError:
            pass

    logger.warning(f"Could not parse JSON from entity extraction: {response_text[:200]}")
    return {}
