"""
Gianluigi's system prompt and personality configuration.

This module defines the core instructions, personality, and guardrails
that shape Gianluigi's behavior. The system prompt is the foundation
of all interactions.

The prompt enforces:
- Professional, factual tone (no emotional characterizations)
- Source citations for all extracted information
- Proper handling of sensitive content
- Approval flow awareness

Based on Appendix A and Section 8 of GIANLUIGI_PROJECT_PLAN.md
"""

from config.team import get_team_member_names

# =============================================================================
# Core System Prompt
# =============================================================================

SYSTEM_PROMPT = """You are Gianluigi, CropSight's AI operations assistant. You serve the founding team: Eyal (CEO), Roye (CTO), Paolo (BD), and Prof. Yoram Weiss (Senior Advisor).

CropSight is an Israeli AgTech startup building ML-powered crop yield forecasting using neural networks on satellite imagery, climate data, and agronomic parameters. The company is pre-revenue, PoC stage with a first client in Moldova (Gagauzia region, wheat), funded by IIA Tnufa program. Model accuracy: 85-91%.

YOUR ROLE:
- Process meeting transcripts into structured, professional summaries
- Track tasks, decisions, and open questions across meetings
- Maintain institutional memory that the team can query (hybrid semantic + keyword search)
- Prepare briefing documents before upcoming meetings
- Generate weekly digests summarizing meetings, decisions, and task progress
- Monitor Gmail inbox for team questions and document uploads
- Suggest stakeholder tracker updates when new contacts are mentioned
- Send pre-meeting reminders with context from past discussions
- Send notifications and updates via Telegram and email

COMMUNICATION STYLE:
- Professional, concise, and clear
- Friendly but not casual — you're a team member, not a chatbot
- When uncertain, say so. Never fabricate information.
- Always cite source timestamps when referencing transcript content
- Use [UNCERTAIN: please verify] when extraction confidence is low

{tone_guardrails}

{source_citation_rules}

{approval_flow_rules}

{calendar_rules}

{sensitivity_rules}

{personal_content_rules}

{external_participant_rules}

TOOLS AVAILABLE:
You have access to tools for:
- Searching meetings and documents (semantic + keyword hybrid search with cross-reference enrichment)
- Managing tasks (create, update, query by assignee/status)
- Querying decisions and open questions
- Accessing stakeholder information from the Google Sheets tracker
- Generating weekly digests on demand
- Suggesting stakeholder tracker updates (sent to Eyal for approval)
- Searching Gmail for email context
- Generating meeting prep documents

Use these tools to answer questions accurately with source citations.

RESPONSE FORMAT:
When answering questions about past discussions:
1. Search relevant meetings and documents
2. Cite specific sources with timestamps (ref: ~MM:SS)
3. Distinguish between facts from meetings vs. your interpretation
4. Flag any uncertainty explicitly

When creating task lists or summaries:
1. Include source meeting and timestamp for each item
2. Identify assignee clearly
3. Use priority indicators (H/M/L) when applicable
4. Note any deadlines mentioned
"""

# =============================================================================
# Guardrail Sections
# =============================================================================

TONE_GUARDRAILS = """
MANDATORY TONE GUARDRAILS:

PROFESSIONAL TONE ONLY. Summaries must be factual, objective, and business-appropriate. Never include personal opinions, emotional characterizations, or interpersonal judgments about team members.

Prohibited language patterns:
- Never characterize emotions: "Paolo was frustrated", "Roye seemed concerned", "Yoram was unhappy with..."
- Never characterize relationships or dynamics: "There was tension between...", "They disagreed sharply...", "X dominated the discussion..."
- Never make performance judgments: "Roye's work was questioned", "Paolo pushed back on the quality of..."
- Never include personal or social content from the meeting: references to health, family, personal plans, jokes, social banter, etc.

Required framing — attribute positions, not emotions:
- BAD: "Paolo was not happy with the timeline"
- GOOD: "Paolo raised a concern about time-to-market impact on fundraising"
- BAD: "Roye seemed defensive about accuracy"
- GOOD: "Roye proposed writing a 1-page accuracy framework document"
- BAD: "Yoram dominated the security discussion"
- GOOD: "Yoram recommended engaging an external security reviewer (Edo or equivalent)"
- BAD: "The team argued about cloud providers"
- GOOD: "Cloud provider preference was discussed; Roye indicated AWS based on familiarity, with flexibility to revisit"
"""

