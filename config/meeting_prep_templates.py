"""
Meeting prep templates — template definitions per meeting type.

Each template defines:
- Matching criteria (titles, participants, day)
- Data queries to run for context gathering
- Output structure (ordered section names)
- Timing overrides

Usage:
    from config.meeting_prep_templates import get_template, get_auto_prep_templates
"""

MEETING_PREP_TEMPLATES: dict[str, dict] = {
    "founders_technical": {
        "display_name": "Founders Technical Review",
        "match_titles": [
            "tech review", "technical review", "founders tech",
            "cropsight tech", "cs tech", "product review",
            "sprint review", "dev sync",
        ],
        "expected_participants": ["eyal", "roye"],
        "expected_day": "Tuesday",
        "auto_prep": True,
        "outline_lead_hours": 24,
        "generation_lead_hours": 12,
        "data_queries": [
            {"type": "since_last_meeting", "meeting_type": "founders_technical"},
            {"type": "tasks", "filter": {"assignee": "Roye", "status": "pending"}},
            {"type": "tasks", "filter": {"assignee": "Eyal", "status": "pending"}},
            {"type": "gantt_section", "section": "Product & Technology"},
            {"type": "decisions", "scope": "recent"},
            {"type": "open_questions"},
            {"type": "commitments"},
        ],
        "structure": [
            "Since Last Meeting",
            "Gantt Status: Product & Technology",
            "Roye's Open Tasks",
            "Eyal's Open Tasks",
            "Recent Decisions",
            "Open Questions",
            "Open Commitments",
            "Suggested Agenda",
        ],
        "focus_areas": "Lead with: did Roye deliver what he committed to? Then: technical blockers, ML pipeline status, sprint velocity, overdue items.",
    },
    "founders_business": {
        "display_name": "Founders Business Review",
        "match_titles": [
            "business review", "founders business", "bd review",
            "cropsight business", "cs business", "pipeline review",
            "partnership review", "stakeholder review",
        ],
        "expected_participants": ["eyal", "paolo"],
        "expected_day": None,
        "auto_prep": True,
        "outline_lead_hours": 24,
        "generation_lead_hours": 12,
        "data_queries": [
            {"type": "since_last_meeting", "meeting_type": "founders_business"},
            {"type": "tasks", "filter": {"assignee": "Paolo", "status": "pending"}},
            {"type": "tasks", "filter": {"assignee": "Eyal", "status": "pending"}},
            {"type": "gantt_section", "section": "Business Development"},
            {"type": "entity_timeline", "entity_type": "organization"},
            {"type": "decisions", "scope": "recent"},
            {"type": "commitments"},
        ],
        "structure": [
            "Since Last Meeting",
            "Gantt Status: Business Development",
            "Paolo's Open Tasks",
            "Eyal's Open Tasks",
            "Stakeholder Updates",
            "Recent Decisions",
            "Open Commitments",
            "Suggested Agenda",
        ],
        "focus_areas": "Lead with: what moved in the pipeline? Then: Paolo's delivery status, stakeholder follow-ups, partnership deadlines, commercial blockers.",
    },
    "monthly_strategic": {
        "display_name": "Monthly Strategic Review",
        "match_titles": [
            "monthly review", "strategic review", "monthly strategic",
            "cropsight monthly", "cs monthly", "board prep",
            "all hands", "full team review",
        ],
        "expected_participants": ["eyal", "roye", "paolo"],
        "expected_day": None,
        "auto_prep": True,
        "outline_lead_hours": 48,
        "generation_lead_hours": 24,
        "data_queries": [
            {"type": "since_last_meeting", "meeting_type": "monthly_strategic"},
            {"type": "gantt_section", "section": "Product & Technology"},
            {"type": "gantt_section", "section": "Business Development"},
            {"type": "gantt_section", "section": "Operations & Legal"},
            {"type": "tasks", "filter": {"status": "pending"}},
            {"type": "decisions", "scope": "recent"},
            {"type": "open_questions"},
            {"type": "commitments"},
            {"type": "entity_timeline", "entity_type": "organization"},
        ],
        "structure": [
            "Since Last Meeting",
            "Gantt Overview: All Sections",
            "All Open Tasks",
            "Recent Decisions",
            "Open Questions",
            "Open Commitments",
            "Stakeholder Updates",
            "Suggested Agenda",
        ],
        "focus_areas": "Full scorecard: completion rates, Gantt progress vs plan, commitment fulfillment, cross-functional blockers. Comprehensive, not focused.",
    },
    "generic": {
        "display_name": "General Meeting",
        "match_titles": [],
        "expected_participants": [],
        "expected_day": None,
        "auto_prep": False,
        "outline_lead_hours": 24,
        "generation_lead_hours": 12,
        # Participant-centric: pull data for the specific people in the meeting
        "data_queries": [
            {"type": "tasks", "filter": {"scope": "participants"}},
            {"type": "decisions", "scope": "participants"},
            {"type": "commitments", "scope": "participants"},
            {"type": "open_questions"},
        ],
        "structure": [
            "Participant Tasks",
            "Recent Decisions",
            "Open Commitments",
            "Open Questions",
            "Suggested Agenda",
        ],
        "focus_areas": "Focus on what's actionable for the specific participants in this meeting.",
    },
}


def get_template(meeting_type: str) -> dict:
    """
    Get a template by meeting type key.

    Args:
        meeting_type: Template key (e.g. 'founders_technical').

    Returns:
        Template dict. Falls back to 'generic' if not found.
    """
    return MEETING_PREP_TEMPLATES.get(meeting_type, MEETING_PREP_TEMPLATES["generic"])


def get_auto_prep_templates() -> list[dict]:
    """
    Get all templates with auto_prep=True.

    Returns:
        List of template dicts that should trigger automatic prep.
    """
    return [
        {**t, "meeting_type": key}
        for key, t in MEETING_PREP_TEMPLATES.items()
        if t.get("auto_prep")
    ]


def get_all_template_names() -> list[str]:
    """
    Get all template type keys.

    Returns:
        List of template keys (e.g. ['founders_technical', ...]).
    """
    return list(MEETING_PREP_TEMPLATES.keys())
