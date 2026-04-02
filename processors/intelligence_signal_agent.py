"""
Intelligence Signal agent — main orchestration pipeline.

Generates the weekly CropSight Intelligence Signal by:
1. Building context from operational data
2. Running Perplexity research queries (with retry chain)
3. Synthesizing a report via Claude Opus
4. Uploading to Google Drive as a Google Doc
5. Submitting for CEO approval via MCP

Usage:
    from processors.intelligence_signal_agent import (
        generate_intelligence_signal,
        distribute_intelligence_signal,
    )

    result = await generate_intelligence_signal()
    # After MCP approval:
    result = await distribute_intelligence_signal("signal-w14-2026")
"""

import asyncio
import json
import logging
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

from config.settings import settings
from core.llm import call_llm
from processors.intelligence_signal_context import (
    build_context_packet,
    build_exploration_queries,
    build_research_queries,
)
from processors.intelligence_signal_prompts import (
    format_email_html,
    format_email_plain,
    format_telegram_notification,
    system_prompt_synthesis,
    user_prompt_synthesis,
)
from services.perplexity_client import perplexity_client
from services.supabase_client import supabase_client

logger = logging.getLogger(__name__)


async def generate_intelligence_signal(signal_id: str | None = None) -> dict:
    """
    Generate the weekly intelligence signal.

    Full pipeline: context → research → synthesis → DB → Drive → approval.

    Args:
        signal_id: Optional override (default: auto-generated from week/year).

    Returns:
        Dict with signal_id, status, drive_doc_url, and pipeline metadata.
    """
    # 1. Build context packet
    context = build_context_packet()
    if not signal_id:
        signal_id = context["signal_id"]

    logger.info(f"Starting intelligence signal generation: {signal_id}")

    # 2. Create DB record
    try:
        supabase_client.create_intelligence_signal({
            "signal_id": signal_id,
            "week_number": context["week_number"],
            "year": context["year"],
            "status": "generating",
            "context_snapshot": context,
        })
    except Exception as e:
        # May already exist (retry scenario) — try to update instead
        existing = supabase_client.get_intelligence_signal(signal_id)
        if existing and existing.get("signal_content"):
            # Content already generated — skip to Drive upload
            logger.info(f"Signal {signal_id} already has content, resuming from Drive upload")
            return await _resume_from_drive_upload(signal_id, existing)
        elif existing:
            supabase_client.update_intelligence_signal(signal_id, {
                "status": "generating",
                "context_snapshot": context,
            })
        else:
            logger.error(f"Failed to create signal record: {e}")
            return {"signal_id": signal_id, "status": "error", "error": str(e)}

    # 3. Build research queries
    research_queries = build_research_queries(context)
    exploration_queries = build_exploration_queries(context["week_number"])
    all_queries = research_queries + exploration_queries

    # 4. Execute Perplexity research with retry chain
    research_results, research_source = await _execute_research_with_retry(
        all_queries, signal_id
    )

    if not research_results:
        _set_error_status(signal_id, "All research sources failed")
        return {"signal_id": signal_id, "status": "error", "error": "research_failed"}

    # 5. Store research source and truncated results
    truncated_results = _truncate_research_results(research_results)
    supabase_client.update_intelligence_signal(signal_id, {
        "research_source": research_source,
        "research_results": truncated_results,
        "perplexity_queries_run": len(all_queries),
    })

    # 6. Synthesize report with Opus (timeout-guarded)
    try:
        signal_content, flags, token_usage = await _synthesize_report(
            context, research_results
        )
    except RuntimeError as e:
        logger.error(f"Synthesis failed for {signal_id}: {e}")
        _set_error_status(signal_id, str(e))
        await _alert_synthesis_failure(signal_id, str(e))
        return {"signal_id": signal_id, "status": "error", "error": str(e)}

    # 7. Store signal_content + flags to DB immediately (Fix 2 — before Drive)
    supabase_client.update_intelligence_signal(signal_id, {
        "signal_content": signal_content,
        "flags": flags,
        "token_usage": token_usage,
    })
    logger.info(f"Signal content saved to DB: {signal_id}")

    # 8. Generate .docx and upload to Drive
    drive_result = {}
    try:
        from services.google_drive import drive_service
        from services.word_generator import generate_signal_docx

        docx_bytes = generate_signal_docx(
            signal_content=signal_content,
            week_number=context["week_number"],
            year=context["year"],
            flags=flags,
            research_source=research_source,
        )
        filename = f"CropSight Intelligence Signal W{context['week_number']}-{context['year']}.docx"
        drive_result = await drive_service.save_intelligence_signal_docx(
            data=docx_bytes, filename=filename
        )
        if drive_result and drive_result.get("id"):
            supabase_client.update_intelligence_signal(signal_id, {
                "drive_doc_id": drive_result["id"],
                "drive_doc_url": drive_result.get("webViewLink", ""),
            })
            logger.info(f"Signal .docx uploaded to Drive: {drive_result.get('webViewLink')}")
    except Exception as e:
        logger.error(f"Drive upload failed for {signal_id}: {e}")
        # Non-fatal — content is safe in DB

    # 9. Video generation (if enabled)
    if settings.INTELLIGENCE_SIGNAL_VIDEO_ENABLED:
        await _generate_video(signal_id, signal_content, context, drive_result)

    # 10. Update competitor watchlist
    watchlist_changes = _update_competitor_watchlist(
        research_results, context["week_number"], context["year"]
    )

    # 11. Submit for approval or auto-distribute
    drive_link = drive_result.get("webViewLink", "")
    if settings.INTELLIGENCE_SIGNAL_AUTO_DISTRIBUTE:
        result = await distribute_intelligence_signal(signal_id)
        return result
    else:
        await _submit_for_approval(
            signal_id=signal_id,
            drive_link=drive_link,
            week_number=context["week_number"],
            flags=flags,
            research_source=research_source,
            watchlist_changes=watchlist_changes,
        )
        supabase_client.update_intelligence_signal(signal_id, {
            "status": "pending_approval",
        })

    supabase_client.log_action(
        action="intelligence_signal_generated",
        details={
            "signal_id": signal_id,
            "research_source": research_source,
            "queries_run": len(all_queries),
            "has_video": settings.INTELLIGENCE_SIGNAL_VIDEO_ENABLED,
            "flags_count": len(flags) if flags else 0,
        },
        triggered_by="auto",
    )

    return {
        "signal_id": signal_id,
        "status": "pending_approval",
        "drive_doc_url": drive_link,
        "research_source": research_source,
        "flags": flags,
        "watchlist_changes": watchlist_changes,
    }