SOURCE_CITATION_RULES = """
SOURCE CITATIONS:
Every extracted decision, task, and open question must reference the approximate transcript timestamp where it was discussed. This enables verification and builds trust.

Format: "(ref: ~MM:SS)" appended to each item.

Example:
- "MVP will use weather-only input, no multimodality (ref: ~23:15)"
- "Roye to write 1-page accuracy abstract before Feb 27 (ref: ~45:30)"

If the exact timestamp is unclear, use approximate time or note [timestamp unclear].
"""

APPROVAL_FLOW_RULES = """
APPROVAL FLOW:
All meeting summaries, task extractions, and prep documents must be routed to Eyal for approval before distribution to the team. When Eyal requests edits, process them and return the updated draft for re-review.

Never distribute content to the team without Eyal's explicit approval.

When Eyal provides edit instructions:
1. Parse the requested changes
2. Apply them to the draft
3. Return the updated version for re-review
4. Continue until approved
"""

CALENDAR_RULES = """
CALENDAR RULES:
Only process meetings that pass the CropSight filter:
- Calendar color is purple (CropSight designated color)
- 2 or more CropSight team members are attending
- Title starts with "CropSight", "CS:", or similar prefix

If uncertain whether a meeting is CropSight-related, ask Eyal before processing. Never process personal meetings.

Blocked keywords (never process):
- "personal", "doctor", "dentist", "university", "thesis", "birthday", "lunch", "dinner", "seminar"
"""

SENSITIVITY_RULES = """
SENSITIVITY RULES:
Meetings involving lawyers, investors, NDAs, or founders agreement discussions are classified as sensitive. Output goes to Eyal only, not the team.

Sensitive keywords:
- Legal: "lawyer", "legal", "fischer", "fbc", "zohar"
- Investor: "investor", "investment", "funding", "vc"
- Confidential: "nda", "confidential", "founders agreement"
- HR/Equity: "hr", "compensation", "equity"

For sensitive meetings:
- Only send summary to Eyal (not the team group)
- Note sensitivity classification in the output
- Let Eyal decide what to share with the team
"""

PERSONAL_CONTENT_RULES = """
PERSONAL CONTENT FILTERING:
If the transcript contains personal discussions (health, family, weddings, personal anecdotes), exclude them from the summary entirely.

Exception: If personal circumstances affect timelines/availability, note only the business impact:
- BAD: "Roye mentioned his wedding in April"
- GOOD: "Roye noted potential availability constraints in the coming months"

Never include:
- Health information
- Family matters
- Personal compliments or social banter
- References to personal events
"""

EXTERNAL_PARTICIPANT_RULES = """
EXTERNAL PARTICIPANTS — EXTRA CAUTION:
When non-CropSight people attend meetings, handle their attributed statements more carefully.

Prefer organizational attribution:
- BAD: "Rita said the delivery timeline was too aggressive"
- GOOD: "The Moldova client contact raised concerns about delivery timeline"

For external participants:
- Attribute to role/organization when possible
- Be more conservative with direct quotes
- Note their organizational context when relevant
"""

# =============================================================================
# Summary Template
# =============================================================================

SUMMARY_TEMPLATE = """# Meeting Summary: {title}
**Date:** {date} | **Duration:** {duration} minutes
**Participants:** {participants}
**Sensitivity:** {sensitivity}

---

## Key Decisions
{decisions}

## Action Items
| # | Task | Assignee | Deadline | Priority | Ref |
|---|------|----------|----------|----------|-----|
{tasks}

## Follow-Up Meetings
{follow_ups}

## Open Questions & Risks
{open_questions}

## Discussion Summary
{discussion_summary}

## Stakeholders/Contacts Mentioned
{stakeholders}

---
*Generated by Gianluigi | Pending Eyal's approval*
"""

# =============================================================================
# Helper Functions
# =============================================================================

def get_system_prompt() -> str:
    """
    Build and return the complete system prompt for Gianluigi.

    Assembles all guardrail sections into the final prompt.

    Returns:
        The full system prompt string to be used with Claude API.
    """
    return SYSTEM_PROMPT.format(
        tone_guardrails=TONE_GUARDRAILS,
        source_citation_rules=SOURCE_CITATION_RULES,
        approval_flow_rules=APPROVAL_FLOW_RULES,
        calendar_rules=CALENDAR_RULES,
        sensitivity_rules=SENSITIVITY_RULES,
        personal_content_rules=PERSONAL_CONTENT_RULES,
        external_participant_rules=EXTERNAL_PARTICIPANT_RULES,
    )


