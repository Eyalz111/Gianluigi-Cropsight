"""
Google Sheets API integration.

This module handles reading and writing to Google Sheets:
- Task Tracker: Writing extracted tasks, updating status
- Stakeholder Tracker: Reading stakeholder information for context

Sheets:
1. CropSight Task Tracker (NEW - created by Gianluigi)
   Columns: Task, Assignee, Source Meeting, Deadline, Status, Priority, Created Date, Updated Date

2. CropSight Stakeholder Tracker (EXISTING - Eyal's sheet)
   Columns: Organization/Name, Type, Short Description, Contact Person + Email,
            Desired Outcome, Priority, Primary Action Type, Owner, Next Action,
            Due Date, Secondary Action Type, Owner, Next Action, Due Date, Status, Notes

Usage:
    from services.google_sheets import sheets_service

    # Add a task to the tracker
    await sheets_service.add_task(task_data)

    # Look up stakeholder info
    info = await sheets_service.get_stakeholder_info(name="Rita")
"""

import logging
from datetime import datetime
from typing import Any

from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build

from config.settings import settings
from core.dates import parse_human_date

logger = logging.getLogger(__name__)


# =========================================================================
# Color Constants — RGB dicts for Google Sheets API (0.0–1.0 scale)
# =========================================================================

def _hex_color(hex_str: str) -> dict:
    """Convert '#RRGGBB' to Sheets API color dict."""
    return {
        "red": int(hex_str[1:3], 16) / 255,
        "green": int(hex_str[3:5], 16) / 255,
        "blue": int(hex_str[5:7], 16) / 255,
    }


COLORS = {
    # Category colors (Task Tracker — column G in Phase 10 layout)
    "product_tech": _hex_color("#BBDEFB"),       # Light Blue
    "business_dev": _hex_color("#C8E6C9"),        # Light Green
    "marketing": _hex_color("#FFE0B2"),           # Light Orange
    "finance": _hex_color("#E1BEE7"),             # Light Purple
    "legal_ip": _hex_color("#FFCDD2"),            # Light Red
    "operations_hr": _hex_color("#B2DFDB"),       # Light Teal

    # Priority colors
    "priority_high": _hex_color("#FFCDD2"),       # Light Red
    "priority_medium": _hex_color("#FFF9C4"),     # Light Yellow
    "priority_low": _hex_color("#C8E6C9"),        # Light Green

    # Status colors (shared)
    "status_overdue": _hex_color("#FFCDD2"),      # Light Red
    "status_done": _hex_color("#C8E6C9"),         # Light Green
    "status_in_progress": _hex_color("#FFF9C4"),  # Light Yellow
    "status_pending": _hex_color("#BBDEFB"),      # Light Blue

    # Stakeholder status colors
    "status_new": _hex_color("#BBDEFB"),          # Light Blue
    "status_active": _hex_color("#C8E6C9"),       # Light Green
    "status_inactive": _hex_color("#E0E0E0"),     # Light Gray
    "status_completed": _hex_color("#A5D6A7"),    # Medium Green

    # Banding / borders
    "banding_even": _hex_color("#F5F5F5"),        # Very Light Gray
    "banding_odd": _hex_color("#FFFFFF"),          # White
    "border_gray": _hex_color("#E0E0E0"),         # Light Gray

    # Staleness (Last Update column) — deliberately distinct from the status
    # palette: a stale row is a prompt to look, not a status of its own.
    "stale_warn": _hex_color("#FFE0B2"),          # Light Orange — 30d+
    "stale_alert": _hex_color("#FFCDD2"),         # Light Red — 60d+

    # Header
    "header_bg": _hex_color("#1A237E"),           # Dark Blue
    "header_text": _hex_color("#FFFFFF"),         # White
}

# Category labels for data validation — the Gantt-area taxonomy (2026-06
# realignment). Source of truth is the live `areas` table; derived from the
# single static mirror (models.schemas.TaskCategory) so the lists can't drift.
from models.schemas import TaskCategory as _TaskCategory  # noqa: E402

TASK_CATEGORIES = [c.value for c in _TaskCategory]

# Status labels for data validation ('archived' moves the row to the Archive tab)
TASK_STATUSES = ["pending", "in_progress", "done", "overdue", "archived"]
STAKEHOLDER_STATUSES = ["New", "Active", "Inactive", "Completed"]

# Priority labels for data validation
PRIORITIES = ["H", "M", "L"]


# =========================================================================
# Formatting Helper Functions
# =========================================================================

def _conditional_format_rule(
    sheet_id: int, col_index: int, text: str, color: dict, rule_index: int = 0
) -> dict:
    """Build one addConditionalFormatRule request for TEXT_CONTAINS."""
    return {
        "addConditionalFormatRule": {
            "rule": {
                "ranges": [{
                    "sheetId": sheet_id,
                    "startColumnIndex": col_index,
                    "endColumnIndex": col_index + 1,
                    "startRowIndex": 1,
                }],
                "booleanRule": {
                    "condition": {
                        "type": "TEXT_CONTAINS",
                        "values": [{"userEnteredValue": text}],
                    },
                    "format": {"backgroundColor": color},
                },
            },
            "index": rule_index,
        }
    }


def _column_width_request(sheet_id: int, col_index: int, width_px: int) -> dict:
    """Build one updateDimensionProperties request for a fixed column width."""
    return {
        "updateDimensionProperties": {
            "range": {
                "sheetId": sheet_id,
                "dimension": "COLUMNS",
                "startIndex": col_index,
                "endIndex": col_index + 1,
            },
            "properties": {"pixelSize": width_px},
            "fields": "pixelSize",
        }
    }


def _staleness_format_rule(
    sheet_id: int, col_index: int, days: int, color: dict, rule_index: int = 0
) -> dict:
    """Highlight a date cell older than `days` — the staleness signal.

    A CUSTOM_FORMULA rule rather than TEXT_CONTAINS: the cell holds a date, and
    what matters is how OLD it is, which no substring can express. Blank cells
    are excluded so a task with no recorded update isn't painted as stale.
    """
    col_letter = chr(ord("A") + col_index)
    return {
        "addConditionalFormatRule": {
            "rule": {
                "ranges": [{
                    "sheetId": sheet_id,
                    "startColumnIndex": col_index,
                    "endColumnIndex": col_index + 1,
                    "startRowIndex": 1,
                }],
                "booleanRule": {
                    "condition": {
                        "type": "CUSTOM_FORMULA",
                        "values": [{
                            "userEnteredValue":
                                f"=AND(${col_letter}2<>\"\", TODAY()-DATEVALUE(${col_letter}2)>={days})"
                        }],
                    },
                    "format": {"backgroundColor": color},
                },
            },
            "index": rule_index,
        }
    }


def _banding_request(sheet_id: int, num_cols: int) -> dict:
    """Build an addBanding request for alternating row colors (skip header)."""
    return {
        "addBanding": {
            "bandedRange": {
                "range": {
                    "sheetId": sheet_id,
                    "startRowIndex": 1,
                    "startColumnIndex": 0,
                    "endColumnIndex": num_cols,
                },
                "rowProperties": {
                    "firstBandColor": COLORS["banding_odd"],
                    "secondBandColor": COLORS["banding_even"],
                },
            }
        }
    }


def _border_request(sheet_id: int, num_cols: int) -> dict:
    """Build an updateBorders request with light gray borders on all cells."""
    border_style = {
        "style": "SOLID",
        "color": COLORS["border_gray"],
    }
    return {
        "updateBorders": {
            "range": {
                "sheetId": sheet_id,
                "startRowIndex": 0,
                "startColumnIndex": 0,
                "endColumnIndex": num_cols,
            },
            "top": border_style,
            "bottom": border_style,
            "left": border_style,
            "right": border_style,
            "innerHorizontal": border_style,
            "innerVertical": border_style,
        }
    }


def _data_validation_request(
    sheet_id: int, col_index: int, values: list[str]
) -> dict:
    """Build a setDataValidation request with a dropdown (ONE_OF_LIST)."""
    return {
        "setDataValidation": {
            "range": {
                "sheetId": sheet_id,
                "startRowIndex": 1,
                "startColumnIndex": col_index,
                "endColumnIndex": col_index + 1,
            },
            "rule": {
                "condition": {
                    "type": "ONE_OF_LIST",
                    "values": [{"userEnteredValue": v} for v in values],
                },
                "showCustomUi": True,
                "strict": False,
            },
        }
    }


def _text_wrap_request(sheet_id: int, col_index: int) -> dict:
    """Build a repeatCell request that sets wrapStrategy: WRAP on a column."""
    return {
        "repeatCell": {
            "range": {
                "sheetId": sheet_id,
                "startRowIndex": 1,
                "startColumnIndex": col_index,
                "endColumnIndex": col_index + 1,
            },
            "cell": {
                "userEnteredFormat": {
                    "wrapStrategy": "WRAP",
                }
            },
            "fields": "userEnteredFormat.wrapStrategy",
        }
    }


# =========================================================================
# Column Mapping Constants — single source of truth for column layout
# =========================================================================

# Task Tracker: new Phase 10 layout (reordered for quick scanning)
TASK_COLUMNS = {
    "priority": "A",       # H/M/L — narrow, color-coded
    "label": "B",          # 2-3 word project label — quick-scan column
    "task": "C",           # Full task description
    "owner": "D",          # Assignee
    "deadline": "E",       # Due date
    "status": "F",         # pending/in_progress/done/overdue
    "category": "G",       # Product & Tech, BD & Sales, etc.
    "source_meeting": "H", # Meeting where task originated
    "created": "I",        # Creation date
    "id": "J",             # Task UUID — robust Sheet<->DB identity (v3 reconcile)
}

# Operational floor (PR5): append Urgency=K AFTER the col-J UUID identity
# (never relocate J — reconcile keys the Sheet<->DB match on it). Flag-gated; off =
# the A:J 10-column layout. The Area column (was L) was removed in the 2026-06
# category realignment — Category (col G) now carries the Gantt-area taxonomy.
if getattr(settings, "TASK_SHEET_URGENCY_AREA_ENABLED", False):
    TASK_COLUMNS["urgency"] = "K"

# Last Update = L, appended AFTER urgency by the same never-relocate rule.
#
# Why it exists: deadlines are legitimately optional here (75% of open tasks
# have none), so a due-date view is nearly empty and reads as a defect list when
# it isn't one. STALENESS is the pressure signal that always applies — but
# `updated_at` was not in the sheet at all (col I is *Created*), so it could not
# be computed or sorted on. Sorting by this column IS the weekly agenda.
# System-owned: written by reconcile, protected alongside H:J. [2026-07-22]
if getattr(settings, "TASK_SHEET_LAST_UPDATE_ENABLED", False):
    TASK_COLUMNS["last_update"] = "L"

# Column indices (0-based) for formatting operations
TASK_COL_INDEX = {k: ord(v) - ord("A") for k, v in TASK_COLUMNS.items()}

# Decision Tracker columns
DECISION_COLUMNS = {
    "label": "A",
    "decision": "B",
    "rationale": "C",
    "confidence": "D",
    "source_meeting": "E",
    "date": "F",
    "status": "G",
}

DECISION_COL_INDEX = {k: ord(v) - ord("A") for k, v in DECISION_COLUMNS.items()}

# The decision id lives at col H but is deliberately NOT in DECISION_COLUMNS —
# that map drives editable-cell writes, and the id is system-owned. Readers use
# row[7] directly. Named here so writers stop reaching for a key that isn't
# there: DECISION_COLUMNS['id'] raised KeyError. [2026-07-22]
DECISION_ID_COLUMN = "H"

# Header labels for sheet display (order matches column mapping).
# _BASE is the always-present A:J layout; flag-gated columns are appended.
TASK_TRACKER_HEADERS_BASE = [
    "Priority", "Project", "Task", "Owner", "Deadline",
    "Status", "Area", "Source Meeting", "Created", "ID",
]
TASK_TRACKER_HEADERS = list(TASK_TRACKER_HEADERS_BASE)
if getattr(settings, "TASK_SHEET_URGENCY_AREA_ENABLED", False):
    TASK_TRACKER_HEADERS = TASK_TRACKER_HEADERS + ["Urgency"]
if getattr(settings, "TASK_SHEET_LAST_UPDATE_ENABLED", False):
    TASK_TRACKER_HEADERS = TASK_TRACKER_HEADERS + ["Last Update"]

DECISION_TRACKER_HEADERS = [
    "Project", "Decision", "Rationale", "Confidence",
    "Source Meeting", "Date", "Status",
]

# Archive = the Tasks layout + when it left + WHAT IT WAS + WHY.
#
# Prior Status exists because auto-archival flips `done` -> `archived`, which
# erases the difference between work that was FINISHED and work that was
# abandoned. Without it, Archive cannot answer "what did we ship last quarter",
# which is the only reason to keep it rather than delete rows.
ARCHIVE_HEADERS = TASK_TRACKER_HEADERS + ["Archived", "Prior Status", "Reason"]

# ---------------------------------------------------------------------------
# Meetings tab (2026-07) — follow_up_meetings, Nechama's queue.
#
# Editable A:F (snapshot-tracked, manual-wins). G:J are system-owned and
# protected: Agenda/Prep are extraction context she reads, not fields she
# manages, and Source/ID are identity. Keeping the editable set small is
# deliberate — every editable column is a conflict surface.
# ---------------------------------------------------------------------------
MEETING_TAB_NAME = "Meetings"

MEETING_COLUMNS = {
    "title": "A",           # what the meeting is
    "label": "B",           # Project — same vocabulary as Tasks/Decisions
    "led_by": "C",          # who owns making it happen
    "proposed_date": "D",   # when it was proposed for
    "participants": "E",    # comma-separated
    "status": "F",          # not_scheduled / scheduled / held / dropped
    "agenda": "G",          # system-owned (from extraction)
    "prep_needed": "H",     # system-owned
    "source_meeting": "I",  # system-owned
    "id": "J",              # UUID — the reconcile identity
}
MEETING_COL_INDEX = {k: ord(v) - ord("A") for k, v in MEETING_COLUMNS.items()}

MEETING_TRACKER_HEADERS = [
    "Meeting", "Project", "Led By", "Proposed Date", "Participants",
    "Status", "Agenda", "Prep Needed", "Source Meeting", "ID",
]

