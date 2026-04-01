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


class TaskPriority(str, Enum):
    """Priority level of a task."""
    HIGH = "H"
    MEDIUM = "M"
    LOW = "L"


class Sensitivity(str, Enum):
    """Sensitivity classification of a meeting."""
    NORMAL = "normal"
    SENSITIVE = "sensitive"
    LEGAL = "legal"


class ApprovalStatus(str, Enum):
    """Status of approval flow."""
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"


class TaskCategory(str, Enum):
    """Category of a task for organizational alignment."""
    PRODUCT_TECH = "Product & Tech"
    BD_SALES = "BD & Sales"
    LEGAL_COMPLIANCE = "Legal & Compliance"
    FINANCE_FUNDRAISING = "Finance & Fundraising"
    OPERATIONS_HR = "Operations & HR"
    STRATEGY_RESEARCH = "Strategy & Research"


class QuestionStatus(str, Enum):
    """Status of an open question."""
    OPEN = "open"
    RESOLVED = "resolved"


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
    sensitivity: Sensitivity = Sensitivity.NORMAL
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
    sensitivity: Sensitivity = Sensitivity.NORMAL
    created_at: datetime | None = None


class Task(BaseModel):
    """A task extracted from a meeting or created manually."""
    id: UUID | None = None
    meeting_id: UUID | None = None
    title: str
    assignee: str
    category: TaskCategory | None = None
    deadline: date | None = None
    status: TaskStatus = TaskStatus.PENDING
    priority: TaskPriority = TaskPriority.MEDIUM
    transcript_timestamp: str | None = None
    sensitivity: Sensitivity = Sensitivity.NORMAL
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
    sensitivity: Sensitivity = Sensitivity.NORMAL
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
