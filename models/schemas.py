"""
Pydantic models for all data types.

This module defines typed data models for:
- Database records (meetings, tasks, decisions, etc.)
- API request/response schemas
- Internal data structures

All models use Pydantic v2 for validation and serialization.

Usage:
    from models.schemas import Meeting, Task, Decision

    meeting = Meeting(
        title="MVP Focus",
        date=datetime.now(),
        participants=["Eyal", "Roye"]
    )
"""

from datetime import datetime, date
from enum import Enum
from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field


# =============================================================================
# Enums
# =============================================================================

class TaskStatus(str, Enum):
    """Status of a task."""
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    DONE = "done"
    OVERDUE = "overdue"
    # Removed from the working set without claiming completion (irrelevant,
    # duplicate, finished long ago). Archived tasks stay in the DB for history/
    # citations, move to the Archive tab in the tracker sheet, and are excluded
    # from briefs, digests, reminders, and the reconcile re-add path.
    ARCHIVED = "archived"


class TaskPriority(str, Enum):
    """Priority level of a task — its IMPORTANCE (long-term/business impact)."""
    HIGH = "H"
    MEDIUM = "M"
    LOW = "L"


class TaskUrgency(str, Enum):
    """Time-pressure of a task — SEPARATE from priority (importance).

    Together, priority x urgency form an Eisenhower-style matrix. Urgency is
    derived from deadline proximity + explicit time-pressure language; it lets us
    flag a time-pressing task WITHOUT inventing a deadline ("ASAP" -> urgency H,
    deadline null).
    """
    HIGH = "H"
    MEDIUM = "M"
    LOW = "L"


class Sensitivity(str, Enum):
    """Audience-aware sensitivity tier for meetings and items.

    Hierarchy: CEO(4) > FOUNDERS(3) > TEAM(2) > PUBLIC(1)
    - PUBLIC — safe for anyone (press-released decisions)
    - TEAM — future all-employees tier (reserved)
    - FOUNDERS — OK for full founding team (default for operational discussions)
    - CEO — Eyal only (investor, legal, interpersonal, confidential)
    """
    PUBLIC = "public"
    TEAM = "team"
    FOUNDERS = "founders"
    CEO = "ceo"

    # Backward-compatible aliases for migration period
    @classmethod
    def from_legacy(cls, value: str) -> "Sensitivity":
        """Convert legacy sensitivity values to new tiers."""
        legacy_map = {
            "normal": cls.FOUNDERS,
            "sensitive": cls.CEO,
            "legal": cls.CEO,
            "team": cls.FOUNDERS,
            "ceo_only": cls.CEO,
            "restricted": cls.CEO,
        }
        return legacy_map.get(value, cls.FOUNDERS)


# Numeric tier levels for comparison and filtering
TIER_LEVELS = {"public": 1, "team": 2, "founders": 3, "ceo": 4}


def filter_by_sensitivity(items: list[dict], max_level: int) -> list[dict]:
    """Filter items to include only those at or below the given sensitivity level.

    Items without a sensitivity field default to 'founders' (level 3).
    Legacy values ('ceo_only', 'restricted') map to CEO level (4).

    Args:
        items: List of dicts with optional 'sensitivity' key.
        max_level: Maximum tier level to include (1=public, 2=team, 3=founders, 4=ceo).

    Returns:
        Filtered list containing only items at or below max_level.
    """
    # Include legacy mappings for safety.
    # 'legal' MUST be here: distribution._SENSITIVITY_TO_BAND and
    # Sensitivity.from_legacy both treat it as CEO-tier, but this map didn't —
    # so a 'legal' item fell through to the default 3 and SURVIVED a
    # founders-band cap. Not present in live data today (only team/ceo/normal/
    # founders are stored), but the classifier's SENSITIVE_KEYWORDS include
    # legal terms, so the two maps must not disagree. [2026-07-22]
    level_map = {
        **TIER_LEVELS,
        "ceo_only": 4, "restricted": 4, "sensitive": 4, "legal": 4,
        "normal": 3,
    }
    return [
        item for item in items
        if level_map.get(item.get("sensitivity", "founders"), 3) <= max_level
    ]