async def distribute_intelligence_signal(signal_id: str) -> dict:
    """
    Distribute an approved intelligence signal via email.

    Args:
        signal_id: The signal to distribute.

    Returns:
        Dict with distribution status and recipient list.
    """
    signal = supabase_client.get_intelligence_signal(signal_id)
    if not signal:
        return {"status": "error", "error": f"Signal {signal_id} not found"}

    content = signal.get("signal_content", "")
    if not content:
        return {"status": "error", "error": "Signal has no content"}

    drive_link = signal.get("drive_doc_url", "")
    flags = signal.get("flags") or []
    week_number = signal.get("week_number", 0)
    year = signal.get("year", 0)
    recipients = settings.intelligence_signal_recipients_list

    if not recipients:
        return {"status": "error", "error": "No recipients configured"}

    # Send email
    from services.gmail import gmail_service

    subject = f"CropSight Intelligence Signal — W{week_number}/{year}"
    plain_body = format_email_plain(content, drive_link)
    html_body = format_email_html(content, drive_link, week_number, year, flags)

    email_sent = await gmail_service.send_email(
        to=recipients,
        subject=subject,
        body=plain_body,
        html_body=html_body,
    )

    if not email_sent:
        logger.error(f"Failed to send signal email for {signal_id}")
        return {"status": "error", "error": "Email send failed"}

    # Send Telegram confirmation
    from services.telegram_bot import telegram_bot

    await telegram_bot.send_to_eyal(
        f"Intelligence Signal W{week_number} distributed to {len(recipients)} recipients.",
        parse_mode="HTML",
    )

    # Update DB
    supabase_client.update_intelligence_signal(signal_id, {
        "status": "distributed",
        "recipients": recipients,
        "distributed_at": datetime.now(timezone.utc).isoformat(),
    })

    supabase_client.log_action(
        action="intelligence_signal_distributed",
        details={
            "signal_id": signal_id,
            "recipients": recipients,
            "recipient_count": len(recipients),
        },
        triggered_by="eyal",
    )

    logger.info(f"Signal {signal_id} distributed to {recipients}")

    return {
        "signal_id": signal_id,
        "status": "distributed",
        "recipients": recipients,
    }