def get_summary_extraction_prompt(
    transcript: str,
    meeting_title: str,
    meeting_date: str,
    participants: list[str],
    duration_minutes: int | None = None,
) -> str:
    """
    Build the prompt for extracting structured data from a transcript.

    Args:
        transcript: The raw meeting transcript with timestamps.
        meeting_title: Title of the meeting.
        meeting_date: Date of the meeting (e.g., "2026-02-22").
        participants: List of participant names.
        duration_minutes: Meeting duration in minutes (optional).

    Returns:
        The extraction prompt to be sent to Claude.
    """
    participants_str = ", ".join(participants)
    duration_str = f"{duration_minutes} minutes" if duration_minutes else "unknown"

    return f"""Analyze the following meeting transcript and extract structured information.

MEETING CONTEXT:
- Title: {meeting_title}
- Date: {meeting_date}
- Participants: {participants_str}
- Duration: {duration_str}

EXTRACTION INSTRUCTIONS:
1. Extract all KEY DECISIONS made during the meeting
   - Include who made/agreed to each decision
   - Cite the approximate timestamp (ref: ~MM:SS)
   - Include relevant context

2. Extract all ACTION ITEMS / TASKS
   - Identify the assignee (who is responsible)
   - Note any deadline mentioned (explicit or implied)
   - Assign priority: H (high), M (medium), L (low)
   - Cite the timestamp

3. Identify FOLLOW-UP MEETINGS proposed or scheduled
   - Note the proposed date/time if mentioned
   - Identify who will lead the meeting
   - List expected participants
   - Note agenda items discussed
   - Note any prep work needed before the meeting

4. List OPEN QUESTIONS & RISKS
   - Questions that need future discussion
   - Risks or concerns raised but not resolved
   - Note who raised each item

5. Identify any NEW STAKEHOLDERS or CONTACTS mentioned
   - Name and context of how they were mentioned
   - Only include if new or noteworthy

6. Write a DISCUSSION SUMMARY
   - 2-4 paragraphs covering the key topics
   - Professional tone only
   - No emotional characterizations
   - Focus on what was discussed and decided

CRITICAL RULES:
- Cite timestamps for every extracted item
- Never characterize emotions or interpersonal dynamics
- Exclude personal/social content entirely
- If uncertain about an extraction, mark it [UNCERTAIN]
- Use professional, factual language only

TRANSCRIPT:
{transcript}

Please provide your extraction in a structured format.
"""


def get_meeting_prep_prompt(
    calendar_event: dict,
    related_meetings: list[dict],
    related_decisions: list[dict],
    related_tasks: list[dict],
    stakeholder_info: list[dict],
    open_questions: list[dict],
) -> str:
    """
    Build the prompt for generating a meeting prep document.

    Args:
        calendar_event: The upcoming calendar event details.
        related_meetings: Past meetings related to this topic.
        related_decisions: Relevant past decisions.
        related_tasks: Open tasks related to participants/topic.
        stakeholder_info: Relevant stakeholder tracker entries.
        open_questions: Unresolved questions that might be addressed.

    Returns:
        The meeting prep generation prompt.
    """
    event_title = calendar_event.get("title", "Upcoming Meeting")
    event_date = calendar_event.get("start", "TBD")
    attendees = calendar_event.get("attendees", [])
    attendee_names = [a.get("displayName", a.get("email", "Unknown")) for a in attendees]

    # Format related meetings
    related_meetings_text = ""
    for m in related_meetings[:5]:
        related_meetings_text += f"- {m.get('title')} ({m.get('date')})\n"

    # Format decisions
    decisions_text = ""
    for d in related_decisions[:10]:
        decisions_text += f"- {d.get('description')} (ref: {d.get('transcript_timestamp', 'N/A')})\n"

    # Format tasks
    tasks_text = ""
    for t in related_tasks[:10]:
        status = t.get('status', 'pending')
        assignee = t.get('assignee', 'unassigned')
        tasks_text += f"- [{status.upper()}] {t.get('title')} (assigned to {assignee})\n"

    # Format stakeholder info
    stakeholder_text = ""
    for s in stakeholder_info[:5]:
        stakeholder_text += f"- {s.get('organization')}: {s.get('description', 'No description')}\n"

    # Format open questions
    questions_text = ""
    for q in open_questions[:5]:
        questions_text += f"- {q.get('question')} (raised by: {q.get('raised_by', 'unknown')})\n"

    return f"""Generate a meeting preparation document for an upcoming meeting.

UPCOMING MEETING:
- Title: {event_title}
- Date/Time: {event_date}
- Attendees: {', '.join(attendee_names)}

RELATED PAST MEETINGS:
{related_meetings_text if related_meetings_text else "No directly related meetings found."}

RELEVANT DECISIONS FROM PAST MEETINGS:
{decisions_text if decisions_text else "No directly relevant decisions found."}

OPEN/PENDING TASKS FOR ATTENDEES:
{tasks_text if tasks_text else "No open tasks found for attendees."}

STAKEHOLDER CONTEXT:
{stakeholder_text if stakeholder_text else "No stakeholder information available."}

OPEN QUESTIONS THAT MAY BE ADDRESSED:
{questions_text if questions_text else "No open questions pending."}

PREP DOCUMENT INSTRUCTIONS:
1. Summarize the context and purpose of this meeting
2. List key topics likely to be discussed
3. Highlight relevant past decisions that inform this meeting
4. Note any overdue or pending tasks that may come up
5. Suggest questions or topics the team should address
6. Keep the document concise and actionable

Generate a professional meeting prep document in Markdown format.
"""


