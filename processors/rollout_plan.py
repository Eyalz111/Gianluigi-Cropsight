"""
Rollout plan — the staged env-flag cutovers the orchestrator walks through.

A plain Python list (NOT YAML) — 5 entries today, future rollouts just append.
Each stage names the target date, the env vars to flip, and (optionally) which
audit_log action to count for "shadow diff" context in the reminder.

Restart-safe state lives in audit_log (action=`rollout_applied` with stage_id);
the orchestrator picks the FIRST unapplied stage whose target_date <= today.
A reminder fires daily at ROLLOUT_CHECK_HOUR until the stage is applied — so a
missed day re-fires the next morning (no Skip/Hold button by design; if a stage
proves wrong, edit this file).
"""

# Each stage: stage_id, target_date (YYYY-MM-DD, IST), env_changes (str values),
# description (shown in the reminder), audit_action_summary (optional — what to
# count in the audit_log for "shadow diff" context).
ROLLOUT_PLAN: list[dict] = [
    {
        "stage_id": "phase3_cutover_strict_calendar",
        "target_date": "2026-06-02",
        "description": (
            "Cut over STRICT_CALENDAR_FILTER. Turn input-hygiene SHADOW OFF and "
            "enable strict calendar filter only (keep email gate + uncertain-exclusion "
            "still in legacy)."
        ),
        "env_changes": {
            "INPUT_HYGIENE_SHADOW_MODE": "false",
            "STRICT_CALENDAR_FILTER": "true",
            "STRICT_UNCERTAIN_EXCLUSION": "false",
            "EMAIL_BUSINESS_GATE": "false",
        },
        "audit_action_summary": "input_hygiene_shadow",
    },
    {
        "stage_id": "phase3_add_strict_uncertain",
        "target_date": "2026-06-04",
        "description": "Add STRICT_UNCERTAIN_EXCLUSION on top of the strict calendar filter.",
        "env_changes": {"STRICT_UNCERTAIN_EXCLUSION": "true"},
        "audit_action_summary": "input_hygiene_shadow",
    },
    {
        "stage_id": "phase3_add_email_business_gate",
        "target_date": "2026-06-06",
        "description": "Add EMAIL_BUSINESS_GATE — strict email classifier on top of strict calendar.",
        "env_changes": {"EMAIL_BUSINESS_GATE": "true"},
        "audit_action_summary": "input_hygiene_shadow",
    },
    {
        "stage_id": "phase3_morning_brief_v2_live",
        "target_date": "2026-06-08",
        "description": "Drop the v2 morning-brief shadow preview → v2 becomes the live brief.",
        "env_changes": {"MORNING_BRIEF_V2_SHADOW": "false"},
        "audit_action_summary": "morning_brief_headline_status",
    },
]


def get_stage(stage_id: str) -> dict | None:
    """Look up a stage by id (None if not found)."""
    for stage in ROLLOUT_PLAN:
        if stage.get("stage_id") == stage_id:
            return stage
    return None
