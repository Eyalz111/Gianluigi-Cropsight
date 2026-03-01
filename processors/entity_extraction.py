"""
Entity extraction and linking for v0.3 Tier 2.

Extracts stakeholders from meeting transcripts and links them to the
entity registry. "Stakeholder" means a specific person or organization
that CropSight has (or could have) a direct business relationship with.

Two-pass approach:
  1. Claude Haiku extracts candidate stakeholders from the transcript.
  2. Claude Haiku validates each candidate (is it a real stakeholder
     or a generic/irrelevant name?). This replaces a hardcoded blocklist.

Entity resolution is local (name matching), not LLM-based, to keep
it fast and free.

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

from config.settings import settings
from core.llm import call_llm
from config.team import get_team_member_names, TEAM_MEMBERS
from services.supabase_client import supabase_client

logger = logging.getLogger(__name__)


async def extract_and_link_entities(
    meeting_id: str,
    transcript: str,
    participants: list[str],
    pre_extracted: list[dict] | None = None,
) -> dict:
    """
    Validate and link stakeholder entities to the registry.

    Two modes:
    - With pre_extracted (normal flow): Opus already extracted stakeholders
      from the full transcript. We just validate + link. No extra extraction
      call needed — saves one LLM call and covers the full transcript.
    - Without pre_extracted (fallback): Runs standalone Haiku extraction
      from a transcript sample. Used if called outside the main pipeline.

    Flow:
    1. Get candidates (from Opus output or fallback Haiku extraction).
    2. Filter out participants/team members.
    3. Use Claude Haiku to validate candidates (filter junk).
    4. Resolve each entity against the existing registry (local matching).
    5. Create new entity records for unmatched entities.
    6. Batch-create entity_mention records.

    Args:
        meeting_id: UUID of the meeting.
        transcript: Full transcript text.
        participants: List of meeting participant names.
        pre_extracted: Optional stakeholders already extracted by Opus.

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

    # Step 1: Get candidates — use Opus output or fall back to Haiku
    if pre_extracted:
        raw_entities = _filter_known_names(pre_extracted, participants)
        logger.info(
            f"Using {len(raw_entities)} pre-extracted stakeholders "
            f"(from {len(pre_extracted)} Opus candidates)"
        )
    else:
        raw_entities = await _extract_raw_entities(transcript, participants)

    if not raw_entities:
        logger.info(f"No stakeholders found in meeting {meeting_id}")
        return result

    # Step 2: Validate candidates via LLM (filter out junk)
    validated = await _validate_entities(raw_entities)
    if not validated:
        logger.info(f"All candidates filtered by validation in {meeting_id}")
        return result

    logger.info(
        f"Entity extraction: {len(raw_entities)} candidates -> "
        f"{len(validated)} validated"
    )

    # Step 3: Fetch existing entities for resolution
    existing_entities = supabase_client.list_entities(limit=500)

    # Step 4: Resolve and create mentions
    mentions_to_create = []

    for raw in validated:
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

    # Step 5: Batch create mentions
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


def _filter_known_names(
    candidates: list[dict],
    participants: list[str],
) -> list[dict]:
    """
    Filter out team members and participants from pre-extracted stakeholders.

    The Opus extraction prompt tells it to exclude these, but we double-check
    here in case any slip through.

    Args:
        candidates: Stakeholders extracted by Opus.
        participants: Meeting participant names.

    Returns:
        Filtered list with known names removed.
    """
    team_names = get_team_member_names()
    exclude = set(n.lower() for n in (participants + team_names))
    # Also exclude first names
    for name in team_names:
        exclude.add(name.split()[0].lower())
    for p in participants:
        exclude.add(p.split()[0].lower())

    filtered = []
    for c in candidates:
        name_lower = c.get("name", "").lower().strip()
        if not name_lower or name_lower in exclude or len(name_lower) < 2:
            continue
        filtered.append(c)
    return filtered