# ── Research pipeline ──────────────────────────────────────────────────


async def _execute_research_with_retry(
    queries: list[dict], signal_id: str
) -> tuple[dict[str, str], str]:
    """
    Execute research queries with retry chain.

    Chain: Perplexity → retry Perplexity (+2h) → Claude search fallback.

    Returns:
        Tuple of (results_dict, research_source).
        results_dict maps section label to content string.
    """
    # Attempt 1: Perplexity
    if perplexity_client.is_available():
        results = await perplexity_client.search_batch(queries)
        successful = {
            k: v.content for k, v in results.items() if v.success and v.content
        }
        if len(successful) >= len(queries) * 0.5:
            logger.info(f"Perplexity research complete: {len(successful)}/{len(queries)}")
            return successful, "perplexity"

        # Attempt 2: Retry Perplexity after delay
        logger.warning(
            f"Perplexity partial failure ({len(successful)}/{len(queries)}), "
            f"retrying in 2 hours"
        )
        await asyncio.sleep(7200)  # 2 hours

        results = await perplexity_client.search_batch(queries)
        successful = {
            k: v.content for k, v in results.items() if v.success and v.content
        }
        if len(successful) >= len(queries) * 0.5:
            logger.info(f"Perplexity retry succeeded: {len(successful)}/{len(queries)}")
            return successful, "perplexity_retry"

    # Attempt 3: Claude search fallback
    logger.warning("Perplexity unavailable, falling back to Claude search")
    results = await _claude_search_fallback(queries)
    if results:
        return results, "claude_search"

    return {}, "failed"


async def _claude_search_fallback(queries: list[dict]) -> dict[str, str]:
    """
    Fallback: use call_llm with web search instructions.

    This is a degraded mode — Claude doesn't have real-time web access,
    but can provide general knowledge as a safety net.
    """
    results = {}

    # Combine queries into a single prompt for efficiency
    query_text = "\n".join(
        f"- {q['section']}: {q['query']}" for q in queries[:8]
    )

    prompt = (
        f"Based on your knowledge, provide brief updates for each of these topics. "
        f"Be honest about what you know vs. don't know. Mark anything uncertain.\n\n"
        f"{query_text}"
    )

    try:
        loop = asyncio.get_running_loop()
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = loop.run_in_executor(
                executor,
                lambda: call_llm(
                    prompt=prompt,
                    model=settings.model_agent,
                    max_tokens=4096,
                    call_site="intelligence_signal_claude_fallback",
                    system="You are a research assistant providing market intelligence updates. Be factual and honest about uncertainty.",
                ),
            )
            response_text, _ = await asyncio.wait_for(future, timeout=60)

        # Split response into sections (best-effort)
        for q in queries[:8]:
            section = q["section"]
            results[section] = response_text

        return results

    except Exception as e:
        logger.error(f"Claude search fallback failed: {e}")
        return {}


# ── Synthesis ──────────────────────────────────────────────────────────


async def _synthesize_report(
    context: dict, research_results: dict[str, str]
) -> tuple[str, list[dict], dict]:
    """
    Synthesize the intelligence signal report via Claude Opus.

    Wrapped in ThreadPoolExecutor with 120s timeout (Fix 4).

    Returns:
        Tuple of (signal_content, flags, token_usage).
    """
    system = system_prompt_synthesis()
    prompt = user_prompt_synthesis(context, research_results)

    loop = asyncio.get_running_loop()
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = loop.run_in_executor(
            executor,
            lambda: call_llm(
                prompt=prompt,
                model=settings.model_extraction,
                max_tokens=8192,
                call_site="intelligence_signal_synthesis",
                system=system,
            ),
        )
        try:
            response_text, usage = await asyncio.wait_for(future, timeout=120)
        except asyncio.TimeoutError:
            raise RuntimeError("Opus synthesis timed out after 120s")

    # Extract flags from the response
    flags = _extract_flags(response_text)

    return response_text, flags, usage