class ApprovalStatus(str, Enum):
    """Status of approval flow."""
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"


class DeadlineConfidence(str, Enum):
    """How confident we are that a task's deadline is real.

    EXPLICIT — a participant stated a specific date/timeframe verbatim
               ("by March 15", "before W22", "next Tuesday"). Trustworthy,
               notification-worthy.
    INFERRED — no date was stated; the LLM guessed from context. Too noisy
               to fire reminders on, but still shown in read paths with a
               visual marker (~prefix).
    NONE     — no timing signal at all. Default.
    """
    EXPLICIT = "EXPLICIT"
    INFERRED = "INFERRED"
    NONE = "NONE"


# Fallback category for tasks that genuinely fit no Gantt area. The canonical
# category values are NOT hardcoded here — they are the live `areas` table rows
# (seeded from the Gantt board sections), so the task taxonomy and the Gantt
# stay structurally aligned. Resolve free text via supabase_client.resolve_category().
GENERAL_CATEGORY = "General"


class TaskCategory(str, Enum):
    """Task category = Gantt board area (2026-06 realignment).

    Mirrors the live `areas` table (source of truth, seeded from the Gantt
    sections) so static consumers (prompts, tests) have a fallback list. If the
    Gantt board areas change, update this mirror — runtime paths read the DB.
    """
    PRODUCT_TECH = "PRODUCT & TECHNOLOGY"
    SALES_BD = "SALES & BUSINESS DEVELOPMENT"
    FUNDRAISING_IR = "FUNDRAISING & INVESTOR RELATIONS"
    LEGAL_CORP_FINANCE = "LEGAL, CORPORATE & FINANCE"
    CLIENT_DELIVERY_OPS = "CLIENT DELIVERY & OPERATIONS"
    TEAM_HR = "TEAM & HUMAN RESOURCES"
    GENERAL = GENERAL_CATEGORY


class QuestionStatus(str, Enum):
    """Status of an open question."""
    OPEN = "open"
    RESOLVED = "resolved"


class TopicStatus(str, Enum):
    """Current status of a topic thread.

    Used inside TopicState.current_status to drive the morning brief's
    "Needs attention" surfacing — blocked and stale topics get flagged.
    """
    ACTIVE = "active"                    # progressing normally
    BLOCKED = "blocked"                  # waiting on external action
    PENDING_DECISION = "pending_decision"  # open decision point
    STALE = "stale"                      # no mention in 30+ days
    CLOSED = "closed"                    # explicitly resolved


class OpenItem(BaseModel):
    """A single unresolved item attached to a topic state."""
    kind: str                            # 'task' | 'question' | 'blocker'
    description: str
    owner: str | None = None
    source_meeting_id: str | None = None


class LastDecision(BaseModel):
    """The most recent decision made on a topic."""
    text: str
    date: str                            # ISO date
    meeting_id: str | None = None        # LLM sometimes lacks a specific meeting_id
    meeting_title: str | None = None


class TopicState(BaseModel):
    """
    Structured, continuously-updated state for a topic thread.

    Stored as the state_json column on topic_threads. Populated incrementally
    by update_topic_state() (Haiku) after each meeting mention, plus a
    one-time Sonnet backfill for legacy threads. Sits alongside the
    evolution_summary prose narrative — both are queryable via MCP.
    """
    current_status: TopicStatus = TopicStatus.ACTIVE
    summary: str = ""                    # 2-3 sentence current-state narrative
    stakeholders: list[str] = Field(default_factory=list)
    open_items: list[OpenItem] = Field(default_factory=list)
    last_decision: LastDecision | None = None
    key_facts: list[str] = Field(default_factory=list)
    last_activity_date: str | None = None  # ISO date
    version: int = 1                     # bumped on each update


# =============================================================================
# Knowledge Foundation (v2.5) — Area/Topic briefs + typed links (graph-lite)
# =============================================================================

class LinkType(str, Enum):
    """Typed relationships in the knowledge graph-lite layer (knowledge_links)."""
    BELONGS_TO = "belongs_to"      # topic -> area; sub-topic -> topic
    SUPERSEDES = "supersedes"      # decision -> decision; topic -> topic (merge winner)
    ADVANCES = "advances"          # task -> topic; milestone -> area/topic
    BLOCKS = "blocks"              # topic -> topic; task -> task
    RELATES_TO = "relates_to"      # loose association
    DERIVED_FROM = "derived_from"  # brief fact -> source


