"""
Tool definitions for Claude API tool use.

This module defines all tools that Gianluigi can use to interact with
external services (Supabase, Google Drive, etc.) through the Claude API.

Each tool is defined with:
- name: Unique identifier
- description: What the tool does (for Claude to understand)
- input_schema: JSON Schema for the tool's parameters

Tool implementations are in the services/ modules.
"""


# =============================================================================
# v0.1 Tool Definitions
# =============================================================================

TOOL_SEARCH_MEETINGS = {
    "name": "search_meetings",
    "description": """
        Semantic search over embedded meeting chunks.
        Use this to find discussions about specific topics across all meetings.
        Returns relevant transcript excerpts with meeting metadata.
    """,
    "input_schema": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "The search query (e.g., 'cloud provider decision')"
            },
            "date_from": {
                "type": "string",
                "description": "Optional start date filter (ISO format: YYYY-MM-DD)"
            },
            "date_to": {
                "type": "string",
                "description": "Optional end date filter (ISO format: YYYY-MM-DD)"
            }
        },
        "required": ["query"]
    }
}

TOOL_GET_MEETING_SUMMARY = {
    "name": "get_meeting_summary",
    "description": """
        Retrieve the full processed summary for a specific meeting.
        Returns the formatted summary including decisions, tasks, and notes.
    """,
    "input_schema": {
        "type": "object",
        "properties": {
            "meeting_id": {
                "type": "string",
                "description": "UUID of the meeting to retrieve"
            }
        },
        "required": ["meeting_id"]
    }
}

TOOL_CREATE_TASK = {
    "name": "create_task",
    "description": """
        Create a new task and add it to the task tracker.
        Stores in Supabase and updates the Google Sheet Task Tracker.
    """,
    "input_schema": {
        "type": "object",
        "properties": {
            "title": {
                "type": "string",
                "description": "Description of the task"
            },
            "assignee": {
                "type": "string",
                "description": "Who is responsible (eyal, roye, paolo, yoram, or 'team')"
            },
            "deadline": {
                "type": "string",
                "description": "Due date in ISO format (YYYY-MM-DD)"
            },
            "priority": {
                "type": "string",
                "enum": ["H", "M", "L"],
                "description": "Priority: H (high), M (medium), L (low)"
            },
            "category": {
                "type": "string",
                "enum": [
                    "Product & Tech",
                    "BD & Sales",
                    "Legal & Compliance",
                    "Finance & Fundraising",
                    "Operations & HR",
                    "Strategy & Research"
                ],
                "description": "Task category for organizational alignment"
            },
            "meeting_id": {
                "type": "string",
                "description": "Optional: UUID of source meeting if task came from a meeting"
            }
        },
        "required": ["title", "assignee", "priority"]
    }
}

TOOL_GET_TASKS = {
    "name": "get_tasks",
    "description": """
        Retrieve tasks filtered by assignee and/or status.
        Use this to answer questions like "What are my open tasks?"
    """,
    "input_schema": {
        "type": "object",
        "properties": {
            "assignee": {
                "type": "string",
                "description": "Filter by assignee name (optional)"
            },
            "status": {
                "type": "string",
                "enum": ["pending", "in_progress", "done", "overdue"],
                "description": "Filter by task status (optional)"
            },
            "category": {
                "type": "string",
                "enum": [
                    "Product & Tech",
                    "BD & Sales",
                    "Legal & Compliance",
                    "Finance & Fundraising",
                    "Operations & HR",
                    "Strategy & Research"
                ],
                "description": "Filter by task category (optional)"
            }
        },
        "required": []
    }
}

TOOL_UPDATE_TASK = {
    "name": "update_task",
    "description": """
        Update an existing task's status or deadline.
        Also updates the Google Sheet Task Tracker.
    """,
    "input_schema": {
        "type": "object",
        "properties": {
            "task_id": {
                "type": "string",
                "description": "UUID of the task to update"
            },
            "status": {
                "type": "string",
                "enum": ["pending", "in_progress", "done"],
                "description": "New status (optional)"
            },
            "deadline": {
                "type": "string",
                "description": "New deadline in ISO format (optional)"
            }
        },
        "required": ["task_id"]
    }
}

