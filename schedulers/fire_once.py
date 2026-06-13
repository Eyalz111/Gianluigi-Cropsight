"""
Restart-safe fire-once reconstruction for the sleep-until schedulers (audit P4-03).

A sleep-until scheduler (knowledge nightly/weekly, intelligence signal, reconcile)
holds its "already ran this period" guard in memory. A Cloud Run cycle in the
trigger window loses it, so the scheduler can RE-FIRE (a second nightly
consolidation, a duplicate weekly-signal approval ping, a re-run of the live-sheet
reconcile). On boot, each scheduler rebuilds its guard from its last SUCCESSFUL
heartbeat (written by services.supabase_client.upsert_scheduler_heartbeat, which
records last_run_at + details per run).

Only a status='ok' heartbeat counts — an errored run must be allowed to retry.
Everything here is best-effort and never raises: a reconstruction miss is no worse
than today's in-memory-only behavior, and a reconstruction that over-sets the guard
errs toward SKIP (the safe direction for a live-write scheduler).
"""

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

_ISRAEL_TZ = ZoneInfo("Asia/Jerusalem")


def last_ok_heartbeat(name: str) -> dict | None:
    """The scheduler's heartbeat row iff its last run was status='ok', else None."""
    try:
        from services.supabase_client import supabase_client
        hb = supabase_client.get_scheduler_heartbeat(name)
    except Exception:
        return None
    if hb and hb.get("status") == "ok":
        return hb
    return None


def last_ok_run_ist(name: str) -> datetime | None:
    """IST datetime of the scheduler's last status='ok' run, or None."""
    hb = last_ok_heartbeat(name)
    if not hb:
        return None
    raw = hb.get("last_run_at")
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(_ISRAEL_TZ)
    except (ValueError, TypeError):
        return None


def last_ok_day_key(name: str) -> str | None:
    """`YYYY-MM-DD` (IST) of the last successful run — daily fire-once guard."""
    dt = last_ok_run_ist(name)
    return dt.strftime("%Y-%m-%d") if dt else None


def last_ok_week_key(name: str) -> str | None:
    """`w{week}-{year}` (IST ISO week) of the last successful run — weekly guard."""
    dt = last_ok_run_ist(name)
    if not dt:
        return None
    iso = dt.isocalendar()
    return f"w{iso[1]}-{iso[0]}"
