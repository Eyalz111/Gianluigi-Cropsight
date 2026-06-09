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
        # HELD by Eyal (2026-06-09) — deferred from 2026-06-06 so the orchestrator
        # stops the daily nag and the intel-signal checkpoint below can surface.
        # Re-surfaces 2026-06-19; apply whenever ready (or push the date again).
        "target_date": "2026-06-19",
        "description": "Add EMAIL_BUSINESS_GATE — strict email classifier on top of strict calendar. (Held since Jun 6.)",
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
    {
        # Reminder/checkpoint (not a feature cutover): nudge Eyal to verify the
        # Intelligence Signal redesign PR1 on its first live weekly run, then
        # continue the redesign. Fires the morning after the weekly signal
        # (signal = Thu 18:00 IST; this = Fri). env_changes re-asserts the
        # already-live SAFE_DISTRIBUTE flag so [✅ Apply] is a clean idempotent
        # confirm-and-dismiss. The audit count surfaces whether the signal actually
        # distributed this week.
        "stage_id": "intel_signal_pr1_verify_continue",
        "target_date": "2026-06-12",
        "description": (
            "✅ Intelligence Signal PR1 (restart-safe distribution) is LIVE. This "
            "Thursday's weekly signal was the first run on the new safe-distribute "
            "path — verify it reached the team cleanly (one email, no duplicate, no "
            "silent loss; the audit count below = distributions). Then CONTINUE the "
            "redesign: PR2 = approve-content-before-video + a Telegram approve/reject "
            "keyboard. Tap Apply to confirm & dismiss."
        ),
        "env_changes": {"INTELLIGENCE_SIGNAL_SAFE_DISTRIBUTE": "true"},
        "audit_action_summary": "intelligence_signal_distributed",
    },
]


def get_stage(stage_id: str) -> dict | None:
    """Look up a stage by id (None if not found)."""
    for stage in ROLLOUT_PLAN:
        if stage.get("stage_id") == stage_id:
            return stage
    return None