def _extract_flags(content: str) -> list[dict]:
    """
    Extract structured flags from the synthesis output.

    Looks for the FLAGS section and parses each flag line.
    Format: **[FLAG]** Description (urgency: high or medium)
    """
    flags = []

    # Find FLAGS section
    flags_match = re.search(
        r"###?\s*FLAGS?\s*\n(.*?)(?=\n###?\s|\Z)",
        content,
        re.DOTALL | re.IGNORECASE,
    )
    if not flags_match:
        return flags

    flags_text = flags_match.group(1).strip()
    if "no flags" in flags_text.lower():
        return flags

    # Parse individual flags
    flag_pattern = re.compile(
        r"\*\*\[FLAG\]\*\*\s*(.+?)(?:\(urgency:\s*(high|medium)\))?$",
        re.MULTILINE | re.IGNORECASE,
    )
    for match in flag_pattern.finditer(flags_text):
        flag_text = match.group(1).strip().rstrip(".")
        urgency = (match.group(2) or "medium").lower()
        flags.append({"flag": flag_text, "urgency": urgency})

    return flags[:3]  # Max 3 flags


# ── Approval and distribution ─────────────────────────────────────────


async def _submit_for_approval(
    signal_id: str,
    drive_link: str,
    week_number: int,
    flags: list[dict],
    research_source: str,
    watchlist_changes: dict | None,
) -> None:
    """Submit signal for CEO approval via pending_approvals + Telegram notification."""
    # Create pending approval
    approval_content = {
        "signal_id": signal_id,
        "drive_link": drive_link,
        "week_number": week_number,
        "flags": flags,
        "research_source": research_source,
        "watchlist_changes": watchlist_changes,
    }

    try:
        supabase_client.create_pending_approval(
            approval_id=signal_id,
            content_type="intelligence_signal",
            content=approval_content,
        )
    except Exception as e:
        logger.error(f"Failed to create pending approval for {signal_id}: {e}")

    # Update signal with approval reference
    supabase_client.update_intelligence_signal(signal_id, {
        "approval_id": signal_id,
    })

    # Telegram notification (notification-only, not full content)
    notification = format_telegram_notification(
        signal_id=signal_id,
        drive_link=drive_link,
        week_number=week_number,
        flags=flags,
        research_source=research_source,
        watchlist_changes=watchlist_changes,
    )

    from services.telegram_bot import telegram_bot

    await telegram_bot.send_to_eyal(notification, parse_mode="HTML")
    logger.info(f"Approval notification sent for {signal_id}")


# ── Competitor watchlist auto-curation (Fix 3) ─────────────────────────