TOOL_SEARCH_MEMORY = {
    "name": "search_memory",
    "description": """
        Combined search across all of Gianluigi's memory.
        Searches: embedded transcript chunks (semantic), decisions (SQL), tasks (SQL).
        Returns the most relevant information across all sources.
    """,
    "input_schema": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "The search query"
            }
        },
        "required": ["query"]
    }
}

TOOL_LIST_DECISIONS = {
    "name": "list_decisions",
    "description": """
        Retrieve decisions, optionally filtered by meeting or topic.
        Each decision includes context and transcript timestamp citation.
    """,
    "input_schema": {
        "type": "object",
        "properties": {
            "meeting_id": {
                "type": "string",
                "description": "Filter by source meeting UUID (optional)"
            },
            "topic": {
                "type": "string",
                "description": "Filter by topic keyword (optional)"
            }
        },
        "required": []
    }
}

TOOL_GET_OPEN_QUESTIONS = {
    "name": "get_open_questions",
    "description": """
        Retrieve open questions across all meetings.
        These are unresolved issues that need future discussion.
    """,
    "input_schema": {
        "type": "object",
        "properties": {
            "status": {
                "type": "string",
                "enum": ["open", "resolved"],
                "description": "Filter by status (defaults to 'open')"
            }
        },
        "required": []
    }
}

TOOL_GET_STAKEHOLDER_INFO = {
    "name": "get_stakeholder_info",
    "description": """
        Read from the CropSight Stakeholder Tracker Google Sheet.
        Use to find information about external contacts and organizations.
    """,
    "input_schema": {
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "description": "Filter by contact name (optional)"
            },
            "organization": {
                "type": "string",
                "description": "Filter by organization name (optional)"
            }
        },
        "required": []
    }
}

TOOL_INGEST_TRANSCRIPT = {
    "name": "ingest_transcript",
    "description": """
        Process a raw meeting transcript through the full pipeline.
        Extracts structured data, stores in Supabase, triggers approval flow.
        Internal tool — typically called automatically when new transcripts are detected.
    """,
    "input_schema": {
        "type": "object",
        "properties": {
            "file_content": {
                "type": "string",
                "description": "The raw transcript text"
            },
            "meeting_title": {
                "type": "string",
                "description": "Title of the meeting"
            },
            "date": {
                "type": "string",
                "description": "Meeting date (ISO format)"
            },
            "participants": {
                "type": "array",
                "items": {"type": "string"},
                "description": "List of participant names"
            }
        },
        "required": ["file_content", "meeting_title", "date", "participants"]
    }
}

TOOL_INGEST_DOCUMENT = {
    "name": "ingest_document",
    "description": """
        Ingest a document (PDF, doc, etc.) into Gianluigi's knowledge base.
        Summarizes the document and creates searchable embeddings.
    """,
    "input_schema": {
        "type": "object",
        "properties": {
            "content": {
                "type": "string",
                "description": "The document text content"
            },
            "title": {
                "type": "string",
                "description": "Document title"
            },
            "source": {
                "type": "string",
                "enum": ["upload", "email", "drive"],
                "description": "Where the document came from"
            }
        },
        "required": ["content", "title", "source"]
    }
}

TOOL_GET_MEETING_PREP = {
    "name": "get_meeting_prep",
    "description": """
        Generate a meeting preparation document for an upcoming calendar event.
        Searches past meetings, checks stakeholder tracker, identifies relevant tasks.
        Saves to Google Drive and triggers approval flow.
    """,
    "input_schema": {
        "type": "object",
        "properties": {
            "calendar_event_id": {
                "type": "string",
                "description": "Google Calendar event ID"
            }
        },
        "required": ["calendar_event_id"]
    }
}


# =============================================================================
# v0.2 Tool Definitions
# =============================================================================