# Monotonic, like decision statuses: a stale Sheet cell must never un-schedule
# a meeting that already happened.
MEETING_STATUSES = ("not_scheduled", "scheduled", "held", "dropped")
MEETING_STATUS_ORDER = {"not_scheduled": 0, "scheduled": 1, "held": 2, "dropped": 3}

# ---------------------------------------------------------------------------
# Read-only reference tabs (2026-07).
#
# Generated from the DB, never edited. They exist so the workspace answers
# "what's outstanding, and where does it sit?" without anyone querying anything.
# ---------------------------------------------------------------------------
QUESTIONS_TAB_NAME = "Open Questions"
QUESTIONS_HEADERS = [
    "Question", "Raised By", "Project", "Age (days)", "Source Meeting", "Status", "ID",
]

AREAS_TAB_NAME = "Areas"
AREAS_HEADERS = [
    "Area", "Open Tasks", "Overdue", "Open Questions", "Meetings to Schedule",
    "Last Activity", "Current Focus",
]


def _fmt_day(value) -> str:
    """Render a DB timestamp as YYYY-MM-DD for a sheet cell.

    The Last Update column is read by humans and sorted on, so it carries the
    day only — a full ISO timestamp sorts identically but is unreadable in a
    narrow column. Unparseable/missing values become "" rather than "None".
    """
    if not value:
        return ""
    text = str(value)
    return text[:10] if len(text) >= 10 else text


def _sorted_meetings(meetings: list[dict]) -> list[dict]:
    """Order the Meetings tab so it sorts itself into a worklist.

    `not_scheduled` first — that IS the queue — then by proposed date, oldest
    first, so the most overdue booking is the top row. Held/dropped sink to the
    bottom as history.
    """
    def _key(m: dict):
        order = MEETING_STATUS_ORDER.get(
            (m.get("status") or "not_scheduled").strip().lower(), 0
        )
        return (order, str(m.get("proposed_date") or "9999"), m.get("title") or "")
    return sorted(meetings, key=_key)


def _decision_id_enabled() -> bool:
    """Whether the Decisions sheet carries the id column (col H).

    The id is the reconcile identity key — meaningless without the reconcile, so
    it appears only under DECISION_RECONCILE_ENABLED (Phase 2, editable Decisions
    sheet). Resolved at RUNTIME (not a module const) so tests + a Cloud Run flag
    flip both take effect. Off => the historical A:G 7-column layout.
    """
    return getattr(settings, "DECISION_RECONCILE_ENABLED", False)


def _decision_headers() -> list[str]:
    """Decisions header row, runtime-resolved (appends 'ID' when id is enabled)."""
    headers = list(DECISION_TRACKER_HEADERS)
    if _decision_id_enabled():
        headers = headers + ["ID"]
    return headers


def _confidence_cell(d: dict) -> str:
    """Confidence as a sheet string. Missing key -> default 3 (extraction path);
    an explicit NULL -> blank (NOT the literal 'None' str(None) used to write —
    which the reconcile then couldn't parse and left on the sheet)."""
    c = d.get("confidence", 3)
    return "" if c is None else str(c)

# Legacy constant — kept for backward compatibility during transition
TASK_TRACKER_COLUMNS = TASK_TRACKER_HEADERS

# Stakeholder Tracker column configuration (Eyal's existing sheet)
STAKEHOLDER_COLUMNS = [
    "Organization/Name",
    "Type",
    "Short Description",
    "Contact Person + Email",
    "Desired Outcome",
    "Priority",
    "Primary Action Type",
    "Owner",
    "Next Action",
    "Due Date",
    "Secondary Action Type",
    "Secondary Owner",
    "Secondary Next Action",
    "Secondary Due Date",
    "Status",
    "Notes",
    "Deal Stage",
    "Deal Value",
    "Last Interaction",
]