async def _extract_raw_entities(
    transcript: str,
    participants: list[str],
) -> list[dict]:
    """
    Use Claude Haiku to extract candidate stakeholders from a transcript.

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

    # Sample transcript: take beginning + middle + end to cover the whole meeting.
    # Meetings often start with small talk — the real business is deeper in.
    max_chars = 16000
    if len(transcript) > max_chars:
        third = max_chars // 3
        truncated = (
            transcript[:third]
            + "\n\n[...transcript continues...]\n\n"
            + transcript[len(transcript)//2 - third//2 : len(transcript)//2 + third//2]
            + "\n\n[...transcript continues...]\n\n"
            + transcript[-third:]
        )
    else:
        truncated = transcript

    prompt = f"""Extract stakeholders from this CropSight (agtech startup) meeting transcript.

A "stakeholder" is a specific person or organization that CropSight has a DIRECT
business relationship with — someone you'd add to a CRM. Examples:
- A named advisor or contact: "Jason Adelman"
- A partner company in active discussions: "Lavazza", "Ferrero"
- A grant body CropSight applied to: "IIA" (Israel Innovation Authority)
- A specific pilot location tied to operations: "Gagauzia", "Moldova"

Do NOT extract:
- Big tech/infra companies (AWS, Google, Microsoft, IBM, etc.)
- Countries or cities mentioned in passing (not business sites)
- Tools/platforms (Zoom, Slack, WhatsApp, Tactiq, Google Sheets)
- Generic terms (AI, cloud, GPS, machine learning)
- Vague descriptions that aren't proper names
- Transcript artifacts or garbled text

EXCLUDE these participants/team: {', '.join(sorted(exclude_names))}

For each stakeholder, provide:
- name: Proper name (full name for people, official name for orgs)
- type: person / organization / project / location
- context: One sentence — CropSight's relationship with them
- speaker: Who mentioned them
- sentiment: positive / neutral / negative / mixed
- relationship: advisor / investor / partner / client / grant_body / pilot_site / vendor / other

TRANSCRIPT:
{truncated}

Return JSON:
{{"entities": [{{"name": "...", "type": "...", "context": "...", "speaker": "...", "sentiment": "...", "relationship": "..."}}]}}

Return only genuine CropSight stakeholders. An empty list is perfectly fine."""

    try:
        response_text, _ = call_llm(
            prompt=prompt,
            model=settings.model_simple,
            max_tokens=2048,
            call_site="entity_extraction",
        )
        parsed = _parse_json_response(response_text)
        entities = parsed.get("entities", [])

        # Basic post-filter: remove excluded names and very short names
        filtered = []
        for e in entities:
            name_lower = e.get("name", "").lower().strip()
            if not name_lower or name_lower in exclude_names:
                continue
            if len(name_lower) < 2:
                continue
            filtered.append(e)

        logger.info(f"Extracted {len(filtered)} candidate stakeholders")
        return filtered

    except Exception as e:
        logger.error(f"Error extracting entities: {e}")
        return []


async def _validate_entities(candidates: list[dict]) -> list[dict]:
    """
    Use Claude Haiku to validate extracted candidates.

    Takes the list of candidates from pass 1 and asks the LLM to filter
    out anything that isn't a genuine CropSight stakeholder. This replaces
    the hardcoded blocklist — the LLM understands context better than
    a static word list.

    Args:
        candidates: List of candidate entity dicts from _extract_raw_entities.

    Returns:
        Filtered list of validated entities.
    """
    if not candidates:
        return []

    # Build a simple list for the LLM to review
    candidate_lines = []
    for i, c in enumerate(candidates):
        name = c.get("name", "")
        etype = c.get("type", "")
        context = c.get("context", "")
        relationship = c.get("relationship", "")
        candidate_lines.append(
            f"{i+1}. \"{name}\" ({etype}) — {context} [relationship: {relationship}]"
        )

    prompt = f"""Review these candidate stakeholders extracted from a CropSight (agtech startup) meeting transcript.

For each candidate, decide: is this a REAL stakeholder that belongs in a CRM/stakeholder tracker?

KEEP if it's:
- A specific person CropSight works with (advisor, investor, partner contact, client)
- A specific company/organization CropSight has business dealings with
- A specific grant body, government agency CropSight applied to by name
- A named pilot location or project tied to CropSight operations