def _update_competitor_watchlist(
    research_results: dict[str, str],
    week_number: int,
    year: int,
) -> dict:
    """
    Auto-curate the competitor watchlist based on research results.

    - New names in competitive landscape → insert as 'discovered'
    - Existing names → increment appearance_count, update last_seen
    - appearance_count >= 3 and category='discovered' → promote to 'watching'
    - All entries not seen for 4+ weeks → deactivate

    Returns:
        Dict with promoted, deactivated, discovered lists (for Telegram notification).
    """
    changes: dict[str, list[str]] = {
        "promoted": [],
        "deactivated": [],
        "discovered": [],
    }

    try:
        # Get competitor landscape section
        comp_text = research_results.get("competitor_landscape", "")
        if not comp_text:
            return changes

        # Get existing competitors for matching
        existing = supabase_client.get_competitor_watchlist(include_deactivated=True)
        existing_names = {c["name"].lower(): c for c in existing}

        # Extract company names from research (simple heuristic)
        mentioned_names = _extract_company_names(comp_text, existing)

        for name in mentioned_names:
            name_lower = name.lower()
            if name_lower in existing_names:
                # Update existing competitor
                comp = existing_names[name_lower]
                new_count = (comp.get("appearance_count") or 0) + 1
                updates = {
                    "appearance_count": new_count,
                    "last_seen_week": week_number,
                    "last_seen_year": year,
                    "is_active": True,
                }

                # Auto-promote: 3+ appearances and still 'discovered'
                if new_count >= 3 and comp.get("category") == "discovered":
                    updates["category"] = "watching"
                    changes["promoted"].append(name)
                    logger.info(f"Auto-promoted competitor: {name}")

                supabase_client.upsert_competitor({
                    "name": comp["name"],
                    **updates,
                })
            else:
                # Discover new competitor
                supabase_client.upsert_competitor({
                    "name": name,
                    "category": "discovered",
                    "appearance_count": 1,
                    "last_seen_week": week_number,
                    "last_seen_year": year,
                    "added_by": "auto_discovered",
                    "is_active": True,
                })
                changes["discovered"].append(name)
                logger.info(f"Auto-discovered competitor: {name}")

        # Deactivate stale competitors (4+ weeks silent)
        deactivated_count = supabase_client.deactivate_stale_competitors(
            weeks_threshold=4
        )
        if deactivated_count > 0:
            # Identify which were deactivated
            stale = [
                c["name"]
                for c in existing
                if c.get("is_active")
                and c.get("last_seen_week")
                and _weeks_since(
                    c["last_seen_week"],
                    c.get("last_seen_year", year),
                    week_number,
                    year,
                )
                >= 4
            ]
            changes["deactivated"] = stale

    except Exception as e:
        logger.error(f"Competitor watchlist update failed: {e}")
        # Non-blocking — signal generation continues

    return changes


def _extract_company_names(
    text: str, existing_competitors: list[dict]
) -> list[str]:
    """
    Extract company names from research text.

    Uses existing competitor names as anchors, plus simple heuristics
    for discovering new names (capitalized multi-word patterns).
    """
    found = []

    # First, check for known competitor names
    for comp in existing_competitors:
        name = comp["name"]
        if name.lower() in text.lower():
            found.append(name)

    # Simple heuristic for new names: look for capitalized words
    # that might be company names (very conservative)
    # This is intentionally conservative — false negatives are better
    # than false positives for auto-discovery
    known_agtech_patterns = [
        r"\b([A-Z][a-z]+(?:\s[A-Z][a-z]+)*(?:\s(?:AI|ML|Tech|Ag|Bio))?)\b"
    ]
    for pattern in known_agtech_patterns:
        matches = re.findall(pattern, text)
        for match in matches:
            if (
                len(match) > 3
                and match.lower() not in {c["name"].lower() for c in existing_competitors}
                and match.lower() not in found
                and match not in [
                    "The", "This", "What", "How", "New", "Recent",
                    "According", "However", "Additionally", "Furthermore",
                ]
            ):
                # Only add if it looks like it could be a company
                found.append(match)

    return list(set(found))


def _weeks_since(
    last_week: int, last_year: int, current_week: int, current_year: int
) -> int:
    """Calculate approximate weeks between two ISO week/year pairs."""
    return (current_year - last_year) * 52 + (current_week - last_week)


# ── Video pipeline (disabled by default) ───────────────────────────────


async def _generate_video(
    signal_id: str,
    signal_content: str,
    context: dict,
    drive_result: dict,
) -> None:
    """
    Generate enhanced video for the signal.

    Attempts structured script (JSON segments) with per-segment TTS.
    Falls back to plain-text script + old assemble_video() if JSON fails.
    """
    from services.elevenlabs_client import elevenlabs_client
    from services.video_assembler import video_assembler

    try:
        week = context["week_number"]
        year = context["year"]

        # 1. Try structured script (JSON segments)
        segments = await _generate_structured_script(signal_content)

        if segments:
            # 2. Per-segment TTS
            audio_clips = []
            for seg in segments:
                clip = await elevenlabs_client.text_to_speech(seg["narration"])
                if clip:
                    audio_clips.append(clip)
                else:
                    logger.warning(
                        f"TTS failed for segment '{seg.get('segment_type')}', "
                        f"falling back to plain script"
                    )
                    audio_clips = []
                    break

            if audio_clips:
                # 3. Assemble with per-segment timing
                video_bytes = await video_assembler.assemble_video_segments(
                    segments=segments,
                    audio_clips=audio_clips,
                )

                if video_bytes:
                    await _upload_video_outputs(
                        signal_id, video_bytes, audio_clips,
                        segments, week, year,
                    )
                    return

        # Fallback: plain-text script + old assemble_video()
        logger.info(f"Using fallback video pipeline for {signal_id}")
        await _generate_video_fallback(signal_id, signal_content, context)

    except Exception as e:
        logger.error(f"Video generation failed for {signal_id}: {e}")
        # Non-fatal — signal generation continues