class GoogleSheetsService:
    """
    Service for Google Sheets API operations.

    Handles both reading (stakeholder tracker) and writing (task tracker).
    """

    def __init__(self):
        """
        Initialize the Google Sheets service with credentials.
        """
        self._service = None
        self._credentials: Credentials | None = None

    @property
    def service(self):
        """
        Lazy initialization of Sheets API service.

        Checks token freshness on each access — long-running Cloud Run
        instances may have expired tokens between requests.
        """
        if self._service is None:
            self._service = self._build_service()
        else:
            self._ensure_fresh_credentials()
        return self._service

    def _ensure_fresh_credentials(self):
        """Refresh OAuth token if expired. Force rebuild on refresh failure."""
        if self._credentials and (self._credentials.expired or not self._credentials.token):
            try:
                self._credentials.refresh(Request())
            except Exception as e:
                logger.warning(f"Token refresh failed, rebuilding service: {e}")
                self._service = None  # Force full rebuild on next access

    def _execute_with_retry(self, request_factory, max_retries: int = 3, base_delay: float = 1.0):
        """
        Execute a Google Sheets API request with retry on transient errors.

        Caller passes a ZERO-ARG callable that CONSTRUCTS and returns the API
        request (e.g. `lambda: self.service.spreadsheets().values().update(...)`)
        so the request is rebuilt against a fresh service after an idle-wake
        socket rebuild. On broken pipe / connection reset / transport failure
        this nulls self._service so the next `request_factory()` call builds a
        fresh service with a fresh httplib2 transport — Cloud Run idle-then-wake
        cycles leave stale sockets in the pool. Mirrors
        services/google_drive.py::_execute_with_retry. [audit P3-07]

        Retries on: ConnectionError, TimeoutError, OSError, BrokenPipeError,
        and HTTP 5xx / rate limit errors from the Sheets API.
        """
        import time

        for attempt in range(max_retries):
            try:
                self._ensure_fresh_credentials()
                return request_factory().execute()
            except (ConnectionError, TimeoutError, OSError) as e:
                if attempt < max_retries - 1:
                    delay = base_delay * (2 ** attempt)
                    logger.warning(
                        f"Sheets API retry {attempt + 1}/{max_retries}: "
                        f"{type(e).__name__}: {e}. Rebuilding service, retrying in {delay:.1f}s..."
                    )
                    self._service = None  # Force rebuild — stale socket
                    time.sleep(delay)
                else:
                    raise
            except Exception as e:
                error_str = str(e).lower()
                if any(k in error_str for k in (
                    "broken pipe", "connection reset", "transport",
                    "503", "429", "500", "502", "504",
                )):
                    if attempt < max_retries - 1:
                        delay = base_delay * (2 ** attempt)
                        logger.warning(
                            f"Sheets API transient error retry {attempt + 1}/{max_retries}: "
                            f"{type(e).__name__}: {e}. Rebuilding service, retrying in {delay:.1f}s..."
                        )
                        self._service = None
                        time.sleep(delay)
                    else:
                        raise
                else:
                    raise  # Non-transient (4xx, auth, etc.) — don't retry

    def _build_service(self):
        """Build the Google Sheets API service with OAuth2 credentials."""
        if not settings.GOOGLE_CLIENT_ID or not settings.GOOGLE_CLIENT_SECRET:
            raise RuntimeError("Google OAuth credentials not configured")

        if not settings.GOOGLE_REFRESH_TOKEN:
            raise RuntimeError(
                "Google refresh token not configured. "
                "Run the OAuth flow to obtain a refresh token."
            )

        # Create credentials from refresh token
        self._credentials = Credentials(
            token=None,
            refresh_token=settings.GOOGLE_REFRESH_TOKEN,
            token_uri="https://oauth2.googleapis.com/token",
            client_id=settings.GOOGLE_CLIENT_ID,
            client_secret=settings.GOOGLE_CLIENT_SECRET,
            scopes=[
                "https://www.googleapis.com/auth/spreadsheets",
            ],
        )

        # Refresh the token if needed
        if self._credentials.expired or not self._credentials.token:
            self._credentials.refresh(Request())

        return build("sheets", "v4", credentials=self._credentials)

    async def authenticate(self) -> bool:
        """
        Authenticate with Google Sheets API using OAuth2.

        Returns:
            True if authentication successful, False otherwise.
        """
        try:
            # Force service initialization to verify auth
            _ = self.service
            logger.info("Google Sheets API authentication successful")
            return True
        except Exception as e:
            logger.error(f"Google Sheets API authentication failed: {e}")
            return False

    # =========================================================================
    # Task Tracker Operations
    # =========================================================================

    async def add_task(
        self,
        task: str,
        assignee: str,
        source_meeting: str,
        deadline: str | None,
        status: str,
        priority: str,
        created_date: str,
        category: str = "",
        label: str = "",
        task_id: str = "",
        urgency: str = "M",
    ) -> bool:
        """
        Add a new task to the Task Tracker sheet.

        Column order follows TASK_COLUMNS: Priority, Label, Task, Owner,
        Deadline, Status, Category, Source Meeting, Created, ID [, Urgency].

        Args:
            task: Task description.
            assignee: Who is responsible.
            source_meeting: Name of the meeting where task was created.
            deadline: Due date (YYYY-MM-DD) or None.
            status: 'pending', 'in_progress', 'done', 'overdue'.
            priority: 'H', 'M', or 'L'.
            created_date: When the task was created (YYYY-MM-DD).
            category: Task category — a Gantt area name or 'General'.
            label: Project label (e.g., 'Moldova Pilot').
            task_id: The DB task UUID — written to column J so the v3 reconcile
                matches this row by identity. WITHOUT it the row is UUID-less and
                a write-mode reconcile would treat it as new -> a DUPLICATE DB
                task (PR10 fix; callers now pass the id they just created).
            urgency: 'H', 'M', or 'L' — written to column K when the sheet's
                urgency column is enabled.

        Returns:
            True if task was added successfully.
        """
        if not settings.TASK_TRACKER_SHEET_ID:
            logger.warning("TASK_TRACKER_SHEET_ID not configured")
            return False

        # A:J always (J=id matches the 10-column base layout); K appended only
        # when the urgency column is enabled (TASK_COLUMNS then has it).
        values = [
            priority,
            label,
            task,
            assignee,
            deadline or "",
            status,
            category,
            source_meeting,
            created_date,
            task_id,
        ]
        if "urgency" in TASK_COLUMNS:
            values.append(urgency or "M")
        if "last_update" in TASK_COLUMNS:
            values.append(created_date)   # new row: last update == created

        return await self._append_row(
            sheet_id=settings.TASK_TRACKER_SHEET_ID,
            values=values,
            tab_name=settings.TASK_TRACKER_TAB_NAME,
        )

    async def update_task_status(
        self,
        row_number: int,
        status: str,
        updated_date: str | None = None,
    ) -> bool:
        """
        Update a task's status in the Task Tracker.

        Args:
            row_number: The row number of the task (1-indexed, header is row 1).
            status: New status value.
            updated_date: Ignored (kept for backward compat). No Updated column in new layout.

        Returns:
            True if update was successful.
        """
        if not settings.TASK_TRACKER_SHEET_ID:
            logger.warning("TASK_TRACKER_SHEET_ID not configured")
            return False

        try:
            col = TASK_COLUMNS["status"]
            range_name = f"'{settings.TASK_TRACKER_TAB_NAME}'!{col}{row_number}"
            self._execute_with_retry(
                lambda: self.service.spreadsheets().values().update(
                    spreadsheetId=settings.TASK_TRACKER_SHEET_ID,
                    range=range_name,
                    valueInputOption="RAW",
                    body={"values": [[status]]}
                )
            )

            logger.info(f"Updated task row {row_number}: status={status}")
            return True

        except Exception as e:
            logger.error(f"Error updating task status: {e}")
            return False

    # NOTE: a second find_task_row() definition lives below at line ~898 and
    # is the one Python actually exposes (the later definition wins). The
    # earlier copy that lived here was dead code and was deleted in the
    # 2026-04-11 sheets-sync hardening pass. If you need to call it, see the
    # canonical definition further down in this file.

    async def update_task_row(
        self,
        row_number: int,
        **fields,
    ) -> bool:
        """
        Update specific fields of a task row in the Task Tracker.

        Uses TASK_COLUMNS for column mapping.

        Args:
            row_number: The row number to update (1-indexed, header is row 1).
            **fields: Field names to update. Supported: task, label, category,
                      owner/assignee, deadline, status, priority.

        Returns:
            True if update was successful.
        """
        if not settings.TASK_TRACKER_SHEET_ID:
            return False

        # Map field names to TASK_COLUMNS keys (with aliases)
        field_aliases = {"assignee": "owner"}
        column_map = {
            "task": TASK_COLUMNS["task"],
            "label": TASK_COLUMNS["label"],
            "category": TASK_COLUMNS["category"],
            "owner": TASK_COLUMNS["owner"],
            "assignee": TASK_COLUMNS["owner"],
            "deadline": TASK_COLUMNS["deadline"],
            "status": TASK_COLUMNS["status"],
            "priority": TASK_COLUMNS["priority"],
        }
        # PR9: the urgency cell only exists when TASK_SHEET_URGENCY_AREA_ENABLED
        # added K to TASK_COLUMNS. Map it so a DB-side edit (e.g. MCP update_task)
        # can keep the Sheet cell in lockstep — otherwise reconcile would later
        # pull the stale Sheet value back over the edit.
        if "urgency" in TASK_COLUMNS:
            column_map["urgency"] = TASK_COLUMNS["urgency"]

        try:
            batch_data = []

            for field, value in fields.items():
                col = column_map.get(field)
                if col:
                    batch_data.append({
                        "range": f"'{settings.TASK_TRACKER_TAB_NAME}'!{col}{row_number}",
                        "values": [[value if value is not None else ""]],
                    })

            if batch_data:
                self._execute_with_retry(
                    lambda: self.service.spreadsheets().values().batchUpdate(
                        spreadsheetId=settings.TASK_TRACKER_SHEET_ID,
                        body={
                            "valueInputOption": "RAW",
                            "data": batch_data,
                        },
                    )
                )
                logger.info(f"Updated task row {row_number}: {list(fields.keys())}")
                return True
            return False

        except Exception as e:
            logger.error(f"Error updating task row {row_number}: {e}")
            return False

    async def get_all_tasks(self) -> list[dict]:
        """
        Get all tasks from the Task Tracker.

        Returns:
            List of task dicts with all columns (uses TASK_COLUMNS mapping).
        """
        if not settings.TASK_TRACKER_SHEET_ID:
            logger.warning("TASK_TRACKER_SHEET_ID not configured")
            return []

        # MUST include the tab name. A bare "A:I" range is resolved by the
        # Sheets API against whichever sheet sits at index 0, which silently
        # breaks the moment any other tab (e.g. a backup created by
        # rebuild_sheets.py or duplicateSheet) lands in front of "Tasks".
        # See regression test test_get_all_tasks_uses_explicit_tab_name.
        tab_name = settings.TASK_TRACKER_TAB_NAME or "Tasks"
        _last_col = max(TASK_COLUMNS.values())  # 'J' (off) or 'L' (urgency/area on)
        rows = await self._read_sheet_range(
            sheet_id=settings.TASK_TRACKER_SHEET_ID,
            range_name=f"'{tab_name}'!A:{_last_col}"
        )

        if not rows or len(rows) < 2:
            return []

        num_cols = len(TASK_COLUMNS)
        # Skip header row
        tasks = []
        for i, row in enumerate(rows[1:], start=2):
            # Pad row if needed
            while len(row) < num_cols:
                row.append("")

            # Deadline cells are hand-typed ("20.6.26", "2026-06-20", ...).
            # Normalize to ISO at the read boundary so every consumer compares
            # and stores one format; keep the raw text so reconcile can rewrite
            # the cell to ISO. Unparseable text stays raw (deadline == raw) —
            # consumers must never turn that into a NULL (2026-06-11 incident).
            _raw_deadline = row[TASK_COL_INDEX["deadline"]]
            _task = {
                "row_number": i,
                "priority": row[TASK_COL_INDEX["priority"]],
                "label": row[TASK_COL_INDEX["label"]],
                "task": row[TASK_COL_INDEX["task"]],
                "assignee": row[TASK_COL_INDEX["owner"]],
                "source_meeting": row[TASK_COL_INDEX["source_meeting"]],
                "deadline": parse_human_date(_raw_deadline) or _raw_deadline,
                "deadline_raw": _raw_deadline,
                "status": row[TASK_COL_INDEX["status"]],
                "category": row[TASK_COL_INDEX["category"]],
                "created_date": row[TASK_COL_INDEX["created"]],
                "id": row[TASK_COL_INDEX["id"]],
            }
            if "urgency" in TASK_COL_INDEX:
                _task["urgency"] = row[TASK_COL_INDEX["urgency"]]
            if "last_update" in TASK_COL_INDEX:
                _task["last_update"] = row[TASK_COL_INDEX["last_update"]]
            tasks.append(_task)

        return tasks

    async def get_all_decisions(self) -> list[dict]:
        """Get all decisions from the Decisions tab (A:H when the id column is on).

        Returns each row with its row_number + id — the reconcile identity keys
        (Phase 2). Mirrors get_all_tasks. When DECISION_RECONCILE_ENABLED is off
        there is no id column, so id comes back "" (reconcile is gated off then).
        """
        if not settings.TASK_TRACKER_SHEET_ID:
            logger.warning("TASK_TRACKER_SHEET_ID not configured")
            return []

        headers = _decision_headers()
        num_cols = len(headers)
        end_col = chr(ord("A") + num_cols - 1)
        rows = await self._read_sheet_range(
            sheet_id=settings.TASK_TRACKER_SHEET_ID,
            range_name=f"Decisions!A:{end_col}",
        )
        if not rows or len(rows) < 2:
            return []

        include_id = _decision_id_enabled()
        out = []
        for i, row in enumerate(rows[1:], start=2):
            while len(row) < num_cols:
                row.append("")
            out.append({
                "row_number": i,
                "label": row[DECISION_COL_INDEX["label"]],
                "decision": row[DECISION_COL_INDEX["decision"]],
                "rationale": row[DECISION_COL_INDEX["rationale"]],
                "confidence": row[DECISION_COL_INDEX["confidence"]],
                "source_meeting": row[DECISION_COL_INDEX["source_meeting"]],
                "date": row[DECISION_COL_INDEX["date"]],
                "status": row[DECISION_COL_INDEX["status"]],
                "id": row[7] if include_id else "",
            })
        return out

    async def archive_completed_tasks(self, titles_to_archive: list[str]) -> int:
        """
        Move completed tasks from active tab to Archive tab.

        Args:
            titles_to_archive: List of task titles to archive.

        Returns:
            Number of tasks archived.
        """
        if not settings.TASK_TRACKER_SHEET_ID or not titles_to_archive:
            return 0

        try:
            # Find matching rows in active tab
            all_tasks = await self.get_all_tasks()
            titles_lower = {t.lower() for t in titles_to_archive}
            rows_to_archive = [
                t for t in all_tasks
                if t.get("task", "").lower() in titles_lower and t.get("status", "").lower() == "done"
            ]
            return await self.archive_task_rows(rows_to_archive, reason="auto-30d")

        except Exception as e:
            logger.error(f"Error archiving tasks: {e}")
            return 0

    def _ensure_archive_tab(self) -> None:
        """Create the Archive tab (Tasks layout + Archived/Prior Status/Reason)."""
        meta = self._execute_with_retry(
            lambda: self.service.spreadsheets().get(
                spreadsheetId=settings.TASK_TRACKER_SHEET_ID,
                fields="sheets.properties",
            )
        )
        archive_exists = any(
            s.get("properties", {}).get("title") == "Archive"
            for s in meta.get("sheets", [])
        )
        if archive_exists:
            return
        self._execute_with_retry(
            lambda: self.service.spreadsheets().batchUpdate(
                spreadsheetId=settings.TASK_TRACKER_SHEET_ID,
                body={"requests": [{"addSheet": {"properties": {"title": "Archive"}}}]},
            )
        )
        archive_headers = ARCHIVE_HEADERS
        self._execute_with_retry(
            lambda: self.service.spreadsheets().values().update(
                spreadsheetId=settings.TASK_TRACKER_SHEET_ID,
                range=f"Archive!A1:{chr(ord('A') + len(archive_headers) - 1)}1",
                valueInputOption="RAW",
                body={"values": [archive_headers]},
            )
        )

    async def archive_task_rows(
        self, sheet_rows: list[dict], reason: str = "manual"
    ) -> int:
        """
        Move Tasks-tab rows (dicts from get_all_tasks, row_number included) to
        the Archive tab and delete them from the active tab.

        The archive row mirrors the Tasks layout (including the col-J UUID so an
        archived task stays identifiable), plus Archived date, PRIOR STATUS and
        REASON.

        Prior Status matters because auto-archival flips `done` -> `archived`,
        which would otherwise erase the distinction between work that was
        FINISHED and work that was abandoned. Archive is meant to answer "what
        did we ship last quarter, by area" — that is impossible if every row
        just says `archived`. [2026-07-22]

        Returns the number of rows moved.
        """
        if not settings.TASK_TRACKER_SHEET_ID or not sheet_rows:
            return 0

        tab_name = settings.TASK_TRACKER_TAB_NAME
        self._ensure_archive_tab()

        # IDEMPOTENCY (P1-10): the move is append-then-delete. If a prior cycle's
        # delete leg failed after its append succeeded, those UUIDs already live
        # in Archive AND are still active here. Skip re-appending them (otherwise
        # the Archive tab accrues a duplicate every cycle), but still delete their
        # active copies below so the ghost self-heals. The col-J UUID is the key.
        existing_archive_ids: set[str] = set()
        try:
            resp = self._execute_with_retry(
                lambda: self.service.spreadsheets().values().get(
                    spreadsheetId=settings.TASK_TRACKER_SHEET_ID,
                    range="Archive!J2:J",
                )
            )
            existing_archive_ids = {
                (r[0] or "").strip()
                for r in (resp.get("values", []) or [])
                if r and (r[0] or "").strip()
            }
        except Exception as e:
            # Non-fatal: worst case we re-append a duplicate (the old behaviour).
            logger.warning(f"[archive] could not read existing Archive UUIDs: {e}")

        archive_rows = []
        for t in sheet_rows:
            if (t.get("id") or "").strip() in existing_archive_ids:
                continue  # already in Archive from a half-completed prior move
            _row = [
                t.get("priority", ""), t.get("label", ""), t.get("task", ""),
                t.get("assignee", ""), str(t.get("deadline", "") or ""),
                t.get("status", ""), t.get("category", ""),
                t.get("source_meeting", ""), t.get("created_date", ""),
                t.get("id", ""),
            ]
            if "urgency" in TASK_COLUMNS:
                _row.append(t.get("urgency") or "")
            if "last_update" in TASK_COLUMNS:
                _row.append(t.get("last_update") or "")
            _row.append(datetime.now().strftime("%Y-%m-%d"))  # Archived date
            # Prior Status: what the task was BEFORE the archive flip. Taken
            # from the sheet row, which still holds the pre-archive value.
            _prior = (t.get("prior_status") or t.get("status") or "").strip()
            _row.append("done" if _prior == "archived" else _prior)
            _row.append(reason)
            archive_rows.append(_row)

        if archive_rows:
            num_archive_cols = len(ARCHIVE_HEADERS)
            self._execute_with_retry(
                lambda: self.service.spreadsheets().values().append(
                    spreadsheetId=settings.TASK_TRACKER_SHEET_ID,
                    range=f"Archive!A:{chr(ord('A') + num_archive_cols - 1)}",
                    valueInputOption="RAW",
                    insertDataOption="INSERT_ROWS",
                    body={"values": archive_rows},
                )
            )

        # Delete archived rows from active tab (bottom-up to preserve row numbers).
        # This leg is isolated: an append that succeeded leaves the rows safely in
        # Archive, so a delete failure must be LOUD (rows now duplicated on both
        # tabs) — fire a CRITICAL with the exact rows/UUIDs for operator cleanup,
        # and re-raise so the caller doesn't log it as a benign "rows stay put".
        row_numbers = sorted(
            [t["row_number"] for t in sheet_rows if t.get("row_number")], reverse=True
        )
        sid = self._get_sheet_id_by_name(settings.TASK_TRACKER_SHEET_ID, tab_name)
        if sid is not None and row_numbers:
            requests = [
                {"deleteDimension": {
                    "range": {"sheetId": sid, "dimension": "ROWS",
                              "startIndex": r - 1, "endIndex": r}
                }}
                for r in row_numbers
            ]
            try:
                self._execute_with_retry(
                    lambda: self.service.spreadsheets().batchUpdate(
                        spreadsheetId=settings.TASK_TRACKER_SHEET_ID,
                        body={"requests": requests},
                    )
                )
            except Exception as e:
                stuck = [
                    {"row": t.get("row_number"), "id": t.get("id"), "task": t.get("task")}
                    for t in sheet_rows if t.get("row_number")
                ]
                logger.critical(
                    "[archive] DELETE leg failed after append — rows now exist on "
                    "BOTH tabs and need manual removal from the active tab. "
                    f"Affected: {stuck}. Error: {e}"
                )
                raise

        logger.info(f"Archived {len(archive_rows)} task rows to Archive tab")
        return len(archive_rows)

    async def _delete_rows_by_id(
        self, tab_name: str, ids: list[str], reader, label: str
    ) -> int:
        """
        Remove specific rows from `tab_name` by their UUID column, bottom-up.

        Targeted alternative to the rebuild_*_sheet clear-and-rewrite. A full
        rebuild repaints EVERY cell from the DB, which silently destroys any
        human edit not yet pulled by reconcile — until the next reconcile runs,
        the sheet cell is the ONLY record of that edit, and the rebuild also
        leaves `sheet_snapshots` untouched so the loss is undetectable
        afterwards (cell == snapshot => reconcile does nothing). With two people
        editing the sheet daily that is a routine data-loss path, so paths that
        only need to REMOVE rows must remove exactly those rows. [2026-07-22]

        Returns the number of rows deleted. Never raises — callers treat sheet
        upkeep as best-effort and must not fail their primary operation on it.
        """
        wanted = {str(i).strip() for i in (ids or []) if str(i or "").strip()}
        if not wanted:
            return 0
        try:
            rows = await reader()
            row_numbers = sorted(
                [r["row_number"] for r in rows
                 if str(r.get("id") or "").strip() in wanted and r.get("row_number")],
                reverse=True,
            )
            if not row_numbers:
                return 0
            sid = self._get_sheet_id_by_name(settings.TASK_TRACKER_SHEET_ID, tab_name)
            if sid is None:
                logger.warning(f"[{label}] tab {tab_name!r} not found — no rows removed")
                return 0
            requests = [
                {"deleteDimension": {
                    "range": {"sheetId": sid, "dimension": "ROWS",
                              "startIndex": r - 1, "endIndex": r}
                }}
                for r in row_numbers
            ]
            self._execute_with_retry(
                lambda: self.service.spreadsheets().batchUpdate(
                    spreadsheetId=settings.TASK_TRACKER_SHEET_ID,
                    body={"requests": requests},
                )
            )
            logger.info(f"[{label}] removed {len(row_numbers)} row(s) from {tab_name}")
            return len(row_numbers)
        except Exception as e:
            logger.error(f"[{label}] targeted row removal from {tab_name} failed: {e}")
            return 0

    async def delete_task_rows_by_id(self, task_ids: list[str]) -> int:
        """Remove specific Tasks-tab rows by col-J UUID. See _delete_rows_by_id."""
        tab = settings.TASK_TRACKER_TAB_NAME or "Tasks"
        return await self._delete_rows_by_id(
            tab, task_ids, self.get_all_tasks, "reject-cascade"
        )

    async def delete_decision_rows_by_id(self, decision_ids: list[str]) -> int:
        """Remove specific Decisions-tab rows by col-H UUID. See _delete_rows_by_id."""
        if not _decision_id_enabled():
            # No id column => no safe way to identify rows. Caller falls back.
            return 0
        return await self._delete_rows_by_id(
            "Decisions", decision_ids, self.get_all_decisions, "reject-cascade"
        )

    async def rebuild_tasks_sheet(
        self, tasks_from_db: list[dict], force_empty: bool = False
    ) -> bool:
        """
        Rebuild the Tasks sheet from Supabase data.

        Clears the sheet completely and writes all tasks with consistent
        formatting. Tasks are sorted by: Status (pending→in_progress→overdue→done)
        → Priority (H→M→L) → Created date (newest first).

        Args:
            tasks_from_db: List of task dicts from Supabase.
            force_empty: When False (default), refuse to clear the sheet if
                tasks_from_db is empty — this prevents an upstream Supabase
                read error (which silently returns []) from wiping the live
                sheet. The 2026-04 incidents that lost the Tasks sheet trace
                back to this exact failure mode. Set to True only when you
                genuinely intend to render an empty Tasks sheet (e.g. a
                deliberate reset script).

        Returns:
            True if rebuild was successful.
        """
        if not settings.TASK_TRACKER_SHEET_ID:
            return False

        # Defensive guard: refuse to wipe a populated sheet with no data.
        # This is the safety net for the "Sheets vanished" incidents.
        if not tasks_from_db and not force_empty:
            logger.error(
                "rebuild_tasks_sheet refused: tasks_from_db is empty and "
                "force_empty=False. This usually means an upstream Supabase "
                "read failed silently. Investigate before re-running with "
                "force_empty=True."
            )
            try:
                from services.supabase_client import supabase_client
                supabase_client.log_action(
                    action="sheets_rebuild_refused_empty",
                    details={"sheet": "Tasks"},
                    triggered_by="system",
                )
            except Exception as log_err:
                logger.error(f"Could not audit-log refusal: {log_err}")
            return False

        try:
            tab_name = settings.TASK_TRACKER_TAB_NAME

            # Archived tasks live on the Archive tab, never the working view.
            tasks_from_db = [
                t for t in tasks_from_db if (t.get("status") or "") != "archived"
            ]

            # Sort tasks: active statuses first, then by priority, then newest
            status_order = {"pending": 0, "in_progress": 1, "overdue": 2, "done": 3}
            priority_order = {"H": 0, "M": 1, "L": 2}

            def sort_key(t):
                return (
                    status_order.get(t.get("status", "pending"), 9),
                    priority_order.get(t.get("priority", "M"), 9),
                    t.get("created_at", ""),  # string sort, newest last
                )

            sorted_tasks = sorted(tasks_from_db, key=sort_key)

            # Build rows in TASK_COLUMNS order
            rows = [TASK_TRACKER_HEADERS]  # header first
            for t in sorted_tasks:
                created = str(t.get("created_at", ""))[:10]
                # source_meeting comes from the joined meetings object or direct field
                meeting_info = t.get("meetings") or {}
                source = (
                    t.get("source_meeting", "")
                    or (meeting_info.get("title", "") if isinstance(meeting_info, dict) else "")
                )
                _row = [
                    t.get("priority", "M"),
                    t.get("label", ""),
                    t.get("title", ""),
                    t.get("assignee", ""),
                    str(t.get("deadline", "") or ""),
                    t.get("status", "pending"),
                    t.get("category", ""),
                    source,
                    created,
                    t.get("id", ""),
                ]
                if "urgency" in TASK_COLUMNS:
                    _row.append(t.get("urgency") or "M")
                if "last_update" in TASK_COLUMNS:
                    _row.append(_fmt_day(t.get("updated_at")))
                rows.append(_row)

            # Clear the tab and write fresh data
            num_cols = len(TASK_COLUMNS)
            end_col = chr(ord("A") + num_cols - 1)
            clear_range = f"'{tab_name}'!A:{end_col}"

            # Route BOTH clear and write through the retry wrapper so an
            # idle-wake broken pipe between them can't wipe the sheet. [audit P3-02]
            self._execute_with_retry(
                lambda: self.service.spreadsheets().values().clear(
                    spreadsheetId=settings.TASK_TRACKER_SHEET_ID,
                    range=clear_range,
                )
            )

            # Write all data (header + tasks). If this fails AFTER the clear
            # succeeded, the live view is empty — fire a CRITICAL alert with the
            # row count so an operator can rebuild immediately. [audit P3-02]
            write_range = f"'{tab_name}'!A1:{end_col}{len(rows)}"
            try:
                self._execute_with_retry(
                    lambda: self.service.spreadsheets().values().update(
                        spreadsheetId=settings.TASK_TRACKER_SHEET_ID,
                        range=write_range,
                        valueInputOption="RAW",
                        body={"values": rows},
                    )
                )
            except Exception as write_err:
                logger.critical(
                    f"Tasks sheet CLEARED but write FAILED — {len(sorted_tasks)} rows "
                    f"missing from the live view: {write_err}"
                )
                try:
                    from services.supabase_client import supabase_client
                    supabase_client.log_action(
                        action="sheets_rebuild_write_failed",
                        details={"sheet": "Tasks", "row_count": len(sorted_tasks), "error": str(write_err)},
                        triggered_by="system",
                    )
                except Exception:
                    pass
                raise

            logger.info(f"Rebuilt Tasks sheet: {len(sorted_tasks)} tasks written")

            # Audit-log every clear-and-rewrite so future incidents can be
            # diff'd against the audit_log timeline.
            try:
                from services.supabase_client import supabase_client
                supabase_client.log_action(
                    action="sheets_rebuild_tasks",
                    details={"row_count": len(sorted_tasks)},
                    triggered_by="system",
                )
            except Exception as log_err:
                logger.warning(f"Could not audit-log rebuild: {log_err}")

            # Apply formatting
            await self.format_task_tracker()

            return True

        except Exception as e:
            logger.error(f"Error rebuilding Tasks sheet: {e}")
            return False

    async def rebuild_decisions_sheet(
        self, decisions_from_db: list[dict], force_empty: bool = False
    ) -> bool:
        """
        Rebuild the Decisions sheet from Supabase data.

        Args:
            decisions_from_db: List of decision dicts from Supabase.
            force_empty: When False (default), refuse to clear the sheet if
                decisions_from_db is empty. Mirrors rebuild_tasks_sheet's
                guard against silent Supabase read failures wiping live data.

        Returns:
            True if rebuild was successful.
        """
        if not settings.TASK_TRACKER_SHEET_ID:
            return False

        if not decisions_from_db and not force_empty:
            logger.error(
                "rebuild_decisions_sheet refused: decisions_from_db is empty "
                "and force_empty=False. Investigate before re-running with "
                "force_empty=True."
            )
            try:
                from services.supabase_client import supabase_client
                supabase_client.log_action(
                    action="sheets_rebuild_refused_empty",
                    details={"sheet": "Decisions"},
                    triggered_by="system",
                )
            except Exception as log_err:
                logger.error(f"Could not audit-log refusal: {log_err}")
            return False

        try:
            # Ensure tab exists
            sheet_id = await self.ensure_decisions_tab()
            if sheet_id is None:
                return False

            # Sort decisions by date (newest first)
            sorted_decisions = sorted(
                decisions_from_db,
                key=lambda d: d.get("created_at", ""),
                reverse=True,
            )

            # Build rows
            include_id = _decision_id_enabled()
            headers = _decision_headers()
            rows = [headers]
            for d in sorted_decisions:
                meeting_info = d.get("meetings") or {}
                source = (
                    d.get("source_meeting", "")
                    or (meeting_info.get("title", "") if isinstance(meeting_info, dict) else "")
                )
                row = [
                    d.get("label", ""),
                    d.get("description", ""),
                    d.get("rationale", ""),
                    _confidence_cell(d),
                    source,
                    str(d.get("created_at", ""))[:10],
                    d.get("decision_status", "Active"),
                ]
                if include_id:
                    row.append(d.get("id", ""))   # col H — reconcile identity key
                rows.append(row)

            # Clear and rewrite
            num_cols = len(headers)
            end_col = chr(ord("A") + num_cols - 1)

            # Route BOTH clear and write through the retry wrapper. The
            # Decisions sheet has NO reconcile self-heal, so a clear-then-failed-
            # write is a PERMANENT wipe — retry + CRITICAL alert. [audit P3-02]
            self._execute_with_retry(
                lambda: self.service.spreadsheets().values().clear(
                    spreadsheetId=settings.TASK_TRACKER_SHEET_ID,
                    range=f"Decisions!A:{end_col}",
                )
            )

            try:
                self._execute_with_retry(
                    lambda: self.service.spreadsheets().values().update(
                        spreadsheetId=settings.TASK_TRACKER_SHEET_ID,
                        range=f"Decisions!A1:{end_col}{len(rows)}",
                        valueInputOption="RAW",
                        body={"values": rows},
                    )
                )
            except Exception as write_err:
                logger.critical(
                    f"Decisions sheet CLEARED but write FAILED — {len(sorted_decisions)} rows "
                    f"PERMANENTLY missing (no reconcile self-heal): {write_err}"
                )
                try:
                    from services.supabase_client import supabase_client
                    supabase_client.log_action(
                        action="sheets_rebuild_write_failed",
                        details={"sheet": "Decisions", "row_count": len(sorted_decisions), "error": str(write_err)},
                        triggered_by="system",
                    )
                except Exception:
                    pass
                raise

            logger.info(f"Rebuilt Decisions sheet: {len(sorted_decisions)} decisions written")

            # Lock the system-owned columns once the sheet carries ids (reconcile
            # live). Best-effort — never fail the rebuild on a protection hiccup.
            if include_id:
                try:
                    await self.format_decision_tracker(sheet_id)
                except Exception as fmt_err:
                    logger.warning(f"Could not protect Decisions sheet: {fmt_err}")

            try:
                from services.supabase_client import supabase_client
                supabase_client.log_action(
                    action="sheets_rebuild_decisions",
                    details={"row_count": len(sorted_decisions)},
                    triggered_by="system",
                )
            except Exception as log_err:
                logger.warning(f"Could not audit-log rebuild: {log_err}")

            return True

        except Exception as e:
            logger.error(f"Error rebuilding Decisions sheet: {e}")
            return False

    async def find_task_row(self, task_description: str) -> int | None:
        """
        Find the row number for a task by its description.

        PREFER `find_task_row_by_id` — title matching is ambiguous (two tasks can
        share a title, and a renamed task no longer matches), which is why the
        reconcile engine abandoned it for the col-J UUID (audit P1-03). This is
        kept for callers that genuinely have no id.

        Args:
            task_description: The task text to search for.

        Returns:
            Row number if found, None otherwise.
        """
        tasks = await self.get_all_tasks()

        for task in tasks:
            if task["task"].lower() == task_description.lower():
                return task["row_number"]

        return None

    async def find_task_row_by_id(self, task_id: str) -> int | None:
        """
        Find a task's row number by its col-J UUID — the unambiguous identity.

        Title matching can resolve to the WRONG row when two tasks share a title,
        which means a targeted cell write lands on someone else's task. The UUID
        is written to col J by every creation path, so callers holding a task id
        should always use this. [2026-07-22]
        """
        tid = str(task_id or "").strip()
        if not tid:
            return None
        for task in await self.get_all_tasks():
            if str(task.get("id") or "").strip() == tid:
                return task.get("row_number")
        return None

    # =========================================================================
    # Stakeholder Tracker Operations
    # =========================================================================

    async def get_stakeholder_info(
        self,
        name: str | None = None,
        organization: str | None = None
    ) -> list[dict]:
        """
        Search the Stakeholder Tracker for matching entries.

        Args:
            name: Filter by contact/organization name (partial match).
            organization: Filter by organization name (partial match).

        Returns:
            List of matching stakeholder records.
        """
        all_stakeholders = await self.get_all_stakeholders()

        if not name and not organization:
            return all_stakeholders

        # Filter by name/organization
        filtered = []
        for s in all_stakeholders:
            if name:
                # Check organization name and contact person
                name_lower = name.lower()
                if (
                    name_lower in s.get("organization_name", "").lower()
                    or name_lower in s.get("contact_person", "").lower()
                ):
                    filtered.append(s)
                    continue

            if organization:
                org_lower = organization.lower()
                if org_lower in s.get("organization_name", "").lower():
                    filtered.append(s)
                    continue

        return filtered

    async def get_all_stakeholders(self) -> list[dict]:
        """
        Get all stakeholders from the Stakeholder Tracker.

        Returns:
            List of all stakeholder records.
        """
        if not settings.STAKEHOLDER_TRACKER_SHEET_ID:
            logger.warning("STAKEHOLDER_TRACKER_SHEET_ID not configured")
            return []

        tab = settings.STAKEHOLDER_TAB_NAME
        rows = await self._read_sheet_range(
            sheet_id=settings.STAKEHOLDER_TRACKER_SHEET_ID,
            range_name=f"'{tab}'!A:P"  # 16 columns
        )

        if not rows or len(rows) < 2:
            return []

        # Skip header row
        stakeholders = []
        for i, row in enumerate(rows[1:], start=2):
            # Pad row if needed
            while len(row) < len(STAKEHOLDER_COLUMNS):
                row.append("")

            stakeholders.append({
                "row_number": i,
                "organization_name": row[0],
                "type": row[1],
                "description": row[2],
                "contact_person": row[3],
                "desired_outcome": row[4],
                "priority": row[5],
                "primary_action_type": row[6],
                "owner": row[7],
                "next_action": row[8],
                "due_date": row[9],
                "secondary_action_type": row[10] if len(row) > 10 else "",
                "secondary_owner": row[11] if len(row) > 11 else "",
                "secondary_next_action": row[12] if len(row) > 12 else "",
                "secondary_due_date": row[13] if len(row) > 13 else "",
                "status": row[14] if len(row) > 14 else "",
                "notes": row[15] if len(row) > 15 else "",
                "deal_stage": row[16] if len(row) > 16 else "",
                "deal_value": row[17] if len(row) > 17 else "",
                "last_interaction": row[18] if len(row) > 18 else "",
            })

        return stakeholders

    async def suggest_stakeholder_update(
        self,
        organization: str,
        updates: dict
    ) -> dict:
        """
        Prepare a stakeholder update suggestion (for Eyal approval in v0.2).

        Args:
            organization: The organization to update.
            updates: Dict of field names to new values.

        Returns:
            Dict with current values and suggested updates.
        """
        # Find the stakeholder
        stakeholders = await self.get_stakeholder_info(organization=organization)

        if not stakeholders:
            return {
                "found": False,
                "organization": organization,
                "suggested_updates": updates,
            }

        current = stakeholders[0]

        return {
            "found": True,
            "organization": organization,
            "row_number": current["row_number"],
            "current_values": current,
            "suggested_updates": updates,
        }

    # =========================================================================
    # Batch Operations
    # =========================================================================

    async def add_tasks_batch(self, tasks: list[dict]) -> bool:
        """
        Add multiple tasks to the Task Tracker.

        Args:
            tasks: List of task dicts with keys matching add_task params.

        Returns:
            True if all tasks were added successfully.
        """
        if not settings.TASK_TRACKER_SHEET_ID:
            logger.warning("TASK_TRACKER_SHEET_ID not configured")
            return False

        if not tasks:
            return True

        values = []
        for task in tasks:
            created = task.get("created_date", datetime.now().strftime("%Y-%m-%d"))
            _row = [
                task.get("priority", "M"),
                task.get("label", ""),
                task.get("task", ""),
                task.get("assignee", ""),
                task.get("deadline", ""),
                task.get("status", "pending"),
                task.get("category", ""),
                task.get("source_meeting", ""),
                created,
                task.get("id", ""),
            ]
            if "urgency" in TASK_COLUMNS:
                _row.append(task.get("urgency") or "M")
            if "last_update" in TASK_COLUMNS:
                _row.append(_fmt_day(task.get("updated_at")))
            values.append(_row)

        return await self._append_rows(
            sheet_id=settings.TASK_TRACKER_SHEET_ID,
            values=values,
            tab_name=settings.TASK_TRACKER_TAB_NAME,
        )

    async def add_follow_ups_as_tasks(
        self,
        follow_ups: list[dict],
        source_meeting: str,
        created_date: str,
    ) -> bool:
        """
        DEPRECATED (2026-07-22) — use add_meetings_batch_to_sheet().

        Added follow-up meetings to the TASKS tab as "Schedule: [title]" rows.
        It wrote only 9 columns and NO col-J UUID, so the reconcile engine
        classified every one as a hand-added row and created a DUPLICATE `tasks`
        row for it on each run — the same real-world item then existed in both
        `follow_up_meetings` and `tasks`. Confirmed live: Tasks row 200 was
        "Schedule: Virtual Friday sync meeting" with an empty id.

        Kept (unused) only so an old caller fails loudly in review rather than
        silently reintroducing the duplication. Follow-ups now live on the
        Meetings tab with their real UUID.

        Args:
            follow_ups: List of follow-up meeting dicts.
            source_meeting: Name of the source meeting.
            created_date: Date the task was created (YYYY-MM-DD).

        Returns:
            True if all follow-ups were added successfully.
        """
        if not settings.TASK_TRACKER_SHEET_ID:
            logger.warning("TASK_TRACKER_SHEET_ID not configured")
            return False

        if not follow_ups:
            return True

        values = []
        for f in follow_ups:
            title = f.get("title", "Follow-up meeting")
            led_by = f.get("led_by", "Team")
            proposed = f.get("proposed_date", "")
            prep = f.get("prep_needed", "")

            task_desc = f"Schedule: {title}"
            if proposed:
                task_desc += f" ({proposed})"
            if prep:
                task_desc += f" | Prep: {prep}"

            values.append([
                "H",               # priority
                "",                # label — follow-ups don't have a project label
                task_desc,         # task
                led_by,            # owner
                "",                # deadline — follow-ups often don't have a parseable date
                "pending",         # status
                "General",         # category — follow-up scheduling, no Gantt area
                source_meeting,    # source meeting
                created_date,      # created
            ])

        return await self._append_rows(
            sheet_id=settings.TASK_TRACKER_SHEET_ID,
            values=values,
            tab_name=settings.TASK_TRACKER_TAB_NAME,
        )

    async def add_stakeholders_batch(
        self,
        stakeholders: list[dict],
        source_meeting: str,
    ) -> bool:
        """
        Add new stakeholders to the Stakeholder Tracker sheet.

        Only adds stakeholders not already present in the sheet.

        Args:
            stakeholders: List of stakeholder dicts with 'name' and 'context' keys.
            source_meeting: Name of the meeting where they were mentioned.

        Returns:
            True if stakeholders were added successfully.
        """
        if not settings.STAKEHOLDER_TRACKER_SHEET_ID:
            logger.warning("STAKEHOLDER_TRACKER_SHEET_ID not configured")
            return False

        if not stakeholders:
            return True

        # Get existing stakeholders to avoid duplicates
        existing = await self.get_all_stakeholders()
        existing_names = {
            s.get("organization_name", "").lower() for s in existing
        }

        values = []
        for s in stakeholders:
            name = s.get("name", "")
            if not name:
                continue
            if name.lower() in existing_names:
                logger.debug(f"Stakeholder '{name}' already exists, skipping")
                continue

            context = s.get("context", "")
            values.append([
                name,                   # Organization/Name
                "",                     # Type (to be filled by Eyal)
                context,                # Short Description
                "",                     # Contact Person + Email
                "",                     # Desired Outcome
                "",                     # Priority
                "",                     # Primary Action Type
                "",                     # Owner
                "",                     # Next Action
                "",                     # Due Date
                "",                     # Secondary Action Type
                "",                     # Secondary Owner
                "",                     # Secondary Next Action
                "",                     # Secondary Due Date
                "New",                  # Status
                f"Mentioned in: {source_meeting}",  # Notes
            ])

        if not values:
            logger.info("No new stakeholders to add (all already exist)")
            return True

        try:
            tab = settings.STAKEHOLDER_TAB_NAME
            logger.info(f"Adding {len(values)} new stakeholders (filtered from {len(stakeholders)} extracted)")
            self._execute_with_retry(
                lambda: self.service.spreadsheets().values().append(
                    spreadsheetId=settings.STAKEHOLDER_TRACKER_SHEET_ID,
                    range=f"'{tab}'!A:P",
                    valueInputOption="RAW",
                    insertDataOption="INSERT_ROWS",
                    body={"values": values}
                )
            )

            logger.info(f"Added {len(values)} new stakeholders to tracker")
            return True

        except Exception as e:
            logger.error(f"Error adding stakeholders: {e}")
            return False

    # =========================================================================
    # Helper Methods
    # =========================================================================

    def _get_first_sheet_id(self, spreadsheet_id: str) -> int:
        """
        Get the sheetId of the first tab in a spreadsheet.

        Google Sheets batchUpdate requires numeric sheetId, which is NOT
        always 0. This fetches the actual ID from the spreadsheet metadata.

        Args:
            spreadsheet_id: The Google Sheets spreadsheet ID.

        Returns:
            The numeric sheetId of the first sheet tab.
        """
        metadata = self._execute_with_retry(
            lambda: self.service.spreadsheets().get(
                spreadsheetId=spreadsheet_id,
                fields="sheets.properties.sheetId",
            )
        )
        return metadata["sheets"][0]["properties"]["sheetId"]

    def _get_sheet_title(self, spreadsheet_id: str, sheet_id: int) -> str | None:
        """Get the title of a sheet by its numeric sheetId."""
        metadata = self._execute_with_retry(
            lambda: self.service.spreadsheets().get(
                spreadsheetId=spreadsheet_id,
                fields="sheets.properties",
            )
        )
        for sheet in metadata.get("sheets", []):
            props = sheet.get("properties", {})
            if props.get("sheetId") == sheet_id:
                return props.get("title")
        return None

    def _get_sheet_id_by_name(self, spreadsheet_id: str, tab_name: str) -> int | None:
        """
        Get the numeric sheetId of a tab by its name.

        Args:
            spreadsheet_id: The Google Sheets spreadsheet ID.
            tab_name: The name of the tab to find.

        Returns:
            The numeric sheetId, or None if the tab doesn't exist.
        """
        metadata = self._execute_with_retry(
            lambda: self.service.spreadsheets().get(
                spreadsheetId=spreadsheet_id,
                fields="sheets.properties",
            )
        )
        for sheet in metadata.get("sheets", []):
            props = sheet.get("properties", {})
            if props.get("title") == tab_name:
                return props.get("sheetId")
        return None

    def _clear_conditional_format_rules_for_sheet(
        self, spreadsheet_id: str, sheet_id: int
    ) -> list[dict]:
        """deleteConditionalFormatRule requests for ONE tab, by sheetId.

        `_clear_conditional_format_rules` below resolves rules from
        `sheets[0]` — the FIRST tab — which is correct for the Tasks tab it was
        written for and catastrophic for any other caller: formatting the
        Meetings tab with it would delete the Tasks tab's rules instead.
        This variant matches on the sheetId. [2026-07-22]
        """
        try:
            metadata = self._execute_with_retry(
                lambda: self.service.spreadsheets().get(
                    spreadsheetId=spreadsheet_id,
                    fields="sheets(properties.sheetId,conditionalFormats)",
                )
            )
            rules = []
            for sheet in metadata.get("sheets", []):
                if sheet.get("properties", {}).get("sheetId") == sheet_id:
                    rules = sheet.get("conditionalFormats", []) or []
                    break
            # Reverse order so indices don't shift as rules are removed.
            return [
                {"deleteConditionalFormatRule": {"sheetId": sheet_id, "index": i}}
                for i in range(len(rules) - 1, -1, -1)
            ]
        except Exception as e:
            logger.warning(f"Could not enumerate conditional formats for {sheet_id}: {e}")
            return []

    def _clear_conditional_format_rules(self, spreadsheet_id: str) -> list[dict]:
        """
        Build deleteConditionalFormatRule requests for ALL existing rules.

        Fetches sheet metadata to find how many conditional format rules exist,
        then returns delete requests in reverse index order (so indices stay
        valid as rules are removed). This makes formatting idempotent.

        Args:
            spreadsheet_id: The Google Sheets spreadsheet ID.

        Returns:
            List of deleteConditionalFormatRule request dicts.
        """
        metadata = self._execute_with_retry(
            lambda: self.service.spreadsheets().get(
                spreadsheetId=spreadsheet_id,
                fields="sheets.conditionalFormats",
            )
        )

        # Count existing rules on the first sheet
        sheets = metadata.get("sheets", [])
        if not sheets:
            return []

        rules = sheets[0].get("conditionalFormats", [])
        if not rules:
            return []

        sheet_id = self._get_first_sheet_id(spreadsheet_id)

        # Delete in reverse order so indices don't shift
        return [
            {
                "deleteConditionalFormatRule": {
                    "sheetId": sheet_id,
                    "index": i,
                }
            }
            for i in range(len(rules) - 1, -1, -1)
        ]

    async def _read_sheet_range(
        self,
        sheet_id: str,
        range_name: str
    ) -> list[list[Any]]:
        """
        Read a range of cells from a sheet.

        Args:
            sheet_id: Google Sheets ID.
            range_name: A1 notation range (e.g., "A1:Z100").

        Returns:
            2D list of cell values.
        """
        try:
            result = self._execute_with_retry(
                lambda: self.service.spreadsheets().values().get(
                    spreadsheetId=sheet_id,
                    range=range_name
                )
            )
            return result.get("values", [])

        except Exception as e:
            logger.error(f"Error reading sheet range: {e}")
            return []

    async def _write_sheet_range(
        self,
        sheet_id: str,
        range_name: str,
        values: list[list[Any]]
    ) -> bool:
        """
        Write values to a range of cells.

        Args:
            sheet_id: Google Sheets ID.
            range_name: A1 notation range.
            values: 2D list of values to write.

        Returns:
            True if write was successful.
        """
        try:
            self._execute_with_retry(
                lambda: self.service.spreadsheets().values().update(
                    spreadsheetId=sheet_id,
                    range=range_name,
                    valueInputOption="RAW",
                    body={"values": values}
                )
            )

            logger.info(f"Wrote {len(values)} rows to {range_name}")
            return True

        except Exception as e:
            logger.error(f"Error writing sheet range: {e}")
            return False

    async def _append_row(
        self,
        sheet_id: str,
        values: list[Any],
        tab_name: str | None = None,
    ) -> bool:
        """
        Append a row to the end of a sheet.

        Args:
            sheet_id: Google Sheets ID.
            values: List of values for the new row.
            tab_name: Optional tab name to target (e.g. "Tasks").

        Returns:
            True if append was successful.
        """
        return await self._append_rows(sheet_id, [values], tab_name=tab_name)

    async def _append_rows(
        self,
        sheet_id: str,
        values: list[list[Any]],
        tab_name: str | None = None,
    ) -> bool:
        """
        Append multiple rows to the end of a sheet.

        Args:
            sheet_id: Google Sheets ID.
            values: 2D list of values for the new rows.
            tab_name: Optional tab name to target (e.g. "Tasks").
                      When provided, uses "{tab_name}!A:I" to avoid
                      appending to the wrong tab.

        Returns:
            True if append was successful.
        """
        try:
            range_name = f"'{tab_name}'!A:I" if tab_name else "A:I"
            self._execute_with_retry(
                lambda: self.service.spreadsheets().values().append(
                    spreadsheetId=sheet_id,
                    range=range_name,
                    valueInputOption="RAW",
                    insertDataOption="INSERT_ROWS",
                    body={"values": values}
                )
            )

            logger.info(f"Appended {len(values)} rows to sheet")
            return True

        except Exception as e:
            logger.error(f"Error appending rows: {e}")
            return False

    async def _update_cell(self, sheet_id: str, range_name: str, value: str) -> None:
        """Update a single cell value."""
        self._execute_with_retry(
            lambda: self.service.spreadsheets().values().update(
                spreadsheetId=sheet_id,
                range=range_name,
                valueInputOption="RAW",
                body={"values": [[value]]},
            )
        )

    async def _append_row_to_range(
        self,
        sheet_id: str,
        range_name: str,
        values: list,
    ) -> None:
        """Append a row to a specific range in a sheet."""
        self._execute_with_retry(
            lambda: self.service.spreadsheets().values().append(
                spreadsheetId=sheet_id,
                range=range_name,
                valueInputOption="RAW",
                insertDataOption="INSERT_ROWS",
                body={"values": [values]},
            )
        )

    async def apply_stakeholder_update(
        self,
        organization: str,
        updates: dict,
    ) -> bool:
        """
        Apply an approved stakeholder update to the Stakeholder Tracker.

        Args:
            organization: Organization name to update.
            updates: Dict of field names to new values.

        Returns:
            True if update was applied successfully.
        """
        if not settings.STAKEHOLDER_TRACKER_SHEET_ID:
            logger.warning("STAKEHOLDER_TRACKER_SHEET_ID not configured")
            return False

        try:
            # Get all stakeholders to find the row
            all_stakeholders = await self.get_all_stakeholders()
            target_row = None

            for s in all_stakeholders:
                if s.get("organization_name", "").lower() == organization.lower():
                    target_row = s.get("row_number")
                    break

            if target_row:
                # Update existing row
                # Map update keys to column letters
                column_map = {
                    "organization_name": "A",
                    "type": "B",
                    "description": "C",
                    "contact_person": "D",
                    "desired_outcome": "E",
                    "priority": "F",
                    "primary_action_type": "G",
                    "owner": "H",
                    "next_action": "I",
                    "due_date": "J",
                    "status": "O",
                    "notes": "P",
                }

                for field, value in updates.items():
                    col = column_map.get(field)
                    if col:
                        await self._update_cell(
                            sheet_id=settings.STAKEHOLDER_TRACKER_SHEET_ID,
                            range_name=f"{col}{target_row}",
                            value=str(value),
                        )

                logger.info(f"Updated stakeholder: {organization} (row {target_row})")
            else:
                # Append new row
                new_row = [
                    updates.get("organization_name", organization),
                    updates.get("type", ""),
                    updates.get("description", ""),
                    updates.get("contact_person", ""),
                    updates.get("desired_outcome", ""),
                    updates.get("priority", ""),
                    updates.get("primary_action_type", ""),
                    updates.get("owner", ""),
                    updates.get("next_action", ""),
                    updates.get("due_date", ""),
                    "",  # secondary_action_type
                    "",  # secondary_owner
                    "",  # secondary_next_action
                    "",  # secondary_due_date
                    updates.get("status", "New"),
                    updates.get("notes", ""),
                ]

                tab = settings.STAKEHOLDER_TAB_NAME
                await self._append_row_to_range(
                    sheet_id=settings.STAKEHOLDER_TRACKER_SHEET_ID,
                    range_name=f"'{tab}'!A:P",
                    values=new_row,
                )

                logger.info(f"Added new stakeholder: {organization}")

            return True

        except Exception as e:
            logger.error(f"Error applying stakeholder update: {e}")
            return False

    # =========================================================================
    # Decisions Dashboard
    # =========================================================================

    async def ensure_decisions_tab(self) -> int | None:
        """
        Ensure a 'Decisions' tab exists in the Task Tracker spreadsheet.

        Creates the tab with a header row if it doesn't exist.

        Returns:
            The sheetId of the Decisions tab, or None if no spreadsheet configured.
        """
        if not settings.TASK_TRACKER_SHEET_ID:
            return None

        try:
            meta = self._execute_with_retry(
                lambda: self.service.spreadsheets().get(
                    spreadsheetId=settings.TASK_TRACKER_SHEET_ID,
                    fields="sheets.properties",
                )
            )

            for sheet in meta.get("sheets", []):
                props = sheet.get("properties", {})
                if props.get("title") == "Decisions":
                    logger.info(f"Decisions tab already exists (sheetId={props['sheetId']})")
                    return props["sheetId"]

            # Create the tab
            resp = self._execute_with_retry(
                lambda: self.service.spreadsheets().batchUpdate(
                    spreadsheetId=settings.TASK_TRACKER_SHEET_ID,
                    body={
                        "requests": [
                            {"addSheet": {"properties": {"title": "Decisions"}}}
                        ]
                    },
                )
            )

            new_sheet_id = resp["replies"][0]["addSheet"]["properties"]["sheetId"]
            logger.info(f"Created Decisions tab (sheetId={new_sheet_id})")

            # Write header row (Phase 9A schema with label, rationale, confidence;
            # + ID at col H under DECISION_RECONCILE_ENABLED — Phase 2).
            headers = _decision_headers()
            end_col = chr(ord("A") + len(headers) - 1)
            self._execute_with_retry(
                lambda: self.service.spreadsheets().values().update(
                    spreadsheetId=settings.TASK_TRACKER_SHEET_ID,
                    range=f"Decisions!A1:{end_col}1",
                    valueInputOption="RAW",
                    body={"values": [headers]},
                )
            )

            return new_sheet_id

        except Exception as e:
            logger.error(f"Error ensuring Decisions tab: {e}")
            return None

    # =========================================================================
    # Meetings tab (2026-07) — follow_up_meetings get their own home.
    # =========================================================================

    async def ensure_meetings_tab(self) -> int | None:
        """Ensure the 'Meetings' tab exists, with its header row.

        Appended AFTER the existing tabs — never inserted before Tasks. A tab at
        index 0 is what silently broke every sheet read in April 2026, because
        bare A1 ranges resolve against whichever sheet sits first.
        """
        if not settings.TASK_TRACKER_SHEET_ID:
            return None
        try:
            meta = self._execute_with_retry(
                lambda: self.service.spreadsheets().get(
                    spreadsheetId=settings.TASK_TRACKER_SHEET_ID,
                    fields="sheets.properties",
                )
            )
            sheets = meta.get("sheets", [])
            for sheet in sheets:
                props = sheet.get("properties", {})
                if props.get("title") == MEETING_TAB_NAME:
                    return props["sheetId"]

            resp = self._execute_with_retry(
                lambda: self.service.spreadsheets().batchUpdate(
                    spreadsheetId=settings.TASK_TRACKER_SHEET_ID,
                    body={"requests": [{"addSheet": {"properties": {
                        "title": MEETING_TAB_NAME,
                        "index": len(sheets),   # last, never index 0
                    }}}]},
                )
            )
            new_sheet_id = resp["replies"][0]["addSheet"]["properties"]["sheetId"]
            logger.info(f"Created {MEETING_TAB_NAME} tab (sheetId={new_sheet_id})")

            end_col = max(MEETING_COLUMNS.values())
            self._execute_with_retry(
                lambda: self.service.spreadsheets().values().update(
                    spreadsheetId=settings.TASK_TRACKER_SHEET_ID,
                    range=f"'{MEETING_TAB_NAME}'!A1:{end_col}1",
                    valueInputOption="RAW",
                    body={"values": [MEETING_TRACKER_HEADERS]},
                )
            )
            return new_sheet_id
        except Exception as e:
            logger.error(f"Error ensuring {MEETING_TAB_NAME} tab: {e}")
            return None

    @staticmethod
    def _meeting_row(m: dict, source_meeting: str = "") -> list:
        """Build one Meetings-tab row from a follow_up_meetings record."""
        parts = m.get("participants") or []
        agenda = m.get("agenda_items") or []
        src = source_meeting
        if not src:
            mi = m.get("meetings") if isinstance(m.get("meetings"), dict) else {}
            src = (mi or {}).get("title", "") or ""
        return [
            m.get("title") or "",
            m.get("label") or "",
            m.get("led_by") or "",
            _fmt_day(m.get("proposed_date")),
            ", ".join(parts) if isinstance(parts, list) else str(parts or ""),
            m.get("status") or "not_scheduled",
            "; ".join(agenda) if isinstance(agenda, list) else str(agenda or ""),
            m.get("prep_needed") or "",
            src,
            m.get("id") or "",
        ]

    async def get_all_meetings(self) -> list[dict]:
        """Read the Meetings tab, one dict per row with row_number + id."""
        if not settings.TASK_TRACKER_SHEET_ID:
            return []
        num_cols = len(MEETING_TRACKER_HEADERS)
        end_col = max(MEETING_COLUMNS.values())
        rows = await self._read_sheet_range(
            sheet_id=settings.TASK_TRACKER_SHEET_ID,
            range_name=f"'{MEETING_TAB_NAME}'!A:{end_col}",
        )
        if not rows or len(rows) < 2:
            return []
        out = []
        for i, row in enumerate(rows[1:], start=2):
            while len(row) < num_cols:
                row.append("")
            raw_date = row[MEETING_COL_INDEX["proposed_date"]]
            out.append({
                "row_number": i,
                "title": row[MEETING_COL_INDEX["title"]],
                "label": row[MEETING_COL_INDEX["label"]],
                "led_by": row[MEETING_COL_INDEX["led_by"]],
                # Day-first parsing, same convention as the Tasks deadline cell:
                # "20.6.26" must mean 20 June everywhere.
                "proposed_date": parse_human_date(raw_date) or raw_date,
                "proposed_date_raw": raw_date,
                "participants": row[MEETING_COL_INDEX["participants"]],
                "status": row[MEETING_COL_INDEX["status"]],
                "agenda": row[MEETING_COL_INDEX["agenda"]],
                "prep_needed": row[MEETING_COL_INDEX["prep_needed"]],
                "source_meeting": row[MEETING_COL_INDEX["source_meeting"]],
                "id": row[MEETING_COL_INDEX["id"]],
            })
        return out

    async def add_meetings_batch_to_sheet(
        self, meetings: list[dict], source_meeting: str = ""
    ) -> bool:
        """Append follow-up meetings to the Meetings tab (with their UUIDs).

        Replaces add_follow_ups_as_tasks(), which wrote a "Schedule: X" row into
        the TASKS tab with NO col-J UUID — so reconcile classified every one as
        hand-added and created a duplicate `tasks` row on each run.
        """
        if not settings.TASK_TRACKER_SHEET_ID or not meetings:
            return False
        try:
            await self.ensure_meetings_tab()
            values = [self._meeting_row(m, source_meeting) for m in meetings]
            end_col = max(MEETING_COLUMNS.values())
            self._execute_with_retry(
                lambda: self.service.spreadsheets().values().append(
                    spreadsheetId=settings.TASK_TRACKER_SHEET_ID,
                    range=f"'{MEETING_TAB_NAME}'!A:{end_col}",
                    valueInputOption="RAW",
                    insertDataOption="INSERT_ROWS",
                    body={"values": values},
                )
            )
            logger.info(f"Added {len(values)} meeting(s) to the {MEETING_TAB_NAME} tab")
            return True
        except Exception as e:
            logger.error(f"Error adding meetings to sheet: {e}")
            return False

    async def rebuild_meetings_sheet(
        self, meetings_from_db: list[dict], force_empty: bool = False
    ) -> bool:
        """Clear + rewrite the Meetings tab from the DB.

        Carries the same force_empty guard as rebuild_tasks_sheet: a transient
        Supabase read returning [] must never clear a populated tab. That is
        exactly how the Tasks sheet was wiped in April.
        """
        if not settings.TASK_TRACKER_SHEET_ID:
            return False
        if not meetings_from_db and not force_empty:
            logger.error(
                "rebuild_meetings_sheet called with 0 rows and force_empty=False "
                "— refusing to clear a populated tab (transient read guard)."
            )
            return False
        try:
            await self.ensure_meetings_tab()
            end_col = max(MEETING_COLUMNS.values())
            self._execute_with_retry(
                lambda: self.service.spreadsheets().values().clear(
                    spreadsheetId=settings.TASK_TRACKER_SHEET_ID,
                    range=f"'{MEETING_TAB_NAME}'!A2:{end_col}",
                    body={},
                )
            )
            if meetings_from_db:
                rows = [self._meeting_row(m) for m in _sorted_meetings(meetings_from_db)]
                self._execute_with_retry(
                    lambda: self.service.spreadsheets().values().update(
                        spreadsheetId=settings.TASK_TRACKER_SHEET_ID,
                        range=f"'{MEETING_TAB_NAME}'!A2:{end_col}{len(rows) + 1}",
                        valueInputOption="RAW",
                        body={"values": rows},
                    )
                )
            try:
                from services.supabase_client import supabase_client
                supabase_client.log_action(
                    action="sheets_rebuild_meetings",
                    details={"row_count": len(meetings_from_db)},
                    triggered_by="system",
                )
            except Exception as log_err:
                logger.warning(f"Could not audit-log meetings rebuild: {log_err}")
            await self.format_meetings_tab()
            return True
        except Exception as e:
            logger.error(f"Error rebuilding Meetings sheet: {e}")
            return False

    # =========================================================================
    # Read-only reference tabs — Open Questions + Areas.
    # =========================================================================

    async def _ensure_tab(self, tab_name: str, headers: list[str]) -> int | None:
        """Create `tab_name` with a header row if missing; return its sheetId.

        Always appended LAST. A tab at index 0 is what silently broke every
        sheet read in April 2026, because bare A1 ranges resolve against
        whichever sheet sits first.
        """
        if not settings.TASK_TRACKER_SHEET_ID:
            return None
        try:
            meta = self._execute_with_retry(
                lambda: self.service.spreadsheets().get(
                    spreadsheetId=settings.TASK_TRACKER_SHEET_ID,
                    fields="sheets.properties",
                )
            )
            sheets = meta.get("sheets", [])
            for sheet in sheets:
                props = sheet.get("properties", {})
                if props.get("title") == tab_name:
                    return props["sheetId"]
            resp = self._execute_with_retry(
                lambda: self.service.spreadsheets().batchUpdate(
                    spreadsheetId=settings.TASK_TRACKER_SHEET_ID,
                    body={"requests": [{"addSheet": {"properties": {
                        "title": tab_name, "index": len(sheets),
                    }}}]},
                )
            )
            sid = resp["replies"][0]["addSheet"]["properties"]["sheetId"]
            end_col = chr(ord("A") + len(headers) - 1)
            self._execute_with_retry(
                lambda: self.service.spreadsheets().values().update(
                    spreadsheetId=settings.TASK_TRACKER_SHEET_ID,
                    range=f"'{tab_name}'!A1:{end_col}1",
                    valueInputOption="RAW",
                    body={"values": [headers]},
                )
            )
            logger.info(f"Created {tab_name} tab (sheetId={sid})")
            return sid
        except Exception as e:
            logger.error(f"Error ensuring {tab_name} tab: {e}")
            return None

    async def _rebuild_readonly_tab(
        self, tab_name: str, headers: list[str], rows: list[list],
        force_empty: bool = False,
    ) -> bool:
        """Clear + rewrite a generated tab, then lock it.

        `force_empty` guard is mandatory here for the same reason as
        rebuild_tasks_sheet: a transient Supabase read returning [] must never
        clear a populated tab. That is precisely how the Tasks sheet was wiped
        in April 2026.
        """
        if not settings.TASK_TRACKER_SHEET_ID:
            return False
        if not rows and not force_empty:
            logger.error(
                f"_rebuild_readonly_tab({tab_name}) called with 0 rows and "
                f"force_empty=False — refusing to clear a populated tab."
            )
            return False
        try:
            sid = await self._ensure_tab(tab_name, headers)
            if sid is None:
                return False
            end_col = chr(ord("A") + len(headers) - 1)
            self._execute_with_retry(
                lambda: self.service.spreadsheets().values().clear(
                    spreadsheetId=settings.TASK_TRACKER_SHEET_ID,
                    range=f"'{tab_name}'!A2:{end_col}",
                    body={},
                )
            )
            if rows:
                self._execute_with_retry(
                    lambda: self.service.spreadsheets().values().update(
                        spreadsheetId=settings.TASK_TRACKER_SHEET_ID,
                        range=f"'{tab_name}'!A2:{end_col}{len(rows) + 1}",
                        valueInputOption="RAW",
                        body={"values": rows},
                    )
                )
            await self._format_readonly_tab(sid, tab_name, len(headers))
            logger.info(f"Rebuilt {tab_name}: {len(rows)} row(s)")
            return True
        except Exception as e:
            logger.error(f"Error rebuilding {tab_name}: {e}")
            return False

    async def _format_readonly_tab(self, sid: int, tab_name: str, n_cols: int) -> bool:
        """Header styling, widths, banding, and a whole-tab protected range."""
        try:
            desc = f"Gianluigi: {tab_name} is generated — edits are overwritten"
            requests: list[dict] = [
                {"updateSheetProperties": {
                    "properties": {"sheetId": sid,
                                   "gridProperties": {"frozenRowCount": 1}},
                    "fields": "gridProperties.frozenRowCount"}},
                {"repeatCell": {
                    "range": {"sheetId": sid, "startRowIndex": 0, "endRowIndex": 1,
                              "startColumnIndex": 0, "endColumnIndex": n_cols},
                    "cell": {"userEnteredFormat": {
                        "backgroundColor": COLORS["header_bg"],
                        "textFormat": {"bold": True,
                                       "foregroundColor": COLORS["header_text"]}}},
                    "fields": "userEnteredFormat(backgroundColor,textFormat)"}},
                _border_request(sid, n_cols),
            ]
            requests.append(_text_wrap_request(sid, 0))
            try:
                pmeta = self._execute_with_retry(
                    lambda: self.service.spreadsheets().get(
                        spreadsheetId=settings.TASK_TRACKER_SHEET_ID,
                        fields="sheets(properties.sheetId,protectedRanges)",
                    )
                )
                for sheet in pmeta.get("sheets", []):
                    if sheet.get("properties", {}).get("sheetId") != sid:
                        continue
                    for pr in sheet.get("protectedRanges", []):
                        if pr.get("description") == desc:
                            requests.append({"deleteProtectedRange": {
                                "protectedRangeId": pr["protectedRangeId"]}})
            except Exception:
                pass
            # Whole tab, not just some columns — every cell here is generated,
            # and an edit would be silently discarded on the next rebuild.
            requests.append({"addProtectedRange": {"protectedRange": {
                "range": {"sheetId": sid},
                "description": desc,
                "warningOnly": True,
            }}})
            self._execute_with_retry(
                lambda: self.service.spreadsheets().batchUpdate(
                    spreadsheetId=settings.TASK_TRACKER_SHEET_ID,
                    body={"requests": requests},
                )
            )
            return True
        except Exception as e:
            logger.warning(f"Could not format {tab_name}: {e}")
            return False

    async def rebuild_questions_tab(self, questions: list[dict]) -> bool:
        """Open Questions — open only, newest first, grouped by Project.

        The FILTER is the feature. There are 100+ open questions going back to
        May; dumping all of them produces a tab nobody opens. Aging (60d ->
        stale) happens in processors/question_lifecycle; this renders whatever
        is still `open`.
        """
        rows = [[
            (q.get("question") or "")[:500],
            q.get("raised_by") or "",
            q.get("label") or "",
            q.get("age_days", ""),
            q.get("source_meeting") or "",
            q.get("status") or "open",
            q.get("id") or "",
        ] for q in questions]
        return await self._rebuild_readonly_tab(
            QUESTIONS_TAB_NAME, QUESTIONS_HEADERS, rows, force_empty=True
        )

    async def rebuild_areas_tab(self, areas: list[dict]) -> bool:
        """Areas — the index into every other tab, one row per Gantt area."""
        rows = [[
            a.get("name") or "",
            a.get("open_tasks", 0),
            a.get("overdue", 0),
            a.get("open_questions", 0),
            a.get("meetings_to_schedule", 0),
            a.get("last_activity") or "",
            (a.get("current_focus") or "")[:400],
        ] for a in areas]
        return await self._rebuild_readonly_tab(
            AREAS_TAB_NAME, AREAS_HEADERS, rows
        )

    async def add_decisions_batch_to_sheet(
        self,
        decisions: list[dict],
        source_meeting: str,
        meeting_date: str,
    ) -> bool:
        """
        Add a batch of decisions to the Decisions tab.

        Args:
            decisions: List of decision dicts from extraction.
            source_meeting: Name of the source meeting.
            meeting_date: Date of the meeting.

        Returns:
            True if decisions were added successfully.
        """
        if not decisions:
            return True

        if not settings.TASK_TRACKER_SHEET_ID:
            logger.warning("TASK_TRACKER_SHEET_ID not configured")
            return False

        try:
            sheet_id = await self.ensure_decisions_tab()
            if sheet_id is None:
                return False

            include_id = _decision_id_enabled()
            end_col = "H" if include_id else "G"
            rows = []
            for d in decisions:
                row = [
                    d.get("label", ""),
                    d.get("description", ""),
                    d.get("rationale", ""),
                    _confidence_cell(d),
                    source_meeting,
                    str(meeting_date)[:10],
                    d.get("decision_status", "Active"),
                ]
                if include_id:
                    row.append(d.get("id", ""))   # col H — reconcile identity key
                rows.append(row)

            self._execute_with_retry(
                lambda: self.service.spreadsheets().values().append(
                    spreadsheetId=settings.TASK_TRACKER_SHEET_ID,
                    range=f"Decisions!A:{end_col}",
                    valueInputOption="RAW",
                    insertDataOption="INSERT_ROWS",
                    body={"values": rows},
                )
            )

            logger.info(f"Added {len(rows)} decisions to Decisions tab")
            return True

        except Exception as e:
            logger.error(f"Error adding decisions to sheet: {e}")
            return False

    async def format_meetings_tab(self) -> bool:
        """Format the Meetings tab: header, widths, status colours, protection.

        No data-validation dropdowns — they were removed from Tasks for causing
        errors against existing data (and Hebrew values), so Status uses
        conditional formatting instead.
        """
        if not settings.TASK_TRACKER_SHEET_ID:
            return False
        try:
            sid = await self.ensure_meetings_tab()
            if sid is None:
                return False
            n_cols = len(MEETING_TRACKER_HEADERS)
            requests: list[dict] = []

            requests.append({
                "updateSheetProperties": {
                    "properties": {"sheetId": sid,
                                   "gridProperties": {"frozenRowCount": 1}},
                    "fields": "gridProperties.frozenRowCount",
                }
            })
            requests.append({
                "repeatCell": {
                    "range": {"sheetId": sid, "startRowIndex": 0, "endRowIndex": 1,
                              "startColumnIndex": 0, "endColumnIndex": n_cols},
                    "cell": {"userEnteredFormat": {
                        "backgroundColor": COLORS["header_bg"],
                        "textFormat": {"bold": True, "foregroundColor": COLORS["header_text"]},
                    }},
                    "fields": "userEnteredFormat(backgroundColor,textFormat)",
                }
            })

            # A=Meeting, B=Project, C=Led By, D=Proposed, E=Participants,
            # F=Status, G=Agenda, H=Prep, I=Source, J=ID
            for i, w in enumerate([300, 130, 120, 110, 180, 110, 260, 200, 150, 70]):
                requests.append(_column_width_request(sid, i, w))
            requests.append(_text_wrap_request(sid, MEETING_COL_INDEX["title"]))
            requests.append(_text_wrap_request(sid, MEETING_COL_INDEX["agenda"]))

            requests.extend(self._clear_conditional_format_rules_for_sheet(
                settings.TASK_TRACKER_SHEET_ID, sid))
            status_rules = [
                ("not_scheduled", COLORS["status_overdue"]),   # the queue — stands out
                ("scheduled", COLORS["status_in_progress"]),
                ("held", COLORS["status_done"]),
                ("dropped", COLORS["status_inactive"]),
            ]
            for idx, (text, color) in enumerate(status_rules):
                requests.append(_conditional_format_rule(
                    sid, MEETING_COL_INDEX["status"], text, color, idx))

            requests.append(_border_request(sid, n_cols))

            # G:J are system-owned (agenda / prep / source / id).
            _DESC = "Gianluigi: system-owned (agenda / prep / source / id)"
            try:
                pmeta = self._execute_with_retry(
                    lambda: self.service.spreadsheets().get(
                        spreadsheetId=settings.TASK_TRACKER_SHEET_ID,
                        fields="sheets(properties.sheetId,protectedRanges)",
                    )
                )
                for sheet in pmeta.get("sheets", []):
                    if sheet.get("properties", {}).get("sheetId") != sid:
                        continue
                    for pr in sheet.get("protectedRanges", []):
                        if pr.get("description") == _DESC:
                            requests.append({"deleteProtectedRange": {
                                "protectedRangeId": pr["protectedRangeId"]}})
            except Exception:
                pass
            requests.append({
                "addProtectedRange": {
                    "protectedRange": {
                        "range": {
                            "sheetId": sid,
                            "startRowIndex": 1,
                            "startColumnIndex": MEETING_COL_INDEX["agenda"],
                            "endColumnIndex": MEETING_COL_INDEX["id"] + 1,
                        },
                        "description": _DESC,
                        "warningOnly": True,
                    }
                }
            })

            self._execute_with_retry(
                lambda: self.service.spreadsheets().batchUpdate(
                    spreadsheetId=settings.TASK_TRACKER_SHEET_ID,
                    body={"requests": requests},
                )
            )
            logger.info("Applied formatting to the Meetings tab")
            return True
        except Exception as e:
            logger.error(f"Error formatting Meetings tab: {e}")
            return False

    async def format_decision_tracker(self, sheet_id: int | None = None) -> bool:
        """Protect the system-owned Decisions columns (Phase 2, editable sheet).

        Editable (Eyal-owned): Label(A), Decision(B), Rationale(C), Confidence(D),
        Status(G). System-owned/locked: Source Meeting(E), Date(F), id(H). Because
        Status(G) is editable and sits between them, E:F and H are TWO ranges (not
        one contiguous block like tasks). warningOnly so the bot's own writes are
        never blocked; idempotent (drop any prior Gianluigi decision protection,
        then re-add). No-op unless the id column exists (reconcile live).
        """
        if not settings.TASK_TRACKER_SHEET_ID:
            return False
        if not _decision_id_enabled():
            return True  # A:G layout has no locked identity column yet

        try:
            sid = sheet_id if sheet_id is not None else await self.ensure_decisions_tab()
            if sid is None:
                return False

            _EF_DESC = "Gianluigi: system-owned (source_meeting / date)"
            _ID_DESC = "Gianluigi: system-owned (id)"
            requests: list[dict] = []

            # Drop any prior Gianluigi decision protections (idempotent re-apply).
            try:
                pmeta = self._execute_with_retry(
                    lambda: self.service.spreadsheets().get(
                        spreadsheetId=settings.TASK_TRACKER_SHEET_ID,
                        fields="sheets(properties.sheetId,protectedRanges)",
                    )
                )
                for sheet in pmeta.get("sheets", []):
                    if sheet.get("properties", {}).get("sheetId") != sid:
                        continue
                    for pr in sheet.get("protectedRanges", []):
                        if pr.get("description") in (_EF_DESC, _ID_DESC):
                            requests.append({
                                "deleteProtectedRange": {
                                    "protectedRangeId": pr["protectedRangeId"]
                                }
                            })
            except Exception:
                pass

            src = DECISION_COL_INDEX["source_meeting"]  # E = 4
            date_i = DECISION_COL_INDEX["date"]          # F = 5
            id_i = len(DECISION_TRACKER_HEADERS)         # H = 7 (base has no id)
            for start, end, desc in (
                (src, date_i + 1, _EF_DESC),   # E:F
                (id_i, id_i + 1, _ID_DESC),    # H
            ):
                requests.append({
                    "addProtectedRange": {
                        "protectedRange": {
                            "range": {
                                "sheetId": sid,
                                "startRowIndex": 1,  # skip header
                                "startColumnIndex": start,
                                "endColumnIndex": end,
                            },
                            "description": desc,
                            "warningOnly": True,
                        }
                    }
                })

            self._execute_with_retry(
                lambda: self.service.spreadsheets().batchUpdate(
                    spreadsheetId=settings.TASK_TRACKER_SHEET_ID,
                    body={"requests": requests},
                )
            )
            logger.info("Protected system-owned Decisions columns (E:F, H)")
            return True

        except Exception as e:
            logger.error(f"Error formatting Decision Tracker: {e}")
            return False

    # =========================================================================
    # Sheet Formatting
    # =========================================================================

    async def format_task_tracker(self) -> bool:
        """
        Apply professional formatting to the Task Tracker sheet.

        Uses TASK_COLUMNS/TASK_COL_INDEX for column positions.
        No data validation dropdowns (they cause errors with existing data).

        Includes:
        - Dark blue header row with white bold text
        - Frozen header row
        - Fixed column widths: A=50, B=120, C=350, D=90, E=90, F=90, G=120, H=150, I=80
        - Text wrapping on Task column (C)
        - Conditional formatting on Status (F), Category (G), Priority (A)
        - Alternating row colors (zebra striping)
        - Light gray borders on all cells

        Returns:
            True if formatting was applied successfully.
        """
        if not settings.TASK_TRACKER_SHEET_ID:
            logger.warning("TASK_TRACKER_SHEET_ID not configured")
            return False

        try:
            # Find the Tasks tab by name (don't assume it's the first tab)
            sid = self._get_sheet_id_by_name(
                settings.TASK_TRACKER_SHEET_ID, settings.TASK_TRACKER_TAB_NAME
            )
            if sid is None:
                # Fallback to first sheet if tab not found
                sid = self._get_first_sheet_id(settings.TASK_TRACKER_SHEET_ID)
            num_cols = len(TASK_COLUMNS)
            requests = []

            # --- FIRST: Reset ALL rows (1-1000) to white bg + black text ---
            # Must happen before header formatting to prevent inheritance
            requests.append({
                "repeatCell": {
                    "range": {
                        "sheetId": sid,
                        "startRowIndex": 0,
                        "endRowIndex": 1000,
                        "startColumnIndex": 0,
                        "endColumnIndex": num_cols,
                    },
                    "cell": {
                        "userEnteredFormat": {
                            "backgroundColor": {"red": 1, "green": 1, "blue": 1},
                            "textFormat": {
                                "bold": False,
                                "foregroundColor": {"red": 0, "green": 0, "blue": 0},
                                "fontSize": 10,
                            },
                        }
                    },
                    "fields": "userEnteredFormat(backgroundColor,textFormat)",
                }
            })

            # --- Clear existing conditional format rules (idempotent) ---
            requests.extend(
                self._clear_conditional_format_rules(settings.TASK_TRACKER_SHEET_ID)
            )

            # --- Frozen header row ---
            requests.append({
                "updateSheetProperties": {
                    "properties": {
                        "sheetId": sid,
                        "gridProperties": {"frozenRowCount": 1},
                    },
                    "fields": "gridProperties.frozenRowCount",
                }
            })

            # --- Dark blue header with white bold text ---
            requests.append({
                "repeatCell": {
                    "range": {
                        "sheetId": sid,
                        "startRowIndex": 0,
                        "endRowIndex": 1,
                        "startColumnIndex": 0,
                        "endColumnIndex": num_cols,
                    },
                    "cell": {
                        "userEnteredFormat": {
                            "backgroundColor": COLORS["header_bg"],
                            "textFormat": {
                                "bold": True,
                                "foregroundColor": COLORS["header_text"],
                            },
                        }
                    },
                    "fields": "userEnteredFormat(backgroundColor,textFormat)",
                }
            })

            # --- Fixed column widths ---
            # A=Priority(50), B=Project(120), C=Task(350), D=Owner(120),
            # E=Deadline(90), F=Status(90), G=Area(150), H=Source(150),
            # I=Created(80), J=ID(70), K=Urgency(70), L=Last Update(95)
            #
            # This list used to stop at I — 9 widths for a 10/11-column layout —
            # so the identity and urgency columns rendered at whatever width
            # they happened to have. Owner widened to 120 now that assignees are
            # full names (was 90, which truncated "Paolo Vailetti"). [2026-07-22]
            col_widths = [50, 120, 350, 120, 90, 90, 150, 150, 80]
            if "id" in TASK_COL_INDEX:
                col_widths.append(70)
            if "urgency" in TASK_COL_INDEX:
                col_widths.append(70)
            if "last_update" in TASK_COL_INDEX:
                col_widths.append(95)
            for i, w in enumerate(col_widths):
                requests.append(_column_width_request(sid, i, w))

            # --- Text wrapping on Task column (C) ---
            requests.append(_text_wrap_request(sid, TASK_COL_INDEX["task"]))

            # --- Conditional formatting: Priority column (A) ---
            rule_idx = 0
            priority_rules = [
                ("H", COLORS["priority_high"]),
                ("M", COLORS["priority_medium"]),
                ("L", COLORS["priority_low"]),
            ]
            for text, color in priority_rules:
                requests.append(
                    _conditional_format_rule(sid, TASK_COL_INDEX["priority"], text, color, rule_idx)
                )
                rule_idx += 1

            # --- Conditional formatting: Status column (F) ---
            status_rules = [
                ("overdue", COLORS["status_overdue"]),
                ("done", COLORS["status_done"]),
                ("in_progress", COLORS["status_in_progress"]),
                ("pending", COLORS["status_pending"]),
            ]
            for text, color in status_rules:
                requests.append(
                    _conditional_format_rule(sid, TASK_COL_INDEX["status"], text, color, rule_idx)
                )
                rule_idx += 1

            # --- Conditional formatting: Category column (G) ---
            # Gantt-area taxonomy (2026-06 realignment)
            category_rules = [
                ("PRODUCT & TECHNOLOGY", COLORS["product_tech"]),
                ("SALES & BUSINESS DEVELOPMENT", COLORS["business_dev"]),
                ("FUNDRAISING & INVESTOR RELATIONS", COLORS["finance"]),
                ("LEGAL, CORPORATE & FINANCE", COLORS["legal_ip"]),
                ("CLIENT DELIVERY & OPERATIONS", COLORS["marketing"]),
                ("TEAM & HUMAN RESOURCES", COLORS["operations_hr"]),
            ]
            for text, color in category_rules:
                requests.append(
                    _conditional_format_rule(sid, TASK_COL_INDEX["category"], text, color, rule_idx)
                )
                rule_idx += 1

            # --- Staleness on Last Update (L) ---
            # Red BEFORE amber: rules are evaluated in order and the first match
            # wins, so the 60-day rule must be registered first or everything
            # over 30 days would paint amber and the 60-day rule never fire.
            if "last_update" in TASK_COL_INDEX:
                for days, color in ((60, COLORS["stale_alert"]), (30, COLORS["stale_warn"])):
                    requests.append(
                        _staleness_format_rule(
                            sid, TASK_COL_INDEX["last_update"], days, color, rule_idx
                        )
                    )
                    rule_idx += 1

            # --- NO data validation dropdowns (removed — they cause errors) ---

            # --- Remove existing banding then add fresh (idempotent) ---
            try:
                meta = self._execute_with_retry(
                    lambda: self.service.spreadsheets().get(
                        spreadsheetId=settings.TASK_TRACKER_SHEET_ID,
                        fields="sheets.bandedRanges",
                    )
                )
                for sheet in meta.get("sheets", []):
                    for br in sheet.get("bandedRanges", []):
                        requests.append({
                            "deleteBanding": {"bandedRangeId": br["bandedRangeId"]}
                        })
            except Exception:
                pass
            requests.append(_banding_request(sid, num_cols))

            # --- Light gray borders on all cells ---
            requests.append(_border_request(sid, num_cols))

            # --- Protect the system-owned info columns H/I/J (source_meeting,
            #     created, id) so they can't be hand-edited (Phase 1, 2026-07).
            #     Task text (C) + Label (B) and the action fields stay editable —
            #     only the pure-info/identity columns are locked. warningOnly so
            #     the bot's own writes are never blocked; idempotent (drop any
            #     prior Gianluigi protection, then re-add one covering H:J). ---
            _PROTECT_DESC = "Gianluigi: system-owned (source_meeting / created / id)"
            _LAST_UPDATE_PROTECT_DESC = "Gianluigi: system-owned (last update)"
            try:
                pmeta = self._execute_with_retry(
                    lambda: self.service.spreadsheets().get(
                        spreadsheetId=settings.TASK_TRACKER_SHEET_ID,
                        fields="sheets(properties.sheetId,protectedRanges)",
                    )
                )
                for sheet in pmeta.get("sheets", []):
                    if sheet.get("properties", {}).get("sheetId") != sid:
                        continue
                    for pr in sheet.get("protectedRanges", []):
                        if pr.get("description") in (_PROTECT_DESC, _LAST_UPDATE_PROTECT_DESC):
                            requests.append({
                                "deleteProtectedRange": {
                                    "protectedRangeId": pr["protectedRangeId"]
                                }
                            })
            except Exception:
                pass
            requests.append({
                "addProtectedRange": {
                    "protectedRange": {
                        "range": {
                            "sheetId": sid,
                            "startRowIndex": 1,  # skip the header row
                            "startColumnIndex": TASK_COL_INDEX["source_meeting"],  # H
                            "endColumnIndex": TASK_COL_INDEX["id"] + 1,            # through J
                        },
                        "description": _PROTECT_DESC,
                        "warningOnly": True,
                    }
                }
            })
            # Last Update (L) is system-owned too, but NOT contiguous with H:J —
            # Urgency (K) sits between them and stays editable — so it needs its
            # own range. Same delete-then-re-add idempotence via its description.
            if "last_update" in TASK_COL_INDEX:
                requests.append({
                    "addProtectedRange": {
                        "protectedRange": {
                            "range": {
                                "sheetId": sid,
                                "startRowIndex": 1,
                                "startColumnIndex": TASK_COL_INDEX["last_update"],
                                "endColumnIndex": TASK_COL_INDEX["last_update"] + 1,
                            },
                            "description": _LAST_UPDATE_PROTECT_DESC,
                            "warningOnly": True,
                        }
                    }
                })

            self._execute_with_retry(
                lambda: self.service.spreadsheets().batchUpdate(
                    spreadsheetId=settings.TASK_TRACKER_SHEET_ID,
                    body={"requests": requests},
                )
            )

            logger.info("Applied professional formatting to Task Tracker")
            return True

        except Exception as e:
            logger.error(f"Error formatting Task Tracker: {e}")
            return False

    async def format_stakeholder_tracker(self) -> bool:
        """
        Apply professional formatting to the Stakeholder Tracker sheet.

        Includes:
        - Dark blue header row with white bold text
        - Frozen header row
        - Fixed column widths
        - Text wrapping on Description (C) and Notes (P)
        - Conditional formatting on Status (O), Priority (F)
        - Data validation dropdowns on Status, Priority
        - Alternating row colors (zebra striping)
        - Light gray borders on all cells
        - Clears existing conditional format rules first (idempotent)

        Returns:
            True if formatting was applied successfully.
        """
        if not settings.STAKEHOLDER_TRACKER_SHEET_ID:
            logger.warning("STAKEHOLDER_TRACKER_SHEET_ID not configured")
            return False

        try:
            num_cols = len(STAKEHOLDER_COLUMNS)  # 19 columns
            sid = self._get_first_sheet_id(settings.STAKEHOLDER_TRACKER_SHEET_ID)
            requests = []

            # --- Clear existing conditional format rules (idempotent) ---
            requests.extend(
                self._clear_conditional_format_rules(
                    settings.STAKEHOLDER_TRACKER_SHEET_ID
                )
            )

            # --- Frozen header row ---
            requests.append({
                "updateSheetProperties": {
                    "properties": {
                        "sheetId": sid,
                        "gridProperties": {"frozenRowCount": 1},
                    },
                    "fields": "gridProperties.frozenRowCount",
                }
            })

            # --- Dark blue header with white bold text ---
            requests.append({
                "repeatCell": {
                    "range": {
                        "sheetId": sid,
                        "startRowIndex": 0,
                        "endRowIndex": 1,
                        "startColumnIndex": 0,
                        "endColumnIndex": num_cols,
                    },
                    "cell": {
                        "userEnteredFormat": {
                            "backgroundColor": COLORS["header_bg"],
                            "textFormat": {
                                "bold": True,
                                "foregroundColor": COLORS["header_text"],
                            },
                        }
                    },
                    "fields": "userEnteredFormat(backgroundColor,textFormat)",
                }
            })

            # --- Fixed column widths ---
            # A=180, B=90, C=200, D=160, E=160, F=70,
            # G-J=120 each (primary action), K-N=120 each (secondary action),
            # O=90, P=200
            col_widths = [
                180, 90, 200, 160, 160, 70,    # A-F
                120, 120, 120, 120,             # G-J (Primary Action)
                120, 120, 120, 120,             # K-N (Secondary Action)
                90, 200,                        # O-P
            ]
            for i, w in enumerate(col_widths):
                requests.append(_column_width_request(sid, i, w))

            # --- Text wrapping on Description (C=2) and Notes (P=15) ---
            requests.append(_text_wrap_request(sid, 2))
            requests.append(_text_wrap_request(sid, 15))

            # --- Conditional formatting: Status column (O = index 14) ---
            rule_idx = 0
            status_rules = [
                ("New", COLORS["status_new"]),
                ("Active", COLORS["status_active"]),
                ("Inactive", COLORS["status_inactive"]),
                ("Completed", COLORS["status_completed"]),
            ]
            for text, color in status_rules:
                requests.append(
                    _conditional_format_rule(sid, 14, text, color, rule_idx)
                )
                rule_idx += 1

            # --- Conditional formatting: Priority column (F = index 5) ---
            priority_rules = [
                ("H", COLORS["priority_high"]),
                ("M", COLORS["priority_medium"]),
                ("L", COLORS["priority_low"]),
            ]
            for text, color in priority_rules:
                requests.append(
                    _conditional_format_rule(sid, 5, text, color, rule_idx)
                )
                rule_idx += 1

            # --- Data validation dropdowns ---
            requests.append(
                _data_validation_request(sid, 14, STAKEHOLDER_STATUSES)  # Status (O)
            )
            requests.append(
                _data_validation_request(sid, 5, PRIORITIES)  # Priority (F)
            )

            # --- Alternating row colors (zebra striping) ---
            requests.append(_banding_request(sid, num_cols))

            # --- Light gray borders on all cells ---
            requests.append(_border_request(sid, num_cols))

            self._execute_with_retry(
                lambda: self.service.spreadsheets().batchUpdate(
                    spreadsheetId=settings.STAKEHOLDER_TRACKER_SHEET_ID,
                    body={"requests": requests},
                )
            )

            logger.info("Applied professional formatting to Stakeholder Tracker")
            return True

        except Exception as e:
            logger.error(f"Error formatting Stakeholder Tracker: {e}")
            return False

    async def ensure_task_tracker_headers(self) -> bool:
        """
        Ensure the Task Tracker sheet has proper headers.

        Creates headers if the sheet is empty.
        """
        if not settings.TASK_TRACKER_SHEET_ID:
            return False

        try:
            # Same bare-range hazard as get_all_tasks(): qualify with the tab.
            # Width comes from the live layout, not a hardcoded A1:J1 — that
            # range predates the col-J UUID / col-K Urgency / col-L Last Update
            # appends and would have truncated the header row. [2026-07-22]
            tab_name = settings.TASK_TRACKER_TAB_NAME or "Tasks"
            last_col = max(TASK_COLUMNS.values())
            rows = await self._read_sheet_range(
                sheet_id=settings.TASK_TRACKER_SHEET_ID,
                range_name=f"'{tab_name}'!A1:{last_col}1"
            )

            if not rows:
                # Add headers
                await self._write_sheet_range(
                    sheet_id=settings.TASK_TRACKER_SHEET_ID,
                    range_name=f"'{tab_name}'!A1:{last_col}1",
                    values=[TASK_TRACKER_HEADERS]
                )
                logger.info("Created Task Tracker headers")

            return True

        except Exception as e:
            logger.error(f"Error ensuring headers: {e}")
            return False


# Singleton instance
sheets_service = GoogleSheetsService()