REJECT if it's:
- A big tech company used as infrastructure (AWS, Google, IBM, Microsoft, etc.)
- A country, city, or region mentioned casually (not a specific pilot/office site)
- A generic/vague term, not a proper entity name
- A tool, platform, or technology (not a business partner)
- A university/hospital mentioned in passing (not a specific CropSight partner)
- A transcript artifact, mis-transcription, or garbled name

CANDIDATES:
{chr(10).join(candidate_lines)}

Return JSON with ONLY the numbers of candidates to KEEP:
{{"keep": [1, 3, 5]}}

If none should be kept, return: {{"keep": []}}"""

    try:
        response_text, _ = call_llm(
            prompt=prompt,
            model=settings.model_simple,
            max_tokens=512,
            call_site="entity_validation",
        )
        parsed = _parse_json_response(response_text)
        keep_indices = parsed.get("keep", [])

        # Convert 1-based indices to 0-based and filter
        validated = []
        for idx in keep_indices:
            zero_idx = idx - 1
            if 0 <= zero_idx < len(candidates):
                validated.append(candidates[zero_idx])

        logger.info(
            f"Validation: {len(candidates)} candidates -> "
            f"{len(validated)} kept"
        )
        return validated

    except Exception as e:
        # On validation failure, pass through all candidates rather
        # than losing everything. Better to have some junk than miss
        # real stakeholders.
        logger.warning(f"Entity validation failed, passing all through: {e}")
        return candidates


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


def review_entity_health() -> dict:
    """
    Weekly entity registry health check.

    Scans the entity registry for quality issues:
    1. Team-member entities that slipped through (auto-cleaned).
    2. Orphan entities with zero mentions across all meetings.
    3. New entities added this week (for Eyal to review).

    Call from the weekly digest to keep the registry clean.

    Returns:
        Dict with:
        - auto_cleaned: List of entity names removed.
        - orphans: List of entities with no mentions.
        - new_this_week: List of entities created in the last 7 days.
        - total_entities: Total remaining entity count.
    """
    from datetime import datetime, timedelta

    result = {
        "auto_cleaned": [],
        "orphans": [],
        "new_this_week": [],
        "total_entities": 0,
    }

    try:
        entities = supabase_client.list_entities(limit=500)
        one_week_ago = datetime.now() - timedelta(days=7)

        # Build a set of team member names (lowercase) to catch any that slipped in
        team_names = set(n.lower() for n in get_team_member_names())
        team_first_names = set(n.split()[0].lower() for n in get_team_member_names())

        for entity in entities:
            eid = entity["id"]
            name = entity.get("canonical_name", "")
            name_lower = name.lower().strip()

            # 1. Auto-clean team-member entities
            if name_lower in team_names or name_lower in team_first_names:
                try:
                    supabase_client.client.table("entity_mentions").delete().eq("entity_id", eid).execute()
                    supabase_client.client.table("entities").delete().eq("id", eid).execute()
                    result["auto_cleaned"].append(name)
                    logger.info(f"Auto-cleaned team member entity: {name}")
                except Exception as e:
                    logger.warning(f"Failed to auto-clean entity '{name}': {e}")
                continue

            # 2. Check for orphans (no mentions in any meeting)
            mentions = supabase_client.get_entity_mentions(entity_id=eid, limit=1)
            if not mentions:
                result["orphans"].append({
                    "name": name,
                    "type": entity.get("entity_type", ""),
                    "id": eid,
                })

            # 3. Check if created this week
            created_at = entity.get("created_at", "")
            if created_at:
                try:
                    created_dt = datetime.fromisoformat(
                        str(created_at).replace("Z", "+00:00")
                    ).replace(tzinfo=None)
                    if created_dt >= one_week_ago:
                        result["new_this_week"].append({
                            "name": name,
                            "type": entity.get("entity_type", ""),
                        })
                except (ValueError, TypeError):
                    pass

        # Recount after cleanup
        remaining = supabase_client.list_entities(limit=500)
        result["total_entities"] = len(remaining)

        logger.info(
            f"Entity health check: {len(result['auto_cleaned'])} cleaned, "
            f"{len(result['orphans'])} orphans, "
            f"{len(result['new_this_week'])} new this week, "
            f"{result['total_entities']} total"
        )

    except Exception as e:
        logger.error(f"Error in entity health check: {e}")

    return result