class BriefCitation(BaseModel):
    """Provenance for a fact in a topic/area brief. Sensitivity follows data."""
    source_type: str = "meeting"          # 'meeting' | 'document' | 'email' | 'injection'
    source_id: str | None = None
    meeting_title: str | None = None
    date: str | None = None               # ISO date
    sensitivity: Sensitivity = Sensitivity.FOUNDERS


class BriefFact(BaseModel):
    """A single fact in a brief, tagged with its source sensitivity tier.

    Per-fact tagging lets a brief be rendered at any audience tier later
    (e.g. filter out CEO facts for a FOUNDERS view) instead of collapsing the
    whole brief to the max tier. See V2.5_STRATEGY.md sensitivity note (#6).
    """
    text: str
    sensitivity: Sensitivity = Sensitivity.FOUNDERS
    citation: BriefCitation | None = None


class TopicBrief(BaseModel):
    """Living brief for a topic thread (stored as topic_threads.brief_json).

    Richer successor to TopicState. Runs in parallel with state_json during
    shadow-run; state_json is deprecated only after cutover + 4 weeks clean.
    """
    narrative: str = ""                                   # FOUNDERS-safe current-state prose
    facts: list[BriefFact] = Field(default_factory=list)  # fact-tagged store (per-fact tier)
    current_status: TopicStatus = TopicStatus.ACTIVE
    open_items: list[OpenItem] = Field(default_factory=list)
    stakeholders: list[str] = Field(default_factory=list)
    recent_decisions: list[LastDecision] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list)
    next_actions: list[str] = Field(default_factory=list)
    citations: list[BriefCitation] = Field(default_factory=list)
    sensitivity: Sensitivity = Sensitivity.FOUNDERS       # max tier across facts (quick gate)
    last_synthesized_at: str | None = None                # ISO timestamp
    version: int = 1


class AreaBrief(BaseModel):
    """Living brief for an Area/sphere (stored as areas.brief_json).

    Aggregates child topic briefs into a strategic view.
    """
    narrative: str = ""
    topic_summaries: list[str] = Field(default_factory=list)
    cross_topic_patterns: list[str] = Field(default_factory=list)
    strategic_state: str = ""
    facts: list[BriefFact] = Field(default_factory=list)
    citations: list[BriefCitation] = Field(default_factory=list)
    sensitivity: Sensitivity = Sensitivity.FOUNDERS
    last_synthesized_at: str | None = None
    version: int = 1


class DecisionBrief(BaseModel):
    """Living brief for a decision (stored as decisions.brief_json) — Phase 2 PR C.

    The decision-thread analog of TopicBrief: the current synthesized state of a
    decision plus its position in the supersession chain. Assembled DETERMINISTICALLY
    today (no LLM); the later weekly decision-synthesis phase enriches `narrative`.
    """
    summary: str = ""                                     # current decision text
    narrative: str = ""                                   # LLM-enriched later (empty for now)
    status: str = "active"                                # active | superseded | reversed
    rationale: str = ""
    supersedes: list[str] = Field(default_factory=list)   # decision ids this replaced (ancestors)
    superseded_by: str | None = None                      # decision id that replaced this
    related: list[str] = Field(default_factory=list)      # linked decision ids (knowledge graph)
    chain_length: int = 1                                 # size of the supersession chain
    last_referenced_at: str | None = None
    sensitivity: Sensitivity = Sensitivity.FOUNDERS
    last_synthesized_at: str | None = None                # ISO timestamp
    version: int = 1


class KnowledgeLink(BaseModel):
    """A typed relationship in the knowledge_links table (graph-lite)."""
    from_type: str                        # 'topic' | 'area' | 'decision' | 'task' | 'meeting' | 'milestone'
    from_id: str
    to_type: str
    to_id: str
    link_type: LinkType
    confidence: float | None = None       # 0..1 (LLM-inferred); None for deterministic links
    source_meeting_id: str | None = None
    created_by: str = "auto"              # 'auto' | 'eyal' | 'backfill'


