"""
Supabase client for database operations.

This module handles all interactions with Supabase (PostgreSQL + pgvector):
- Connection management
- CRUD operations for all tables (meetings, decisions, tasks, etc.)
- Vector similarity search for semantic queries
- Audit logging

The database schema is defined in scripts/setup_supabase.sql

Usage:
    from services.supabase_client import db

    # Store a new meeting
    meeting = await db.create_meeting(...)

    # Semantic search
    results = await db.search_embeddings(query_embedding, limit=10)
"""

import json
import logging
import math
from datetime import datetime, date, timedelta
from typing import Any, Optional
from uuid import UUID

from supabase import create_client, Client
from supabase.lib.client_options import ClientOptions

from config.settings import settings

logger = logging.getLogger(__name__)


class SupabaseClient:
    """
    Client for all Supabase database operations.

    Handles both structured data (PostgreSQL) and vector search (pgvector).
    All methods are synchronous as supabase-py uses httpx sync client.
    """

    def __init__(self):
        """
        Initialize the Supabase client with credentials from settings.
        """
        self._client: Client | None = None

    @property
    def client(self) -> Client:
        """
        Lazy initialization of Supabase client.

        Returns:
            Initialized Supabase client.

        Raises:
            RuntimeError: If Supabase credentials are not configured.
        """
        if self._client is None:
            if not settings.SUPABASE_URL or not settings.SUPABASE_KEY:
                raise RuntimeError(
                    "Supabase credentials not configured. "
                    "Set SUPABASE_URL and SUPABASE_KEY in .env"
                )
            self._client = create_client(
                settings.SUPABASE_URL,
                settings.SUPABASE_KEY,
            )
        return self._client

    def _serialize_datetime(self, dt: datetime | date | str | None) -> str | None:
        """
        Convert datetime to ISO format string for Supabase.

        Handles datetime objects, date objects, ISO strings, and gracefully
        drops unparseable human-readable date strings (e.g., from Claude).
        """
        if dt is None:
            return None
        if isinstance(dt, datetime):
            return dt.isoformat()
        if isinstance(dt, date):
            return dt.isoformat()
        if isinstance(dt, str):
            # Try to parse ISO format strings
            try:
                return datetime.fromisoformat(dt).isoformat()
            except ValueError:
                pass
            # Try common date formats
            import re
            # Extract just the ISO date/datetime portion, discard trailing text
            iso_match = re.match(r'^(\d{4}-\d{2}-\d{2}(?:T\d{2}:\d{2}(?::\d{2})?(?:[+-]\d{2}:?\d{2}|Z)?)?)', dt)
            if iso_match:
                return iso_match.group(1)  # Return only the parseable portion
            # Unparseable (e.g., "Friday February 27th at 4 PM") — return None
            # to avoid Supabase TIMESTAMPTZ parse errors
            logger.warning(f"Could not parse date string: {dt!r}, storing as NULL")
            return None
        return str(dt)

    def _serialize_uuid(self, uuid_val: UUID | str | None) -> str | None:
        """Convert UUID to string for Supabase."""
        if uuid_val is None:
            return None
        return str(uuid_val)

    # =========================================================================
    # Meetings
    # =========================================================================

    def create_meeting(
        self,
        date: datetime,
        title: str,
        participants: list[str],
        raw_transcript: str | None = None,
        summary: str | None = None,
        sensitivity: str = "normal",
        source_file_path: str | None = None,
        duration_minutes: int | None = None,
    ) -> dict:
        """
        Create a new meeting record.

        Args:
            date: Meeting date and time.
            title: Meeting title.
            participants: List of participant names.
            raw_transcript: Full transcript text.
            summary: Processed summary (if available).
            sensitivity: 'normal', 'sensitive', or 'legal'.
            source_file_path: Google Drive path to original Tactiq export.
            duration_minutes: Meeting duration in minutes.

        Returns:
            The created meeting record with UUID.
        """
        data = {
            "date": self._serialize_datetime(date),
            "title": title,
            "participants": participants,
            "raw_transcript": raw_transcript,
            "summary": summary,
            "sensitivity": sensitivity,
            "source_file_path": source_file_path,
            "duration_minutes": duration_minutes,
            "approval_status": "pending",
        }

        result = self.client.table("meetings").insert(data).execute()
        logger.info(f"Created meeting: {title} (ID: {result.data[0]['id']})")

        # Log the action
        self.log_action(
            action="meeting_created",
            details={"meeting_id": result.data[0]["id"], "title": title},
            triggered_by="auto",
        )

        return result.data[0]

    def get_meeting(self, meeting_id: str) -> dict | None:
        """
        Retrieve a meeting by its UUID.

        Args:
            meeting_id: UUID of the meeting.

        Returns:
            Meeting record or None if not found.
        """
        result = (
            self.client.table("meetings")
            .select("*")
            .eq("id", meeting_id)
            .execute()
        )
        return result.data[0] if result.data else None

    def update_meeting(self, meeting_id: str, **updates) -> dict:
        """
        Update a meeting record.

        Args:
            meeting_id: UUID of the meeting to update.
            **updates: Fields to update (e.g., summary="...", approval_status="approved")

        Returns:
            Updated meeting record.
        """
        # Serialize datetime fields if present
        if "date" in updates:
            updates["date"] = self._serialize_datetime(updates["date"])
        if "approved_at" in updates:
            updates["approved_at"] = self._serialize_datetime(updates["approved_at"])

        result = (
            self.client.table("meetings")
            .update(updates)
            .eq("id", meeting_id)
            .execute()
        )
        logger.info(f"Updated meeting {meeting_id}: {list(updates.keys())}")
        if not result.data:
            logger.warning(f"Meeting {meeting_id} not found for update")
            return {}
        return result.data[0]

    def delete_meeting_cascade(self, meeting_id: str) -> dict:
        """
        Delete a meeting and all related data.

        Explicitly deletes all child tables before the meeting itself,
        because ON DELETE CASCADE only fires if the parent delete succeeds —
        and it won't succeed if any un-cascaded FK still references it.

        Deletion order (children first, then parent):
        1. embeddings (source_id, no FK cascade)
        2. task_mentions (meeting_id FK)
        3. entity_mentions (meeting_id FK)
        4. commitments (meeting_id FK)
        5. decisions (meeting_id FK)
        6. follow_up_meetings (source_meeting_id FK)
        7. open_questions (meeting_id FK)
        8. pending_approvals (by approval_id = meeting_id)
        9. tasks (meeting_id, ON DELETE SET NULL would orphan)
        10. meeting record itself

        Args:
            meeting_id: UUID of the meeting to delete.

        Returns:
            Dict with counts of deleted records by type.
        """
        counts = {"embeddings": 0, "tasks": 0, "meetings": 0}

        try:
            # 1. Delete embeddings (source_id references meeting, no cascade)
            emb_result = (
                self.client.table("embeddings")
                .delete()
                .eq("source_id", meeting_id)
                .execute()
            )
            counts["embeddings"] = len(emb_result.data) if emb_result.data else 0

            # 2-7. Delete all child tables that reference meetings
            for table, fk_col in [
                ("task_mentions", "meeting_id"),
                ("entity_mentions", "meeting_id"),
                ("commitments", "meeting_id"),
                ("decisions", "meeting_id"),
                ("follow_up_meetings", "source_meeting_id"),
                ("open_questions", "meeting_id"),
            ]:
                try:
                    self.client.table(table).delete().eq(fk_col, meeting_id).execute()
                except Exception as e:
                    logger.debug(f"Skipping {table} cleanup: {e}")

            # 8. Delete pending approvals (approval_id = meeting_id string)
            try:
                self.client.table("pending_approvals").delete().eq(
                    "approval_id", meeting_id
                ).execute()
            except Exception as e:
                logger.debug(f"Skipping pending_approvals cleanup: {e}")

            # 9. Delete tasks (ON DELETE SET NULL would orphan them)
            task_result = (
                self.client.table("tasks")
                .delete()
                .eq("meeting_id", meeting_id)
                .execute()
            )
            counts["tasks"] = len(task_result.data) if task_result.data else 0

            # 10. Delete the meeting itself (should now be clean)
            mtg_result = (
                self.client.table("meetings")
                .delete()
                .eq("id", meeting_id)
                .execute()
            )
            counts["meetings"] = len(mtg_result.data) if mtg_result.data else 0

            logger.info(
                f"Cascade-deleted meeting {meeting_id}: "
                f"{counts['embeddings']} embeddings, "
                f"{counts['tasks']} tasks, "
                f"{counts['meetings']} meetings"
            )

        except Exception as e:
            logger.error(f"Error during cascade delete of {meeting_id}: {e}")

        return counts

    def find_meeting_by_source(self, source_file_path: str) -> dict | None:
        """
        Find a meeting by its source file path (case-insensitive partial match).

        Args:
            source_file_path: Filename or path fragment to search for.

        Returns:
            Meeting record or None.
        """
        result = (
            self.client.table("meetings")
            .select("*")
            .ilike("source_file_path", f"%{source_file_path}%")
            .limit(1)
            .execute()
        )
        return result.data[0] if result.data else None

    def search_meetings_by_title(self, title_query: str, limit: int = 10) -> list[dict]:
        """
        Search meetings by title (case-insensitive partial match).

        Args:
            title_query: Partial title to search for.
            limit: Maximum results.

        Returns:
            List of matching meeting records.
        """
        result = (
            self.client.table("meetings")
            .select("id, title, date, source_file_path")
            .ilike("title", f"%{title_query}%")
            .order("date", desc=True)
            .limit(limit)
            .execute()
        )
        return result.data

    def list_meetings(
        self,
        date_from: datetime | None = None,
        date_to: datetime | None = None,
        approval_status: str | None = None,
        limit: int = 50,
    ) -> list[dict]:
        """
        List meetings with optional filtering.

        Args:
            date_from: Start date filter.
            date_to: End date filter.
            approval_status: Filter by approval status.
            limit: Maximum number of results.

        Returns:
            List of meeting records.
        """
        query = self.client.table("meetings").select("*")

        if date_from:
            query = query.gte("date", self._serialize_datetime(date_from))
        if date_to:
            query = query.lte("date", self._serialize_datetime(date_to))
        if approval_status:
            query = query.eq("approval_status", approval_status)

        result = query.order("date", desc=True).limit(limit).execute()
        return result.data

    # =========================================================================
    # Decisions
    # =========================================================================

    def create_decision(
        self,
        meeting_id: str,
        description: str,
        context: str | None = None,
        participants_involved: list[str] | None = None,
        transcript_timestamp: str | None = None,
    ) -> dict:
        """
        Create a new decision record linked to a meeting.

        Args:
            meeting_id: UUID of the source meeting.
            description: The decision that was made.
            context: Surrounding discussion context.
            participants_involved: Who was involved in the decision.
            transcript_timestamp: Source citation (e.g., "43:28").

        Returns:
            Created decision record.
        """
        data = {
            "meeting_id": meeting_id,
            "description": description,
            "context": context,
            "participants_involved": participants_involved,
            "transcript_timestamp": transcript_timestamp,
        }

        result = self.client.table("decisions").insert(data).execute()
        logger.debug(f"Created decision: {description[:50]}...")
        return result.data[0]

    def create_decisions_batch(
        self,
        meeting_id: str,
        decisions: list[dict],
    ) -> list[dict]:
        """
        Create multiple decisions in a single batch.

        Args:
            meeting_id: UUID of the source meeting.
            decisions: List of decision dicts with description, context, etc.

        Returns:
            List of created decision records.
        """
        data = [
            {
                "meeting_id": meeting_id,
                "description": d.get("description"),
                "context": d.get("context"),
                "participants_involved": d.get("participants_involved"),
                "transcript_timestamp": d.get("transcript_timestamp"),
            }
            for d in decisions
        ]

        result = self.client.table("decisions").insert(data).execute()
        logger.info(f"Created {len(result.data)} decisions for meeting {meeting_id}")
        return result.data

    def list_decisions(
        self,
        meeting_id: str | None = None,
        topic: str | None = None,
        limit: int = 100,
    ) -> list[dict]:
        """
        List decisions with optional filtering.

        Args:
            meeting_id: Filter by source meeting.
            topic: Filter by topic keyword (searches description).
            limit: Maximum number of results.

        Returns:
            List of decision records.
        """
        query = self.client.table("decisions").select("*, meetings(title, date)")

        if meeting_id:
            query = query.eq("meeting_id", meeting_id)
        if topic:
            query = query.ilike("description", f"%{topic}%")

        result = query.order("created_at", desc=True).limit(limit).execute()
        return result.data

    # =========================================================================
    # Tasks
    # =========================================================================

    def create_task(
        self,
        title: str,
        assignee: str,
        priority: str = "M",
        deadline: date | None = None,
        meeting_id: str | None = None,
        transcript_timestamp: str | None = None,
        status: str = "pending",
        category: str | None = None,
    ) -> dict:
        """
        Create a new task.

        Args:
            title: Task description.
            assignee: Who is responsible.
            priority: 'H' (high), 'M' (medium), 'L' (low).
            deadline: Due date.
            meeting_id: Source meeting UUID (optional).
            transcript_timestamp: Source citation (optional).
            status: Initial status (default: 'pending').
            category: Task category (e.g., 'Product & Tech', 'BD & Sales').

        Returns:
            Created task record.
        """
        data = {
            "title": title,
            "assignee": assignee,
            "priority": priority,
            "deadline": self._serialize_datetime(deadline),
            "meeting_id": meeting_id,
            "transcript_timestamp": transcript_timestamp,
            "status": status,
            "category": category,
        }

        result = self.client.table("tasks").insert(data).execute()
        logger.info(f"Created task: {title} (assigned to {assignee})")

        self.log_action(
            action="task_created",
            details={
                "task_id": result.data[0]["id"],
                "title": title,
                "assignee": assignee,
            },
            triggered_by="auto",
        )

        return result.data[0]

    def create_tasks_batch(
        self,
        meeting_id: str,
        tasks: list[dict],
    ) -> list[dict]:
        """
        Create multiple tasks in a single batch.

        Args:
            meeting_id: UUID of the source meeting.
            tasks: List of task dicts with title, assignee, etc.

        Returns:
            List of created task records.
        """
        # Filter out tasks missing required fields (assignee or title)
        valid_tasks = []
        for t in tasks:
            if not t.get("assignee"):
                logger.warning(
                    f"Skipping task with no assignee: {t.get('title', '?')}"
                )
                continue
            if not t.get("title"):
                logger.warning(f"Skipping task with no title")
                continue
            valid_tasks.append(t)

        if not valid_tasks:
            logger.info("No valid tasks to insert after filtering")
            return []

        data = [
            {
                "meeting_id": meeting_id,
                "title": t.get("title"),
                "assignee": t.get("assignee"),
                "priority": t.get("priority", "M"),
                "deadline": self._serialize_datetime(t.get("deadline")),
                "transcript_timestamp": t.get("transcript_timestamp"),
                "status": "pending",
                "category": t.get("category"),
            }
            for t in valid_tasks
        ]

        result = self.client.table("tasks").insert(data).execute()
        logger.info(f"Created {len(result.data)} tasks for meeting {meeting_id}")
        return result.data

    def get_tasks(
        self,
        assignee: str | None = None,
        status: str | None = None,
        category: str | None = None,
        include_overdue: bool = True,
        limit: int = 100,
    ) -> list[dict]:
        """
        Get tasks with optional filtering.

        Args:
            assignee: Filter by assignee name.
            status: Filter by status ('pending', 'in_progress', 'done', 'overdue').
            category: Filter by task category (e.g., 'Product & Tech').
            include_overdue: Include overdue tasks when filtering by status.
            limit: Maximum number of results.

        Returns:
            List of task records.
        """
        query = self.client.table("tasks").select("*, meetings(title, date)")

        if assignee:
            query = query.ilike("assignee", assignee)

        if status:
            if include_overdue and status in ("pending", "in_progress"):
                query = query.in_("status", [status, "overdue"])
            else:
                query = query.eq("status", status)

        if category:
            query = query.eq("category", category)

        result = query.order("deadline", desc=False).limit(limit).execute()
        return result.data

    def update_task(
        self,
        task_id: str,
        status: str | None = None,
        deadline: date | None = None,
        **other_updates,
    ) -> dict:
        """
        Update a task's status or deadline.

        Args:
            task_id: UUID of the task to update.
            status: New status (optional).
            deadline: New deadline (optional).
            **other_updates: Any other fields to update.

        Returns:
            Updated task record.
        """
        updates = {**other_updates}
        if status is not None:
            updates["status"] = status
        if deadline is not None:
            updates["deadline"] = self._serialize_datetime(deadline)

        result = (
            self.client.table("tasks")
            .update(updates)
            .eq("id", task_id)
            .execute()
        )
        logger.info(f"Updated task {task_id}: {list(updates.keys())}")
        return result.data[0]

    # =========================================================================
    # Follow-up Meetings
    # =========================================================================

    def create_follow_up_meeting(
        self,
        source_meeting_id: str,
        title: str,
        led_by: str,
        proposed_date: datetime | None = None,
        participants: list[str] | None = None,
        agenda_items: list[str] | None = None,
        prep_needed: str | None = None,
    ) -> dict:
        """
        Create a follow-up meeting record.

        Args:
            source_meeting_id: UUID of the meeting where this was identified.
            title: Title of the follow-up meeting.
            led_by: Who will lead the meeting.
            proposed_date: When it should happen.
            participants: Who should attend.
            agenda_items: What should be discussed.
            prep_needed: What needs to happen before.

        Returns:
            Created follow-up meeting record.
        """
        data = {
            "source_meeting_id": source_meeting_id,
            "title": title,
            "led_by": led_by,
            "proposed_date": self._serialize_datetime(proposed_date),
            "participants": participants,
            "agenda_items": agenda_items,
            "prep_needed": prep_needed,
        }

        result = self.client.table("follow_up_meetings").insert(data).execute()
        logger.info(f"Created follow-up meeting: {title}")
        return result.data[0]

    def create_follow_ups_batch(
        self,
        source_meeting_id: str,
        follow_ups: list[dict],
    ) -> list[dict]:
        """
        Create multiple follow-up meetings in a single batch.

        Args:
            source_meeting_id: UUID of the source meeting.
            follow_ups: List of follow-up meeting dicts.

        Returns:
            List of created follow-up meeting records.
        """
        data = [
            {
                "source_meeting_id": source_meeting_id,
                "title": f.get("title"),
                "led_by": f.get("led_by"),
                "proposed_date": self._serialize_datetime(f.get("proposed_date")),
                "participants": f.get("participants"),
                "agenda_items": f.get("agenda_items"),
                "prep_needed": f.get("prep_needed"),
            }
            for f in follow_ups
        ]

        result = self.client.table("follow_up_meetings").insert(data).execute()
        logger.info(f"Created {len(result.data)} follow-up meetings")
        return result.data

    def list_follow_up_meetings(
        self,
        source_meeting_id: str | None = None,
        limit: int = 50,
    ) -> list[dict]:
        """
        List follow-up meetings.

        Args:
            source_meeting_id: Filter by source meeting.
            limit: Maximum number of results.

        Returns:
            List of follow-up meeting records.
        """
        query = self.client.table("follow_up_meetings").select(
            "*, meetings(title, date)"
        )

        if source_meeting_id:
            query = query.eq("source_meeting_id", source_meeting_id)

        result = query.order("proposed_date", desc=False).limit(limit).execute()
        return result.data

    # =========================================================================
    # Open Questions
    # =========================================================================

    def create_open_question(
        self,
        meeting_id: str,
        question: str,
        raised_by: str | None = None,
    ) -> dict:
        """
        Create an open question record.

        Args:
            meeting_id: UUID of the source meeting.
            question: The question text.
            raised_by: Who raised the question.

        Returns:
            Created open question record.
        """
        data = {
            "meeting_id": meeting_id,
            "question": question,
            "raised_by": raised_by,
            "status": "open",
        }

        result = self.client.table("open_questions").insert(data).execute()
        logger.debug(f"Created open question: {question[:50]}...")
        return result.data[0]

    def create_open_questions_batch(
        self,
        meeting_id: str,
        questions: list[dict],
    ) -> list[dict]:
        """
        Create multiple open questions in a single batch.

        Args:
            meeting_id: UUID of the source meeting.
            questions: List of question dicts with question, raised_by.

        Returns:
            List of created open question records.
        """
        data = [
            {
                "meeting_id": meeting_id,
                "question": q.get("question"),
                "raised_by": q.get("raised_by"),
                "status": "open",
            }
            for q in questions
        ]

        result = self.client.table("open_questions").insert(data).execute()
        logger.info(f"Created {len(result.data)} open questions")
        return result.data

    def get_open_questions(
        self,
        status: str = "open",
        meeting_id: str | None = None,
        limit: int = 100,
    ) -> list[dict]:
        """
        Get open questions by status.

        Args:
            status: Filter by status ('open' or 'resolved').
            meeting_id: Filter by source meeting.
            limit: Maximum number of results.

        Returns:
            List of open question records.
        """
        # Disambiguate the meetings join — open_questions has two FKs to meetings
        query = self.client.table("open_questions").select(
            "*, meetings!open_questions_meeting_id_fkey(title, date)"
        )
        query = query.eq("status", status)

        if meeting_id:
            query = query.eq("meeting_id", meeting_id)

        result = query.order("created_at", desc=True).limit(limit).execute()
        return result.data

    def resolve_question(
        self,
        question_id: str,
        resolved_in_meeting_id: str | None = None,
    ) -> dict:
        """
        Mark a question as resolved.

        Args:
            question_id: UUID of the question to resolve.
            resolved_in_meeting_id: UUID of meeting where it was resolved.

        Returns:
            Updated question record.
        """
        updates = {
            "status": "resolved",
            "resolved_in_meeting_id": resolved_in_meeting_id,
        }

        result = (
            self.client.table("open_questions")
            .update(updates)
            .eq("id", question_id)
            .execute()
        )
        logger.info(f"Resolved question {question_id}")
        return result.data[0]

    # =========================================================================
    # Documents
    # =========================================================================

    def create_document(
        self,
        title: str,
        source: str,
        file_type: str | None = None,
        summary: str | None = None,
        drive_path: str | None = None,
        document_type: str | None = None,
    ) -> dict:
        """
        Create a document record.

        Args:
            title: Document title.
            source: 'upload', 'email', or 'drive'.
            file_type: File extension (pdf, docx, etc.).
            summary: Document summary.
            drive_path: Google Drive path.
            document_type: Classification (strategy, legal, technical, pitch, client, other).

        Returns:
            Created document record.
        """
        data = {
            "title": title,
            "source": source,
            "file_type": file_type,
            "summary": summary,
            "drive_path": drive_path,
            "document_type": document_type,
        }

        result = self.client.table("documents").insert(data).execute()
        logger.info(f"Created document: {title}")

        self.log_action(
            action="document_ingested",
            details={"document_id": result.data[0]["id"], "title": title},
            triggered_by="auto",
        )

        return result.data[0]

    def get_document(self, document_id: str) -> dict | None:
        """
        Retrieve a document by its UUID.

        Args:
            document_id: UUID of the document.

        Returns:
            Document record or None if not found.
        """
        result = (
            self.client.table("documents")
            .select("*")
            .eq("id", document_id)
            .execute()
        )
        return result.data[0] if result.data else None

    def list_documents(
        self,
        source: str | None = None,
        document_type: str | None = None,
        limit: int = 50,
    ) -> list[dict]:
        """
        List documents with optional filtering.

        Args:
            source: Filter by source ('upload', 'email', 'drive').
            document_type: Filter by type ('strategy', 'legal', 'technical', etc.).
            limit: Maximum number of results.

        Returns:
            List of document records.
        """
        query = self.client.table("documents").select("*")

        if source:
            query = query.eq("source", source)
        if document_type:
            query = query.eq("document_type", document_type)

        result = query.order("ingested_at", desc=True).limit(limit).execute()
        return result.data

    def search_documents_by_title(self, search_term: str, limit: int = 5) -> list[dict]:
        """
        Search documents by title (case-insensitive partial match).

        Args:
            search_term: Text to search for in document titles.
            limit: Maximum number of results.

        Returns:
            List of matching document records.
        """
        result = (
            self.client.table("documents")
            .select("*")
            .ilike("title", f"%{search_term}%")
            .order("ingested_at", desc=True)
            .limit(limit)
            .execute()
        )
        return result.data

    # =========================================================================
    # Embeddings (Vector Search)
    # =========================================================================

    def store_embedding(
        self,
        source_type: str,
        source_id: str,
        chunk_text: str,
        chunk_index: int,
        embedding: list[float],
        speaker: str | None = None,
        timestamp_range: str | None = None,
        metadata: dict | None = None,
    ) -> dict:
        """
        Store a text chunk with its embedding vector.

        Args:
            source_type: 'meeting' or 'document'.
            source_id: UUID of the source meeting or document.
            chunk_text: The text content of this chunk.
            chunk_index: Position in the source (0-indexed).
            embedding: The vector embedding (1536 dimensions).
            speaker: Who said this (for meeting chunks).
            timestamp_range: e.g., "43:00-45:30".
            metadata: Additional context as JSON.

        Returns:
            Created embedding record.
        """
        data = {
            "source_type": source_type,
            "source_id": source_id,
            "chunk_text": chunk_text,
            "chunk_index": chunk_index,
            "embedding": embedding,
            "speaker": speaker,
            "timestamp_range": timestamp_range,
            "metadata": metadata,
        }

        result = self.client.table("embeddings").insert(data).execute()
        return result.data[0]

    def store_embeddings_batch(
        self,
        embeddings: list[dict],
    ) -> list[dict]:
        """
        Store multiple embeddings in a single batch.

        Args:
            embeddings: List of embedding dicts with all required fields.

        Returns:
            List of created embedding records.
        """
        result = self.client.table("embeddings").insert(embeddings).execute()
        logger.info(f"Stored {len(result.data)} embeddings")
        return result.data

    def search_embeddings(
        self,
        query_embedding: list[float],
        limit: int = 10,
        source_type: str | None = None,
        similarity_threshold: float | None = None,
    ) -> list[dict]:
        """
        Search for similar embeddings using cosine similarity.

        Uses Supabase's RPC function for vector similarity search.

        Args:
            query_embedding: The embedding vector of the search query.
            limit: Maximum number of results to return.
            source_type: Optional filter by source type ('meeting', 'document').
            similarity_threshold: Minimum similarity score (0-1).

        Returns:
            List of matching chunks with similarity scores.
        """
        if similarity_threshold is None:
            from config.settings import settings
            similarity_threshold = settings.SIMILARITY_THRESHOLD

        # Call the similarity search RPC function
        # Note: This requires a custom function in Supabase
        result = self.client.rpc(
            "match_embeddings",
            {
                "query_embedding": query_embedding,
                "match_threshold": similarity_threshold,
                "match_count": limit,
                "filter_source_type": source_type,
            },
        ).execute()

        return result.data

    def search_fulltext(
        self,
        query_text: str,
        limit: int = 20,
        source_type: str | None = None,
    ) -> list[dict]:
        """
        Full-text search on embeddings using PostgreSQL tsvector.

        Uses the search_embeddings_fulltext RPC function which searches
        the chunk_text_tsv generated column with plainto_tsquery.

        Args:
            query_text: The search query (natural language).
            limit: Maximum number of results.
            source_type: Optional filter by source type ('meeting', 'document').

        Returns:
            List of matching chunks ranked by relevance.
        """
        result = self.client.rpc(
            "search_embeddings_fulltext",
            {
                "search_query": query_text,
                "match_count": limit,
                "filter_source_type": source_type,
            },
        ).execute()
        return result.data

    def delete_embeddings_for_source(
        self,
        source_type: str,
        source_id: str,
    ) -> int:
        """
        Delete all embeddings for a specific source.

        Useful when reprocessing a meeting or document.

        Args:
            source_type: 'meeting' or 'document'.
            source_id: UUID of the source.

        Returns:
            Number of embeddings deleted.
        """
        result = (
            self.client.table("embeddings")
            .delete()
            .eq("source_type", source_type)
            .eq("source_id", source_id)
            .execute()
        )
        count = len(result.data)
        logger.info(f"Deleted {count} embeddings for {source_type}/{source_id}")
        return count

    # =========================================================================
    # Task Mentions (v0.3 — Cross-Reference Intelligence)
    # =========================================================================

    def create_task_mention(
        self,
        task_id: str,
        meeting_id: str,
        mention_text: str,
        implied_status: str | None = None,
        confidence: str = "medium",
        evidence: str | None = None,
        transcript_timestamp: str | None = None,
    ) -> dict:
        """
        Record a task being mentioned in a meeting.

        Used for cross-meeting tracking: when a task from Meeting A
        is discussed in Meeting B, this creates the link.

        Args:
            task_id: UUID of the task being mentioned.
            meeting_id: UUID of the meeting where it was mentioned.
            mention_text: How the task was referenced in the meeting.
            implied_status: Status implied by the mention ('done', 'in_progress', or None).
            confidence: Confidence level ('high', 'medium', 'low').
            evidence: Exact quote from transcript supporting the inference.
            transcript_timestamp: When in the meeting it was mentioned.

        Returns:
            Created task_mention record.
        """
        data = {
            "task_id": task_id,
            "meeting_id": meeting_id,
            "mention_text": mention_text,
            "implied_status": implied_status,
            "confidence": confidence,
            "evidence": evidence,
            "transcript_timestamp": transcript_timestamp,
        }

        result = self.client.table("task_mentions").insert(data).execute()
        logger.info(f"Created task mention: task={task_id} in meeting={meeting_id}")
        return result.data[0]

    def create_task_mentions_batch(self, mentions: list[dict]) -> list[dict]:
        """
        Batch insert multiple task mentions.

        Inserts one at a time to skip any with invalid FK references
        (e.g., deleted tasks) without failing the whole batch.

        Args:
            mentions: List of mention dicts (task_id, meeting_id, mention_text, etc.).

        Returns:
            List of successfully created task_mention records.
        """
        if not mentions:
            return []

        created = []
        for mention in mentions:
            try:
                result = self.client.table("task_mentions").insert(mention).execute()
                if result.data:
                    created.append(result.data[0])
            except Exception as e:
                logger.warning(
                    f"Skipping task mention (task_id={mention.get('task_id')}): {e}"
                )
        if created:
            logger.info(f"Created {len(created)} task mentions")
        return created

    def get_task_mentions(
        self,
        task_id: str | None = None,
        meeting_id: str | None = None,
        limit: int = 50,
    ) -> list[dict]:
        """
        Query task mentions with optional filters.

        Args:
            task_id: Filter by task UUID.
            meeting_id: Filter by meeting UUID.
            limit: Maximum number of results.

        Returns:
            List of task mention records with task title/assignee joined.
        """
        query = self.client.table("task_mentions").select(
            "*, tasks(title, assignee, status, category)"
        )

        if task_id:
            query = query.eq("task_id", task_id)
        if meeting_id:
            query = query.eq("meeting_id", meeting_id)

        result = query.order("created_at", desc=True).limit(limit).execute()
        return result.data

    # =========================================================================
    # Entity Registry (v0.3 Tier 2)
    # =========================================================================

    def create_entity(
        self,
        canonical_name: str,
        entity_type: str,
        aliases: list[str] | None = None,
        metadata: dict | None = None,
        first_seen_meeting_id: str | None = None,
    ) -> dict:
        """
        Create a new entity record.

        Args:
            canonical_name: Primary name for the entity.
            entity_type: One of: person, organization, project, technology, location.
            aliases: Alternative names / spellings.
            metadata: Extra info (role, website, etc.) as JSON.
            first_seen_meeting_id: UUID of the meeting where first mentioned.

        Returns:
            Created entity record.
        """
        data = {
            "canonical_name": canonical_name,
            "entity_type": entity_type,
            "aliases": aliases or [],
            "metadata": metadata or {},
        }
        if first_seen_meeting_id:
            data["first_seen_meeting_id"] = first_seen_meeting_id

        result = self.client.table("entities").insert(data).execute()
        logger.info(f"Created entity: {canonical_name} ({entity_type})")
        return result.data[0]

    def get_entity(self, entity_id: str) -> dict | None:
        """Get an entity by UUID."""
        result = (
            self.client.table("entities")
            .select("*")
            .eq("id", entity_id)
            .execute()
        )
        return result.data[0] if result.data else None

    def find_entity_by_name(self, name: str) -> dict | None:
        """
        Find an entity by name (case-insensitive).

        Searches canonical_name first, then aliases array.

        Args:
            name: Name to search for.

        Returns:
            Matching entity record or None.
        """
        # 1. Try exact match on canonical_name (case-insensitive)
        result = (
            self.client.table("entities")
            .select("*")
            .ilike("canonical_name", name)
            .execute()
        )
        if result.data:
            return result.data[0]

        # 2. Search aliases array using Postgres 'cs' (contains) operator
        # Supabase supports .contains() for array columns
        result = (
            self.client.table("entities")
            .select("*")
            .contains("aliases", [name])
            .execute()
        )
        if result.data:
            return result.data[0]

        return None

    def list_entities(
        self,
        entity_type: str | None = None,
        limit: int = 100,
    ) -> list[dict]:
        """
        List entities with optional type filter.

        Args:
            entity_type: Filter by type (person, organization, etc.).
            limit: Maximum results.

        Returns:
            List of entity records.
        """
        query = self.client.table("entities").select("*")
        if entity_type:
            query = query.eq("entity_type", entity_type)
        result = query.order("created_at", desc=True).limit(limit).execute()
        return result.data

    def create_entity_mention(
        self,
        entity_id: str,
        meeting_id: str,
        mention_text: str,
        context: str | None = None,
        speaker: str | None = None,
        sentiment: str | None = None,
        transcript_timestamp: str | None = None,
    ) -> dict:
        """Create a single entity mention record."""
        data = {
            "entity_id": entity_id,
            "meeting_id": meeting_id,
            "mention_text": mention_text,
        }
        if context:
            data["context"] = context
        if speaker:
            data["speaker"] = speaker
        if sentiment:
            data["sentiment"] = sentiment
        if transcript_timestamp:
            data["transcript_timestamp"] = transcript_timestamp

        result = self.client.table("entity_mentions").insert(data).execute()
        return result.data[0]

    def create_entity_mentions_batch(self, mentions: list[dict]) -> list[dict]:
        """
        Batch-insert entity mentions, skipping FK errors.

        Same pattern as create_task_mentions_batch: inserts one-at-a-time
        so a single bad FK doesn't fail the whole batch.

        Args:
            mentions: List of mention dicts.

        Returns:
            List of successfully created records.
        """
        if not mentions:
            return []

        created = []
        for mention in mentions:
            try:
                result = (
                    self.client.table("entity_mentions")
                    .insert(mention)
                    .execute()
                )
                if result.data:
                    created.append(result.data[0])
            except Exception as e:
                logger.warning(
                    f"Skipping entity mention (entity_id={mention.get('entity_id')}): {e}"
                )
        if created:
            logger.info(f"Created {len(created)} entity mentions")
        return created

    def get_entity_mentions(
        self,
        entity_id: str | None = None,
        meeting_id: str | None = None,
        limit: int = 50,
    ) -> list[dict]:
        """
        Query entity mentions with optional filters.

        Joins to entities and meetings tables for context.

        Args:
            entity_id: Filter by entity UUID.
            meeting_id: Filter by meeting UUID.
            limit: Maximum results.

        Returns:
            List of entity mention records.
        """
        query = self.client.table("entity_mentions").select(
            "*, entities(canonical_name, entity_type), meetings(title, date)"
        )
        if entity_id:
            query = query.eq("entity_id", entity_id)
        if meeting_id:
            query = query.eq("meeting_id", meeting_id)

        result = query.order("created_at", desc=True).limit(limit).execute()
        return result.data

    def get_entity_timeline(
        self,
        entity_id: str,
        limit: int = 50,
    ) -> list[dict]:
        """
        Get chronological timeline of entity mentions across meetings.

        Args:
            entity_id: UUID of the entity.
            limit: Maximum results.

        Returns:
            List of mentions ordered by meeting date (oldest first).
        """
        result = (
            self.client.table("entity_mentions")
            .select("*, meetings(title, date, participants)")
            .eq("entity_id", entity_id)
            .order("created_at")
            .limit(limit)
            .execute()
        )
        return result.data

    # =========================================================================
    # Commitment Tracking (v0.3 Tier 2)
    # =========================================================================

    def create_commitment(
        self,
        meeting_id: str,
        speaker: str,
        commitment_text: str,
        context: str | None = None,
        implied_deadline: str | None = None,
        linked_task_id: str | None = None,
    ) -> dict:
        """
        Create a new commitment record.

        Args:
            meeting_id: UUID of the meeting where commitment was made.
            speaker: Who made the commitment.
            commitment_text: What they committed to.
            context: Surrounding discussion context.
            implied_deadline: Deadline mentioned (e.g. "next week", "by Friday").
            linked_task_id: UUID of linked task if one exists.

        Returns:
            Created commitment record.
        """
        data = {
            "meeting_id": meeting_id,
            "speaker": speaker,
            "commitment_text": commitment_text,
            "status": "open",
        }
        if context:
            data["context"] = context
        if implied_deadline:
            data["implied_deadline"] = implied_deadline
        if linked_task_id:
            data["linked_task_id"] = linked_task_id

        result = self.client.table("commitments").insert(data).execute()
        logger.info(f"Created commitment: {speaker} — {commitment_text[:50]}")
        return result.data[0]

    def create_commitments_batch(
        self,
        meeting_id: str,
        commitments: list[dict],
    ) -> list[dict]:
        """
        Batch-insert commitments for a meeting.

        Args:
            meeting_id: UUID of the source meeting.
            commitments: List of commitment dicts.

        Returns:
            List of successfully created records.
        """
        if not commitments:
            return []

        created = []
        for c in commitments:
            try:
                data = {
                    "meeting_id": meeting_id,
                    "speaker": c.get("speaker", "Unknown"),
                    "commitment_text": c.get("commitment_text", ""),
                    "context": c.get("context"),
                    "implied_deadline": c.get("implied_deadline"),
                    "linked_task_id": c.get("linked_task_id"),
                    "status": "open",
                }
                result = self.client.table("commitments").insert(data).execute()
                if result.data:
                    created.append(result.data[0])
            except Exception as e:
                logger.warning(f"Skipping commitment: {e}")
        if created:
            logger.info(f"Created {len(created)} commitments for meeting {meeting_id}")
        return created

    def get_commitments(
        self,
        speaker: str | None = None,
        status: str | None = None,
        meeting_id: str | None = None,
        limit: int = 50,
    ) -> list[dict]:
        """
        Query commitments with optional filters.

        Args:
            speaker: Filter by speaker name.
            status: Filter by status (open, fulfilled, overdue, withdrawn).
            meeting_id: Filter by source meeting.
            limit: Maximum results.

        Returns:
            List of commitment records with meeting title joined.
        """
        query = self.client.table("commitments").select(
            "*, meetings!commitments_meeting_id_fkey(title, date)"
        )
        if speaker:
            query = query.ilike("speaker", f"%{speaker}%")
        if status:
            query = query.eq("status", status)
        if meeting_id:
            query = query.eq("meeting_id", meeting_id)

        result = query.order("created_at", desc=True).limit(limit).execute()
        return result.data

    def fulfill_commitment(
        self,
        commitment_id: str,
        fulfilled_in_meeting_id: str | None = None,
        evidence: str | None = None,
    ) -> dict:
        """
        Mark a commitment as fulfilled.

        Args:
            commitment_id: UUID of the commitment.
            fulfilled_in_meeting_id: Meeting where fulfillment was detected.
            evidence: Quote or evidence of fulfillment.

        Returns:
            Updated commitment record.
        """
        data = {"status": "fulfilled"}
        if fulfilled_in_meeting_id:
            data["fulfilled_in_meeting_id"] = fulfilled_in_meeting_id
        if evidence:
            data["evidence"] = evidence

        result = (
            self.client.table("commitments")
            .update(data)
            .eq("id", commitment_id)
            .execute()
        )
        logger.info(f"Fulfilled commitment: {commitment_id}")
        return result.data[0] if result.data else {}

    # =========================================================================
    # Pending Approvals (v0.4 — Persistent Approval State)
    # =========================================================================

    def create_pending_approval(
        self,
        approval_id: str,
        content_type: str,
        content: dict,
        auto_publish_at: str | None = None,
    ) -> dict:
        """
        Create a pending approval record.

        Args:
            approval_id: Meeting UUID or prefixed ID (e.g. "prep-2026-03-01").
            content_type: 'meeting_summary', 'meeting_prep', or 'weekly_digest'.
            content: Full content dict (stored as JSONB).
            auto_publish_at: ISO timestamp for auto-publish, or None for manual mode.

        Returns:
            Created pending approval record.
        """
        data = {
            "approval_id": approval_id,
            "content_type": content_type,
            "content": content,
            "status": "pending",
        }
        if auto_publish_at:
            data["auto_publish_at"] = auto_publish_at

        result = self.client.table("pending_approvals").insert(data).execute()
        logger.info(f"Created pending approval: {approval_id} ({content_type})")
        return result.data[0]

    def get_pending_approval(self, approval_id: str) -> dict | None:
        """
        Get a pending approval by its approval_id.

        Args:
            approval_id: The approval ID to look up.

        Returns:
            Approval record dict or None if not found.
        """
        result = (
            self.client.table("pending_approvals")
            .select("*")
            .eq("approval_id", approval_id)
            .execute()
        )
        return result.data[0] if result.data else None

    def update_pending_approval(
        self,
        approval_id: str,
        status: str | None = None,
        content: dict | None = None,
    ) -> dict:
        """
        Update a pending approval record.

        Args:
            approval_id: The approval ID to update.
            status: New status (pending/approved/rejected/editing).
            content: Updated content dict.

        Returns:
            Updated record.
        """
        data = {}
        if status is not None:
            data["status"] = status
        if content is not None:
            data["content"] = content

        result = (
            self.client.table("pending_approvals")
            .update(data)
            .eq("approval_id", approval_id)
            .execute()
        )
        return result.data[0] if result.data else {}

    def delete_pending_approval(self, approval_id: str) -> bool:
        """
        Delete a pending approval record.

        Args:
            approval_id: The approval ID to delete.

        Returns:
            True if a record was deleted, False if not found.
        """
        result = (
            self.client.table("pending_approvals")
            .delete()
            .eq("approval_id", approval_id)
            .execute()
        )
        deleted = len(result.data) > 0 if result.data else False
        if deleted:
            logger.info(f"Deleted pending approval: {approval_id}")
        return deleted

    def get_pending_auto_publishes(self) -> list[dict]:
        """
        Get all pending approvals that have an auto_publish_at timestamp.

        Used on startup to reconstruct auto-publish timers.

        Returns:
            List of pending approval records with auto_publish_at set.
        """
        result = (
            self.client.table("pending_approvals")
            .select("*")
            .eq("status", "pending")
            .not_.is_("auto_publish_at", "null")
            .execute()
        )
        return result.data

    # =========================================================================
    # Calendar Classifications (v0.4.1 — Meeting Classification Memory)
    # =========================================================================

    def remember_classification(
        self,
        title: str,
        is_cropsight: bool,
        classified_by: str = "eyal",
    ) -> dict:
        """
        Store a meeting classification for future reference.

        Args:
            title: The meeting title as classified.
            is_cropsight: Whether it was classified as CropSight.
            classified_by: Who made the classification (default 'eyal').

        Returns:
            Created record dict.
        """
        data = {
            "title": title,
            "is_cropsight": is_cropsight,
            "classified_by": classified_by,
        }
        result = self.client.table("calendar_classifications").insert(data).execute()
        logger.info(
            f"Remembered classification: '{title}' → "
            f"{'CropSight' if is_cropsight else 'Personal'}"
        )
        return result.data[0] if result.data else {}

    def get_classification_by_title(self, title: str) -> dict | None:
        """
        Look up a classification by exact title match (case-insensitive).

        Args:
            title: The meeting title to look up.

        Returns:
            Classification record or None if not found.
        """
        result = (
            self.client.table("calendar_classifications")
            .select("*")
            .eq("title_lower", title.lower())
            .limit(1)
            .execute()
        )
        return result.data[0] if result.data else None

    def get_all_classifications(self, limit: int = 100) -> list[dict]:
        """
        Get all stored classifications (for fuzzy matching).

        Args:
            limit: Maximum records to return.

        Returns:
            List of classification records.
        """
        result = (
            self.client.table("calendar_classifications")
            .select("*")
            .order("created_at", desc=True)
            .limit(limit)
            .execute()
        )
        return result.data

    # =========================================================================
    # Audit Log
    # =========================================================================

    def log_action(
        self,
        action: str,
        details: dict | None = None,
        triggered_by: str = "auto",
    ) -> dict:
        """
        Log an action to the audit trail.

        Args:
            action: Action type (e.g., 'meeting_processed', 'task_created').
            details: Additional context as JSON.
            triggered_by: Who/what triggered ('auto', 'eyal', 'roye', etc.).

        Returns:
            Created audit log entry.
        """
        data = {
            "action": action,
            "details": details,
            "triggered_by": triggered_by,
        }

        result = self.client.table("audit_log").insert(data).execute()
        return result.data[0]

    def get_audit_log(
        self,
        action: str | None = None,
        triggered_by: str | None = None,
        limit: int = 100,
    ) -> list[dict]:
        """
        Retrieve audit log entries.

        Args:
            action: Filter by action type.
            triggered_by: Filter by who triggered the action.
            limit: Maximum number of entries.

        Returns:
            List of audit log entries.
        """
        query = self.client.table("audit_log").select("*")

        if action:
            query = query.eq("action", action)
        if triggered_by:
            query = query.eq("triggered_by", triggered_by)

        result = query.order("created_at", desc=True).limit(limit).execute()
        return result.data

    # =========================================================================
    # Combined Search (Memory Search) — Hybrid with RRF
    # =========================================================================

    @staticmethod
    def _reciprocal_rank_fusion(
        *ranked_lists: list[dict],
        k: int = 60,
        id_key: str = "id",
    ) -> list[dict]:
        """
        Merge multiple ranked lists using Reciprocal Rank Fusion (RRF).

        RRF is a simple, effective way to combine results from different
        search methods (e.g., vector + keyword). Each item gets a score of
        1/(k + rank + 1) from each list it appears in, and scores are summed.

        Args:
            *ranked_lists: Multiple ranked result lists (best first).
            k: Smoothing constant (default 60, standard in literature).
            id_key: The dict key to use as unique identifier.

        Returns:
            Merged list sorted by combined RRF score (highest first).
        """
        scores: dict[str, float] = {}
        items: dict[str, dict] = {}

        for ranked_list in ranked_lists:
            for rank, item in enumerate(ranked_list):
                item_id = str(item.get(id_key, id(item)))
                scores[item_id] = scores.get(item_id, 0) + 1.0 / (k + rank + 1)
                if item_id not in items:
                    items[item_id] = item

        # Sort by RRF score descending and attach scores
        sorted_ids = sorted(scores, key=lambda x: scores[x], reverse=True)
        result_list = []
        for item_id in sorted_ids:
            item = dict(items[item_id])
            item["rrf_score"] = scores[item_id]
            result_list.append(item)
        return result_list

    @staticmethod
    def _apply_time_weighting(
        results: list[dict],
        half_life_days: int | None = None,
    ) -> list[dict]:
        """
        Boost recent results using time-weighted scoring.

        Applies a recency boost that blends with the existing RRF score.
        Recent meetings get a higher score; older meetings decay smoothly.

        The formula: final_score = 0.7 * rrf_score + 0.3 * recency_boost
        where recency_boost = 1.0 / (1.0 + days_ago / half_life_days)

        Args:
            results: List of search result dicts (must have rrf_score and metadata).
            half_life_days: Number of days for the recency boost to halve.

        Returns:
            Re-sorted list with updated rrf_score values.
        """
        if half_life_days is None:
            half_life_days = settings.RECENCY_HALFLIFE_DAYS

        now = datetime.now()

        for item in results:
            meeting_date_str = item.get("metadata", {}).get("meeting_date") if item.get("metadata") else None
            if meeting_date_str:
                try:
                    meeting_date = datetime.fromisoformat(
                        str(meeting_date_str).replace("Z", "+00:00")
                    ).replace(tzinfo=None)
                    days_ago = max(0, (now - meeting_date).days)
                    recency_boost = 1.0 / (1.0 + days_ago / half_life_days)
                    rrf = item.get("rrf_score", 0)
                    item["rrf_score"] = rrf * 0.7 + recency_boost * 0.3
                except (ValueError, TypeError):
                    pass

        return sorted(results, key=lambda x: x.get("rrf_score", 0), reverse=True)

    def search_memory(
        self,
        query_embedding: list[float],
        query_text: str,
        limit: int = 10,
    ) -> dict:
        """
        Hybrid search across all memory sources using RRF fusion.

        Combines:
        1. Vector search (semantic similarity via pgvector)
        2. Full-text search (keyword matching via tsvector)
        3. Keyword search in decisions (ILIKE)
        4. Keyword search in tasks (ILIKE)

        Results from (1) and (2) are merged using Reciprocal Rank Fusion
        to get the best of both worlds — semantic understanding AND
        exact keyword matching.

        Args:
            query_embedding: Embedding vector for semantic search.
            query_text: Text for keyword and full-text search.
            limit: Maximum results per category.

        Returns:
            Dict with 'embeddings', 'decisions', 'tasks' keys.
        """
        results = {
            "embeddings": [],
            "decisions": [],
            "tasks": [],
        }

        # 1. Semantic search (vector) — top 20 candidates
        # Lower threshold for contextual embeddings (metadata-prefixed chunks
        # have different similarity characteristics than raw-text embeddings)
        vector_results = []
        try:
            vector_results = self.search_embeddings(
                query_embedding=query_embedding,
                limit=20,
                similarity_threshold=settings.SIMILARITY_THRESHOLD_CONTEXTUAL,
            )
        except Exception as e:
            logger.warning(f"Embedding search failed: {e}")

        # 2. Full-text search (keyword) — top 20 candidates
        fulltext_results = []
        try:
            fulltext_results = self.search_fulltext(
                query_text=query_text,
                limit=20,
            )
        except Exception as e:
            logger.warning(f"Full-text search failed: {e}")

        # 3. Merge via Reciprocal Rank Fusion (deduplicates by chunk ID)
        if vector_results or fulltext_results:
            merged = self._reciprocal_rank_fusion(
                vector_results, fulltext_results, id_key="id"
            )
            # v0.3: Apply time-weighted boost so recent meetings rank higher
            merged = self._apply_time_weighting(merged)
            results["embeddings"] = merged[:limit]

        # 4. Keyword search in decisions (ILIKE — small table, cheap)
        try:
            results["decisions"] = self.list_decisions(topic=query_text, limit=limit)
        except Exception as e:
            logger.warning(f"Decision search failed: {e}")

        # 5. Keyword search in tasks (ILIKE — small table, cheap)
        try:
            task_results = (
                self.client.table("tasks")
                .select("*, meetings(title)")
                .ilike("title", f"%{query_text}%")
                .limit(limit)
                .execute()
            )
            results["tasks"] = task_results.data
        except Exception as e:
            logger.warning(f"Task search failed: {e}")

        return results

    # =========================================================================
    # Cross-Reference Enrichment
    # =========================================================================

    def enrich_chunks_with_context(
        self,
        chunks: list[dict],
    ) -> list[dict]:
        """
        For each retrieved chunk, attach meeting title + related decisions/tasks.

        This makes search results much more useful — instead of just showing
        a raw text chunk, the caller also gets the meeting name, date,
        participants, and any decisions/tasks from the same meeting.

        Uses a dict cache so each meeting_id is only queried once
        (important when multiple chunks come from the same meeting).

        Args:
            chunks: List of chunk dicts from search results.

        Returns:
            List of enriched chunk dicts with extra context fields.
        """
        meeting_cache: dict[str, dict] = {}
        enriched = []

        for chunk in chunks:
            source_id = chunk.get("source_id")
            source_type = chunk.get("source_type", "meeting")
            enriched_chunk = dict(chunk)

            if source_type == "meeting" and source_id:
                # Look up meeting (cached to avoid repeated queries)
                if source_id not in meeting_cache:
                    meeting = self.get_meeting(source_id)
                    if meeting:
                        # Get related decisions and tasks for this meeting
                        decisions = self.list_decisions(meeting_id=source_id, limit=5)
                        tasks = self.get_tasks(status=None)
                        meeting_tasks = [
                            t for t in tasks if t.get("meeting_id") == source_id
                        ][:5]
                        meeting_cache[source_id] = {
                            "meeting": meeting,
                            "decisions": decisions,
                            "tasks": meeting_tasks,
                        }
                    else:
                        meeting_cache[source_id] = None

                cached = meeting_cache.get(source_id)
                if cached:
                    enriched_chunk["meeting_title"] = cached["meeting"].get("title")
                    enriched_chunk["meeting_date"] = cached["meeting"].get("date")
                    enriched_chunk["meeting_participants"] = cached["meeting"].get(
                        "participants", []
                    )
                    enriched_chunk["related_decisions"] = cached["decisions"]
                    enriched_chunk["related_tasks"] = cached["tasks"]

            # v0.3: Fetch neighboring chunks for expanded context
            chunk_index = chunk.get("chunk_index")
            if source_id and chunk_index is not None:
                try:
                    neighbor_indices = [chunk_index - 1, chunk_index + 1]
                    neighbors = (
                        self.client.table("embeddings")
                        .select("chunk_text, chunk_index")
                        .eq("source_id", source_id)
                        .in_("chunk_index", neighbor_indices)
                        .execute()
                    )
                    if neighbors.data:
                        sorted_neighbors = sorted(
                            neighbors.data, key=lambda x: x["chunk_index"]
                        )
                        enriched_chunk["expanded_context"] = " ".join(
                            n["chunk_text"] for n in sorted_neighbors
                        )
                except Exception as e:
                    logger.debug(f"Could not fetch neighbor chunks: {e}")

            enriched.append(enriched_chunk)

        return enriched


# Singleton instance for easy import
db = SupabaseClient()

# Alias for backward compatibility
supabase_client = db