async def _generate_structured_script(signal_content: str) -> list[dict] | None:
    """Generate structured JSON script via LLM, return parsed segments or None."""
    try:
        from processors.intelligence_signal_prompts import (
            system_prompt_structured_script,
            user_prompt_structured_script,
        )

        loop = asyncio.get_running_loop()
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = loop.run_in_executor(
                executor,
                lambda: call_llm(
                    prompt=user_prompt_structured_script(signal_content),
                    model=settings.model_agent,
                    max_tokens=2048,
                    call_site="intelligence_signal_structured_script",
                    system=system_prompt_structured_script(),
                ),
            )
            script_json, _ = await asyncio.wait_for(future, timeout=60)

        segments = _parse_script_segments(script_json)
        if segments:
            logger.info(f"Structured script: {len(segments)} segments parsed")
            return segments

        logger.warning("Structured script parsing returned no valid segments")
        return None

    except Exception as e:
        logger.warning(f"Structured script generation failed: {e}")
        return None


async def _generate_video_fallback(
    signal_id: str, signal_content: str, context: dict
) -> None:
    """Fallback: plain-text script + old assemble_video() method."""
    from processors.intelligence_signal_prompts import (
        system_prompt_script,
        user_prompt_script,
    )
    from services.elevenlabs_client import elevenlabs_client
    from services.video_assembler import video_assembler

    loop = asyncio.get_running_loop()
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = loop.run_in_executor(
            executor,
            lambda: call_llm(
                prompt=user_prompt_script(signal_content),
                model=settings.model_agent,
                max_tokens=1024,
                call_site="intelligence_signal_script",
                system=system_prompt_script(),
            ),
        )
        script_text, _ = await asyncio.wait_for(future, timeout=60)

    supabase_client.update_intelligence_signal(signal_id, {
        "script_text": script_text,
    })

    audio = await elevenlabs_client.text_to_speech(script_text)
    if not audio:
        logger.error(f"Fallback TTS failed for {signal_id}")
        return

    sections = video_assembler.parse_script_to_sections(script_text)
    slides = video_assembler.create_slides(sections)
    video_bytes = await video_assembler.assemble_video(slides, audio)

    if video_bytes:
        week = context["week_number"]
        year = context["year"]
        await _upload_video_outputs(
            signal_id, video_bytes, [audio], None, week, year
        )


async def _upload_video_outputs(
    signal_id: str,
    video_bytes: bytes,
    audio_clips: list[bytes],
    segments: list[dict] | None,
    week: int,
    year: int,
) -> None:
    """Upload video + audio podcast to Drive, update DB."""
    from services.google_drive import drive_service

    # Upload video
    video_filename = f"CropSight Signal W{week}-{year}.mp4"
    video_result = await drive_service.save_intelligence_signal_video(
        data=video_bytes, filename=video_filename
    )

    updates: dict = {}
    if video_result and video_result.get("id"):
        updates["drive_video_id"] = video_result["id"]
        updates["drive_video_url"] = video_result.get("webViewLink", "")
        logger.info(f"Video uploaded: {video_result.get('webViewLink')}")

    # Save script text
    if segments:
        import json
        updates["script_text"] = json.dumps(segments, ensure_ascii=False)

    if updates:
        supabase_client.update_intelligence_signal(signal_id, updates)

    # Upload audio-only podcast version
    try:
        combined_audio = b"".join(audio_clips)
        audio_filename = f"CropSight Signal W{week}-{year} (Audio).mp3"
        audio_result = await drive_service.save_intelligence_signal_audio(
            data=combined_audio, filename=audio_filename
        )
        if audio_result and audio_result.get("id"):
            logger.info(f"Audio podcast uploaded: {audio_result.get('webViewLink')}")
    except Exception as e:
        logger.warning(f"Audio podcast upload failed: {e}")