TOOL_GENERATE_WEEKLY_DIGEST = {
    "name": "generate_weekly_digest",
    "description": """
        Generate a weekly digest document summarizing the past week's meetings,
        decisions, task progress, and upcoming meetings. Can be triggered manually
        or runs automatically on Sunday evenings.
    """,
    "input_schema": {
        "type": "object",
        "properties": {
            "week_start": {
                "type": "string",
                "description": "Optional: Monday of the week to summarize (ISO YYYY-MM-DD). Defaults to current week."
            }
        },
        "required": []
    }
}

TOOL_UPDATE_STAKEHOLDER_TRACKER = {
    "name": "update_stakeholder_tracker",
    "description": """
        Suggest an update to the CropSight Stakeholder Tracker.
        This will send a suggestion to Eyal for approval before making changes.
        Use when a meeting mentions a new contact or organization, or when
        stakeholder information needs updating.
    """,
    "input_schema": {
        "type": "object",
        "properties": {
            "stakeholder_name": {
                "type": "string",
                "description": "Name of the contact person"
            },
            "organization": {
                "type": "string",
                "description": "Organization name"
            },
            "updates": {
                "type": "object",
                "description": "Fields to update (e.g., type, description, contact_person, status, notes)"
            },
            "source_meeting_id": {
                "type": "string",
                "description": "Optional: UUID of the meeting where this was mentioned"
            }
        },
        "required": ["stakeholder_name", "organization", "updates"]
    }
}

TOOL_SEARCH_GMAIL = {
    "name": "search_gmail",
    "description": """
        Search Gianluigi's Gmail inbox for relevant emails.
        Useful for finding context from team email threads,
        document attachments, or previous correspondence.
    """,
    "input_schema": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Search query (Gmail search syntax supported)"
            },
            "max_results": {
                "type": "integer",
                "description": "Maximum number of results (default 5)"
            }
        },
        "required": ["query"]
    }
}


# =============================================================================
# v0.3 Tier 2 Tool Definitions
# =============================================================================

TOOL_GET_ENTITY_INFO = {
    "name": "get_entity_info",
    "description": """
        Look up an entity (person, organization, project, etc.) in the entity registry.
        Returns canonical name, type, aliases, metadata, and recent mentions.
        Use for questions like "Who is Jason Adelman?" or "What do we know about Lavazza?"
    """,
    "input_schema": {
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "description": "Name of the entity to look up"
            },
            "entity_type": {
                "type": "string",
                "enum": ["person", "organization", "project", "technology", "location"],
                "description": "Optional: filter by entity type"
            }
        },
        "required": ["name"]
    }
}

TOOL_GET_ENTITY_TIMELINE = {
    "name": "get_entity_timeline",
    "description": """
        Get a chronological timeline of all mentions of an entity across meetings.
        Shows when the entity was discussed, by whom, and in what context.
        Use for questions like "What's our history with Ferrero?" or
        "Show me all discussions about the Moldova project."
    """,
    "input_schema": {
        "type": "object",
        "properties": {
            "entity_id": {
                "type": "string",
                "description": "UUID of the entity (get this from get_entity_info first)"
            },
            "limit": {
                "type": "integer",
                "description": "Maximum number of mentions to return (default 20)"
            }
        },
        "required": ["entity_id"]
    }
}

TOOL_GET_COMMITMENTS = {
    "name": "get_commitments",
    "description": """
        Retrieve verbal commitments made in meetings.
        These are promises like "I'll send that by Friday" that may not be formal tasks.
        Can filter by speaker or status (open, fulfilled, overdue).
    """,
    "input_schema": {
        "type": "object",
        "properties": {
            "speaker": {
                "type": "string",
                "description": "Filter by who made the commitment (optional)"
            },
            "status": {
                "type": "string",
                "enum": ["open", "fulfilled", "overdue", "withdrawn"],
                "description": "Filter by commitment status (optional)"
            }
        },
        "required": []
    }
}


# =============================================================================
# v1.0 Phase 2 — Gantt Integration Tool Definitions
# =============================================================================