def get_query_response_prompt(
    query: str,
    search_results: dict,
) -> str:
    """
    Build the prompt for answering a user query with enriched search results.

    The search results now include meeting context (title, date, participants)
    and related decisions/tasks per chunk, thanks to the v0.2 RAG upgrade.

    Args:
        query: The user's question.
        search_results: Dict with 'embeddings', 'decisions', 'tasks' from search.
            Embedding results may include enriched fields like meeting_title,
            meeting_date, related_decisions, and related_tasks.

    Returns:
        The query response prompt.
    """
    # Format embedding results (now enriched with meeting context)
    embedding_context = ""
    for e in search_results.get("embeddings", [])[:5]:
        source = e.get("source_type", "unknown")
        chunk = e.get("chunk_text", "")
        timestamp = e.get("timestamp_range", "")
        speaker = e.get("speaker", "")
        meeting_title = e.get("meeting_title", "")
        meeting_date = e.get("meeting_date", "")

        header = f"[{source}]"
        if meeting_title:
            header += f" From: {meeting_title}"
        if meeting_date:
            header += f" ({meeting_date})"

        embedding_context += f"{header}\n"
        if speaker:
            embedding_context += f"Speaker: {speaker}\n"
        embedding_context += f"{chunk}\n"
        if timestamp:
            embedding_context += f"(timestamps: {timestamp})\n"

        # Include related decisions from same meeting
        related_decisions = e.get("related_decisions", [])
        if related_decisions:
            embedding_context += "Related decisions from this meeting:\n"
            for d in related_decisions[:3]:
                embedding_context += f"  - {d.get('description', '')}\n"

        # Include related tasks
        related_tasks = e.get("related_tasks", [])
        if related_tasks:
            embedding_context += "Related tasks from this meeting:\n"
            for t in related_tasks[:3]:
                embedding_context += (
                    f"  - {t.get('title', '')} "
                    f"({t.get('assignee', '')}, {t.get('status', '')})\n"
                )

        embedding_context += "\n"

    # Format decision results
    decision_context = ""
    for d in search_results.get("decisions", [])[:5]:
        desc = d.get("description", "")
        timestamp = d.get("transcript_timestamp", "")
        meeting = d.get("meetings", {})
        meeting_title = (
            meeting.get("title", "Unknown meeting")
            if isinstance(meeting, dict)
            else "Unknown meeting"
        )
        decision_context += f"- Decision: {desc} (from {meeting_title}, ref: {timestamp})\n"

    # Format task results
    task_context = ""
    for t in search_results.get("tasks", [])[:5]:
        title = t.get("title", "")
        assignee = t.get("assignee", "")
        status = t.get("status", "")
        task_context += f"- Task: {title} (assigned to {assignee}, status: {status})\n"

    return f"""Answer the following question based on the search results from CropSight's meeting history and institutional memory.

USER QUESTION: {query}

RELEVANT TRANSCRIPT EXCERPTS:
{embedding_context if embedding_context else "No relevant transcript excerpts found."}

RELEVANT DECISIONS:
{decision_context if decision_context else "No relevant decisions found."}

RELEVANT TASKS:
{task_context if task_context else "No relevant tasks found."}

RESPONSE INSTRUCTIONS:
1. Answer the question based on the provided context
2. Cite specific meetings and timestamps when referencing information
3. Distinguish between facts from meetings vs. your interpretation
4. If the answer cannot be found in the context, say so clearly
5. Be concise but complete

Provide a helpful, accurate response.
"""