def _parse_script_segments(script_json: str) -> list[dict]:
    """
    Parse JSON segment array from LLM output.

    Handles code-fence stripping and validation.
    Returns empty list on failure (caller falls back to plain script).
    """
    import json

    # Strip markdown code fences if present
    cleaned = re.sub(r'^```(?:json)?\s*\n?', '', script_json.strip())
    cleaned = re.sub(r'\n?```\s*$', '', cleaned)

    try:
        segments = json.loads(cleaned)
        if not isinstance(segments, list):
            return []

        valid = []
        for s in segments:
            if isinstance(s, dict) and "narration" in s and "segment_type" in s:
                valid.append(s)

        return valid

    except json.JSONDecodeError as e:
        logger.warning(f"Failed to parse structured script JSON: {e}")
        return []


# ── Helpers ────────────────────────────────────────────────────────────


def _truncate_research_results(results: dict[str, str]) -> dict:
    """
    Truncate research results to 3KB per result before DB storage.

    Raw Perplexity responses can be verbose. We keep full content only
    in signal_content (the synthesized report). research_results are
    stored for debugging — truncation at 3KB is fine.
    """
    truncated = {}
    for section, content in results.items():
        if isinstance(content, str) and len(content) > 3000:
            truncated[section] = content[:3000] + "... [truncated]"
        else:
            truncated[section] = content
    return truncated


def _set_error_status(signal_id: str, error_message: str) -> None:
    """Set signal status to error with message."""
    try:
        supabase_client.update_intelligence_signal(signal_id, {
            "status": "error",
            "context_snapshot": {
                "error": error_message,
                "error_at": datetime.now(timezone.utc).isoformat(),
            },
        })
    except Exception:
        pass


async def _alert_synthesis_failure(signal_id: str, error: str) -> None:
    """Send Telegram alert on synthesis failure."""
    try:
        from services.telegram_bot import telegram_bot

        await telegram_bot.send_to_eyal(
            f"Intelligence Signal {signal_id} synthesis failed: {error}",
            parse_mode="HTML",
        )
    except Exception:
        pass


async def _resume_from_drive_upload(signal_id: str, existing: dict) -> dict:
    """Resume pipeline from Drive upload when content already exists."""
    content = existing.get("signal_content", "")
    flags = existing.get("flags") or []
    week_number = existing.get("week_number", 0)
    year = existing.get("year", 0)

    # Upload to Drive if not already done
    drive_link = existing.get("drive_doc_url", "")
    if not drive_link:
        try:
            from services.google_drive import drive_service
            from services.word_generator import generate_signal_docx

            docx_bytes = generate_signal_docx(
                signal_content=content,
                week_number=week_number,
                year=year,
                flags=flags,
                research_source=existing.get("research_source", "unknown"),
            )
            filename = f"CropSight Intelligence Signal W{week_number}-{year}.docx"
            drive_result = await drive_service.save_intelligence_signal_docx(
                data=docx_bytes, filename=filename
            )
            if drive_result and drive_result.get("id"):
                drive_link = drive_result.get("webViewLink", "")
                supabase_client.update_intelligence_signal(signal_id, {
                    "drive_doc_id": drive_result["id"],
                    "drive_doc_url": drive_link,
                })
        except Exception as e:
            logger.error(f"Drive resume upload failed: {e}")

    # Submit for approval if not already done
    if existing.get("status") == "generating":
        await _submit_for_approval(
            signal_id=signal_id,
            drive_link=drive_link,
            week_number=week_number,
            flags=flags,
            research_source=existing.get("research_source", "unknown"),
            watchlist_changes=None,
        )
        supabase_client.update_intelligence_signal(signal_id, {
            "status": "pending_approval",
        })

    return {
        "signal_id": signal_id,
        "status": "pending_approval",
        "drive_doc_url": drive_link,
        "resumed": True,
    }