TOOL_GET_GANTT_STATUS = {
    "name": "get_gantt_status",
    "description": """
        Get the current Gantt chart status for a given week.
        Returns parsed, structured data for all sections including owner,
        status (active/planned/blocked/completed), and item type.
        Use for questions like "What's happening this week?" or
        "What's the plan for W14?"
    """,
    "input_schema": {
        "type": "object",
        "properties": {
            "week": {
                "type": "integer",
                "description": "Week number (e.g., 11, 14). Defaults to current week if not specified."
            }
        },
        "required": []
    }
}

TOOL_GET_GANTT_SECTION = {
    "name": "get_gantt_section",
    "description": """
        Deep dive into a specific Gantt section across multiple weeks.
        Returns parsed cell data for all subsections within a section.
        Use for questions like "Show me Product & Technology for W11-W15"
        or "What's in the Commercial section?"
        Section names are fuzzy matched — "Product & Tech" works.
    """,
    "input_schema": {
        "type": "object",
        "properties": {
            "section": {
                "type": "string",
                "description": "Section name (e.g., 'Product & Technology', 'Commercial'). Fuzzy matched."
            },
            "weeks": {
                "type": "array",
                "items": {"type": "integer"},
                "description": "List of week numbers to include (defaults to current week ± 2)"
            }
        },
        "required": ["section"]
    }
}

TOOL_GET_MEETING_CADENCE = {
    "name": "get_meeting_cadence",
    "description": """
        Get the expected meeting cadence from the Gantt's Meeting Cadence tab.
        Returns meeting definitions including name, frequency, and expected attendees.
        Use for questions like "What meetings are scheduled this week?"
        or "What's our meeting cadence?"
    """,
    "input_schema": {
        "type": "object",
        "properties": {
            "week": {
                "type": "integer",
                "description": "Week number for context (defaults to current week)"
            }
        },
        "required": []
    }
}

TOOL_GET_GANTT_HORIZON = {
    "name": "get_gantt_horizon",
    "description": """
        Look ahead in the Gantt to find upcoming milestones and transitions.
        Use for questions like "What's coming up?" or
        "What milestones are in the next 2 months?"
    """,
    "input_schema": {
        "type": "object",
        "properties": {
            "weeks_ahead": {
                "type": "integer",
                "description": "How many weeks to look ahead (default 8)"
            }
        },
        "required": []
    }
}

TOOL_PROPOSE_GANTT_UPDATE = {
    "name": "propose_gantt_update",
    "description": """
        Propose changes to the Gantt chart. Creates a proposal that requires
        CEO approval before being applied. Each change must include an owner
        prefix (like [R], [E], [E/R]) and a status.
        Use when Eyal asks to update, add, or modify Gantt entries.
        NEVER writes directly — always creates a proposal for approval.
    """,
    "input_schema": {
        "type": "object",
        "properties": {
            "changes": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "section": {
                            "type": "string",
                            "description": "Gantt section (e.g., 'Product & Technology')"
                        },
                        "subsection": {
                            "type": "string",
                            "description": "Subsection/row (e.g., 'Execution', 'Planning')"
                        },
                        "week": {
                            "type": "integer",
                            "description": "Target week number (for single-week changes)"
                        },
                        "week_start": {
                            "type": "integer",
                            "description": "Start week (for range changes, use instead of week)"
                        },
                        "week_end": {
                            "type": "integer",
                            "description": "End week (for range changes, use with week_start)"
                        },
                        "value": {
                            "type": "string",
                            "description": "Cell value with owner prefix (e.g., '[R] MVP Sprint 2')"
                        },
                        "status": {
                            "type": "string",
                            "enum": ["active", "planned", "blocked", "completed", ""],
                            "description": "Status for cell color (active=green, planned=blue, blocked=red)"
                        },
                        "reason": {
                            "type": "string",
                            "description": "Why this change is being made"
                        },
                        "force_mode": {
                            "type": "string",
                            "enum": ["replace", "append"],
                            "description": "Only set this AFTER getting a 'needs_confirmation' response. 'append' adds to existing content, 'replace' overwrites. Do NOT set on first call — let the system detect conflicts automatically."
                        }
                    },
                    "required": ["section", "subsection", "value", "status", "reason"]
                },
                "description": "List of cell changes to propose"
            },
            "source": {
                "type": "string",
                "description": "Source of the change (meeting, telegram, email, manual)"
            }
        },
        "required": ["changes"]
    }
}