# =============================================================================
# Core Models
# =============================================================================

class Meeting(BaseModel):
    """A processed meeting record."""
    id: UUID | None = None
    date: datetime
    title: str
    participants: list[str]
    duration_minutes: int | None = None
    raw_transcript: str | None = None
    summary: str | None = None
    sensitivity: Sensitivity = Sensitivity.FOUNDERS
    source_file_path: str | None = None
    approval_status: ApprovalStatus = ApprovalStatus.PENDING
    approved_at: datetime | None = None
    created_at: datetime | None = None


class Decision(BaseModel):
    """A decision extracted from a meeting."""
    id: UUID | None = None
    meeting_id: UUID
    description: str
    context: str | None = None
    participants_involved: list[str] | None = None
    transcript_timestamp: str | None = None  # e.g., "43:28"
    sensitivity: Sensitivity = Sensitivity.FOUNDERS
    created_at: datetime | None = None


class Task(BaseModel):
    """A task extracted from a meeting or created manually."""
    id: UUID | None = None
    meeting_id: UUID | None = None
    title: str
    assignee: str
    # Free string validated against the live areas table (resolve_category);
    # TaskCategory enum is a static mirror for prompts/tests.
    category: str | None = None
    deadline: date | None = None
    deadline_confidence: DeadlineConfidence = DeadlineConfidence.NONE
    status: TaskStatus = TaskStatus.PENDING
    priority: TaskPriority = TaskPriority.MEDIUM
    urgency: TaskUrgency = TaskUrgency.MEDIUM
    # DEPRECATED (2026-06 category realignment): category now carries the
    # Gantt-area value. Columns retained in the DB; no longer written.
    area_id: UUID | None = None
    area_label: str = "non-area"
    transcript_timestamp: str | None = None
    sensitivity: Sensitivity = Sensitivity.FOUNDERS
    created_at: datetime | None = None
    updated_at: datetime | None = None


class FollowUpMeeting(BaseModel):
    """A follow-up meeting identified from a meeting."""
    id: UUID | None = None
    source_meeting_id: UUID
    title: str
    proposed_date: datetime | None = None
    led_by: str
    participants: list[str] | None = None
    agenda_items: list[str] | None = None
    prep_needed: str | None = None
    created_at: datetime | None = None


class OpenQuestion(BaseModel):
    """An open question raised in a meeting."""
    id: UUID | None = None
    meeting_id: UUID
    question: str
    raised_by: str | None = None
    status: QuestionStatus = QuestionStatus.OPEN
    resolved_in_meeting_id: UUID | None = None
    sensitivity: Sensitivity = Sensitivity.FOUNDERS
    created_at: datetime | None = None


class Document(BaseModel):
    """A document ingested into the knowledge base."""
    id: UUID | None = None
    title: str
    source: str  # 'upload', 'email', 'drive'
    file_type: str | None = None
    summary: str | None = None
    drive_path: str | None = None
    ingested_at: datetime | None = None


class Embedding(BaseModel):
    """A text embedding for semantic search."""
    id: UUID | None = None
    source_type: str  # 'meeting', 'document'
    source_id: UUID
    chunk_text: str
    chunk_index: int | None = None
    speaker: str | None = None
    timestamp_range: str | None = None
    embedding: list[float] | None = None  # Vector
    metadata: dict[str, Any] | None = None
    created_at: datetime | None = None


class AuditLogEntry(BaseModel):
    """An entry in the audit log."""
    id: UUID | None = None
    action: str  # e.g., 'meeting_processed', 'task_created'
    details: dict[str, Any] | None = None
    triggered_by: str = "auto"  # 'auto', 'eyal', 'roye', etc.
    created_at: datetime | None = None


# =============================================================================
# Request/Response Models
# =============================================================================

class SearchQuery(BaseModel):
    """A search query for semantic search."""
    query: str
    date_from: date | None = None
    date_to: date | None = None
    source_type: str | None = None  # 'meeting', 'document', or None for all
    limit: int = Field(default=10, ge=1, le=100)


