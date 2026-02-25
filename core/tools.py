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
# All Tools (v0.1 + v0.2)
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
]
