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

    def get_last_meeting_of_type(self, meeting_type: str) -> dict | None:
        """Get the most recent meeting with the given meeting_type."""
        try:
            result = self.client.table("meetings").select("*").eq(
                "meeting_type", meeting_type
            ).order("meeting_date", desc=True).limit(1).execute()
            return result.data[0] if result.data else None
        except Exception as e:
            logger.warning(f"Failed to get last meeting of type {meeting_type}: {e}")
            return None

    def get_changes_since(self, since_date: str, limit: int = 30) -> dict:
        """Get tasks completed, new decisions, and commitments fulfilled since a date.

        Args:
            since_date: ISO date string (e.g. '2026-03-10').
            limit: Max items per category.

        Returns:
            Dict with 'tasks_completed', 'tasks_newly_overdue', 'new_decisions', 'commitments_fulfilled'.
        """
        result = {}
        try:
            completed = self.client.table("tasks").select("*").eq(
                "status", "completed"
            ).gte("updated_at", since_date).limit(limit).execute()
            result["tasks_completed"] = completed.data if completed.data else []
        except Exception:
            result["tasks_completed"] = []

        try:
            overdue = self.client.table("tasks").select("*").eq(
                "status", "pending"
            ).lte("deadline", datetime.now().isoformat()).gte(
                "deadline", since_date
            ).limit(limit).execute()
            result["tasks_newly_overdue"] = overdue.data if overdue.data else []
        except Exception:
            result["tasks_newly_overdue"] = []

        try:
            decisions = self.client.table("decisions").select("*").gte(
                "created_at", since_date
            ).limit(limit).execute()
            result["new_decisions"] = decisions.data if decisions.data else []
        except Exception:
            result["new_decisions"] = []

        try:
            fulfilled = self.client.table("commitments").select("*").eq(
                "status", "fulfilled"
            ).gte("updated_at", since_date).limit(limit).execute()
            result["commitments_fulfilled"] = fulfilled.data if fulfilled.data else []
        except Exception:
            result["commitments_fulfilled"] = []

        return result

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

        # Exclude debrief pseudo-meetings from listing
        query = query.neq("source_file_path", "debrief")

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
        data = []
        for d in decisions:
            row = {
                "meeting_id": meeting_id,
                "description": d.get("description"),
                "context": d.get("context"),
                "participants_involved": d.get("participants_involved"),
                "transcript_timestamp": d.get("transcript_timestamp"),
            }
            # Phase 9A: decision intelligence fields
            if d.get("rationale"):
                row["rationale"] = d["rationale"]
            if d.get("options_considered"):
                row["options_considered"] = d["options_considered"]
            if d.get("confidence"):
                row["confidence"] = d["confidence"]
            if d.get("label"):
                row["label"] = d["label"]
            # Default review_date: 30 days from meeting
            if d.get("review_date"):
                row["review_date"] = d["review_date"]
            data.append(row)

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

    def get_meetings_by_participant_overlap(
        self,
        participants: list[str],
        exclude_meeting_id: str | None = None,
        limit: int = 3,
    ) -> list[dict]:
        """
        Get recent meetings with overlapping participants.

        Uses Postgres array overlap (&&) to find meetings where at least
        one participant matches.

        Args:
            participants: List of participant names to match.
            exclude_meeting_id: Meeting ID to exclude (current meeting).
            limit: Max results.

        Returns:
            List of meeting records ordered by date desc.
        """
        if not participants:
            return []

        # Build array literal for Postgres && operator
        query = (
            self.client.table("meetings")
            .select("id, title, date, participants, summary")
            .eq("approval_status", "approved")
            .order("date", desc=True)
            .limit(limit + 1)  # +1 in case we need to exclude current
        )

        # Use overlap filter: participants && ARRAY[...]
        query = query.overlaps("participants", participants)

        result = query.execute()
        meetings = result.data or []

        # Exclude current meeting if specified
        if exclude_meeting_id:
            meetings = [m for m in meetings if m.get("id") != exclude_meeting_id]

        return meetings[:limit]

    def update_decision(
        self,
        decision_id: str,
        **updates,
    ) -> dict:
        """Update a decision's fields (status, review_date, rationale, etc.)."""
        result = (
            self.client.table("decisions")
            .update(updates)
            .eq("id", decision_id)
            .execute()
        )
        if result.data:
            logger.info(f"Updated decision {decision_id}: {list(updates.keys())}")
            return result.data[0]
        return {}

    def get_decisions_for_review(self, days_ahead: int = 30) -> list[dict]:
        """Get active decisions with review_date within the next N days."""
        from datetime import datetime, timedelta, timezone

        cutoff = (datetime.now(timezone.utc) + timedelta(days=days_ahead)).strftime("%Y-%m-%d")
        result = (
            self.client.table("decisions")
            .select("*, meetings(title, date)")
            .eq("decision_status", "active")
            .not_.is_("review_date", "null")
            .lte("review_date", cutoff)
            .order("review_date")
            .execute()
        )
        return result.data or []

    def mark_decision_superseded(self, old_id: str, new_id: str) -> None:
        """Mark an old decision as superseded by a new one."""
        self.client.table("decisions").update({
            "decision_status": "superseded",
            "superseded_by": new_id,
        }).eq("id", old_id).execute()
        logger.info(f"Decision {old_id} superseded by {new_id}")

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
        # Filter out tasks missing title (assignee can be empty — unassigned tasks are valid)
        valid_tasks = [t for t in tasks if t.get("title")]
        if not valid_tasks:
            logger.info("No valid tasks to insert after filtering")
            return []

        data = [
            {
                "meeting_id": meeting_id,
                "title": t.get("title"),
                "assignee": t.get("assignee", ""),
                "priority": t.get("priority", "M"),
                "deadline": self._serialize_datetime(t.get("deadline")),
                "transcript_timestamp": t.get("transcript_timestamp"),
                "status": "pending",
                "category": t.get("category"),
                "label": t.get("label"),
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

    def get_tasks_without_assignee(self, limit: int = 50) -> list[dict]:
        """Get open tasks with empty or null assignee."""
        result = (
            self.client.table("tasks")
            .select("*, meetings(title, date)")
            .in_("status", ["pending", "in_progress", "overdue"])
            .or_("assignee.is.null,assignee.eq.")
            .order("created_at", desc=True)
            .limit(limit)
            .execute()
        )
        return result.data or []

    def get_tasks_without_deadline(self, limit: int = 50) -> list[dict]:
        """Get open tasks with no deadline set."""
        result = (
            self.client.table("tasks")
            .select("*, meetings(title, date)")
            .in_("status", ["pending", "in_progress", "overdue"])
            .is_("deadline", "null")
            .order("created_at", desc=True)
            .limit(limit)
            .execute()
        )
        return result.data or []

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
        expires_at: str | None = None,
    ) -> dict:
        """
        Create a pending approval record.

        Args:
            approval_id: Meeting UUID or prefixed ID (e.g. "prep-2026-03-01").
            content_type: 'meeting_summary', 'meeting_prep', or 'weekly_digest'.
            content: Full content dict (stored as JSONB).
            auto_publish_at: ISO timestamp for auto-publish, or None for manual mode.
            expires_at: ISO timestamp for graceful expiry, or None for no expiry.

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
        if expires_at:
            data["expires_at"] = expires_at

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

    def get_pending_approvals_by_status(self, status: str = "pending") -> list[dict]:
        """
        Get all pending approvals with a given status, newest first.

        Used to find the most recent pending approval when Eyal types
        'approve' or 'reject' as free text instead of using buttons.

        Returns:
            List of pending approval records.
        """
        result = (
            self.client.table("pending_approvals")
            .select("*")
            .eq("status", status)
            .order("created_at", desc=True)
            .limit(5)
            .execute()
        )
        return result.data

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

    def expire_pending_approvals(self) -> list[dict]:
        """
        Expire pending approvals whose expires_at is in the past.

        Updates status to 'expired' and returns the expired rows.

        Returns:
            List of expired approval records.
        """
        now = datetime.now().isoformat()
        try:
            result = (
                self.client.table("pending_approvals")
                .select("*")
                .eq("status", "pending")
                .not_.is_("expires_at", "null")
                .lt("expires_at", now)
                .execute()
            )
            if not result.data:
                return []

            expired = []
            for row in result.data:
                self.client.table("pending_approvals").update(
                    {"status": "expired"}
                ).eq("approval_id", row["approval_id"]).execute()
                expired.append(row)
                logger.info(f"Expired approval: {row['approval_id']} ({row['content_type']})")

            return expired
        except Exception as e:
            logger.error(f"Error expiring pending approvals: {e}")
            return []

    def get_pending_approval_summary(self) -> list[dict]:
        """
        Get a summary of all pending approvals for /status display.

        Returns:
            List of dicts with approval_id, content_type, created_at, expires_at.
        """
        result = (
            self.client.table("pending_approvals")
            .select("approval_id, content_type, created_at, expires_at")
            .eq("status", "pending")
            .order("created_at", desc=True)
            .limit(20)
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

    def update_classification_meeting_type(self, title: str, meeting_type: str) -> dict | None:
        """
        Set the meeting_type on a calendar classification by title.

        Args:
            title: Meeting title (matched case-insensitive via title_lower).
            meeting_type: Template key (e.g. 'founders_technical').

        Returns:
            Updated record or None.
        """
        try:
            result = (
                self.client.table("calendar_classifications")
                .update({"meeting_type": meeting_type})
                .eq("title_lower", title.lower())
                .execute()
            )
            if result.data:
                logger.info(f"Updated classification meeting_type: '{title}' → {meeting_type}")
                return result.data[0]
            # No existing row — create one
            data = {
                "title": title,
                "is_cropsight": True,
                "classified_by": "system",
                "meeting_type": meeting_type,
            }
            result = self.client.table("calendar_classifications").insert(data).execute()
            logger.info(f"Created classification with meeting_type: '{title}' → {meeting_type}")
            return result.data[0] if result.data else None
        except Exception as e:
            logger.error(f"Error updating classification meeting_type: {e}")
            return None

    def get_classifications_by_meeting_type(self, meeting_type: str) -> list[dict]:
        """
        Get all classifications with a given meeting_type.

        Args:
            meeting_type: Template key to filter by.

        Returns:
            List of matching classification records.
        """
        try:
            result = (
                self.client.table("calendar_classifications")
                .select("*")
                .eq("meeting_type", meeting_type)
                .execute()
            )
            return result.data
        except Exception as e:
            logger.error(f"Error getting classifications by meeting_type: {e}")
            return []

    def get_pending_prep_outlines(self) -> list[dict]:
        """
        Get all pending prep outline approvals.

        Returns:
            List of pending_approvals where content_type='prep_outline' and status='pending'.
        """
        try:
            result = (
                self.client.table("pending_approvals")
                .select("*")
                .eq("content_type", "prep_outline")
                .eq("status", "pending")
                .order("created_at", desc=True)
                .execute()
            )
            return result.data
        except Exception as e:
            logger.error(f"Error getting pending prep outlines: {e}")
            return []

    # =========================================================================
    # Orphan Cleanup Helpers (v0.5)
    # =========================================================================

    def get_stale_pending_approvals(self, days: int = 7) -> list[dict]:
        """
        Get pending approvals older than N days.

        Args:
            days: Number of days after which an approval is considered stale.

        Returns:
            List of stale pending approval records.
        """
        cutoff = (datetime.now() - timedelta(days=days)).isoformat()
        result = (
            self.client.table("pending_approvals")
            .select("*")
            .eq("status", "pending")
            .lt("created_at", cutoff)
            .execute()
        )
        return result.data

    def get_orphan_embedding_ids(self) -> list[str]:
        """
        Find embedding IDs whose source_id doesn't exist in meetings or documents.

        Returns:
            List of orphan embedding UUIDs.
        """
        # Get all unique source_ids from embeddings
        emb_result = (
            self.client.table("embeddings")
            .select("id, source_id, source_type")
            .execute()
        )
        if not emb_result.data:
            return []

        # Get all meeting IDs
        meeting_result = self.client.table("meetings").select("id").execute()
        meeting_ids = {str(m["id"]) for m in (meeting_result.data or [])}

        # Get all document IDs
        doc_result = self.client.table("documents").select("id").execute()
        doc_ids = {str(d["id"]) for d in (doc_result.data or [])}

        # Find orphans: embeddings whose source doesn't exist
        orphan_ids = []
        for emb in emb_result.data:
            source_id = str(emb.get("source_id", ""))
            source_type = emb.get("source_type", "meeting")
            if source_type == "meeting" and source_id not in meeting_ids:
                orphan_ids.append(str(emb["id"]))
            elif source_type == "document" and source_id not in doc_ids:
                orphan_ids.append(str(emb["id"]))

        return orphan_ids

    def delete_embeddings_by_ids(self, ids: list[str]) -> int:
        """
        Delete embeddings by their IDs.

        Args:
            ids: List of embedding UUIDs to delete.

        Returns:
            Number of embeddings deleted.
        """
        if not ids:
            return 0

        # Delete in batches of 100 to avoid query size limits
        total_deleted = 0
        for i in range(0, len(ids), 100):
            batch = ids[i : i + 100]
            result = (
                self.client.table("embeddings")
                .delete()
                .in_("id", batch)
                .execute()
            )
            total_deleted += len(result.data) if result.data else 0

        logger.info(f"Deleted {total_deleted} orphan embeddings")
        return total_deleted

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
                    base_score = rrf * 0.7 + recency_boost * 0.3
                    # Source-type weighting from settings
                    source_type = item.get("metadata", {}).get("source_type") if item.get("metadata") else None
                    source_weight_map = {
                        "debrief": settings.RAG_WEIGHT_DEBRIEF,
                        "decision": settings.RAG_WEIGHT_DECISION,
                        "email": settings.RAG_WEIGHT_EMAIL,
                        "meeting": settings.RAG_WEIGHT_MEETING,
                        "document": settings.RAG_WEIGHT_DOCUMENT,
                        "gantt_change": settings.RAG_WEIGHT_GANTT,
                    }
                    source_weight = source_weight_map.get(source_type, 1.0)
                    item["rrf_score"] = base_score * source_weight
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

    # =========================================================================
    # v1.0 — Gantt Schema
    # =========================================================================

    def upsert_gantt_schema_rows(self, rows: list[dict]) -> list[dict]:
        """Bulk upsert gantt schema rows."""
        result = self.client.table("gantt_schema").upsert(rows).execute()
        return result.data or []

    def get_gantt_schema(self, sheet_name: str | None = None) -> list[dict]:
        """Get gantt schema rows, optionally filtered by sheet."""
        query = self.client.table("gantt_schema").select("*")
        if sheet_name:
            query = query.eq("sheet_name", sheet_name)
        query = query.order("row_number")
        result = query.execute()
        return result.data or []

    def get_gantt_protected_rows(self, sheet_name: str) -> list[dict]:
        """Get protected rows only for a sheet."""
        result = (
            self.client.table("gantt_schema")
            .select("*")
            .eq("sheet_name", sheet_name)
            .eq("protected", True)
            .order("row_number")
            .execute()
        )
        return result.data or []

    # =========================================================================
    # v1.0 — Gantt Proposals
    # =========================================================================

    def create_gantt_proposal(self, source_type: str, source_id: str | None, changes: list[dict]) -> dict:
        """Create a new Gantt change proposal."""
        row = {
            "source_type": source_type,
            "changes": changes,
        }
        if source_id:
            row["source_id"] = str(source_id)
        result = self.client.table("gantt_proposals").insert(row).execute()
        return result.data[0] if result.data else {}

    def get_gantt_proposal(self, proposal_id: str) -> dict | None:
        """Get a single Gantt proposal by ID."""
        result = (
            self.client.table("gantt_proposals")
            .select("*")
            .eq("id", str(proposal_id))
            .execute()
        )
        return result.data[0] if result.data else None

    def get_gantt_proposals(self, status: str | None = None, limit: int = 20) -> list[dict]:
        """List Gantt proposals, optionally filtered by status."""
        query = self.client.table("gantt_proposals").select("*")
        if status:
            query = query.eq("status", status)
        query = query.order("proposed_at", desc=True).limit(limit)
        result = query.execute()
        return result.data or []

    def update_gantt_proposal(self, proposal_id: str, status: str, reviewed_by: str | None = None, rejection_reason: str | None = None) -> dict:
        """Update a Gantt proposal's status."""
        updates = {"status": status, "reviewed_at": datetime.now().isoformat()}
        if reviewed_by:
            updates["reviewed_by"] = reviewed_by
        if rejection_reason:
            updates["rejection_reason"] = rejection_reason
        result = (
            self.client.table("gantt_proposals")
            .update(updates)
            .eq("id", str(proposal_id))
            .execute()
        )
        return result.data[0] if result.data else {}

    # =========================================================================
    # v1.0 — Gantt Snapshots
    # =========================================================================

    def create_gantt_snapshot(self, proposal_id: str, sheet_name: str, cell_references: list[str], old_values: dict, new_values: dict) -> dict:
        """Create a snapshot of cell values before a Gantt write."""
        row = {
            "proposal_id": str(proposal_id),
            "sheet_name": sheet_name,
            "cell_references": cell_references,
            "old_values": old_values,
            "new_values": new_values,
        }
        result = self.client.table("gantt_snapshots").insert(row).execute()
        return result.data[0] if result.data else {}

    def get_gantt_snapshots(self, proposal_id: str) -> list[dict]:
        """Get all snapshots for a given proposal."""
        result = (
            self.client.table("gantt_snapshots")
            .select("*")
            .eq("proposal_id", str(proposal_id))
            .order("created_at")
            .execute()
        )
        return result.data or []

    # =========================================================================
    # v1.0 — Debrief Sessions
    # =========================================================================

    def create_debrief_session(self, date: str) -> dict:
        """Create a new debrief session."""
        row = {"date": date}
        result = self.client.table("debrief_sessions").insert(row).execute()
        return result.data[0] if result.data else {}

    def get_debrief_session(self, session_id: str) -> dict | None:
        """Get a single debrief session by ID."""
        result = (
            self.client.table("debrief_sessions")
            .select("*")
            .eq("id", str(session_id))
            .execute()
        )
        return result.data[0] if result.data else None

    def get_active_debrief_session(self) -> dict | None:
        """Get the currently active (in_progress) debrief session."""
        result = (
            self.client.table("debrief_sessions")
            .select("*")
            .eq("status", "in_progress")
            .order("created_at", desc=True)
            .limit(1)
            .execute()
        )
        return result.data[0] if result.data else None

    def update_debrief_session(self, session_id: str, **kwargs) -> dict:
        """Update a debrief session's fields."""
        result = (
            self.client.table("debrief_sessions")
            .update(kwargs)
            .eq("id", str(session_id))
            .execute()
        )
        return result.data[0] if result.data else {}

    # =========================================================================
    # v1.0 — Email Scans
    # =========================================================================

    def create_email_scan(
        self,
        scan_type: str,
        email_id: str,
        date: str,
        sender: str | None = None,
        subject: str | None = None,
        classification: str | None = None,
        extracted_items: list | None = None,
        recipient: str | None = None,
        thread_id: str | None = None,
        direction: str = "inbound",
        approved: bool | None = None,
        attachments_processed: list | None = None,
    ) -> dict:
        """Record a scanned email with all Phase 4 fields."""
        row = {
            "scan_type": scan_type,
            "email_id": email_id,
            "date": date,
            "direction": direction,
        }
        if sender:
            row["sender"] = sender
        if subject:
            row["subject"] = subject
        if classification:
            row["classification"] = classification
        if extracted_items is not None:
            row["extracted_items"] = extracted_items
        if recipient:
            row["recipient"] = recipient
        if thread_id:
            row["thread_id"] = thread_id
        if approved is not None:
            row["approved"] = approved
        if attachments_processed is not None:
            row["attachments_processed"] = attachments_processed
        result = self.client.table("email_scans").insert(row).execute()
        return result.data[0] if result.data else {}

    def update_email_scan(self, scan_id: str, **kwargs) -> dict:
        """Update an email scan record (e.g., approved=True after brief approval)."""
        if not kwargs:
            return {}
        result = (
            self.client.table("email_scans")
            .update(kwargs)
            .eq("id", scan_id)
            .execute()
        )
        return result.data[0] if result.data else {}

    def get_email_scans(self, scan_type: str | None = None, date_from: str | None = None, limit: int = 50) -> list[dict]:
        """List email scans with optional filters."""
        query = self.client.table("email_scans").select("*")
        if scan_type:
            query = query.eq("scan_type", scan_type)
        if date_from:
            query = query.gte("date", date_from)
        query = query.order("date", desc=True).limit(limit)
        result = query.execute()
        return result.data or []

    def get_unapproved_email_scans(
        self,
        scan_type: str | None = None,
        date_from: str | None = None,
    ) -> list[dict]:
        """Get scans classified as relevant/borderline but not yet approved. Used by morning brief."""
        query = (
            self.client.table("email_scans")
            .select("*")
            .eq("approved", False)
            .in_("classification", ["relevant", "borderline"])
        )
        if scan_type:
            query = query.eq("scan_type", scan_type)
        if date_from:
            query = query.gte("date", date_from)
        query = query.order("date", desc=True).limit(100)
        result = query.execute()
        return result.data or []

    def get_tracked_thread_ids(self, days: int = 30, scan_type: str | None = None) -> set[str]:
        """Get distinct thread_ids from recent relevant email_scans."""
        from datetime import datetime, timedelta
        cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
        query = (
            self.client.table("email_scans")
            .select("thread_id")
            .gte("date", cutoff)
            .in_("classification", ["relevant", "borderline"])
            .not_.is_("thread_id", "null")
        )
        if scan_type:
            query = query.eq("scan_type", scan_type)
        query = query.limit(500)
        result = query.execute()
        return {row["thread_id"] for row in (result.data or []) if row.get("thread_id")}

    def get_last_scan_date(self, scan_type: str = "daily") -> str | None:
        """Get date of most recent scan. For /emailscan rate limiting."""
        result = (
            self.client.table("email_scans")
            .select("date")
            .eq("scan_type", scan_type)
            .order("date", desc=True)
            .limit(1)
            .execute()
        )
        if result.data:
            return result.data[0].get("date")
        return None

    def is_email_already_scanned(self, email_id: str) -> bool:
        """Check if an email has already been scanned (dedup)."""
        result = (
            self.client.table("email_scans")
            .select("id")
            .eq("email_id", email_id)
            .limit(1)
            .execute()
        )
        return bool(result.data)

    # =========================================================================
    # v1.0 — MCP Sessions
    # =========================================================================

    def create_mcp_session(self, session_date: str, summary: str, decisions_made: list | None = None, pending_items: list | None = None) -> dict:
        """Create a new MCP session record."""
        row = {
            "session_date": session_date,
            "summary": summary,
        }
        if decisions_made is not None:
            row["decisions_made"] = decisions_made
        if pending_items is not None:
            row["pending_items"] = pending_items
        result = self.client.table("mcp_sessions").insert(row).execute()
        return result.data[0] if result.data else {}

    def get_latest_mcp_session(self) -> dict | None:
        """Get the most recent MCP session."""
        result = (
            self.client.table("mcp_sessions")
            .select("*")
            .order("session_date", desc=True)
            .limit(1)
            .execute()
        )
        return result.data[0] if result.data else None

    # =========================================================================
    # v1.0 — Weekly Reports
    # =========================================================================

    def create_weekly_report(self, week_number: int, year: int, data: dict | None = None) -> dict:
        """Create a new weekly report record."""
        row = {"week_number": week_number, "year": year}
        if data is not None:
            row["data"] = data
        result = self.client.table("weekly_reports").insert(row).execute()
        return result.data[0] if result.data else {}

    def get_weekly_report(self, week_number: int, year: int) -> dict | None:
        """Get a weekly report by week number and year."""
        result = (
            self.client.table("weekly_reports")
            .select("*")
            .eq("week_number", week_number)
            .eq("year", year)
            .limit(1)
            .execute()
        )
        return result.data[0] if result.data else None

    def update_weekly_report(self, report_id: str, **kwargs) -> dict:
        """Update a weekly report's fields."""
        result = (
            self.client.table("weekly_reports")
            .update(kwargs)
            .eq("id", str(report_id))
            .execute()
        )
        return result.data[0] if result.data else {}

    # =========================================================================
    # v1.0 — Weekly Review Sessions
    # =========================================================================

    def create_weekly_review_session(self, week_number: int, year: int, **kwargs) -> dict:
        """Create a new weekly review session."""
        row = {"week_number": week_number, "year": year, **kwargs}
        result = self.client.table("weekly_review_sessions").insert(row).execute()
        return result.data[0] if result.data else {}

    def get_weekly_review_session(self, session_id: str) -> dict | None:
        """Get a weekly review session by ID."""
        result = (
            self.client.table("weekly_review_sessions")
            .select("*")
            .eq("id", str(session_id))
            .limit(1)
            .execute()
        )
        return result.data[0] if result.data else None

    def get_active_weekly_review_session(self) -> dict | None:
        """Get the current active weekly review session (preparing/ready/in_progress/confirming)."""
        result = (
            self.client.table("weekly_review_sessions")
            .select("*")
            .in_("status", ["preparing", "ready", "in_progress", "confirming"])
            .order("created_at", desc=True)
            .limit(1)
            .execute()
        )
        return result.data[0] if result.data else None

    def update_weekly_review_session(self, session_id: str, **kwargs) -> dict:
        """Update a weekly review session's fields."""
        from datetime import datetime
        kwargs["updated_at"] = datetime.utcnow().isoformat()
        result = (
            self.client.table("weekly_review_sessions")
            .update(kwargs)
            .eq("id", str(session_id))
            .execute()
        )
        return result.data[0] if result.data else {}

    def get_weekly_report_by_token(self, access_token: str) -> dict | None:
        """Get a weekly report by its per-report access token."""
        result = (
            self.client.table("weekly_reports")
            .select("*")
            .eq("access_token", access_token)
            .limit(1)
            .execute()
        )
        return result.data[0] if result.data else None

    def count_items_since(self, table: str, since_timestamp: str) -> int:
        """Count items created since a given timestamp."""
        try:
            result = (
                self.client.table(table)
                .select("id", count="exact")
                .gt("created_at", since_timestamp)
                .execute()
            )
            return result.count or 0
        except Exception:
            return 0

    def get_stale_tasks(self, days: int = 14) -> list[dict]:
        """Get tasks that have been pending for more than N days."""
        from datetime import datetime, timedelta
        cutoff = (datetime.utcnow() - timedelta(days=days)).isoformat()
        result = (
            self.client.table("tasks")
            .select("*")
            .eq("status", "pending")
            .lt("created_at", cutoff)
            .order("created_at", desc=False)
            .limit(50)
            .execute()
        )
        return result.data or []

    def get_debrief_sessions_for_week(self, week_start: str, week_end: str) -> list[dict]:
        """Get debrief sessions within a date range."""
        result = (
            self.client.table("debrief_sessions")
            .select("*")
            .gte("date", week_start)
            .lte("date", week_end)
            .order("date", desc=True)
            .execute()
        )
        return result.data or []

    def get_email_scans_for_week(self, week_start: str, week_end: str) -> list[dict]:
        """Get email scans within a date range."""
        result = (
            self.client.table("email_scans")
            .select("*")
            .gte("created_at", week_start)
            .lte("created_at", week_end)
            .order("created_at", desc=True)
            .execute()
        )
        return result.data or []

    def get_pending_gantt_proposals(self) -> list[dict]:
        """Get all pending Gantt change proposals."""
        result = (
            self.client.table("gantt_proposals")
            .select("*")
            .eq("status", "pending")
            .order("proposed_at", desc=True)
            .execute()
        )
        return result.data or []

    # =========================================================================
    # v1.0 — Meeting Prep History
    # =========================================================================

    def create_meeting_prep_history(self, meeting_type: str, meeting_date: str, prep_content: dict, calendar_event_id: str | None = None) -> dict:
        """Create a meeting prep history record."""
        row = {
            "meeting_type": meeting_type,
            "meeting_date": meeting_date,
            "prep_content": prep_content,
        }
        if calendar_event_id:
            row["calendar_event_id"] = calendar_event_id
        result = self.client.table("meeting_prep_history").insert(row).execute()
        return result.data[0] if result.data else {}

    def get_meeting_prep_history(self, meeting_type: str | None = None, limit: int = 10) -> list[dict]:
        """List meeting prep history with optional type filter."""
        query = self.client.table("meeting_prep_history").select("*")
        if meeting_type:
            query = query.eq("meeting_type", meeting_type)
        query = query.order("meeting_date", desc=True).limit(limit)
        result = query.execute()
        return result.data or []

    def update_meeting_prep_history(self, prep_id: str, status: str, **kwargs) -> dict:
        """Update a meeting prep history record's status."""
        updates = {"status": status, **kwargs}
        result = (
            self.client.table("meeting_prep_history")
            .update(updates)
            .eq("id", str(prep_id))
            .execute()
        )
        return result.data[0] if result.data else {}

    # =========================================================================
    # Scheduler Heartbeats
    # =========================================================================

    def upsert_scheduler_heartbeat(
        self,
        name: str,
        status: str = "ok",
        details: dict | None = None,
    ) -> None:
        """
        Record a scheduler heartbeat (last successful/failed run).

        Args:
            name: Scheduler name (e.g., "transcript_watcher").
            status: "ok" or "error".
            details: Optional error details or metrics.
        """
        from datetime import datetime, timezone

        data = {
            "scheduler_name": name,
            "last_run_at": datetime.now(timezone.utc).isoformat(),
            "status": status,
            "details": details or {},
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        self.client.table("scheduler_heartbeats").upsert(
            data, on_conflict="scheduler_name"
        ).execute()

    def get_scheduler_heartbeats(self) -> list[dict]:
        """Get all scheduler heartbeat records."""
        result = (
            self.client.table("scheduler_heartbeats")
            .select("*")
            .order("scheduler_name")
            .execute()
        )
        return result.data or []

    # =========================================================================
    # Token Usage (Cost Queries)
    # =========================================================================

    def get_token_usage_summary(self, days: int = 7) -> list[dict]:
        """
        Get token usage records for the past N days.

        Args:
            days: Number of days to look back.

        Returns:
            List of token_usage records.
        """
        from datetime import datetime, timedelta, timezone

        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        result = (
            self.client.table("token_usage")
            .select("*")
            .gte("created_at", cutoff)
            .order("created_at", desc=True)
            .limit(2000)
            .execute()
        )
        return result.data or []


    # =========================================================================
    # Canonical Projects (Phase 10)
    # =========================================================================

    def get_canonical_projects(self, status: str = "active") -> list[dict]:
        """Get all canonical projects, optionally filtered by status."""
        try:
            query = self.client.table("canonical_projects").select("*")
            if status:
                query = query.eq("status", status)
            result = query.order("name").execute()
            return result.data or []
        except Exception as e:
            logger.error(f"Error getting canonical projects: {e}")
            return []

    def add_canonical_project(
        self,
        name: str,
        description: str = "",
        aliases: list[str] | None = None,
    ) -> dict | None:
        """Add a new canonical project. Returns the created project or None."""
        try:
            result = self.client.table("canonical_projects").insert({
                "name": name,
                "description": description,
                "aliases": aliases or [],
                "status": "active",
            }).execute()

            if result.data:
                project = result.data[0]

                # Retroactively resolve any unmatched_labels that match
                self._resolve_unmatched_labels(name, aliases or [])

                logger.info(f"Created canonical project: {name}")
                return project
            return None
        except Exception as e:
            logger.error(f"Error adding canonical project '{name}': {e}")
            return None

    def _resolve_unmatched_labels(self, name: str, aliases: list[str]) -> int:
        """
        Remove unmatched_labels entries that match the new canonical project.

        Returns count of resolved labels.
        """
        try:
            # Fetch all unmatched labels
            result = self.client.table("unmatched_labels").select("id, label").execute()
            if not result.data:
                return 0

            # Check which labels match the new project name or aliases
            match_terms = {name.lower()} | {a.lower() for a in aliases}
            to_delete = []
            for row in result.data:
                if row.get("label", "").lower() in match_terms:
                    to_delete.append(row["id"])

            if to_delete:
                for uid in to_delete:
                    self.client.table("unmatched_labels").delete().eq("id", uid).execute()
                logger.info(f"Resolved {len(to_delete)} unmatched labels for '{name}'")

            return len(to_delete)
        except Exception as e:
            logger.error(f"Error resolving unmatched labels: {e}")
            return 0

    def store_unmatched_label(
        self,
        label: str,
        meeting_id: str | None = None,
        meeting_title: str = "",
        context: str = "",
    ) -> None:
        """Store a label that didn't match any canonical project."""
        try:
            data = {
                "label": label,
                "meeting_title": meeting_title,
                "context": context,
            }
            if meeting_id:
                data["meeting_id"] = meeting_id
            self.client.table("unmatched_labels").insert(data).execute()
            logger.debug(f"Stored unmatched label: {label}")
        except Exception as e:
            logger.error(f"Error storing unmatched label '{label}': {e}")

    def get_unmatched_labels(self, days: int = 7) -> list[dict]:
        """Get unmatched labels from the past N days."""
        try:
            from datetime import datetime, timedelta, timezone
            cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
            result = (
                self.client.table("unmatched_labels")
                .select("*")
                .gte("created_at", cutoff)
                .order("created_at", desc=True)
                .execute()
            )
            return result.data or []
        except Exception as e:
            logger.error(f"Error getting unmatched labels: {e}")
            return []

    def match_label_to_canonical(self, label: str) -> str | None:
        """
        Try to match a label to a canonical project name.

        Returns the canonical name if matched, None otherwise.
        Checks exact name match first, then alias match.
        """
        try:
            projects = self.get_canonical_projects(status="active")
            label_lower = label.lower().strip()

            # Exact name match
            for p in projects:
                if p["name"].lower() == label_lower:
                    return p["name"]

            # Alias match
            for p in projects:
                for alias in (p.get("aliases") or []):
                    if alias.lower() == label_lower:
                        return p["name"]

            return None
        except Exception as e:
            logger.error(f"Error matching label '{label}': {e}")
            return None


# Singleton instance for easy import
db = SupabaseClient()

# Alias for backward compatibility
supabase_client = db