class SearchResult(BaseModel):
    """A single search result."""
    source_type: str
    source_id: UUID
    chunk_text: str
    score: float
    metadata: dict[str, Any] | None = None


class MeetingExtractionResult(BaseModel):
    """Result of processing a meeting transcript."""
    meeting: Meeting
    decisions: list[Decision]
    tasks: list[Task]
    follow_ups: list[FollowUpMeeting]
    open_questions: list[OpenQuestion]
    stakeholders_mentioned: list[dict[str, str]] | None = None


class ApprovalRequest(BaseModel):
    """A request for approval."""
    meeting_id: UUID
    content_type: str  # 'meeting_summary', 'meeting_prep', etc.
    preview: str
    full_content: str
    drive_draft_link: str | None = None
    sent_at: datetime | None = None


class ApprovalResponse(BaseModel):
    """Response to an approval request."""
    meeting_id: UUID
    action: str  # 'approve', 'reject', 'edit'
    edits: list[dict[str, Any]] | None = None
    responded_by: str = "eyal"
    responded_at: datetime | None = None


# =============================================================================
# Calendar Models
# =============================================================================

class CalendarEvent(BaseModel):
    """A Google Calendar event."""
    id: str
    title: str
    start: datetime
    end: datetime
    attendees: list[dict[str, str]] | None = None
    color_id: str | None = None
    location: str | None = None
    description: str | None = None


# =============================================================================
# Helper Models
# =============================================================================

class TeamMember(BaseModel):
    """A CropSight team member."""
    id: str  # 'eyal', 'roye', 'paolo', 'yoram'
    name: str
    role: str
    email: str
    is_admin: bool = False


class StakeholderEntry(BaseModel):
    """An entry from the Stakeholder Tracker sheet."""
    organization: str
    type: str | None = None
    description: str | None = None
    contact_person: str | None = None
    contact_email: str | None = None
    desired_outcome: str | None = None
    priority: str | None = None
    status: str | None = None
    notes: str | None = None


# =============================================================================
# v1.0 Enums
# =============================================================================

class GanttProposalStatus(str, Enum):
    """Status of a Gantt change proposal."""
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    ROLLED_BACK = "rolled_back"


class DebriefStatus(str, Enum):
    """Status of an end-of-day debrief session."""
    IN_PROGRESS = "in_progress"
    CONFIRMING = "confirming"
    APPROVED = "approved"
    CANCELLED = "cancelled"


class WeeklyReviewStatus(str, Enum):
    """Status of a weekly review session."""
    PREPARING = "preparing"
    READY = "ready"
    IN_PROGRESS = "in_progress"
    CONFIRMING = "confirming"
    APPROVED = "approved"
    CANCELLED = "cancelled"


class EmailClassification(str, Enum):
    """Classification of a scanned email."""
    RELEVANT = "relevant"
    BORDERLINE = "borderline"
    FALSE_POSITIVE = "false_positive"
    SKIPPED = "skipped"


class IntentType(str, Enum):
    """Classified intent of an inbound message."""
    QUESTION = "question"
    TASK_UPDATE = "task_update"
    INFORMATION_INJECTION = "information_injection"
    GANTT_REQUEST = "gantt_request"
    DEBRIEF = "debrief"
    APPROVAL_RESPONSE = "approval_response"
    WEEKLY_REVIEW = "weekly_review"
    MEETING_PREP_REQUEST = "meeting_prep_request"
    AMBIGUOUS = "ambiguous"


# =============================================================================
# v1.0 Models — Gantt Integration
# =============================================================================

class GanttSchemaRow(BaseModel):
    """Schema definition for a row in the operational Gantt chart."""
    id: UUID | None = None
    workspace_id: str = "cropsight"
    sheet_name: str
    section: str
    subsection: str | None = None
    row_number: int
    owner_column: str = "C"
    due_column: str = "D"
    first_week_column: str = "E"
    week_offset: int = 9
    protected: bool = False
    notes: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


class GanttProposal(BaseModel):
    """A proposed change to the Gantt chart, pending CEO approval."""
    id: UUID | None = None
    workspace_id: str = "cropsight"
    status: GanttProposalStatus = GanttProposalStatus.PENDING
    source_type: str | None = None
    source_id: UUID | None = None
    changes: list[dict] = Field(default_factory=list)
    proposed_at: datetime | None = None
    reviewed_at: datetime | None = None
    reviewed_by: str | None = None
    rejection_reason: str | None = None


