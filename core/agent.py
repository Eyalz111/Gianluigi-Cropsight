"""
Main Claude agent with tool use capabilities.

This module contains the GianluigiAgent class which orchestrates all
interactions with the Claude API using tool use for structured actions.

The agent:
- Receives user messages or system triggers (e.g., new transcript detected)
- Uses the system prompt from system_prompt.py
- Has access to tools defined in tools.py
- Processes responses and executes tool calls
- Maintains conversation context within a session

Usage:
    from core.agent import GianluigiAgent

    agent = GianluigiAgent()
    response = await agent.process_message(
        user_message="What did we decide about cloud providers?",
        user_id="eyal"
    )
"""

import logging
from typing import Any

from anthropic import Anthropic

from config.settings import settings
from config.team import get_team_member
from core.conversation_agent import ConversationAgent
from core.router import classify_intent
from core.system_prompt import (
    get_system_prompt,
    get_meeting_prep_prompt,
    get_query_response_prompt,
)
from core.tools import TOOL_DEFINITIONS
from services.supabase_client import supabase_client
from services.embeddings import embedding_service

logger = logging.getLogger(__name__)


class GianluigiAgent:
    """
    The main Gianluigi AI agent powered by Claude.

    Handles all AI interactions including:
    - Processing user queries via Telegram/email
    - Extracting structured data from transcripts
    - Generating meeting summaries and prep documents
    - Semantic search across the knowledge base
    """

    def __init__(self):
        """
        Initialize the agent with Claude client and configuration.
        """
        self.client = Anthropic(api_key=settings.ANTHROPIC_API_KEY)
        self.model = settings.model_agent
        self.system_prompt = get_system_prompt()
        # Prompt caching: cache the ~4,300-token system prompt across calls
        # within 5-minute windows. Cached reads cost 10% of normal input price.
        self.system_prompt_cached = [{
            "type": "text",
            "text": self.system_prompt,
            "cache_control": {"type": "ephemeral"}
        }]
        self.tools = TOOL_DEFINITIONS
        self.max_tool_iterations = 10  # Prevent infinite tool loops

        # v1.0: Conversation Agent handles the tool-use dialogue loop
        self.conversation_agent = ConversationAgent(
            tool_executor=self._execute_tool_call
        )

    def _classify_query(self, user_message: str) -> str:
        """
        Classify query type for optimized retrieval.

        Uses simple keyword matching (no LLM call needed) to determine
        the type of query, enabling pre-fetching of relevant context.

        Args:
            user_message: The user's query text.

        Returns:
            Query type: 'task_status', 'entity_lookup', 'decision_history', or 'general'.
        """
        msg_lower = user_message.lower()

        if any(w in msg_lower for w in [
            "status of", "progress on", "where are we",
            "update on", "how is", "what happened with",
        ]):
            return "task_status"

        if any(w in msg_lower for w in [
            "what do we know about", "history with", "tell me about",
            "who is", "what company", "background on",
        ]):
            return "entity_lookup"

        if any(w in msg_lower for w in [
            "when did we decide", "why did we decide", "what was decided",
            "decision about", "did we agree",
        ]):
            return "decision_history"

        return "general"

    async def _get_query_context(self, query_type: str, user_message: str) -> str:
        """
        Pre-fetch extra context based on query type.

        For task_status queries, fetches relevant tasks + task mentions.
        For entity_lookup, fetches mentions across meetings.
        For decision_history, fetches related decisions.

        Args:
            query_type: The classified query type.
            user_message: The user's query text.

        Returns:
            Extra context string to inject into the conversation.
        """
        context_parts = []

        if query_type == "task_status":
            # Pre-fetch relevant tasks and their cross-meeting mentions
            try:
                all_tasks = supabase_client.get_tasks(status=None, limit=50)
                # Find tasks mentioned in the query
                msg_lower = user_message.lower()
                relevant_tasks = [
                    t for t in all_tasks
                    if any(word in t.get("title", "").lower()
                           for word in msg_lower.split() if len(word) > 3)
                ]
                if relevant_tasks:
                    context_parts.append("[TASK STATUS CONTEXT]")
                    for t in relevant_tasks[:5]:
                        context_parts.append(
                            f"- \"{t.get('title')}\" ({t.get('assignee')}, "
                            f"status: {t.get('status')}, "
                            f"category: {t.get('category', 'N/A')})"
                        )
                        # Fetch task mentions
                        mentions = supabase_client.get_task_mentions(
                            task_id=t.get("id"), limit=5
                        )
                        for m in mentions:
                            context_parts.append(
                                f"  Mentioned in meeting: \"{m.get('mention_text', '')[:80]}\" "
                                f"(confidence: {m.get('confidence', 'N/A')})"
                            )
            except Exception as e:
                logger.debug(f"Error pre-fetching task context: {e}")

        elif query_type == "entity_lookup":
            try:
                # Extract the entity name from the query
                msg_lower = user_message.lower()
                # Try to find an entity matching key terms
                keywords = [w for w in msg_lower.split() if len(w) > 3]
                for keyword in keywords[:3]:
                    entity = supabase_client.find_entity_by_name(keyword)
                    if entity:
                        context_parts.append("[ENTITY CONTEXT]")
                        context_parts.append(
                            f"Entity: {entity.get('canonical_name')} "
                            f"(type: {entity.get('entity_type')})"
                        )
                        aliases = entity.get("aliases", [])
                        if aliases:
                            context_parts.append(
                                f"  Aliases: {', '.join(aliases)}"
                            )
                        # Fetch recent mentions
                        mentions = supabase_client.get_entity_mentions(
                            entity_id=entity.get("id"), limit=5
                        )
                        for m in mentions:
                            meeting_info = m.get("meetings", {}) or {}
                            meeting_title = meeting_info.get("title", "Unknown")
                            context_parts.append(
                                f"  Mentioned in \"{meeting_title}\": "
                                f"{m.get('context', m.get('mention_text', ''))[:100]}"
                            )
                        break  # Found an entity, stop searching
            except Exception as e:
                logger.debug(f"Error pre-fetching entity context: {e}")

        elif query_type == "decision_history":
            try:
                # Extract key terms for decision search
                msg_lower = user_message.lower()
                keywords = [w for w in msg_lower.split() if len(w) > 3]
                if keywords:
                    context_parts.append("[DECISION HISTORY CONTEXT]")
                    for keyword in keywords[:3]:
                        decisions = supabase_client.list_decisions(
                            topic=keyword, limit=5
                        )
                        for d in decisions:
                            meeting_info = d.get("meetings", {}) or {}
                            meeting_title = meeting_info.get("title", "Unknown")
                            context_parts.append(
                                f"- Decision: \"{d.get('description', '')[:100]}\" "
                                f"(from: {meeting_title})"
                            )
            except Exception as e:
                logger.debug(f"Error pre-fetching decision context: {e}")

        return "\n".join(context_parts) if context_parts else ""

    async def process_message(
        self,
        user_message: str,
        user_id: str,
        conversation_history: list | None = None
    ) -> dict:
        """
        Process a user message and return the agent's response.

        This is the main entry point for user queries via Telegram or email.
        Routes through the multi-agent pipeline: Router → Conversation Agent.

        Args:
            user_message: The message from the user.
            user_id: Identifier for the user (eyal, roye, paolo, yoram).
            conversation_history: Optional list of previous messages for context.

        Returns:
            Dict containing:
            - response: The text response to the user
            - actions: List of actions taken (tools called)
            - sources: List of sources cited
        """
        logger.info(f"Processing message from {user_id}: {user_message[:50]}...")

        # Step 1: Classify intent via Router Agent
        intent = await classify_intent(
            message=user_message,
            conversation_mode=None,  # Phase 1: no active modes
            user_id=user_id,
        )
        logger.info(f"Router classified intent as: {intent}")

        # Step 2: Pre-fetch context based on query type (existing v0.3 logic)
        query_type = self._classify_query(user_message)
        extra_context = ""
        if query_type != "general":
            extra_context = await self._get_query_context(query_type, user_message)
            logger.info(f"Query classified as '{query_type}', pre-fetched context")

        # Step 3: Dispatch to Conversation Agent
        # Phase 1: All intents route to Conversation Agent regardless of classification.
        # Phase 3+ will branch here: debrief → DebriefFlow, weekly_review → WeeklyReviewFlow,
        # gantt_request → Operator Agent, etc.
        result = await self.conversation_agent.respond(
            user_message=user_message,
            user_id=user_id,
            conversation_history=conversation_history,
            intent=intent,
            extra_context=extra_context,
        )

        # Log the interaction
        supabase_client.log_action(
            action="message_processed",
            details={
                "user_id": user_id,
                "message_preview": user_message[:100],
                "intent": intent,
                "tools_used": [a["tool"] for a in result.get("actions", [])],
            },
            triggered_by=user_id,
        )

        return result

    async def process_transcript(
        self,
        transcript_content: str,
        meeting_title: str,
        meeting_date: str,
        participants: list[str]
    ) -> dict:
        """
        Process a raw meeting transcript and extract structured data.

        This is triggered when a new Tactiq export is detected in Google Drive.
        Delegates to the transcript_processor module for full pipeline.

        Args:
            transcript_content: The raw transcript text with speaker labels.
            meeting_title: Title of the meeting.
            meeting_date: Date of the meeting (ISO format).
            participants: List of participant names.

        Returns:
            Dict containing extracted decisions, tasks, follow-ups, etc.
        """
        # Import here to avoid circular imports
        from processors.transcript_processor import process_transcript

        return await process_transcript(
            file_content=transcript_content,
            meeting_title=meeting_title,
            meeting_date=meeting_date,
            participants=participants,
        )

    async def generate_meeting_prep(
        self,
        calendar_event: dict
    ) -> dict:
        """
        Generate a meeting preparation document for an upcoming meeting.

        Args:
            calendar_event: Google Calendar event dict with title, attendees, etc.

        Returns:
            Dict containing:
            - prep_document: The formatted prep document
            - meeting_id: ID of related past meetings
            - approval_status: 'pending'
        """
        event_title = calendar_event.get("title", "Upcoming Meeting")
        attendees = calendar_event.get("attendees", [])

        logger.info(f"Generating prep for: {event_title}")

        # Search for related past meetings
        query_embedding = await embedding_service.embed_text(event_title)
        related_chunks = supabase_client.search_embeddings(
            query_embedding=query_embedding,
            similarity_threshold=0.6,
            limit=10,
            source_type="meeting",
        )

        # Get related decisions
        # Search decisions by keyword from title
        title_keywords = event_title.lower().split()
        related_decisions = []
        for keyword in title_keywords[:3]:  # Limit to avoid too many queries
            if len(keyword) > 3:  # Skip short words
                decisions = supabase_client.list_decisions(topic=keyword)
                related_decisions.extend(decisions[:5])

        # Get tasks for attendees
        related_tasks = []
        for attendee in attendees:
            email = attendee.get("email", "")
            # Try to match email to team member
            from config.team import TEAM_MEMBERS
            for member_id, member in TEAM_MEMBERS.items():
                if member.get("email") == email:
                    tasks = supabase_client.get_tasks(
                        assignee=member["name"],
                        status="pending"
                    )
                    related_tasks.extend(tasks[:5])
                    break

        # Get open questions
        open_questions = supabase_client.list_open_questions(status="open")[:5]

        # Get stakeholder info (placeholder - needs Google Sheets integration)
        stakeholder_info = []

        # Extract related meeting IDs from chunks
        related_meetings = []
        seen_meeting_ids = set()
        for chunk in related_chunks:
            meeting_id = chunk.get("source_id")
            if meeting_id and meeting_id not in seen_meeting_ids:
                seen_meeting_ids.add(meeting_id)
                meeting = supabase_client.get_meeting(meeting_id)
                if meeting:
                    related_meetings.append(meeting)

        # Build the prep prompt
        prep_prompt = get_meeting_prep_prompt(
            calendar_event=calendar_event,
            related_meetings=related_meetings[:5],
            related_decisions=related_decisions[:10],
            related_tasks=related_tasks[:10],
            stakeholder_info=stakeholder_info,
            open_questions=open_questions,
        )

        # Generate prep document with Claude (background task, uses background tier)
        response = self.client.messages.create(
            model=settings.model_background,
            max_tokens=2048,
            system=self.system_prompt,
            messages=[{"role": "user", "content": prep_prompt}],
        )

        prep_document = self._extract_text_response(response)

        # Log the action
        supabase_client.log_action(
            action="meeting_prep_generated",
            details={
                "event_title": event_title,
                "related_meetings_count": len(related_meetings),
                "related_decisions_count": len(related_decisions),
            },
            triggered_by="auto",
        )

        logger.info(f"Generated prep document for: {event_title}")

        return {
            "prep_document": prep_document,
            "related_meetings": [m.get("id") for m in related_meetings],
            "approval_status": "pending",
        }

    async def _execute_tool_call(self, tool_name: str, tool_input: dict) -> Any:
        """
        Execute a tool call and return the result.

        Routes tool calls to their respective implementations.

        Args:
            tool_name: Name of the tool to execute.
            tool_input: Input parameters for the tool.

        Returns:
            Tool execution result (varies by tool).
        """
        # Tool routing
        if tool_name == "search_meetings":
            return await self._tool_search_meetings(tool_input)

        elif tool_name == "get_meeting_summary":
            return await self._tool_get_meeting_summary(tool_input)

        elif tool_name == "create_task":
            return await self._tool_create_task(tool_input)

        elif tool_name == "get_tasks":
            return await self._tool_get_tasks(tool_input)

        elif tool_name == "update_task":
            return await self._tool_update_task(tool_input)

        elif tool_name == "search_memory":
            return await self._tool_search_memory(tool_input)

        elif tool_name == "list_decisions":
            return await self._tool_list_decisions(tool_input)

        elif tool_name == "get_open_questions":
            return await self._tool_get_open_questions(tool_input)

        elif tool_name == "get_stakeholder_info":
            return await self._tool_get_stakeholder_info(tool_input)

        elif tool_name == "ingest_transcript":
            return await self._tool_ingest_transcript(tool_input)

        elif tool_name == "ingest_document":
            return await self._tool_ingest_document(tool_input)

        elif tool_name == "get_meeting_prep":
            return await self._tool_get_meeting_prep(tool_input)

        # v0.2 tools
        elif tool_name == "generate_weekly_digest":
            return await self._tool_generate_weekly_digest(tool_input)

        elif tool_name == "update_stakeholder_tracker":
            return await self._tool_update_stakeholder_tracker(tool_input)

        elif tool_name == "search_gmail":
            return await self._tool_search_gmail(tool_input)

        # v0.3 Tier 2 tools
        elif tool_name == "get_entity_info":
            return await self._tool_get_entity_info(tool_input)

        elif tool_name == "get_entity_timeline":
            return await self._tool_get_entity_timeline(tool_input)

        elif tool_name == "get_commitments":
            return await self._tool_get_commitments(tool_input)

        # v1.0 Phase 2 — Gantt Integration tools
        elif tool_name == "get_gantt_status":
            return await self._tool_get_gantt_status(tool_input)

        elif tool_name == "get_gantt_section":
            return await self._tool_get_gantt_section(tool_input)

        elif tool_name == "get_meeting_cadence":
            return await self._tool_get_meeting_cadence(tool_input)

        elif tool_name == "get_gantt_horizon":
            return await self._tool_get_gantt_horizon(tool_input)

        elif tool_name == "propose_gantt_update":
            return await self._tool_propose_gantt_update(tool_input)

        elif tool_name == "get_gantt_history":
            return await self._tool_get_gantt_history(tool_input)

        elif tool_name == "rollback_gantt_update":
            return await self._tool_rollback_gantt_update(tool_input)

        else:
            raise ValueError(f"Unknown tool: {tool_name}")

    # =========================================================================
    # Tool Implementations
    # =========================================================================

    async def _tool_search_meetings(self, input: dict) -> dict:
        """Semantic search over meeting transcripts."""
        query = input.get("query", "")

        # Generate embedding for query
        query_embedding = await embedding_service.embed_text(query)

        # Search embeddings
        results = supabase_client.search_embeddings(
            query_embedding=query_embedding,
            similarity_threshold=0.65,
            limit=10,
            source_type="meeting",
        )

        # Format results
        formatted = []
        for r in results:
            formatted.append({
                "chunk_text": r.get("chunk_text", ""),
                "speaker": r.get("speaker"),
                "timestamp_range": r.get("timestamp_range"),
                "similarity": r.get("similarity"),
                "source_id": r.get("source_id"),
            })

        return {"results": formatted, "count": len(formatted)}

    async def _tool_get_meeting_summary(self, input: dict) -> dict:
        """Retrieve a meeting summary by ID."""
        meeting_id = input.get("meeting_id", "")
        meeting = supabase_client.get_meeting(meeting_id)

        if not meeting:
            return {"error": "Meeting not found"}

        result = {
            "title": meeting.get("title"),
            "date": meeting.get("date"),
            "summary": meeting.get("summary"),
            "participants": meeting.get("participants"),
        }

        # Add Drive link so the user can navigate directly
        if settings.MEETING_SUMMARIES_FOLDER_ID:
            result["summaries_folder"] = (
                f"https://drive.google.com/drive/folders/{settings.MEETING_SUMMARIES_FOLDER_ID}"
            )

        return result

    async def _tool_create_task(self, input: dict) -> dict:
        """Create a new task."""
        task = supabase_client.create_task(
            title=input.get("title", ""),
            assignee=input.get("assignee", "team"),
            deadline=input.get("deadline"),
            priority=input.get("priority", "M"),
            status="pending",
            meeting_id=input.get("meeting_id"),
            category=input.get("category"),
        )
        return {"success": True, "task_id": task.get("id"), "task": task}

    async def _tool_get_tasks(self, input: dict) -> dict:
        """Get tasks filtered by assignee/status/category."""
        assignee = input.get("assignee")

        # Resolve short names (e.g., "eyal") to full names ("Eyal Zror")
        # The LLM may pass just a first name or member_id
        if assignee:
            from config.team import TEAM_MEMBERS
            member = get_team_member(assignee)
            if member:
                assignee = member["name"]
            else:
                # Check if it matches any full name (case-insensitive)
                for m in TEAM_MEMBERS.values():
                    if m["name"].lower() == assignee.lower():
                        assignee = m["name"]
                        break

        tasks = supabase_client.get_tasks(
            assignee=assignee,
            status=input.get("status"),
            category=input.get("category"),
        )

        result = {"tasks": tasks, "count": len(tasks)}

        # Add Sheets link so the user can navigate directly
        if settings.TASK_TRACKER_SHEET_ID:
            result["task_tracker"] = (
                f"https://docs.google.com/spreadsheets/d/{settings.TASK_TRACKER_SHEET_ID}/edit"
            )

        return result

    async def _tool_update_task(self, input: dict) -> dict:
        """Update a task's status or deadline."""
        task_id = input.get("task_id", "")

        task = supabase_client.update_task(
            task_id=task_id,
            status=input.get("status"),
            deadline=input.get("deadline"),
        )
        return {"success": True, "task": task}

    async def _tool_search_memory(self, input: dict) -> dict:
        """Combined search across all memory sources."""
        query = input.get("query", "")

        # Generate embedding
        query_embedding = await embedding_service.embed_text(query)

        # Search all sources
        results = supabase_client.search_memory(
            query_embedding=query_embedding,
            query_text=query,
        )

        # Add Drive link so the user can navigate directly
        if settings.DOCUMENTS_FOLDER_ID:
            results["documents_folder"] = (
                f"https://drive.google.com/drive/folders/{settings.DOCUMENTS_FOLDER_ID}"
            )

        return results

    async def _tool_list_decisions(self, input: dict) -> dict:
        """List decisions filtered by meeting or topic."""
        decisions = supabase_client.list_decisions(
            meeting_id=input.get("meeting_id"),
            topic=input.get("topic"),
        )
        return {"decisions": decisions, "count": len(decisions)}

    async def _tool_get_open_questions(self, input: dict) -> dict:
        """Get open questions."""
        status = input.get("status", "open")
        questions = supabase_client.list_open_questions(status=status)
        return {"questions": questions, "count": len(questions)}

    async def _tool_get_stakeholder_info(self, input: dict) -> dict:
        """Get stakeholder information from the tracker."""
        from services.google_sheets import sheets_service

        try:
            stakeholders = await sheets_service.get_stakeholder_info(
                name=input.get("name"),
                organization=input.get("organization"),
            )
            return {"stakeholders": stakeholders, "count": len(stakeholders)}
        except Exception as e:
            logger.error(f"Error fetching stakeholder info: {e}")
            return {"stakeholders": [], "count": 0, "error": str(e)}

    async def _tool_ingest_transcript(self, input: dict) -> dict:
        """Process a transcript through the full pipeline."""
        result = await self.process_transcript(
            transcript_content=input.get("file_content", ""),
            meeting_title=input.get("meeting_title", "Untitled Meeting"),
            meeting_date=input.get("date", ""),
            participants=input.get("participants", []),
        )
        return result

    async def _tool_ingest_document(self, input: dict) -> dict:
        """Ingest a document into the knowledge base."""
        # Create document record
        document = supabase_client.create_document(
            title=input.get("title", "Untitled"),
            source=input.get("source", "upload"),
            summary="",  # Will be generated
        )
        doc_id = document["id"]

        # Generate embeddings
        content = input.get("content", "")
        embedded_chunks = await embedding_service.chunk_and_embed_document(
            document=content,
            document_id=doc_id
        )

        # Store embeddings
        if embedded_chunks:
            embedding_records = [
                {
                    "source_type": "document",
                    "source_id": doc_id,
                    "chunk_text": chunk["text"],
                    "chunk_index": chunk["chunk_index"],
                    "embedding": chunk["embedding"],
                    "metadata": chunk.get("metadata", {}),
                }
                for chunk in embedded_chunks
            ]
            supabase_client.store_embeddings_batch(embedding_records)

        return {
            "success": True,
            "document_id": doc_id,
            "chunks_created": len(embedded_chunks),
        }

    async def _tool_get_meeting_prep(self, input: dict) -> dict:
        """Generate meeting prep document."""
        # This would need Google Calendar integration
        # For now, accept a minimal event dict
        calendar_event = {
            "id": input.get("calendar_event_id", ""),
            "title": input.get("title", "Upcoming Meeting"),
            "attendees": [],
        }
        return await self.generate_meeting_prep(calendar_event)

    # =========================================================================
    # v0.2 Tool Implementations
    # =========================================================================

    async def _tool_generate_weekly_digest(self, input: dict) -> dict:
        """Generate a weekly digest document."""
        from processors.weekly_digest import generate_weekly_digest
        from datetime import datetime

        week_start = None
        if input.get("week_start"):
            try:
                week_start = datetime.fromisoformat(input["week_start"])
            except ValueError:
                pass

        result = await generate_weekly_digest(week_start=week_start)
        if not result:
            return {"error": "Failed to generate weekly digest"}
        return result

    async def _tool_update_stakeholder_tracker(self, input: dict) -> dict:
        """Suggest a stakeholder tracker update (requires Eyal approval)."""
        from guardrails.approval_flow import submit_stakeholder_updates_for_approval

        result = await submit_stakeholder_updates_for_approval(
            stakeholder_name=input.get("stakeholder_name", ""),
            organization=input.get("organization", ""),
            updates=input.get("updates", {}),
            source_meeting_id=input.get("source_meeting_id"),
        )
        return result

    async def _tool_search_gmail(self, input: dict) -> dict:
        """Search Gianluigi's Gmail inbox."""
        from services.gmail import gmail_service

        query = input.get("query", "")
        max_results = input.get("max_results", 5)

        try:
            # Use Gmail API search
            results = gmail_service.service.users().messages().list(
                userId="me",
                q=query,
                maxResults=max_results,
            ).execute()

            messages = results.get("messages", [])
            detailed = []

            for msg in messages[:max_results]:
                full_msg = await gmail_service.get_message(msg["id"])
                if full_msg:
                    detailed.append({
                        "from": full_msg.get("from", ""),
                        "subject": full_msg.get("subject", ""),
                        "date": full_msg.get("date", ""),
                        "snippet": full_msg.get("snippet", ""),
                    })

            return {"results": detailed, "count": len(detailed)}

        except Exception as e:
            logger.error(f"Error searching Gmail: {e}")
            return {"results": [], "count": 0, "error": str(e)}

    # =========================================================================
    # v0.3 Tier 2 Tool Implementations
    # =========================================================================

    async def _tool_get_entity_info(self, input: dict) -> dict:
        """Look up an entity by name."""
        name = input.get("name", "")
        entity = supabase_client.find_entity_by_name(name)

        if not entity:
            return {"error": f"No entity found matching '{name}'"}

        # Get recent mentions
        mentions = supabase_client.get_entity_mentions(
            entity_id=entity["id"], limit=10
        )

        return {
            "entity": entity,
            "recent_mentions": mentions,
            "mention_count": len(mentions),
        }

    async def _tool_get_entity_timeline(self, input: dict) -> dict:
        """Get chronological entity timeline."""
        entity_id = input.get("entity_id", "")
        limit = input.get("limit", 20)

        entity = supabase_client.get_entity(entity_id)
        if not entity:
            return {"error": f"Entity not found: {entity_id}"}

        timeline = supabase_client.get_entity_timeline(
            entity_id=entity_id, limit=limit
        )

        return {
            "entity": entity,
            "timeline": timeline,
            "mention_count": len(timeline),
        }

    async def _tool_get_commitments(self, input: dict) -> dict:
        """Get commitments with optional filters."""
        speaker = input.get("speaker")

        # Resolve short names like get_tasks does
        if speaker:
            from config.team import TEAM_MEMBERS
            member = get_team_member(speaker)
            if member:
                speaker = member["name"]

        commitments = supabase_client.get_commitments(
            speaker=speaker,
            status=input.get("status"),
        )

        result = {"commitments": commitments, "count": len(commitments)}

        # Add Sheets link so the user can navigate directly
        if settings.TASK_TRACKER_SHEET_ID:
            result["task_tracker"] = (
                f"https://docs.google.com/spreadsheets/d/{settings.TASK_TRACKER_SHEET_ID}/edit"
            )

        return result

    # =========================================================================
    # v1.0 Phase 2 — Gantt Integration Tool Implementations
    # =========================================================================

    async def _tool_get_gantt_status(self, input: dict) -> dict:
        """Get Gantt status for a week."""
        from services.gantt_manager import gantt_manager
        return await gantt_manager.get_gantt_status(week=input.get("week"))

    async def _tool_get_gantt_section(self, input: dict) -> dict:
        """Deep dive into a Gantt section."""
        from services.gantt_manager import gantt_manager
        return await gantt_manager.get_gantt_section(
            section=input.get("section", ""),
            weeks=input.get("weeks"),
        )

    async def _tool_get_meeting_cadence(self, input: dict) -> dict:
        """Get meeting cadence from Gantt."""
        from services.gantt_manager import gantt_manager
        return await gantt_manager.get_meeting_cadence(week=input.get("week"))

    async def _tool_get_gantt_horizon(self, input: dict) -> dict:
        """Get upcoming milestones from Gantt."""
        from services.gantt_manager import gantt_manager
        return await gantt_manager.get_gantt_horizon(
            weeks_ahead=input.get("weeks_ahead", 8)
        )

    async def _tool_propose_gantt_update(self, input: dict) -> dict:
        """Propose Gantt changes — creates approval request."""
        from services.gantt_manager import gantt_manager
        from guardrails.approval_flow import submit_for_approval

        changes = input.get("changes", [])
        source = input.get("source", "telegram")

        result = await gantt_manager.propose_gantt_update(
            changes=changes,
            source=source,
        )

        if result.get("status") in ("rejected", "needs_confirmation"):
            return result

        # Submit for approval via the standard flow
        proposal_id = result.get("proposal_id")
        if proposal_id:
            await submit_for_approval(
                content_type="gantt_update",
                content={
                    "proposal_id": proposal_id,
                    "changes": result.get("changes", []),
                    "changes_count": result.get("changes_count", 0),
                    "source": source,
                },
                meeting_id=f"gantt-{proposal_id}",
            )
            # Tell Claude the approval request was already sent to Telegram,
            # so it should NOT repeat the proposal details in its response.
            result["approval_sent"] = True
            result["note"] = (
                "Approval request sent to Telegram. "
                "Just confirm briefly that the proposal was submitted — "
                "do NOT repeat the change details."
            )

        return result

    async def _tool_get_gantt_history(self, input: dict) -> dict:
        """Get recent Gantt changes."""
        from services.gantt_manager import gantt_manager
        return await gantt_manager.get_gantt_history(
            limit=input.get("limit", 10)
        )

    async def _tool_rollback_gantt_update(self, input: dict) -> dict:
        """Rollback a Gantt change."""
        from services.gantt_manager import gantt_manager
        return await gantt_manager.rollback_proposal(
            proposal_id=input.get("proposal_id")
        )

    # =========================================================================
    # Helper Methods
    # =========================================================================

    def _extract_text_response(self, response) -> str:
        """Extract text content from Claude response."""
        text_parts = []
        for block in response.content:
            if hasattr(block, "text"):
                text_parts.append(block.text)
        return "\n".join(text_parts)


# Singleton instance for easy import
gianluigi_agent = GianluigiAgent()