TOOL_GET_GANTT_HISTORY = {
    "name": "get_gantt_history",
    "description": """
        Get recent changes made to the Gantt chart.
        Shows approved proposals with their diffs (old value → new value).
        Use for questions like "What changed in the Gantt recently?"
        or "What updates were made last week?"
    """,
    "input_schema": {
        "type": "object",
        "properties": {
            "limit": {
                "type": "integer",
                "description": "Maximum number of entries to return (default 10)"
            }
        },
        "required": []
    }
}

TOOL_ROLLBACK_GANTT_UPDATE = {
    "name": "rollback_gantt_update",
    "description": """
        Undo a Gantt change by restoring cells from the saved snapshot.
        If no proposal_id is given, rolls back the most recently approved change.
        Use when Eyal says "undo the last Gantt change" or
        "rollback that Gantt update".
    """,
    "input_schema": {
        "type": "object",
        "properties": {
            "proposal_id": {
                "type": "string",
                "description": "UUID of the proposal to rollback (optional, defaults to most recent)"
            }
        },
        "required": []
    }
}


# =============================================================================
# v1.0 Phase 4 — Email Intelligence Tool Definitions
# =============================================================================

TOOL_GET_EMAIL_INTELLIGENCE = {
    "name": "get_email_intelligence",
    "description": """
        Search email intelligence — extracted items from scanned emails.
        Returns structured items (tasks, decisions, commitments, information)
        extracted from team and personal email correspondence.
        When citing results, say "from email correspondence" — never quote raw email text.
    """,
    "input_schema": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Search query to match against extracted email items"
            },
            "sender": {
                "type": "string",
                "description": "Filter by sender email (optional)"
            },
            "days": {
                "type": "integer",
                "description": "Number of days to search back (default 30)"
            },
        },
        "required": ["query"],
    },
}


# =============================================================================
# All Tools (v0.1 + v0.2 + v0.3 + v1.0)
# =============================================================================

TOOL_DEFINITIONS = [
    # v0.1 tools
    TOOL_SEARCH_MEETINGS,
    TOOL_GET_MEETING_SUMMARY,
    TOOL_CREATE_TASK,
    TOOL_GET_TASKS,
    TOOL_UPDATE_TASK,
    TOOL_SEARCH_MEMORY,
    TOOL_LIST_DECISIONS,
    TOOL_GET_OPEN_QUESTIONS,
    TOOL_GET_STAKEHOLDER_INFO,
    TOOL_INGEST_TRANSCRIPT,
    TOOL_INGEST_DOCUMENT,
    TOOL_GET_MEETING_PREP,
    # v0.2 tools
    TOOL_GENERATE_WEEKLY_DIGEST,
    TOOL_UPDATE_STAKEHOLDER_TRACKER,
    TOOL_SEARCH_GMAIL,
    # v0.3 Tier 2 tools
    TOOL_GET_ENTITY_INFO,
    TOOL_GET_ENTITY_TIMELINE,
    TOOL_GET_COMMITMENTS,
    # v1.0 Phase 2 — Gantt Integration tools
    TOOL_GET_GANTT_STATUS,
    TOOL_GET_GANTT_SECTION,
    TOOL_GET_MEETING_CADENCE,
    TOOL_GET_GANTT_HORIZON,
    TOOL_PROPOSE_GANTT_UPDATE,
    TOOL_GET_GANTT_HISTORY,
    TOOL_ROLLBACK_GANTT_UPDATE,
    # v1.0 Phase 4 — Email Intelligence tools
    TOOL_GET_EMAIL_INTELLIGENCE,
]
