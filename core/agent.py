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

        # v0.3: Query routing — classify and pre-fetch relevant context
        query_type = self._classify_query(user_message)
        extra_context = ""
        if query_type != "general":
            extra_context = await self._get_query_context(query_type, user_message)
            logger.info(f"Query classified as '{query_type}', pre-fetched context")

        # Build messages array
        messages = []

        # Add conversation history if provided
        if conversation_history:
            messages.extend(conversation_history)

        # Add user context to the message
        team_member = get_team_member(user_id)
        user_context = ""
        if team_member:
            user_context = f"[Message from {team_member['name']} ({team_member['role']})]"

        # Build the full user message with any pre-fetched context
        full_message = user_message
        if user_context:
            full_message = f"{user_context}\n\n{user_message}"
        if extra_context:
            full_message = f"{full_message}\n\n{extra_context}"

        # Add current user message
        messages.append({
            "role": "user",
            "content": full_message,
        })

        # Track actions taken
        actions_taken = []
        sources_cited = []

        # Tool use loop
        iterations = 0
        while iterations < self.max_tool_iterations:
            iterations += 1

            # Call Claude API
            response = self.client.messages.create(
                model=self.model,
                max_tokens=4096,
                system=self.system_prompt_cached,
                tools=self.tools,
                messages=messages,
            )

            # Check stop reason
            if response.stop_reason == "end_turn":
                # No more tool calls, extract final response
                final_text = self._extract_text_response(response)
                break

            elif response.stop_reason == "tool_use":
                # Process tool calls
                tool_results = []

                for content_block in response.content:
                    if content_block.type == "tool_use":
                        tool_name = content_block.name
                        tool_input = content_block.input
                        tool_id = content_block.id

                        logger.info(f"Executing tool: {tool_name}")

                        # Execute the tool
                        try:
                            result = await self._execute_tool_call(tool_name, tool_input)
                            tool_results.append({
                                "type": "tool_result",
                                "tool_use_id": tool_id,
                                "content": str(result),
                            })
                            actions_taken.append({
                                "tool": tool_name,
                                "input": tool_input,
                                "success": True,
                            })
                        except Exception as e:
                            logger.error(f"Tool execution error: {e}")
                            tool_results.append({
                                "type": "tool_result",
                                "tool_use_id": tool_id,
                                "content": f"Error: {str(e)}",
                                "is_error": True,
                            })
                            actions_taken.append({
                                "tool": tool_name,
                                "input": tool_input,
                                "success": False,
                                "error": str(e),
                            })

                # Add assistant response and tool results to messages
                messages.append({"role": "assistant", "content": response.content})
                messages.append({"role": "user", "content": tool_results})

            else:
                # Unexpected stop reason
                logger.warning(f"Unexpected stop reason: {response.stop_reason}")
                final_text = self._extract_text_response(response)
                break

        else:
            # Max iterations reached
            logger.warning("Max tool iterations reached")
            final_text = "I'm sorry, I wasn't able to complete your request. Please try rephrasing."

        # Log the interaction
        supabase_client.log_action(
            action="message_processed",
            details={
                "user_id": user_id,
                "message_preview": user_message[:100],
                "tools_used": [a["tool"] for a in actions_taken],
            },
            triggered_by=user_id,
        )

        return {
            "response": final_text,
            "actions": actions_taken,
            "sources": sources_cited,
        }

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

        return {
            "title": meeting.get("title"),
            "date": meeting.get("date"),
            "summary": meeting.get("summary"),
            "participants": meeting.get("participants"),
        }

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
        return {"tasks": tasks, "count": len(tasks)}

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