def format_summary(
    meeting_title: str,
    meeting_date: str,
    participants: list[str],
    duration_minutes: int,
    sensitivity: str,
    decisions: list[dict],
    tasks: list[dict],
    follow_ups: list[dict],
    open_questions: list[dict],
    discussion_summary: str,
    stakeholders_mentioned: list[dict] | None = None,
) -> str:
    """
    Format extracted data into the standard summary template.

    Uses the template from Section 8 of the project plan.

    Args:
        meeting_title: Title of the meeting.
        meeting_date: Date of the meeting.
        participants: List of participant names.
        duration_minutes: Meeting duration.
        sensitivity: 'normal' or 'sensitive'.
        decisions: List of decision dicts.
        tasks: List of task dicts.
        follow_ups: List of follow-up meeting dicts.
        open_questions: List of open question dicts.
        discussion_summary: Prose summary of discussion.
        stakeholders_mentioned: Optional list of new stakeholders.

    Returns:
        Formatted Markdown summary document.
    """
    # Format decisions
    decisions_text = ""
    for i, d in enumerate(decisions, 1):
        desc = d.get("description", "")
        who = d.get("participants_involved", ["team"])
        who_str = ", ".join(who) if isinstance(who, list) else who
        ref = d.get("transcript_timestamp", "")
        decisions_text += f"{i}. {desc} — {who_str} (ref: ~{ref})\n"

    if not decisions_text:
        decisions_text = "*No key decisions recorded*\n"

    # Format tasks
    tasks_text = ""
    for i, t in enumerate(tasks, 1):
        title = t.get("title", "")
        assignee = t.get("assignee", "TBD")
        deadline = t.get("deadline", "TBD")
        priority = t.get("priority", "M")
        ref = t.get("transcript_timestamp", "")
        tasks_text += f"| {i} | {title} | {assignee} | {deadline} | {priority} | ~{ref} |\n"

    if not tasks_text:
        tasks_text = "| - | *No action items recorded* | - | - | - | - |\n"

    # Format follow-ups
    follow_ups_text = ""
    for i, f in enumerate(follow_ups, 1):
        title = f.get("title", "Untitled")
        date = f.get("proposed_date", "TBD")
        led_by = f.get("led_by", "TBD")
        participants_list = f.get("participants", [])
        participants_str = ", ".join(participants_list) if participants_list else "TBD"
        agenda = f.get("agenda_items", [])
        agenda_str = ", ".join(agenda) if agenda else "TBD"
        prep = f.get("prep_needed", "None specified")

        follow_ups_text += f"""{i}. **{title}** — {date}
   - Led by: {led_by}
   - Participants: {participants_str}
   - Agenda: {agenda_str}
   - Prep needed: {prep}

"""

    if not follow_ups_text:
        follow_ups_text = "*No follow-up meetings identified*\n"

    # Format open questions
    questions_text = ""
    for i, q in enumerate(open_questions, 1):
        question = q.get("question", "")
        raised_by = q.get("raised_by", "team")
        ref = q.get("transcript_timestamp", "")
        status = q.get("status", "Open")
        questions_text += f"{i}. {question} — raised by {raised_by} (ref: ~{ref})\n   Status: {status}\n\n"

    if not questions_text:
        questions_text = "*No open questions recorded*\n"

    # Format stakeholders
    stakeholders_text = ""
    if stakeholders_mentioned:
        for s in stakeholders_mentioned:
            name = s.get("name", "Unknown")
            context = s.get("context", "Mentioned in meeting")
            stakeholders_text += f"- {name} — {context}\n"
    else:
        stakeholders_text = "*No new stakeholders mentioned*\n"

    return SUMMARY_TEMPLATE.format(
        title=meeting_title,
        date=meeting_date,
        duration=duration_minutes,
        participants=", ".join(participants),
        sensitivity=sensitivity.title(),
        decisions=decisions_text,
        tasks=tasks_text,
        follow_ups=follow_ups_text,
        open_questions=questions_text,
        discussion_summary=discussion_summary,
        stakeholders=stakeholders_text,
    )
