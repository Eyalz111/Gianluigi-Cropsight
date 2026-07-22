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
from datetime import datetime, date, timedelta, timezone
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
            # TaskStatus is "done", not "completed" — the old literal matched no
            # rows, so meeting-prep's "what got done since the last meeting" block
            # was permanently empty. [audit P6-02]
            # approved-only: never surface pending-extraction items as fact. [audit P3-15]
            completed = self.client.table("tasks").select("*").eq(
                "status", "done"
            ).eq("approval_status", "approved").gte(
                "updated_at", since_date
            ).limit(limit).execute()
            result["tasks_completed"] = completed.data if completed.data else []
        except Exception:
            result["tasks_completed"] = []

        try:
            overdue = self.client.table("tasks").select("*").eq(
                "status", "pending"
            ).eq("approval_status", "approved").lte(
                "deadline", datetime.now().isoformat()
            ).gte("deadline", since_date).limit(limit).execute()
            result["tasks_newly_overdue"] = overdue.data if overdue.data else []
        except Exception:
            result["tasks_newly_overdue"] = []

        try:
            decisions = self.client.table("decisions").select("*").eq(
                "approval_status", "approved"
            ).gte("created_at", since_date).limit(limit).execute()
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
            # Human-entered dates ("20.6.26", "20/6/2026" — day-first). The
            # 2026-06-11 incident: these returned NULL and erased deadlines.
            from core.dates import parse_human_date
            parsed = parse_human_date(dt)
            if parsed:
                return parsed
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
        sensitivity: str = "founders",
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

    def delete_meeting_cascade(self, meeting_id: str, keep_tombstone: bool = False) -> dict:
        """
        Delete a meeting and all related data.

        Two paths:
        - keep_tombstone=False (hard delete): Relies on DB-level ON DELETE
          CASCADE FKs (see scripts/migrate_tier3_cascade_fks.sql). A single
          DELETE FROM meetings cascades atomically to every child table
          except embeddings, which is polymorphic (source_type: 'meeting'
          or 'document') and has no FK. Pre-counts children for reporting.

        - keep_tombstone=True (T1.9 reject): Keeps the meetings row as a
          tombstone (approval_status='rejected', summary cleared). Because
          the parent row is preserved, the DB cascade doesn't fire and we
          must delete each child table explicitly. The watcher uses the
          tombstone to skip re-processing the same source file.

        Args:
            meeting_id: UUID of the meeting to delete.
            keep_tombstone: When True (used by cascading reject), keeps
                the `meetings` row with approval_status='rejected' and
                clears its transcript + summary.

        Returns:
            Dict with counts of deleted records by type. When keep_tombstone
            is True, `counts['meetings']` will be 0 (row kept) and an extra
            `counts['tombstone']` = 1 is set.
        """
        counts = {
            "embeddings": 0,
            "tasks": 0,
            "meetings": 0,
            "decisions": 0,
            "open_questions": 0,
            "follow_up_meetings": 0,
            "topic_thread_mentions": 0,
        }

        try:
            # Pre-count children so callers (reject flow, cleanup script, MCP)
            # can still report "deleted N tasks, M decisions, ..." — same
            # public contract as pre-T3.2. Count queries are cheap (indexed).
            for table, fk in [
                ("tasks", "meeting_id"),
                ("decisions", "meeting_id"),
                ("open_questions", "meeting_id"),
                ("follow_up_meetings", "source_meeting_id"),
                ("topic_thread_mentions", "meeting_id"),
            ]:
                try:
                    r = (
                        self.client.table(table)
                        .select("id", count="exact")
                        .eq(fk, meeting_id)
                        .execute()
                    )
                    if table in counts:
                        counts[table] = r.count or 0
                except Exception as e:
                    logger.debug(f"Pre-count for {table} skipped: {e}")

            # Capture the topic threads this meeting mentioned BEFORE the mentions
            # are deleted, so we can recompute their meeting_count and drop now-
            # orphaned (zero-mention) threads afterward — otherwise a reject leaves
            # an orphan thread with meeting_count=1 and zero mentions, shown in
            # list_active_threads as a fabricated topic. [audit P1-07]
            affected_topic_ids: set = set()
            try:
                _m = (
                    self.client.table("topic_thread_mentions")
                    .select("topic_id")
                    .eq("meeting_id", meeting_id)
                    .execute()
                    .data
                    or []
                )
                affected_topic_ids = {r.get("topic_id") for r in _m if r.get("topic_id")}
            except Exception as e:
                logger.debug(f"affected-topic capture skipped: {e}")

            # Embeddings are polymorphic (source_type: 'meeting' | 'document')
            # so they have no FK on meetings(id). Always delete explicitly.
            try:
                emb_result = (
                    self.client.table("embeddings")
                    .delete()
                    .eq("source_id", meeting_id)
                    .execute()
                )
                counts["embeddings"] = len(emb_result.data) if emb_result.data else 0
            except Exception as e:
                logger.debug(f"Embedding cleanup skipped: {e}")

            if keep_tombstone:
                # TOMBSTONE PATH: parent row stays, so DB cascade won't fire.
                # Explicitly clear every child table (unchanged behavior from T1.9).
                for table, fk_col in [
                    ("task_mentions", "meeting_id"),
                    ("entity_mentions", "meeting_id"),
                    ("topic_thread_mentions", "meeting_id"),
                    ("commitments", "meeting_id"),
                    ("decisions", "meeting_id"),
                    ("follow_up_meetings", "source_meeting_id"),
                    ("open_questions", "meeting_id"),
                    ("tasks", "meeting_id"),
                ]:
                    try:
                        _res = self.client.table(table).delete().eq(fk_col, meeting_id).execute()
                        # Report the ACTUAL number deleted, not the pre-count (audit AD-02).
                        if table in counts:
                            counts[table] = len(_res.data) if _res.data else 0
                    except Exception as e:
                        # A failed child delete is a real integrity concern — surface
                        # it at WARNING, don't swallow at debug as if it succeeded.
                        logger.warning(f"[cascade] {table} delete FAILED for {meeting_id}: {e}")

                # pending_approvals is keyed by approval_id, not FK'd to meetings.id.
                try:
                    self.client.table("pending_approvals").delete().eq(
                        "approval_id", meeting_id
                    ).execute()
                except Exception as e:
                    logger.debug(f"Skipping pending_approvals cleanup: {e}")

                tombstone_updates = {
                    "approval_status": "rejected",
                    "raw_transcript": None,  # free up space
                    "summary": f"[REJECTED at {datetime.now(timezone.utc).isoformat()}]",
                    "approved_at": None,
                }
                self.client.table("meetings").update(tombstone_updates).eq(
                    "id", meeting_id
                ).execute()
                counts["tombstone"] = 1
                logger.info(
                    f"Cascade-cleared meeting {meeting_id} (tombstone kept): "
                    f"{counts['embeddings']} embeddings, "
                    f"{counts['tasks']} tasks, "
                    f"{counts['decisions']} decisions, "
                    f"{counts['open_questions']} questions, "
                    f"{counts['topic_thread_mentions']} topic_mentions"
                )
            else:
                # HARD-DELETE PATH: rely on FK CASCADE (post-T3.2 migration)
                # for all 8 child tables. pending_approvals still needs an
                # explicit delete (not FK'd to meetings.id).
                try:
                    self.client.table("pending_approvals").delete().eq(
                        "approval_id", meeting_id
                    ).execute()
                except Exception as e:
                    logger.debug(f"Skipping pending_approvals cleanup: {e}")

                mtg_result = (
                    self.client.table("meetings")
                    .delete()
                    .eq("id", meeting_id)
                    .execute()
                )
                counts["meetings"] = len(mtg_result.data) if mtg_result.data else 0
                logger.info(
                    f"Cascade-deleted meeting {meeting_id} (FK CASCADE): "
                    f"{counts['embeddings']} embeddings, "
                    f"{counts['tasks']} tasks, "
                    f"{counts['decisions']} decisions, "
                    f"{counts['open_questions']} questions, "
                    f"{counts['topic_thread_mentions']} topic_mentions, "
                    f"{counts['meetings']} meetings"
                )

            # After the mentions are gone, fix the affected threads: recompute
            # meeting_count from the remaining DISTINCT mentions, and drop any
            # thread now down to zero mentions (an orphan from this reject). [audit P1-07]
            dropped_threads = 0
            for tid in affected_topic_ids:
                try:
                    rows = (
                        self.client.table("topic_thread_mentions")
                        .select("meeting_id")
                        .eq("topic_id", tid)
                        .execute()
                        .data
                        or []
                    )
                    distinct = len({r.get("meeting_id") for r in rows if r.get("meeting_id")})
                    if distinct == 0:
                        self.client.table("topic_threads").delete().eq("id", tid).execute()
                        dropped_threads += 1
                    else:
                        self.client.table("topic_threads").update(
                            {"meeting_count": distinct}
                        ).eq("id", tid).execute()
                except Exception as e:
                    logger.debug(f"topic_thread cleanup for {tid} skipped: {e}")
            if dropped_threads:
                logger.info(f"Dropped {dropped_threads} orphan topic thread(s) after cascade")
            counts["orphan_threads_dropped"] = dropped_threads

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
        sensitivity: str | None = None,
    ) -> list[dict]:
        """
        Create multiple decisions in a single batch.

        Args:
            meeting_id: UUID of the source meeting.
            decisions: List of decision dicts with description, context, etc.
            sensitivity: Meeting tier to stamp on each row at insert. [audit P1-01]

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
            # [audit P1-01] Stamp the tier ATOMICALLY at insert so a later
            # propagate_meeting_sensitivity failure can't leave CEO content at
            # the DB default ('normal' -> founders/team-visible).
            if sensitivity:
                row["sensitivity"] = sensitivity
            # Phase 9A: decision intelligence fields
            if d.get("rationale"):
                row["rationale"] = d["rationale"]
            if d.get("options_considered"):
                row["options_considered"] = d["options_considered"]
            if d.get("confidence"):
                row["confidence"] = d["confidence"]
            # Phase 9A: label is part of the core schema — always write,
            # even empty string. NULL in DB breaks downstream filtering by
            # topic label (v2.4 agentic retrieval depends on it).
            row["label"] = d.get("label")
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
        include_pending: bool = False,
        include_superseded: bool = False,
    ) -> list[dict]:
        """
        List decisions with optional filtering.

        Args:
            meeting_id: Filter by source meeting.
            topic: Filter by topic keyword (searches description).
            limit: Maximum number of results.
            include_pending: When False (default), only return approval_status='approved'
                rows — the safe default for public-facing reads. When True, returns ALL
                statuses, not just pending+approved — the parameter is semantically
                "do not filter", not "only pending". Per the CHECK constraint added in
                Tier 3.1, child rows can only be 'pending' or 'approved', so "all" is
                effectively "both". Use True only from the approval flow internals,
                extraction, edit apply, QA scheduler orphan detection, and similar.

        Returns:
            List of decision records.
        """
        query = self.client.table("decisions").select("*, meetings(title, date)")

        # Tier 3.1 narrow: filter to approved by default so every read path
        # that doesn't explicitly opt into pending gets the right behavior.
        if not include_pending:
            query = query.eq("approval_status", "approved")

        # Bi-temporal (v2.5): hide superseded rows by default. See get_tasks note.
        if not include_superseded:
            query = query.is_("valid_to", "null")

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

    def touch_decision(self, decision_id: str) -> None:
        """Update last_referenced_at timestamp on a decision (Phase 12 A4)."""
        from datetime import datetime, timezone

        now = datetime.now(timezone.utc).isoformat()
        try:
            self.client.table("decisions").update(
                {"last_referenced_at": now}
            ).eq("id", decision_id).execute()
        except Exception as e:
            logger.warning(f"Could not touch decision {decision_id}: {e}")

    def get_stale_decisions(self, days: int = 28) -> list[dict]:
        """
        Get active decisions not referenced in the last N days (Phase 12 A4).

        Returns decisions that either:
        - Have last_referenced_at older than N days ago
        - Have never been referenced (last_referenced_at is null) AND were
          created more than N days ago
        """
        from datetime import datetime, timedelta, timezone

        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()

        # Decisions referenced but stale
        stale_referenced = (
            self.client.table("decisions")
            .select("*, meetings(title, date)")
            .eq("decision_status", "active")
            .not_.is_("last_referenced_at", "null")
            .lt("last_referenced_at", cutoff)
            .order("last_referenced_at")
            .limit(20)
            .execute()
        )

        # Decisions never referenced and old enough
        never_referenced = (
            self.client.table("decisions")
            .select("*, meetings(title, date)")
            .eq("decision_status", "active")
            .is_("last_referenced_at", "null")
            .lt("created_at", cutoff)
            .order("created_at")
            .limit(20)
            .execute()
        )

        results = (stale_referenced.data or []) + (never_referenced.data or [])
        return results[:20]

    def get_decision(self, decision_id: str) -> dict | None:
        """Fetch a single decision by id (all columns). None if absent."""
        try:
            res = (
                self.client.table("decisions")
                .select("*")
                .eq("id", decision_id)
                .limit(1)
                .execute()
            )
            return res.data[0] if res.data else None
        except Exception as e:
            logger.error(f"Error getting decision {decision_id}: {e}")
            return None

    def mark_decision_superseded(self, old_id: str, new_id: str) -> None:
        """Mark an old decision as superseded by a new one."""
        self.client.table("decisions").update({
            "decision_status": "superseded",
            "superseded_by": new_id,
        }).eq("id", old_id).execute()
        logger.info(f"Decision {old_id} superseded by {new_id}")

    def set_decision_parent(self, decision_id: str, parent_id: str) -> None:
        """Set parent_decision_id for decision chain traversal (Phase 12 A6)."""
        try:
            self.client.table("decisions").update(
                {"parent_decision_id": parent_id}
            ).eq("id", decision_id).execute()
        except Exception as e:
            logger.warning(f"Could not set parent for decision {decision_id}: {e}")

    def get_decisions_by_ids(self, ids: list[str]) -> dict[str, dict]:
        """
        Batch-fetch decisions by id → {id: {description, date, sensitivity}}.

        One round-trip (vs get_decision_chain per id). `date` comes from the
        joined meeting. Used by the meeting-summary supersession clause. Never
        raises — returns {} on error.
        """
        if not ids:
            return {}
        try:
            result = (
                self.client.table("decisions")
                .select("id, description, sensitivity, meetings(date)")
                .in_("id", list(ids))
                .execute()
            )
        except Exception as e:
            logger.warning(f"get_decisions_by_ids failed: {e}")
            return {}
        out: dict[str, dict] = {}
        for row in (result.data or []):
            meeting = row.get("meetings")
            if isinstance(meeting, list):
                meeting = meeting[0] if meeting else {}
            meeting = meeting or {}
            out[row["id"]] = {
                "description": row.get("description", ""),
                "date": meeting.get("date"),
                "sensitivity": row.get("sensitivity"),
            }
        return out

    def get_decision_chain(self, decision_id: str) -> list[dict]:
        """
        Traverse the decision chain — ancestors and descendants (Phase 12 A6).

        Walks up via parent_decision_id and down via superseded_by to build
        the full evolution chain of a decision.

        Args:
            decision_id: Starting decision UUID.

        Returns:
            List of decisions in chronological order (oldest first).
        """
        visited = set()
        chain = []

        # Walk up (ancestors via parent_decision_id)
        current_id = decision_id
        ancestors = []
        for _ in range(10):  # Max depth guard
            if not current_id or current_id in visited:
                break
            visited.add(current_id)
            try:
                result = self.client.table("decisions").select(
                    "*, meetings(title, date)"
                ).eq("id", current_id).execute()
                if not result.data:
                    break
                record = result.data[0]
                ancestors.append(record)
                current_id = record.get("parent_decision_id")
            except Exception as e:
                logger.warning(f"Decision chain walk-up failed at {current_id}: {e}")
                break

        # Ancestors are collected child→parent, reverse for chrono order
        ancestors.reverse()
        chain.extend(ancestors)

        # Walk down (descendants via superseded_by)
        current_id = decision_id
        for _ in range(10):  # Max depth guard
            if not current_id:
                break
            try:
                # Find decisions that have this as parent
                result = self.client.table("decisions").select(
                    "*, meetings(title, date)"
                ).eq("parent_decision_id", current_id).execute()
                if not result.data:
                    break
                for child in result.data:
                    child_id = child.get("id")
                    if child_id and child_id not in visited:
                        visited.add(child_id)
                        chain.append(child)
                        current_id = child_id
                        break
                else:
                    break
            except Exception as e:
                logger.warning(f"Decision chain walk-down failed at {current_id}: {e}")
                break

        return chain

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
        deadline_confidence: str = "NONE",
        urgency: str = "M",
        label: str | None = None,
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
            category: Task category — canonicalized HERE against the Gantt-area
                taxonomy (resolve_category), so every creation path (MCP,
                Telegram agent, debrief, sheets, scripts) stores one taxonomy
                no matter what the caller passed.
            deadline_confidence: 'EXPLICIT' | 'INFERRED' | 'NONE'. Controls
                whether reminders + proactive alerts fire. Default 'NONE' means
                the task has no deadline at all. Callers that set a deadline
                must also set this to 'EXPLICIT' or 'INFERRED'.
            urgency: 'H' | 'M' | 'L' — time-pressure, SEPARATE from priority
                (importance). Never implies a deadline.

        Returns:
            Created task record.
        """
        assignee = self.resolve_assignee(assignee)
        data = {
            "title": title,
            "assignee": assignee,
            "priority": priority,
            "deadline": self._serialize_datetime(deadline),
            "meeting_id": meeting_id,
            "transcript_timestamp": transcript_timestamp,
            "status": status,
            "category": self.resolve_category(category),
            "deadline_confidence": deadline_confidence,
            "urgency": urgency,
            "label": label,
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
        sensitivity: str | None = None,
    ) -> list[dict]:
        """
        Create multiple tasks in a single batch.

        Args:
            meeting_id: UUID of the source meeting.
            tasks: List of task dicts with title, assignee, etc.
            sensitivity: Meeting tier to stamp on each row at insert. [audit P1-01]

        Returns:
            List of created task records.
        """
        # Filter out tasks missing title (assignee can be empty — unassigned tasks are valid)
        valid_tasks = [t for t in tasks if t.get("title")]
        if not valid_tasks:
            logger.info("No valid tasks to insert after filtering")
            return []

        # Canonicalize categories at this choke point (one areas fetch per batch)
        # so every batch-creation path stores the Gantt-area taxonomy.
        _areas = self.get_areas()
        # Same for assignees — one roster fetch per batch (2026-07-22).
        _roster = self.list_team_members()

        def _row(t: dict) -> dict:
            raw_deadline = t.get("deadline")
            ser_deadline = self._serialize_datetime(raw_deadline)
            conf = t.get("deadline_confidence", "NONE")
            # An extraction deadline emitted as vague text ("end of July 2026")
            # serializes to None. Don't let it land NULL-but-EXPLICIT (a
            # contradictory state the reminder-confidence filter mis-handles):
            # force NONE so the task surfaces cleanly in get_tasks_without_deadline
            # for the daily-QA gap-fill, and log it distinctly. [audit P1-08]
            if raw_deadline and ser_deadline is None:
                logger.warning(
                    f"create_tasks_batch: dropped unparseable deadline "
                    f"{raw_deadline!r} on task {t.get('title')!r} "
                    f"(meeting {meeting_id}) — flagged for QA gap-fill"
                )
                conf = "NONE"
            return {
                "meeting_id": meeting_id,
                "title": t.get("title"),
                "assignee": self.resolve_assignee(t.get("assignee", ""), roster=_roster),
                "priority": t.get("priority", "M"),
                "deadline": ser_deadline,
                "transcript_timestamp": t.get("transcript_timestamp"),
                "status": "pending",
                "category": self.resolve_category(t.get("category"), areas=_areas),
                "label": t.get("label"),
                "deadline_confidence": conf,
                "urgency": t.get("urgency", "M"),
            }

        data = [_row(t) for t in valid_tasks]

        # [audit P1-01] Stamp the tier ATOMICALLY at insert (belt; propagate is
        # the suspenders) so a propagate failure can't leave CEO tasks team-visible.
        if sensitivity:
            for _row in data:
                _row["sensitivity"] = sensitivity

        # v2.3.1 label-coverage audit — surface the Franciacorta bug (task
        # rows landing with label=NULL despite topic_threading seeing them).
        # Warn so we can grep logs on the next production run.
        labeled = sum(1 for d in data if (d.get("label") or "").strip())
        if labeled < len(data):
            logger.warning(
                f"create_tasks_batch: {len(data) - labeled}/{len(data)} tasks "
                f"have NULL/empty label (meeting {meeting_id}). Upstream drop?"
            )

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
        include_pending: bool = False,
        include_superseded: bool = False,
        include_archived: bool = False,
        meeting_id: str | None = None,
    ) -> list[dict]:
        """
        Get tasks with optional filtering.

        Args:
            meeting_id: Filter to one meeting's tasks, SERVER-SIDE. Always use
                this instead of fetching everything and filtering the result in
                Python — `limit` applies before a client-side filter would run,
                so the meeting's rows can fall outside the returned window
                entirely and silently yield []. That is exactly what blanked a
                distributed summary on 2026-07-17: ordering is by deadline ASC,
                NULL deadlines sort last, so a fresh extraction's tasks were
                never inside the first 100 of 396 rows.
            assignee: Filter by assignee name.
            status: Filter by status ('pending', 'in_progress', 'done', 'overdue',
                'archived'). Passing 'archived' explicitly returns archived tasks
                regardless of include_archived.
            category: Filter by task category (e.g., 'Product & Tech').
            include_overdue: Include overdue tasks when filtering by status.
            limit: Maximum number of results.
            include_pending: When False (default), only return approval_status='approved'
                rows — the safe default for public-facing reads. When True, returns ALL
                statuses, not just pending+approved — the parameter is semantically
                "do not filter", not "only pending". Per the CHECK constraint added in
                Tier 3.1, child rows can only be 'pending' or 'approved', so "all" is
                effectively "both". Use True only from the approval flow internals,
                extraction, edit apply, QA scheduler orphan detection, and similar.
            include_archived: When False (default) and no status filter is given,
                archived tasks (sanctioned removals) are excluded — they should
                never surface in briefs/digests/sync views. The reconcile engine
                passes True (it must see archived rows to move them off the sheet).

        Returns:
            List of task records.
        """
        query = self.client.table("tasks").select("*, meetings(title, date)")

        if meeting_id:
            query = query.eq("meeting_id", meeting_id)

        # Tier 3.1 narrow: filter to approved by default so every read path
        # that doesn't explicitly opt into pending gets the right behavior.
        if not include_pending:
            query = query.eq("approval_status", "approved")

        # Bi-temporal (v2.5): hide superseded rows by default. Columns added in
        # scripts/migrate_phase_v25_knowledge.sql; default-open (valid_to NULL) so
        # all existing rows pass. Requires that migration to be applied first.
        if not include_superseded:
            query = query.is_("valid_to", "null")

        if assignee:
            query = query.ilike("assignee", assignee)

        if status:
            if include_overdue and status in ("pending", "in_progress"):
                query = query.in_("status", [status, "overdue"])
            else:
                query = query.eq("status", status)
        elif not include_archived:
            # No status filter -> "everything in the working set". Archived
            # tasks are removals, not work — exclude unless explicitly asked.
            query = query.neq("status", "archived")

        if category:
            query = query.eq("category", category)

        result = query.order("deadline", desc=False).limit(limit).execute()
        return result.data

    def get_tasks_without_assignee(self, limit: int = 50) -> list[dict]:
        """Get open tasks with empty or null assignee."""
        # approved-only: QA gap-fill must not surface pending-extraction tasks. [audit P3-15]
        result = (
            self.client.table("tasks")
            .select("*, meetings(title, date)")
            .in_("status", ["pending", "in_progress", "overdue"])
            .eq("approval_status", "approved")
            .or_("assignee.is.null,assignee.eq.")
            .order("created_at", desc=True)
            .limit(limit)
            .execute()
        )
        return result.data or []

    def get_tasks_without_deadline(self, limit: int = 50) -> list[dict]:
        """Get open tasks with no deadline set."""
        # approved-only: QA gap-fill must not surface pending-extraction tasks. [audit P3-15]
        result = (
            self.client.table("tasks")
            .select("*, meetings(title, date)")
            .in_("status", ["pending", "in_progress", "overdue"])
            .eq("approval_status", "approved")
            .is_("deadline", "null")
            .order("created_at", desc=True)
            .limit(limit)
            .execute()
        )
        return result.data or []

    def get_task(self, task_id: str) -> dict | None:
        """Fetch a single task by id (all columns, incl. manual_* flags). None if absent."""
        try:
            res = (
                self.client.table("tasks")
                .select("*")
                .eq("id", task_id)
                .limit(1)
                .execute()
            )
            return res.data[0] if res.data else None
        except Exception as e:
            logger.error(f"Error getting task {task_id}: {e}")
            return None

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

        Raises:
            ValueError: If the task does not exist or the update did not hit
                any row. Callers must be prepared to catch this (all existing
                callers are inside try/except blocks — see T1.2 in the plan).
        """
        updates = {**other_updates}
        if status is not None:
            updates["status"] = status
        if deadline is not None:
            serialized = self._serialize_datetime(deadline)
            if serialized is None:
                # The caller PROVIDED a deadline but it didn't parse. Writing
                # NULL here would erase a real deadline from garbage input
                # (the 2026-06-11 incident class) — drop the field instead.
                logger.warning(
                    f"update_task {task_id}: unparseable deadline "
                    f"{deadline!r} — deadline left unchanged"
                )
            else:
                updates["deadline"] = serialized
        # Category carries the Gantt-area taxonomy — canonicalize at this
        # choke point so every update path stores one taxonomy.
        #
        # Membership, NOT truthiness: the old `if updates.get("category")` let
        # category="" skip canonicalization AND still be written, so an edit
        # whose LLM output omitted the field silently BLANKED a task's area —
        # and the QA check couldn't flag it, because it skips empty values.
        # resolve_category("") correctly returns 'General'. [2026-07-22]
        if "category" in updates:
            updates["category"] = self.resolve_category(updates["category"])
        if "assignee" in updates:
            updates["assignee"] = self.resolve_assignee(updates["assignee"])
        updates["updated_at"] = datetime.now(timezone.utc).isoformat()

        result = (
            self.client.table("tasks")
            .update(updates)
            .eq("id", task_id)
            .execute()
        )
        if not result.data:
            raise ValueError(f"Task {task_id} not found or not updated")
        logger.info(f"Updated task {task_id}: {list(updates.keys())}")
        return result.data[0]

    def log_approval_observation(
        self,
        content_type: str,
        action: str,
        content_id: str | None = None,
        original_content: dict | None = None,
        final_content: dict | None = None,
        context: dict | None = None,
    ) -> None:
        """
        Log a single approval-decision observation.

        Fire-and-forget: never raises, never interrupts the calling flow. The
        approval flow (meetings, Gantt proposals, intelligence signals, meeting
        prep, quick inject, sheets sync, deadline edits) calls this AFTER its
        primary action succeeds. A DB failure here must not roll back the
        approval — observations are strictly non-critical telemetry.

        Args:
            content_type: 'meeting_summary' | 'gantt_proposal' |
                'intelligence_signal' | 'meeting_prep' | 'sheets_sync' |
                'quick_inject' | 'deadline_update' | ... (open set)
            action: 'approved' | 'edited' | 'rejected'. Enforced by the
                CHECK constraint on the table — pass anything else and the
                insert fails (logged as warning, not raised).
            content_id: UUID of the underlying record when one exists.
                Polymorphic across tables (no FK), so pass the id of the
                meeting / proposal / signal / prep that was decided on.
            original_content: What Gianluigi proposed. For 'edited' actions,
                this is what the edit distance is computed against.
            final_content: What Eyal accepted (None for 'rejected').
            context: Free-bag metadata — meeting_title, sensitivity,
                item_count, etc. Queried later for pattern analysis.

        edit_distance_pct is computed automatically when both original and
        final content are provided and action=='edited'. It's a
        character-level 1 - SequenceMatcher ratio (0.0 = identical,
        1.0 = completely different), useful for spotting which content types
        get heavy-handed edits vs. minor tweaks.
        """
        edit_distance_pct = None
        if original_content is not None and final_content is not None and action == "edited":
            from difflib import SequenceMatcher
            orig_str = str(original_content)
            final_str = str(final_content)
            if orig_str:
                ratio = SequenceMatcher(None, orig_str, final_str).ratio()
                edit_distance_pct = round(1.0 - ratio, 3)

        try:
            self.client.table("approval_observations").insert({
                "content_type": content_type,
                "action": action,
                "content_id": content_id,
                "original_content": original_content,
                "final_content": final_content,
                "edit_distance_pct": edit_distance_pct,
                "context": context or {},
            }).execute()
        except Exception as e:
            logger.warning(
                f"[observation] failed to log {content_type}/{action}: {e}"
            )
            # Never propagate — observations are telemetry, not load-bearing.

    def update_task_deadline(
        self,
        task_id: str,
        deadline: date | None,
        confidence: str = "EXPLICIT",
    ) -> dict:
        """
        Update a task's deadline with explicit confidence tagging.

        Used by Telegram inline buttons and any manual deadline edit where
        the user actively chose the new date — always defaults to 'EXPLICIT'
        since the choice is deliberate. Clearing a deadline (deadline=None)
        should set confidence='NONE'.

        Args:
            task_id: UUID of the task to update.
            deadline: New deadline, or None to clear.
            confidence: 'EXPLICIT' | 'INFERRED' | 'NONE'. Default 'EXPLICIT'.

        Returns:
            Updated task record.

        Raises:
            ValueError: If the task does not exist. Callers must catch.
        """
        # Clearing the deadline must clear the confidence too — leaving a stale
        # 'EXPLICIT' on a NULL deadline is contradictory and can make the reminder
        # scheduler treat a deadline-less task as reminder-eligible. [audit P3-16]
        if deadline is None:
            confidence = "NONE"
        updates = {
            "deadline": self._serialize_datetime(deadline),
            "deadline_confidence": confidence,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        result = (
            self.client.table("tasks")
            .update(updates)
            .eq("id", task_id)
            .eq("approval_status", "approved")
            .execute()
        )
        if not result.data:
            raise ValueError(f"Task {task_id} not found or not approved")
        logger.info(f"Updated task {task_id} deadline: {deadline} ({confidence})")
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
        sensitivity: str | None = None,
    ) -> list[dict]:
        """
        Create multiple follow-up meetings in a single batch.

        Args:
            source_meeting_id: UUID of the source meeting.
            follow_ups: List of follow-up meeting dicts.
            sensitivity: Meeting tier to stamp on each row at insert. [audit P1-05]
                Only pass once the follow_up_meetings.sensitivity column exists
                (gated by FOLLOW_UP_SENSITIVITY_ENABLED) — writing an unknown
                column would reject the whole insert.

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

        # [audit P1-05] Stamp the tier atomically at insert (mirrors P1-01 for the
        # other 3 child tables) so a propagate failure can't leave a CEO meeting's
        # follow-up team-visible.
        if sensitivity:
            for _row in data:
                _row["sensitivity"] = sensitivity

        result = self.client.table("follow_up_meetings").insert(data).execute()
        logger.info(f"Created {len(result.data)} follow-up meetings")
        return result.data

    def list_follow_up_meetings(
        self,
        source_meeting_id: str | None = None,
        limit: int = 50,
        include_pending: bool = False,
    ) -> list[dict]:
        """
        List follow-up meetings.

        Args:
            source_meeting_id: Filter by source meeting.
            limit: Maximum number of results.
            include_pending: When False (default), only return approval_status='approved'
                rows — the safe default for public-facing reads. When True, returns ALL
                statuses, not just pending+approved — the parameter is semantically
                "do not filter", not "only pending". Per the CHECK constraint added in
                Tier 3.1, child rows can only be 'pending' or 'approved', so "all" is
                effectively "both". Use True only from the approval flow internals,
                extraction, edit apply, QA scheduler orphan detection, and similar.

        Returns:
            List of follow-up meeting records.
        """
        query = self.client.table("follow_up_meetings").select(
            "*, meetings(title, date)"
        )

        # Tier 3.1 narrow: filter to approved by default.
        if not include_pending:
            query = query.eq("approval_status", "approved")

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
        sensitivity: str | None = None,
    ) -> list[dict]:
        """
        Create multiple open questions in a single batch.

        Args:
            meeting_id: UUID of the source meeting.
            questions: List of question dicts with question, raised_by.
            sensitivity: Meeting tier to stamp on each row at insert. [audit P1-01]

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

        # [audit P1-01] Stamp the tier ATOMICALLY at insert (belt; propagate is
        # the suspenders) so a propagate failure can't leave CEO questions visible.
        if sensitivity:
            for _row in data:
                _row["sensitivity"] = sensitivity

        result = self.client.table("open_questions").insert(data).execute()
        logger.info(f"Created {len(result.data)} open questions")
        return result.data

    def get_open_questions(
        self,
        status: str = "open",
        meeting_id: str | None = None,
        limit: int = 100,
        include_pending: bool = False,
    ) -> list[dict]:
        """
        Get open questions by status.

        Args:
            status: Filter by status ('open' or 'resolved'). Note: this is the
                question lifecycle status, not the approval_status.
            meeting_id: Filter by source meeting.
            limit: Maximum number of results.
            include_pending: When False (default), only return approval_status='approved'
                rows — the safe default for public-facing reads. When True, returns ALL
                statuses, not just pending+approved — the parameter is semantically
                "do not filter", not "only pending". Per the CHECK constraint added in
                Tier 3.1, child rows can only be 'pending' or 'approved', so "all" is
                effectively "both". Use True only from the approval flow internals,
                extraction, edit apply, QA scheduler orphan detection, and similar.

        Returns:
            List of open question records.
        """
        # Disambiguate the meetings join — open_questions has two FKs to meetings
        query = self.client.table("open_questions").select(
            "*, meetings!open_questions_meeting_id_fkey(title, date)"
        )

        # Tier 3.1 narrow: filter to approved by default.
        if not include_pending:
            query = query.eq("approval_status", "approved")

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
        content_hash: str | None = None,
        version: int = 1,
        sensitivity: str | None = None,
    ) -> dict:
        """
        Create a document record.

        Args:
            title: Document title.
            source: 'upload', 'email', or 'drive'.
            file_type: File extension (pdf, docx, etc.).
            summary: Document summary.
            drive_path: Google Drive path.
            document_type: Classification category.
            content_hash: SHA-256 hash for dedup (Phase 13 B2).
            version: Document version number (Phase 13 B2).
            sensitivity: Tier (audit P1-09). Only written when provided, so the
                column write stays dark until the migration is applied and
                DOCUMENT_SENSITIVITY_ENABLED is flipped on at the caller.

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
            "version": version,
        }
        if content_hash:
            data["content_hash"] = content_hash
        # Gated: omitted (no column write) unless the caller passes a tier, so a
        # deploy before the migration can't hit a missing column. [audit P1-09]
        if sensitivity:
            data["sensitivity"] = sensitivity

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
                # RPC param is `source_filter` — the old `filter_source_type`
                # silently 404'd the RPC (PGRST202), so search_memory's fulltext
                # half never ran and hybrid search was vector-only. [2026-07-14]
                "source_filter": source_type,
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
    # Task Signals (Phase 12 A5)
    # =========================================================================

    def create_task_signal(
        self,
        task_id: str,
        signal_type: str,
        signal_source: str | None = None,
        confidence: str = "medium",
        details: dict | None = None,
    ) -> dict:
        """
        Record a task completion/progress signal from an external source.

        Args:
            task_id: UUID of the related task.
            signal_type: Type of signal (e.g., 'completion', 'progress',
                        'impediment', 'deadline_change').
            signal_source: Source system (e.g., 'email', 'gantt', 'calendar').
            confidence: Signal confidence ('high', 'medium', 'low').
            details: Additional context as JSON.

        Returns:
            Created task_signal record.
        """
        record = {
            "task_id": task_id,
            "signal_type": signal_type,
            "signal_source": signal_source,
            "confidence": confidence,
            "details": details or {},
        }
        try:
            result = self.client.table("task_signals").insert(record).execute()
            if result.data:
                logger.info(
                    f"Created task signal: {signal_type} for task {task_id} "
                    f"(source={signal_source}, confidence={confidence})"
                )
                return result.data[0]
        except Exception as e:
            logger.warning(f"Could not create task signal for {task_id}: {e}")
        return {}

    def get_task_signals(
        self,
        task_id: str | None = None,
        signal_type: str | None = None,
        signal_source: str | None = None,
        limit: int = 50,
    ) -> list[dict]:
        """
        Query task signals with optional filters.

        Args:
            task_id: Filter by task UUID.
            signal_type: Filter by signal type.
            signal_source: Filter by source system.
            limit: Maximum results.

        Returns:
            List of task_signal records.
        """
        query = self.client.table("task_signals").select("*")

        if task_id:
            query = query.eq("task_id", task_id)
        if signal_type:
            query = query.eq("signal_type", signal_type)
        if signal_source:
            query = query.eq("signal_source", signal_source)

        result = query.order("detected_at", desc=True).limit(limit).execute()
        return result.data or []

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

    def create_task_update_proposal(
        self,
        task_id: str,
        field: str,
        proposed,
        title: str = "",
        current=None,
        source: str = "inference",
    ) -> bool:
        """Propose a change to a manually-set (sticky) task field instead of clobbering it.

        Phase 1 Step 3 (propose-don't-clobber). Emits a 'task_update_proposal'
        pending_approval consumed by decide_proposal (Claude.ai), the /sync review
        (Telegram), and surfaced in the morning brief. Idempotent per (task, field):
        a deterministic approval_id + a pre-check means re-running inference over the
        same field won't stack duplicate cards. Returns True if a proposal now exists.
        """
        approval_id = f"taskprop-{task_id}-{field}"
        try:
            existing = (
                self.client.table("pending_approvals")
                .select("approval_id")
                .eq("approval_id", approval_id)
                .eq("status", "pending")
                .execute()
                .data
            )
            if existing:
                return True  # one already open for this task+field — don't stack
            self.create_pending_approval(
                approval_id=approval_id,
                content_type="task_update_proposal",
                content={
                    "task_id": task_id,
                    "field": field,
                    "proposed": proposed,
                    "current": current,
                    "title": title,
                    "source": source,
                },
            )
            return True
        except Exception as e:
            logger.error(f"Error creating task_update_proposal ({task_id}.{field}): {e}")
            return False

    def create_decision_update_proposal(
        self,
        decision_id: str,
        field: str,
        proposed,
        summary: str = "",
        current=None,
        source: str = "inference",
    ) -> bool:
        """Propose a change to a manually-set (sticky) decision field vs clobbering it.

        Phase 2 PR C (propose-don't-clobber for decisions). Mirrors
        create_task_update_proposal. `field` is a decision manual-flag name
        (description/label/rationale/confidence/status). Emits a
        'decision_update_proposal' consumed by decide_proposal (Claude.ai) + the
        /sync review (Telegram). Idempotent per (decision, field) via a
        deterministic approval_id + pre-check. Returns True if a proposal exists.
        """
        approval_id = f"decupd-{decision_id}-{field}"
        try:
            existing = (
                self.client.table("pending_approvals")
                .select("approval_id")
                .eq("approval_id", approval_id)
                .eq("status", "pending")
                .execute()
                .data
            )
            if existing:
                return True  # one already open for this decision+field — don't stack
            self.create_pending_approval(
                approval_id=approval_id,
                content_type="decision_update_proposal",
                content={
                    "decision_id": decision_id,
                    "field": field,
                    "proposed": proposed,
                    "current": current,
                    "summary": summary,
                    "source": source,
                },
            )
            return True
        except Exception as e:
            logger.error(f"Error creating decision_update_proposal ({decision_id}.{field}): {e}")
            return False

    def create_decision_supersede_proposal(
        self,
        new_id: str,
        old_id: str,
        new_summary: str = "",
        old_summary: str = "",
        source: str = "inference",
    ) -> bool:
        """Propose marking an old decision superseded by a newer one (Phase 2).

        Emits a 'decision_supersede_proposal' pending_approval for Eyal's review —
        never auto-flips (I1). Consumed by decide_proposal (Claude.ai) and the /sync
        review (Telegram). Idempotent per (old, new): a deterministic approval_id +
        a pre-check so re-approving the same meeting won't stack duplicate cards.
        Returns True if a proposal now exists.
        """
        approval_id = f"decprop-{old_id}-{new_id}"
        try:
            existing = (
                self.client.table("pending_approvals")
                .select("approval_id")
                .eq("approval_id", approval_id)
                .eq("status", "pending")
                .execute()
                .data
            )
            if existing:
                return True
            self.create_pending_approval(
                approval_id=approval_id,
                content_type="decision_supersede_proposal",
                content={
                    "old_decision_id": old_id,
                    "new_decision_id": new_id,
                    "old_summary": old_summary,
                    "new_summary": new_summary,
                    "source": source,
                },
            )
            return True
        except Exception as e:
            logger.error(f"Error creating decision_supersede_proposal ({old_id}->{new_id}): {e}")
            return False

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

    def get_card_message_ids(self, approval_id: str) -> list[int]:
        """Telegram approval-card message-ids persisted for this approval.

        [robustness #5/#6, 2026-07-12] Stored inside the pending_approvals
        content JSON (no migration) so the "delete prior cards before sending a
        new one" cleanup survives a Cloud Run restart (which drops the bot's
        in-memory map) AND covers out-of-band resends. Best-effort — returns []
        on any miss so a read failure can never block a card send.
        """
        try:
            row = self.get_pending_approval(approval_id)
            if not row:
                return []
            ids = (row.get("content") or {}).get("_card_message_ids") or []
            return [int(x) for x in ids]
        except Exception as e:
            logger.warning(f"get_card_message_ids failed for {approval_id}: {e}")
            return []

    def set_card_message_ids(self, approval_id: str, message_ids: list[int]) -> None:
        """Replace the persisted approval-card message-ids for this approval.

        Merges into the existing content JSON (never clobbers the meeting
        content). Best-effort — a failure here must never break the card send.
        """
        try:
            row = self.get_pending_approval(approval_id)
            if not row:
                return
            content = dict(row.get("content") or {})
            content["_card_message_ids"] = [int(x) for x in message_ids]
            self.update_pending_approval(approval_id, content=content)
        except Exception as e:
            logger.warning(f"set_card_message_ids failed for {approval_id}: {e}")

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

    def upsert_pending_approval(
        self,
        approval_id: str,
        content_type: str,
        content: dict,
        auto_publish_at: str | None = None,
        expires_at: str | None = None,
    ) -> dict:
        """
        Atomically create or update a pending approval record.

        Uses Supabase upsert (ON CONFLICT approval_id DO UPDATE) to avoid
        the race condition window between delete + create.

        Args:
            approval_id: Meeting UUID or prefixed ID.
            content_type: 'meeting_summary', 'meeting_prep', etc.
            content: Full content dict (stored as JSONB).
            auto_publish_at: ISO timestamp for auto-publish, or None.
            expires_at: ISO timestamp for expiry, or None.

        Returns:
            Upserted pending approval record.
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

        result = (
            self.client.table("pending_approvals")
            .upsert(data, on_conflict="approval_id")
            .execute()
        )
        logger.info(f"Upserted pending approval: {approval_id} ({content_type})")
        return result.data[0] if result.data else {}

    def get_pending_approvals_by_status(
        self, status: str = "pending", limit: int = 200
    ) -> list[dict]:
        """
        Get all pending approvals with a given status, newest first.

        Originally written to find the most recent pending approval when Eyal
        types 'approve'/'reject' as free text, and hard-capped at 5. It now also
        backs MCP `get_proposals` and topic_clustering's dedupe set, where a
        cap of 5 is actively wrong: get_proposals filters by TYPE *after* this
        truncation, so `type="task"` could return [] while task proposals
        existed, and the clustering dedupe would re-propose merges already
        pending. Default raised to 200; the free-text caller only reads [0].
        [2026-07-22]

        Returns:
            List of pending approval records.
        """
        result = (
            self.client.table("pending_approvals")
            .select("*")
            .eq("status", status)
            .order("created_at", desc=True)
            .limit(limit)
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
        # try/except + `or []` so a transient Supabase blip during cold-start
        # reconstruction can't raise out of a fire-and-forget reconstruct task
        # and silently leave auto-publish timers un-rebuilt for the instance's
        # life. Mirrors get_signals_by_status. [audit P3-09]
        try:
            result = (
                self.client.table("pending_approvals")
                .select("*")
                .eq("status", "pending")
                .not_.is_("auto_publish_at", "null")
                .execute()
            )
            return result.data or []
        except Exception as e:
            logger.warning(f"get_pending_auto_publishes failed: {e}")
            return []

    def clear_auto_publish_at(self, approval_id: str) -> bool:
        """Disarm a persisted auto-publish timer (set auto_publish_at = NULL).

        Used to enforce the manual-approval gate: when APPROVAL_MODE is not
        'auto_review', any leftover auto_publish_at from a previous auto_review
        window must be cleared so a restart-time reconstruct can never fire it.
        """
        try:
            self.client.table("pending_approvals").update(
                {"auto_publish_at": None}
            ).eq("approval_id", approval_id).execute()
            return True
        except Exception as e:
            logger.warning(f"clear_auto_publish_at failed for {approval_id}: {e}")
            return False

    def get_signals_by_status(self, status: str) -> list[dict]:
        """
        Get all intelligence_signals rows in a given status.

        Used on startup (and the daily QA backstop) to reconstruct restart-safe
        finalize→distribute jobs for signals left in 'approved_finalizing' by a
        Cloud Run cycle. Mirrors get_pending_auto_publishes.
        """
        result = (
            self.client.table("intelligence_signals")
            .select("*")
            .eq("status", status)
            .execute()
        )
        return result.data or []

    def get_pending_approvals_for_reminders(self) -> list[dict]:
        """
        Get ALL pending approvals (no limit), newest first.

        Used on startup to reconstruct approval-reminder timers — unlike
        get_pending_approvals_by_status (capped at 5 for the free-text approve
        lookup), reconstruction must see every pending item to reschedule its
        remaining reminders.

        Returns:
            List of pending approval records.
        """
        # try/except + `or []` so a cold-start blip can't kill approval-reminder
        # reconstruction. [audit P3-09]
        try:
            result = (
                self.client.table("pending_approvals")
                .select("*")
                .eq("status", "pending")
                .order("created_at", desc=True)
                .execute()
            )
            return result.data or []
        except Exception as e:
            logger.warning(f"get_pending_approvals_for_reminders failed: {e}")
            return []

    def expire_pending_approvals(self) -> list[dict]:
        """
        Expire pending approvals whose expires_at is in the past.

        Updates status to 'expired' and returns the expired rows.

        Returns:
            List of expired approval records.
        """
        now = datetime.now().isoformat()
        try:
            # Single atomic UPDATE…WHERE (returns the updated rows) instead of a
            # select-then-per-row-update loop — a crash mid-loop used to leave
            # some conceptually-expired rows still 'pending' (one stray reminder
            # each). [audit P3-18]
            result = (
                self.client.table("pending_approvals")
                .update({"status": "expired"})
                .eq("status", "pending")
                .not_.is_("expires_at", "null")
                .lt("expires_at", now)
                .execute()
            )
            expired = result.data or []
            if expired:
                logger.info(f"Expired {len(expired)} pending approval(s)")
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

        # Defensive: an audit-insert blip must not raise out of a caller that
        # logs AFTER its primary write (e.g. create_deal) — that would make the
        # caller treat the whole op as failed and duplicate-create on retry.
        # mcp_auth.log_call already wraps this; inline DB-layer callers didn't. [audit P3-10]
        try:
            result = self.client.table("audit_log").insert(data).execute()
            return result.data[0] if result.data else {}
        except Exception as e:
            logger.warning(f"log_action insert failed (non-fatal): {e}")
            return {}

    def get_recent_prep_pings(self, days: int = 2) -> list[dict]:
        """Recent 'prep_ping_sent' audit rows → restart-safe fire-once state.

        Returns the `details` dicts ({event_id, event_start}) of pings sent in the
        last `days`, so the prep-ping scheduler can rebuild its _pinged set on boot
        without double-pinging. SYNC; never raises (returns [] on error).
        """
        from datetime import datetime, timezone, timedelta

        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        try:
            rows = (
                self.client.table("audit_log")
                .select("details")
                .eq("action", "prep_ping_sent")
                .gte("created_at", cutoff)
                .execute()
                .data
                or []
            )
        except Exception as e:
            logger.warning(f"get_recent_prep_pings failed: {e}")
            return []
        return [r.get("details") or {} for r in rows]

    # =====================================================================
    # Morning-brief engagement feedback (v2.5 Phase 3 PR3) — all SYNC.
    # Restart-safe: every callback resolves from the row by brief_id.
    # =====================================================================

    def create_brief_feedback_row(
        self,
        base_brief_id: str,
        brief_date: str | None = None,
        variant: str = "primary",
        section_count: int | None = None,
    ) -> str:
        """Create the feedback row at send time; return the brief_id actually used.

        Appends a -N suffix if a row already exists for the same base id (a
        same-day regenerate), so a second brief never overwrites the first's vote.
        """
        # Best-effort: the feedback row is a nice-to-have (it powers 👍/👎). A DB
        # blip here must NOT suppress the brief itself — return the computed
        # brief_id regardless so the caller can still send. [audit P3-18]
        try:
            existing = (
                self.client.table("morning_brief_feedback")
                .select("brief_id")
                .like("brief_id", f"{base_brief_id}%")
                .execute()
                .data
                or []
            )
            ids = {r["brief_id"] for r in existing}
            brief_id = base_brief_id
            n = 2
            while brief_id in ids:
                brief_id = f"{base_brief_id}-{n}"
                n += 1
            self.client.table("morning_brief_feedback").insert(
                {
                    "brief_id": brief_id,
                    "brief_date": brief_date,
                    "variant": variant,
                    "section_count": section_count,
                    "vote": None,
                }
            ).execute()
            return brief_id
        except Exception as e:
            logger.error(f"Could not create brief feedback row for {base_brief_id}: {e}")
            return base_brief_id

    def set_brief_feedback_vote(
        self, brief_id: str, vote: str, pending_noise: bool = False
    ) -> dict | None:
        """Record a 👍/👎 vote (and optionally open the noise follow-up)."""
        from datetime import datetime, timezone

        result = (
            self.client.table("morning_brief_feedback")
            .update(
                {
                    "vote": vote,
                    "pending_noise_reply": pending_noise,
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                }
            )
            .eq("brief_id", brief_id)
            .execute()
        )
        return (result.data or [None])[0]

    def set_brief_feedback_noise(
        self, brief_id: str, noise_category: str | None = None, noise_note: str | None = None
    ) -> dict | None:
        """Attach the 'what felt like noise?' follow-up and clear the pending flag."""
        from datetime import datetime, timezone

        patch: dict = {
            "pending_noise_reply": False,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        if noise_category is not None:
            patch["noise_category"] = noise_category
        if noise_note is not None:
            patch["noise_note"] = noise_note
        result = (
            self.client.table("morning_brief_feedback")
            .update(patch)
            .eq("brief_id", brief_id)
            .execute()
        )
        return (result.data or [None])[0]

    def get_pending_noise_brief(self) -> dict | None:
        """Most recent brief awaiting a noise follow-up, within the last 24h.

        Used to associate a free-text reply with the right brief after a restart
        (the pending state lives in the DB row, not in-memory). Stale flags (>24h)
        are ignored — the next morning brief supersedes them anyway.
        """
        from datetime import datetime, timezone, timedelta

        cutoff = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
        rows = (
            self.client.table("morning_brief_feedback")
            .select("*")
            .eq("pending_noise_reply", True)
            .gte("updated_at", cutoff)
            .order("updated_at", desc=True)
            .limit(1)
            .execute()
            .data
            or []
        )
        return rows[0] if rows else None

    def get_brief_feedback_trend(self, days: int = 30) -> dict:
        """Up/down counts over the window for the authoritative ('primary') sends only."""
        from datetime import datetime, timezone, timedelta

        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).date().isoformat()
        rows = (
            self.client.table("morning_brief_feedback")
            .select("vote")
            .eq("variant", "primary")
            .gte("brief_date", cutoff)
            .execute()
            .data
            or []
        )
        up = sum(1 for r in rows if r.get("vote") == "up")
        down = sum(1 for r in rows if r.get("vote") == "down")
        return {"up": up, "down": down, "total": len(rows), "days": days}

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
        # approved-only: memory search must not return pending-extraction tasks
        # as fact (list_decisions at step 4 already filters approved). [audit P3-15]
        try:
            task_results = (
                self.client.table("tasks")
                .select("*, meetings(title)")
                .ilike("title", f"%{query_text}%")
                .eq("approval_status", "approved")
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
                        meeting_tasks = self.get_tasks(
                            status=None, meeting_id=source_id, limit=5
                        )
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
        body_text: str | None = None,
    ) -> dict:
        """Record a scanned email with all Phase 4 fields + body (Phase 13 B4)."""
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
        if body_text:
            row["body_text"] = body_text[:50000]  # Truncate to 50K chars
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

    def get_recent_scanned_email_ids(self, limit: int = 200) -> list[str]:
        """
        Recent scanned email message IDs (newest first).

        Used on startup to rebuild the email watcher's in-memory processed-IDs
        set, so a Cloud Run cycle doesn't re-route recently handled mail in the
        narrow window before Gmail's read-status (the primary dedup) catches it.
        """
        result = (
            self.client.table("email_scans")
            .select("email_id")
            .order("date", desc=True)
            .limit(limit)
            .execute()
        )
        return [r["email_id"] for r in (result.data or []) if r.get("email_id")]

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
        # approved-only: a stale-task alert must not count pending-extraction rows. [audit P3-15]
        result = (
            self.client.table("tasks")
            .select("*")
            .eq("status", "pending")
            .eq("approval_status", "approved")
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

    def get_scheduler_heartbeat(self, name: str) -> dict | None:
        """Get a single scheduler's heartbeat row (or None). SYNC; never raises.

        Used for restart-safe fire-once reconstruction — a sleep-until scheduler
        rebuilds its in-memory 'already ran this period' guard from its last
        heartbeat on boot so a Cloud Run cycle can't re-fire. [audit P4-03]
        """
        try:
            result = (
                self.client.table("scheduler_heartbeats")
                .select("*")
                .eq("scheduler_name", name)
                .limit(1)
                .execute()
            )
            return result.data[0] if result.data else None
        except Exception as e:
            logger.warning(f"get_scheduler_heartbeat({name}) failed: {e}")
            return None

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
    # Team Roster (extensible — config/team.py loads this when TEAM_ROSTER_DB_ENABLED)
    # =========================================================================

    def list_team_members(self, status: str = "active") -> list[dict]:
        """All team_members rows (optionally by status). Mirrors get_canonical_projects."""
        try:
            query = self.client.table("team_members").select("*")
            if status:
                query = query.eq("status", status)
            result = query.order("member_key").execute()
            return result.data or []
        except Exception as e:
            logger.error(f"Error getting team members: {e}")
            return []

    def add_team_member(
        self,
        member_key: str,
        name: str,
        role: str = "",
        role_description: str = "",
        primary_email: str = "",
        identities: list[str] | None = None,
        tier: str = "founders",
        telegram_id: int | None = None,
        is_admin: bool = False,
    ) -> dict | None:
        """Add or upsert a team member (idempotent on member_key). Returns the row or None."""
        try:
            result = self.client.table("team_members").upsert({
                "member_key": member_key.lower().strip(),
                "name": name,
                "role": role,
                "role_description": role_description,
                "primary_email": primary_email,
                "identities": identities or ([primary_email] if primary_email else []),
                "tier": tier,
                "telegram_id": telegram_id,
                "is_admin": is_admin,
                "status": "active",
            }, on_conflict="member_key").execute()
            if result.data:
                logger.info(f"Upserted team member: {member_key}")
                return result.data[0]
            return None
        except Exception as e:
            logger.error(f"Error adding team member '{member_key}': {e}")
            return None

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

    # =========================================================================
    # Knowledge Foundation (v2.5) — areas, topic briefs, typed links (graph-lite)
    # =========================================================================

    def get_areas(self, status: str = "active") -> list[dict]:
        """Get all Areas (Layer 3.5 sphere briefs); current (non-superseded) only."""
        try:
            query = self.client.table("areas").select("*").is_("valid_to", "null")
            if status:
                query = query.eq("status", status)
            result = query.order("name").execute()
            return result.data or []
        except Exception as e:
            logger.error(f"Error getting areas: {e}")
            return []

    # Pre-realignment category taxonomy -> Gantt area (2026-06). Covers the
    # historical TaskCategory enum plus the stragglers found live. Ambiguous
    # legacy buckets (Operations & HR, Strategy & Research) are NOT mapped here —
    # the realignment backfill classifies those per-task.
    LEGACY_CATEGORY_MAP = {
        "product & tech": "PRODUCT & TECHNOLOGY",
        "product & technology": "PRODUCT & TECHNOLOGY",
        "technology & product": "PRODUCT & TECHNOLOGY",
        "r&d": "PRODUCT & TECHNOLOGY",
        "bd & sales": "SALES & BUSINESS DEVELOPMENT",
        "sales & bd": "SALES & BUSINESS DEVELOPMENT",
        "marketing & communications": "SALES & BUSINESS DEVELOPMENT",
        "legal & compliance": "LEGAL, CORPORATE & FINANCE",
        "legal & admin": "LEGAL, CORPORATE & FINANCE",
        "finance": "LEGAL, CORPORATE & FINANCE",
        "finance & fundraising": "FUNDRAISING & INVESTOR RELATIONS",
        "finance & business plan": "FUNDRAISING & INVESTOR RELATIONS",
        "investor relations": "FUNDRAISING & INVESTOR RELATIONS",
        "fundraising": "FUNDRAISING & INVESTOR RELATIONS",
    }

    def resolve_category(
        self, name: str | None, areas: list[dict] | None = None
    ) -> str:
        """Canonicalize a task category against the live Gantt areas.

        Category IS the Gantt-area taxonomy (2026-06 realignment): exact
        case-insensitive match against active areas wins, then the legacy
        taxonomy map, then 'General' for blank/none. An unknown non-empty
        value is RETURNED AS-IS (sheets-wins: never destroy what Eyal typed)
        — the QA pass flags non-canonical categories instead.
        """
        from models.schemas import GENERAL_CATEGORY
        label = (name or "").strip()
        if not label or label.lower() in ("general", "non-area", "none", "n/a", "-"):
            return GENERAL_CATEGORY
        try:
            area_rows = areas if areas is not None else self.get_areas()
            for a in area_rows:
                if (a.get("name") or "").strip().lower() == label.lower():
                    return a.get("name")
        except Exception as e:
            logger.warning(f"resolve_category lookup failed (keeping label): {e}")
        return self.LEGACY_CATEGORY_MAP.get(label.lower(), label)

    # Honorifics stripped before name matching, so a roster entry like
    # "Prof. Yoram Weiss" still matches "Yoram" and "Yoram Weiss".
    _HONORIFICS = {"prof", "dr", "mr", "mrs", "ms", "adv", "eng"}

    # Group/placeholder assignees that are NOT a person and must never be
    # coerced into one. They stay verbatim; the QA pass surfaces them.
    _NON_PERSON_ASSIGNEES = {
        "team", "cropsight team", "cropsight technical team", "technical team",
        "everyone", "all", "tbd", "n/a", "-", "unassigned",
    }

    def resolve_assignee(
        self, name: str | None, roster: list[dict] | None = None
    ) -> str:
        """Canonicalize a task/decision assignee against the live team roster.

        Mirrors resolve_category. Live data carried the SAME person under two
        spellings — "Eyal Zror"(31) and "Eyal"(9), and likewise for Paolo, Roye
        and Yoram — which silently broke every assignee filter: get_tasks
        filters with `ilike` and no wildcards, so "Eyal" matched 9 of 40 rows.
        Canonical form is FIRST + LAST (Eyal's call, 2026-07-22).

        Resolution order:
          1. blank                          -> "" (a genuinely unassigned task)
          2. group/placeholder              -> returned verbatim, never guessed
          3. exact match on a roster name   -> that name
          4. first-name-only match          -> the roster's full name
          5. multi-owner ("Paolo, Eyal")    -> the FIRST name, canonicalized
                                               (never split: splitting changes
                                                task counts and history)
          6. anything else                  -> RETURNED AS-IS (never destroy
                                               what a human typed; the QA pass
                                               flags off-roster assignees)
        """
        raw = (name or "").strip()
        if not raw:
            return ""
        if raw.lower() in self._NON_PERSON_ASSIGNEES:
            return raw
        try:
            rows = roster if roster is not None else self.list_team_members()
            names = [(m.get("name") or "").strip() for m in (rows or [])]
            names = [n for n in names if n]
        except Exception as e:
            logger.warning(f"resolve_assignee roster lookup failed (keeping name): {e}")
            return raw

        # Roster names can carry an honorific ("Prof. Yoram Weiss"), so compare
        # against the honorific-stripped form too — otherwise neither "Yoram"
        # nor "Yoram Weiss" matches it and both silently stay off-roster.
        _honorifics = self._HONORIFICS

        def _strip_title(n: str) -> str:
            parts = n.split()
            while parts and parts[0].lower().rstrip(".") in _honorifics:
                parts = parts[1:]
            return " ".join(parts)

        def _match(candidate: str) -> str | None:
            c = _strip_title(candidate.strip()).lower()
            if not c:
                return None
            bare = {full: _strip_title(full).lower() for full in names}
            for full, b in bare.items():             # exact full name
                if b == c or full.lower() == c:
                    return full
            for full, b in bare.items():             # first name alone
                if b.split() and b.split()[0] == c:
                    return full
            # Last name alone — only when it is unambiguous across the roster.
            surname_hits = [
                full for full, b in bare.items()
                if len(b.split()) > 1 and b.split()[-1] == c
            ]
            if len(surname_hits) == 1:
                return surname_hits[0]
            return None

        hit = _match(raw)
        if hit:
            return hit
        # Multi-owner cell: canonicalize the PRIMARY owner only. The other
        # names belong in the task title (Eyal, 2026-07-22).
        if "," in raw or " and " in raw.lower():
            primary = raw.replace(" and ", ",").split(",")[0]
            hit = _match(primary)
            if hit:
                return hit
        return raw

    def add_area(
        self,
        name: str,
        description: str = "",
        gantt_section: str | None = None,
    ) -> dict | None:
        """Add an Area. Idempotent on name (UNIQUE) — returns existing on conflict."""
        try:
            existing = self.client.table("areas").select("*").eq("name", name).execute()
            if existing.data:
                return existing.data[0]
            result = self.client.table("areas").insert({
                "name": name,
                "description": description,
                "gantt_section": gantt_section,
                "status": "active",
            }).execute()
            if result.data:
                logger.info(f"Created area: {name}")
                return result.data[0]
            return None
        except Exception as e:
            logger.error(f"Error adding area '{name}': {e}")
            return None

    def update_area_brief(self, area_id: str, brief_json: dict) -> bool:
        """Write the AreaBrief JSON + bump brief_updated_at."""
        try:
            from datetime import datetime, timezone
            self.client.table("areas").update({
                "brief_json": brief_json,
                "brief_updated_at": datetime.now(timezone.utc).isoformat(),
            }).eq("id", area_id).execute()
            return True
        except Exception as e:
            logger.error(f"Error updating area brief '{area_id}': {e}")
            return False

    def get_topic_thread(self, topic_id: str) -> dict | None:
        """Fetch a single topic_threads row by id."""
        try:
            result = self.client.table("topic_threads").select("*").eq("id", topic_id).execute()
            return result.data[0] if result.data else None
        except Exception as e:
            logger.error(f"Error getting topic thread '{topic_id}': {e}")
            return None

    def update_topic_brief(self, topic_id: str, brief_json: dict) -> bool:
        """Write the TopicBrief JSON + bump brief_updated_at (leaves state_json untouched)."""
        try:
            from datetime import datetime, timezone
            self.client.table("topic_threads").update({
                "brief_json": brief_json,
                "brief_updated_at": datetime.now(timezone.utc).isoformat(),
            }).eq("id", topic_id).execute()
            return True
        except Exception as e:
            logger.error(f"Error updating topic brief '{topic_id}': {e}")
            return False

    def set_topic_area(self, topic_id: str, area_id: str | None) -> bool:
        """Assign a topic thread to an Area (or clear with None)."""
        try:
            self.client.table("topic_threads").update(
                {"area_id": area_id}
            ).eq("id", topic_id).execute()
            return True
        except Exception as e:
            logger.error(f"Error setting topic area '{topic_id}': {e}")
            return False

    def create_knowledge_link(
        self,
        from_type: str,
        from_id: str,
        to_type: str,
        to_id: str,
        link_type: str,
        confidence: float | None = None,
        source_meeting_id: str | None = None,
        created_by: str = "auto",
    ) -> dict | None:
        """Create a typed link (graph-lite). Skips exact-duplicate current links."""
        try:
            dupe = (
                self.client.table("knowledge_links")
                .select("id")
                .eq("from_type", from_type).eq("from_id", from_id)
                .eq("to_type", to_type).eq("to_id", to_id)
                .eq("link_type", link_type)
                .is_("valid_to", "null")
                .execute()
            )
            if dupe.data:
                return dupe.data[0]
            data = {
                "from_type": from_type, "from_id": from_id,
                "to_type": to_type, "to_id": to_id,
                "link_type": link_type, "created_by": created_by,
            }
            if confidence is not None:
                data["confidence"] = confidence
            if source_meeting_id:
                data["source_meeting_id"] = source_meeting_id
            result = self.client.table("knowledge_links").insert(data).execute()
            return result.data[0] if result.data else None
        except Exception as e:
            logger.error(
                f"Error creating knowledge link {from_type}->{to_type} ({link_type}): {e}"
            )
            return None

    def get_knowledge_links(
        self,
        from_type: str | None = None,
        from_id: str | None = None,
        to_type: str | None = None,
        to_id: str | None = None,
        link_type: str | None = None,
        current_only: bool = True,
    ) -> list[dict]:
        """Query typed links with optional filters. current_only hides superseded links."""
        try:
            query = self.client.table("knowledge_links").select("*")
            if from_type:
                query = query.eq("from_type", from_type)
            if from_id:
                query = query.eq("from_id", from_id)
            if to_type:
                query = query.eq("to_type", to_type)
            if to_id:
                query = query.eq("to_id", to_id)
            if link_type:
                query = query.eq("link_type", link_type)
            if current_only:
                query = query.is_("valid_to", "null")
            result = query.order("created_at", desc=True).execute()
            return result.data or []
        except Exception as e:
            logger.error(f"Error getting knowledge links: {e}")
            return []

    def get_related_decisions(self, decision_id: str, link_types: tuple = ("relates_to",)) -> list[dict]:
        """Decisions linked to this one in the knowledge graph — the decision->decision READER.

        Reads knowledge_links in BOTH directions (this decision as from- and to-side)
        for the given link_types, resolves the other endpoint to a decision row.
        The first-ever consumer of decision->decision links (relates_to / supersedes).
        Powers DecisionBrief.related and the get_decision_synthesis MCP view.
        """
        out: list[dict] = []
        seen: set = set()
        try:
            for lt in link_types:
                for lk in self.get_knowledge_links(from_type="decision", from_id=decision_id, link_type=lt):
                    other = lk.get("to_id")
                    if lk.get("to_type") == "decision" and other and other != decision_id:
                        seen.add(other)
                for lk in self.get_knowledge_links(to_type="decision", to_id=decision_id, link_type=lt):
                    other = lk.get("from_id")
                    if lk.get("from_type") == "decision" and other and other != decision_id:
                        seen.add(other)
            for other in seen:
                d = self.get_decision(other)
                if d:
                    out.append(d)
        except Exception as e:
            logger.error(f"Error getting related decisions for '{decision_id}': {e}")
        return out

    def supersede_knowledge_link(self, link_id: str) -> bool:
        """Mark a link as no longer valid (bi-temporal close, never deleted)."""
        try:
            from datetime import datetime, timezone
            now = datetime.now(timezone.utc).isoformat()
            self.client.table("knowledge_links").update(
                {"valid_to": now, "superseded_at": now}
            ).eq("id", link_id).execute()
            return True
        except Exception as e:
            logger.error(f"Error superseding knowledge link '{link_id}': {e}")
            return False

    def supersede_decision(self, decision_id: str, superseded_by: str | None = None) -> bool:
        """Bi-temporally close a decision (hidden from default reads, kept for history)."""
        try:
            from datetime import datetime, timezone
            now = datetime.now(timezone.utc).isoformat()
            update = {"valid_to": now, "superseded_at": now}
            if superseded_by:
                update["superseded_by"] = superseded_by
            self.client.table("decisions").update(update).eq("id", decision_id).execute()
            return True
        except Exception as e:
            logger.error(f"Error superseding decision '{decision_id}': {e}")
            return False

    def supersede_task(self, task_id: str, superseded_by: str | None = None) -> bool:
        """Bi-temporally close a task (hidden from default reads, kept for history)."""
        try:
            from datetime import datetime, timezone
            now = datetime.now(timezone.utc).isoformat()
            update = {"valid_to": now, "superseded_at": now}
            if superseded_by:
                update["superseded_by"] = superseded_by
            self.client.table("tasks").update(update).eq("id", task_id).execute()
            return True
        except Exception as e:
            logger.error(f"Error superseding task '{task_id}': {e}")
            return False

    # =========================================================================
    # Reconcile / sheet-sync helpers (v3 outputs re-architecture)
    # =========================================================================

    _MANUAL_FIELDS = ("status", "deadline", "priority", "assignee", "title", "label")

    def get_sheet_snapshots(self, entity_type: str = "task") -> dict:
        """Last-synced action-field snapshot per task, keyed by task_id."""
        try:
            rows = (
                self.client.table("sheet_snapshots")
                .select("*")
                .eq("entity_type", entity_type)
                .execute()
                .data
                or []
            )
            return {r["task_id"]: r for r in rows if r.get("task_id")}
        except Exception as e:
            logger.error(f"Error getting sheet snapshots: {e}")
            return {}

    def upsert_sheet_snapshot(
        self,
        task_id: str,
        sheet_row: int | None,
        status: str | None,
        deadline: str | None,
        priority: str | None,
        assignee: str | None,
        title: str | None = None,
        label: str | None = None,
    ) -> bool:
        """Write/refresh the current snapshot row for a task (one per task).

        title/label are the content columns (Phase 1, 2026-07) — snapshotted so a
        Sheet edit to Task text/Label can be attributed to Eyal (Sheet-now !=
        snapshot). Older callers that omit them still work (kwargs default None).
        """
        try:
            from datetime import datetime, timezone
            data = {
                "task_id": task_id,
                "entity_type": "task",
                "sheet_row": sheet_row,
                # Coerce empty strings to NULL — Sheet cells come back as "" for
                # blanks, and the DATE column (deadline) rejects "" (22007).
                "status": (status or None),
                "deadline": (deadline or None),
                "priority": (priority or None),
                "assignee": (assignee or None),
                "title": (title or None),
                "label": (label or None),
                "snapshot_at": datetime.now(timezone.utc).isoformat(),
            }
            existing = (
                self.client.table("sheet_snapshots")
                .select("id")
                .eq("task_id", task_id)
                .eq("entity_type", "task")
                .execute()
            )
            if existing.data:
                self.client.table("sheet_snapshots").update(data).eq(
                    "task_id", task_id
                ).eq("entity_type", "task").execute()
            else:
                self.client.table("sheet_snapshots").insert(data).execute()
            return True
        except Exception as e:
            logger.error(f"Error upserting sheet snapshot for '{task_id}': {e}")
            return False

    def mark_task_field_manual(self, task_id: str, field: str, source: str) -> bool:
        """Flag an action field as manually set (sticky). field in status/deadline/priority/assignee."""
        if field not in self._MANUAL_FIELDS:
            logger.warning(f"mark_task_field_manual: unknown field '{field}'")
            return False
        try:
            from datetime import datetime, timezone
            self.client.table("tasks").update({
                f"manual_{field}": True,
                "manual_set_at": datetime.now(timezone.utc).isoformat(),
                "manual_set_source": source,
            }).eq("id", task_id).execute()
            return True
        except Exception as e:
            logger.error(f"Error marking task field manual ({task_id}.{field}): {e}")
            return False

    def clear_manual_flag(self, task_id: str, field: str) -> bool:
        """Clear a sticky flag so inference can write the field again.

        Also clears the provenance pair when no sticky field remains: leaving a
        stale `manual_set_at`/`manual_set_source` behind makes the audit trail
        claim a human edit that has been released. [2026-07-22]
        """
        if field not in self._MANUAL_FIELDS:
            logger.warning(f"clear_manual_flag: unknown field '{field}'")
            return False
        try:
            updates: dict = {f"manual_{field}": False}
            try:
                row = (
                    self.client.table("tasks")
                    .select(",".join(f"manual_{f}" for f in self._MANUAL_FIELDS))
                    .eq("id", task_id).limit(1).execute()
                )
                remaining = row.data[0] if row.data else {}
                still_sticky = any(
                    remaining.get(f"manual_{f}") for f in self._MANUAL_FIELDS if f != field
                )
                if not still_sticky:
                    updates["manual_set_at"] = None
                    updates["manual_set_source"] = None
            except Exception:
                pass  # provenance tidy-up is best-effort; the clear itself matters
            self.client.table("tasks").update(updates).eq("id", task_id).execute()
            return True
        except Exception as e:
            logger.error(f"Error clearing manual flag ({task_id}.{field}): {e}")
            return False

    # =========================================================================
    # Decision reconcile helpers (Phase 2, 2026-07 — editable Decisions sheet).
    # Parallel to the task snapshot helpers above; reuse the sheet_snapshots table
    # via entity_type='decision' (built for this) so the live task path is untouched.
    # =========================================================================

    _DECISION_MANUAL_FIELDS = ("description", "label", "rationale", "confidence", "status")

    def get_decision_snapshots(self) -> dict:
        """Last-synced snapshot per decision, keyed by decision_id."""
        try:
            rows = (
                self.client.table("sheet_snapshots")
                .select("*")
                .eq("entity_type", "decision")
                .execute()
                .data
                or []
            )
            return {r["decision_id"]: r for r in rows if r.get("decision_id")}
        except Exception as e:
            logger.error(f"Error getting decision snapshots: {e}")
            return {}

    def upsert_decision_snapshot(
        self,
        decision_id: str,
        sheet_row: int | None,
        description: str | None,
        label: str | None = None,
        rationale: str | None = None,
        confidence: int | None = None,
        decision_status: str | None = None,
    ) -> bool:
        """Write/refresh the current snapshot row for a decision (one per decision).

        The snapshot records the last-synced editable columns so a Sheet edit can be
        attributed to Eyal (Sheet-now != snapshot) vs an untouched cell. Mirrors
        upsert_sheet_snapshot for tasks; keyed on decision_id with entity_type='decision'.
        """
        try:
            from datetime import datetime, timezone
            # confidence is an INTEGER column — coerce "" / bad values to NULL.
            conf: int | None
            try:
                conf = int(confidence) if confidence not in (None, "") else None
            except (TypeError, ValueError):
                conf = None
            data = {
                "decision_id": decision_id,
                "entity_type": "decision",
                "sheet_row": sheet_row,
                "description": (description or None),
                "label": (label or None),
                "rationale": (rationale or None),
                "confidence": conf,
                "decision_status": (decision_status or None),
                "snapshot_at": datetime.now(timezone.utc).isoformat(),
            }
            existing = (
                self.client.table("sheet_snapshots")
                .select("id")
                .eq("decision_id", decision_id)
                .eq("entity_type", "decision")
                .execute()
            )
            if existing.data:
                self.client.table("sheet_snapshots").update(data).eq(
                    "decision_id", decision_id
                ).eq("entity_type", "decision").execute()
            else:
                self.client.table("sheet_snapshots").insert(data).execute()
            return True
        except Exception as e:
            logger.error(f"Error upserting decision snapshot for '{decision_id}': {e}")
            return False

    def mark_decision_field_manual(self, decision_id: str, field: str, source: str) -> bool:
        """Flag a decision field as manually set (sticky). field in _DECISION_MANUAL_FIELDS."""
        if field not in self._DECISION_MANUAL_FIELDS:
            logger.warning(f"mark_decision_field_manual: unknown field '{field}'")
            return False
        try:
            from datetime import datetime, timezone
            self.client.table("decisions").update({
                f"manual_{field}": True,
                "manual_set_at": datetime.now(timezone.utc).isoformat(),
                "manual_set_source": source,
            }).eq("id", decision_id).execute()
            return True
        except Exception as e:
            logger.error(f"Error marking decision field manual ({decision_id}.{field}): {e}")
            return False

    def clear_decision_manual_flag(self, decision_id: str, field: str) -> bool:
        """Clear a decision sticky flag so inference can write the field again."""
        if field not in self._DECISION_MANUAL_FIELDS:
            logger.warning(f"clear_decision_manual_flag: unknown field '{field}'")
            return False
        try:
            self.client.table("decisions").update(
                {f"manual_{field}": False}
            ).eq("id", decision_id).execute()
            return True
        except Exception as e:
            logger.error(f"Error clearing decision manual flag ({decision_id}.{field}): {e}")
            return False

    # =========================================================================
    # Gantt reconcile helpers (v3 chunk 2 — curated knowledge-view)
    # =========================================================================

    _GANTT_MANUAL_FIELDS = ("status", "timeframe")

    def get_gantt_rows(self, sheet_name: str | None = None, include_superseded: bool = False) -> list:
        """Current curated Gantt rows (topic-tagged), optionally for one sheet."""
        try:
            q = self.client.table("gantt_rows").select("*")
            if not include_superseded:
                q = q.is_("valid_to", "null")
            if sheet_name:
                q = q.eq("sheet_name", sheet_name)
            return q.execute().data or []
        except Exception as e:
            logger.error(f"Error getting gantt rows: {e}")
            return []

    def get_gantt_row_snapshots(self, sheet_name: str | None = None) -> dict:
        """Last-synced timeframe snapshot per gantt row, keyed by gantt_row_id.

        (Named distinctly from get_gantt_snapshots(proposal_id), which reads the
        older gantt_snapshots proposal-rollback table.)"""
        try:
            rows = (
                self.client.table("sheet_snapshots")
                .select("*")
                .eq("entity_type", "gantt_row")
                .execute()
                .data
                or []
            )
            return {r["gantt_row_id"]: r for r in rows if r.get("gantt_row_id")}
        except Exception as e:
            logger.error(f"Error getting gantt snapshots: {e}")
            return {}

    def upsert_gantt_snapshot(
        self, gantt_row_id: str, sheet_row: int | None,
        week_start: int | None, week_end: int | None,
    ) -> bool:
        """Write/refresh the timeframe snapshot for a gantt row (one per row)."""
        try:
            from datetime import datetime, timezone
            data = {
                "gantt_row_id": gantt_row_id,
                "entity_type": "gantt_row",
                "sheet_row": sheet_row,
                "week_start": week_start,
                "week_end": week_end,
                "snapshot_at": datetime.now(timezone.utc).isoformat(),
            }
            existing = (
                self.client.table("sheet_snapshots")
                .select("id")
                .eq("gantt_row_id", gantt_row_id)
                .eq("entity_type", "gantt_row")
                .execute()
            )
            if existing.data:
                self.client.table("sheet_snapshots").update(data).eq(
                    "gantt_row_id", gantt_row_id
                ).eq("entity_type", "gantt_row").execute()
            else:
                self.client.table("sheet_snapshots").insert(data).execute()
            return True
        except Exception as e:
            logger.error(f"Error upserting gantt snapshot for '{gantt_row_id}': {e}")
            return False

    def mark_gantt_field_manual(self, gantt_row_id: str, field: str, source: str) -> bool:
        """Flag a gantt-row field manual (sticky). field in status/timeframe."""
        if field not in self._GANTT_MANUAL_FIELDS:
            logger.warning(f"mark_gantt_field_manual: unknown field '{field}'")
            return False
        try:
            from datetime import datetime, timezone
            self.client.table("gantt_rows").update({
                f"manual_{field}": True,
                "manual_set_at": datetime.now(timezone.utc).isoformat(),
                "manual_set_source": source,
            }).eq("id", gantt_row_id).execute()
            return True
        except Exception as e:
            logger.error(f"Error marking gantt field manual ({gantt_row_id}.{field}): {e}")
            return False

    def clear_gantt_manual_flag(self, gantt_row_id: str, field: str) -> bool:
        """Clear a sticky gantt-row flag so rollup/reconcile can write again."""
        if field not in self._GANTT_MANUAL_FIELDS:
            logger.warning(f"clear_gantt_manual_flag: unknown field '{field}'")
            return False
        try:
            self.client.table("gantt_rows").update(
                {f"manual_{field}": False}
            ).eq("id", gantt_row_id).execute()
            return True
        except Exception as e:
            logger.error(f"Error clearing gantt manual flag ({gantt_row_id}.{field}): {e}")
            return False

    def upsert_gantt_row(self, data: dict) -> dict:
        """Insert/update a curated gantt row keyed by (sheet_name, topic_id)."""
        try:
            from datetime import datetime, timezone
            data = {**data, "updated_at": datetime.now(timezone.utc).isoformat()}
            existing = []
            if data.get("topic_id") and data.get("sheet_name"):
                existing = (
                    self.client.table("gantt_rows")
                    .select("id")
                    .eq("sheet_name", data["sheet_name"])
                    .eq("topic_id", data["topic_id"])
                    .is_("valid_to", "null")
                    .execute()
                    .data
                    or []
                )
            if existing:
                self.client.table("gantt_rows").update(data).eq("id", existing[0]["id"]).execute()
                return {"id": existing[0]["id"], **data}
            res = self.client.table("gantt_rows").insert(data).execute()
            return res.data[0] if res.data else {}
        except Exception as e:
            logger.error(f"Error upserting gantt row: {e}")
            return {}

    # =========================================================================
    # Intelligence Signal Methods
    # =========================================================================

    def create_intelligence_signal(self, data: dict) -> dict:
        """
        Create a new intelligence signal record.

        Args:
            data: Dict with signal_id, week_number, year, and optional fields.

        Returns:
            Created record.
        """
        result = self.client.table("intelligence_signals").insert(data).execute()
        logger.info(f"Created intelligence signal: {data.get('signal_id')}")
        return result.data[0]

    def update_intelligence_signal(self, signal_id: str, updates: dict) -> dict:
        """
        Update an intelligence signal by signal_id.

        Args:
            signal_id: e.g. "signal-w14-2026"
            updates: Fields to update.

        Returns:
            Updated record.
        """
        result = (
            self.client.table("intelligence_signals")
            .update(updates)
            .eq("signal_id", signal_id)
            .execute()
        )
        return result.data[0] if result.data else {}

    def get_intelligence_signal(self, signal_id: str) -> dict | None:
        """
        Get an intelligence signal by signal_id.

        Args:
            signal_id: e.g. "signal-w14-2026"

        Returns:
            Signal record or None.
        """
        result = (
            self.client.table("intelligence_signals")
            .select("*")
            .eq("signal_id", signal_id)
            .execute()
        )
        return result.data[0] if result.data else None

    def get_latest_intelligence_signal(self) -> dict | None:
        """
        Get the most recent intelligence signal.

        Returns:
            Latest signal record or None.
        """
        result = (
            self.client.table("intelligence_signals")
            .select("*")
            .order("created_at", desc=True)
            .limit(1)
            .execute()
        )
        return result.data[0] if result.data else None

    def get_intelligence_signals(self, limit: int = 4) -> list[dict]:
        """
        Get recent intelligence signals.

        Args:
            limit: Max records to return.

        Returns:
            List of signal records, newest first.
        """
        result = (
            self.client.table("intelligence_signals")
            .select("*")
            .order("created_at", desc=True)
            .limit(limit)
            .execute()
        )
        return result.data or []

    def get_competitor_watchlist(
        self, include_deactivated: bool = False
    ) -> list[dict]:
        """
        Get the competitor watchlist.

        Args:
            include_deactivated: If True, include deactivated entries.

        Returns:
            List of competitor records.
        """
        query = self.client.table("competitor_watchlist").select("*")
        if not include_deactivated:
            query = query.eq("is_active", True)
        result = query.order("category").execute()
        return result.data or []

    def upsert_competitor(self, data: dict) -> dict:
        """
        Insert or update a competitor (upsert on name).

        Args:
            data: Competitor data dict with 'name' required.

        Returns:
            Upserted record.
        """
        result = (
            self.client.table("competitor_watchlist")
            .upsert(data, on_conflict="name")
            .execute()
        )
        return result.data[0] if result.data else {}

    def deactivate_stale_competitors(self, weeks_threshold: int = 4) -> int:
        """
        Deactivate competitors not seen for N+ weeks.

        Args:
            weeks_threshold: Weeks of silence before deactivation.

        Returns:
            Count of deactivated competitors.
        """
        from datetime import datetime, timezone

        now = datetime.now(timezone.utc)
        iso_cal = now.isocalendar()
        current_week = iso_cal[1]
        current_year = iso_cal[0]

        # Fetch active competitors with last_seen data
        result = (
            self.client.table("competitor_watchlist")
            .select("id, name, last_seen_week, last_seen_year")
            .eq("is_active", True)
            .not_.is_("last_seen_week", "null")
            .execute()
        )

        deactivated = 0
        for comp in (result.data or []):
            last_week = comp.get("last_seen_week", 0)
            last_year = comp.get("last_seen_year", 0)

            # Calculate weeks since last seen
            weeks_since = (current_year - last_year) * 52 + (current_week - last_week)
            if weeks_since >= weeks_threshold:
                self.client.table("competitor_watchlist").update(
                    {"is_active": False}
                ).eq("id", comp["id"]).execute()
                logger.info(f"Deactivated stale competitor: {comp['name']}")
                deactivated += 1

        return deactivated

    # ── Deal Intelligence (Phase 4) ──────────────────────────────

    def create_deal(
        self,
        name: str,
        organization: str,
        contact_person: str | None = None,
        stage: str = "lead",
        value_estimate: str | None = None,
        probability: int | None = None,
        owner: str = "Eyal",
        next_action: str | None = None,
        next_action_date: date | None = None,
        source: str | None = None,
        notes: str | None = None,
        metadata: dict | None = None,
    ) -> dict:
        """Create a new deal record."""
        data = {
            "name": name,
            "organization": organization,
            "stage": stage,
            "owner": owner,
        }
        if contact_person:
            data["contact_person"] = contact_person
        if value_estimate:
            data["value_estimate"] = value_estimate
        if probability is not None:
            data["probability"] = probability
        if next_action:
            data["next_action"] = next_action
        if next_action_date:
            data["next_action_date"] = self._serialize_datetime(next_action_date)
        if source:
            data["source"] = source
        if notes:
            data["notes"] = notes
        if metadata:
            data["metadata"] = metadata

        result = self.client.table("deals").insert(data).execute()
        logger.info(f"Created deal: {name} ({organization})")

        self.log_action(
            action="deal_created",
            details={"deal_id": result.data[0]["id"], "name": name, "organization": organization},
            triggered_by="auto",
        )

        return result.data[0]

    def update_deal(self, deal_id: str, **updates) -> dict:
        """Update a deal record. Auto-sets updated_at."""
        updates["updated_at"] = datetime.now(timezone.utc).isoformat()
        result = (
            self.client.table("deals")
            .update(updates)
            .eq("id", deal_id)
            .execute()
        )
        if not result.data:
            raise ValueError(f"Deal {deal_id} not found")
        logger.info(f"Updated deal {deal_id}: {list(updates.keys())}")
        return result.data[0]

    def get_deal(self, deal_id: str) -> dict | None:
        """Get a single deal by ID."""
        result = (
            self.client.table("deals")
            .select("*")
            .eq("id", deal_id)
            .execute()
        )
        return result.data[0] if result.data else None

    def get_deals(
        self,
        stage: str | None = None,
        owner: str | None = None,
        limit: int = 50,
    ) -> list[dict]:
        """List deals, optionally filtered by stage or owner."""
        query = self.client.table("deals").select("*")
        if stage:
            query = query.eq("stage", stage)
        if owner:
            query = query.eq("owner", owner)
        result = query.order("updated_at", desc=True).limit(limit).execute()
        return result.data or []

    def get_deal_timeline(self, deal_id: str, limit: int = 20) -> list[dict]:
        """Get interaction history for a deal, newest first."""
        result = (
            self.client.table("deal_interactions")
            .select("*")
            .eq("deal_id", deal_id)
            .order("date", desc=True)
            .limit(limit)
            .execute()
        )
        return result.data or []

    def get_stale_deals(self, days: int = 7) -> list[dict]:
        """Get deals with no interaction in N days (excluding closed/on_hold)."""
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")
        result = (
            self.client.table("deals")
            .select("*")
            .lt("last_interaction_date", cutoff)
            .not_.in_("stage", ["closed_won", "closed_lost", "on_hold"])
            .order("last_interaction_date")
            .execute()
        )
        return result.data or []

    def get_overdue_deal_actions(self) -> list[dict]:
        """Get deals with overdue next_action_date."""
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        result = (
            self.client.table("deals")
            .select("*")
            .lt("next_action_date", today)
            .not_.in_("stage", ["closed_won", "closed_lost", "on_hold"])
            .not_.is_("next_action_date", "null")
            .order("next_action_date")
            .execute()
        )
        return result.data or []

    def create_deal_interaction(
        self,
        deal_id: str,
        interaction_type: str,
        summary: str,
        interaction_date: date | str | None = None,
        source_id: str | None = None,
        source_type: str | None = None,
        created_by: str = "gianluigi",
    ) -> dict:
        """Create an interaction record and update deal's last_interaction_date."""
        if interaction_date is None:
            interaction_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        date_str = self._serialize_datetime(interaction_date) if not isinstance(interaction_date, str) else interaction_date

        data = {
            "deal_id": deal_id,
            "interaction_type": interaction_type,
            "summary": summary,
            "date": date_str,
            "created_by": created_by,
        }
        if source_id:
            data["source_id"] = source_id
        if source_type:
            data["source_type"] = source_type

        result = self.client.table("deal_interactions").insert(data).execute()

        # Update deal's last_interaction_date
        self.client.table("deals").update(
            {"last_interaction_date": date_str, "updated_at": datetime.now(timezone.utc).isoformat()}
        ).eq("id", deal_id).execute()

        logger.info(f"Created {interaction_type} interaction for deal {deal_id}")
        return result.data[0]

    # ── External Commitments ─────────────────────────────────────

    def create_external_commitment(
        self,
        organization: str,
        commitment: str,
        deal_id: str | None = None,
        contact_person: str | None = None,
        promised_by: str = "Eyal",
        promised_to: str | None = None,
        deadline: date | str | None = None,
        source_meeting_id: str | None = None,
        notes: str | None = None,
    ) -> dict:
        """Create an external commitment (promise to an outside party)."""
        data = {
            "organization": organization,
            "commitment": commitment,
            "promised_by": promised_by,
            "status": "open",
        }
        if deal_id:
            data["deal_id"] = deal_id
        if contact_person:
            data["contact_person"] = contact_person
        if promised_to:
            data["promised_to"] = promised_to
        if deadline:
            data["deadline"] = self._serialize_datetime(deadline) if not isinstance(deadline, str) else deadline
        if source_meeting_id:
            data["source_meeting_id"] = source_meeting_id
        if notes:
            data["notes"] = notes

        result = self.client.table("external_commitments").insert(data).execute()
        logger.info(f"Created external commitment to {organization}: {commitment[:50]}")

        self.log_action(
            action="external_commitment_created",
            details={"commitment_id": result.data[0]["id"], "organization": organization},
            triggered_by="auto",
        )

        return result.data[0]

    def update_external_commitment(self, commitment_id: str, **updates) -> dict:
        """Update an external commitment. Auto-sets updated_at."""
        updates["updated_at"] = datetime.now(timezone.utc).isoformat()
        result = (
            self.client.table("external_commitments")
            .update(updates)
            .eq("id", commitment_id)
            .execute()
        )
        if not result.data:
            raise ValueError(f"External commitment {commitment_id} not found")
        logger.info(f"Updated external commitment {commitment_id}: {list(updates.keys())}")
        return result.data[0]

    def get_external_commitments(
        self,
        status: str | None = None,
        organization: str | None = None,
        limit: int = 50,
    ) -> list[dict]:
        """List external commitments, optionally filtered."""
        query = self.client.table("external_commitments").select("*")
        if status:
            query = query.eq("status", status)
        if organization:
            query = query.ilike("organization", f"%{organization}%")
        result = query.order("deadline").limit(limit).execute()
        return result.data or []

    def get_overdue_commitments(self) -> list[dict]:
        """Get open external commitments past their deadline."""
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        result = (
            self.client.table("external_commitments")
            .select("*")
            .eq("status", "open")
            .lt("deadline", today)
            .not_.is_("deadline", "null")
            .order("deadline")
            .execute()
        )
        return result.data or []


# Singleton instance for easy import
db = SupabaseClient()

# Alias for backward compatibility
supabase_client = db