class GanttSnapshot(BaseModel):
    """A snapshot of Gantt cell values before a proposal is applied, for rollback."""
    id: UUID | None = None
    workspace_id: str = "cropsight"
    proposal_id: UUID
    sheet_name: str
    cell_references: list[str] = Field(default_factory=list)
    old_values: dict = Field(default_factory=dict)
    new_values: dict = Field(default_factory=dict)
    created_at: datetime | None = None


# =============================================================================
# v1.0 Models — End-of-Day Debrief
# =============================================================================

class DebriefSession(BaseModel):
    """An interactive end-of-day debrief session with the CEO."""
    id: UUID | None = None
    workspace_id: str = "cropsight"
    date: date
    status: DebriefStatus = DebriefStatus.IN_PROGRESS
    items_captured: list[dict] = Field(default_factory=list)
    pending_questions: list[dict] = Field(default_factory=list)
    calendar_events_covered: list[str] = Field(default_factory=list)
    calendar_events_remaining: list[str] = Field(default_factory=list)
    raw_messages: list[dict] = Field(default_factory=list)
    created_at: datetime | None = None
    updated_at: datetime | None = None


# =============================================================================
# v1.0 Models — Email Intelligence
# =============================================================================

class EmailScan(BaseModel):
    """A scanned email from the daily email intelligence scan."""
    id: UUID | None = None
    workspace_id: str = "cropsight"
    scan_type: str
    email_id: str
    date: datetime
    sender: str | None = None
    subject: str | None = None
    classification: EmailClassification | None = None
    extracted_items: list[dict] | None = None
    approved: bool = False
    created_at: datetime | None = None


# =============================================================================
# v1.0 Models — MCP Server
# =============================================================================

class MCPSession(BaseModel):
    """A Claude.ai MCP work session record."""
    id: UUID | None = None
    workspace_id: str = "cropsight"
    session_date: date
    summary: str
    decisions_made: list[dict] = Field(default_factory=list)
    pending_items: list[dict] = Field(default_factory=list)
    created_at: datetime | None = None


# =============================================================================
# v1.0 Models — Weekly Review & Reports
# =============================================================================

class WeeklyReviewSession(BaseModel):
    """An interactive weekly review session with the CEO."""
    id: UUID | None = None
    workspace_id: str = "cropsight"
    week_number: int
    year: int
    status: WeeklyReviewStatus = WeeklyReviewStatus.PREPARING
    current_part: int = 0
    agenda_data: dict = Field(default_factory=dict)
    gantt_proposals: list[dict] = Field(default_factory=list)
    corrections: list[dict] = Field(default_factory=list)
    report_id: UUID | None = None
    calendar_event_id: str | None = None
    trigger_type: str = "calendar"
    raw_messages: list[dict] = Field(default_factory=list)
    created_at: datetime | None = None
    updated_at: datetime | None = None


class WeeklyReport(BaseModel):
    """A generated weekly report with associated artifacts."""
    id: UUID | None = None
    workspace_id: str = "cropsight"
    week_number: int
    year: int
    report_url: str | None = None
    slide_drive_id: str | None = None
    digest_drive_id: str | None = None
    gantt_backup_drive_id: str | None = None
    data: dict | None = None
    html_content: str | None = None
    access_token: str | None = None
    session_id: UUID | None = None
    status: str = "draft"
    distributed_at: datetime | None = None
    created_at: datetime | None = None


# =============================================================================
# v1.0 Models — Meeting Prep History
# =============================================================================

class MeetingPrepHistory(BaseModel):
    """Historical record of a meeting prep document and its lifecycle."""
    id: UUID | None = None
    workspace_id: str = "cropsight"
    meeting_type: str
    calendar_event_id: str | None = None
    meeting_date: datetime
    prep_content: dict = Field(default_factory=dict)
    status: str = "pending"
    approved_at: datetime | None = None
    distributed_at: datetime | None = None
    recipients: list[str] | None = None
    created_at: datetime | None = None
