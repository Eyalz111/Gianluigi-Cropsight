"""
Reconcile scheduler (v3 outputs re-architecture).

Two cadences, each independently flag-gated:
- TASKS (RECONCILE_ENABLED): midday + pre-nightly Sheet<->DB reconcile.
- GANTT (GANTT_RECONCILE_ENABLED): a weekly pre-digest pass (status rollup +
  timeframe read-back) just before the weekly digest, so the digest reads a
  fresh Gantt.

On-demand reconcile is via the MCP tools. Honors the *_SHADOW_MODE flags (the
engines write nothing while shadow is on).
"""

import asyncio
import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from config.settings import settings

logger = logging.getLogger(__name__)

_ISRAEL_TZ = ZoneInfo("Asia/Jerusalem")
_GANTT_TIMEOUT_S = 1800  # hard 30-min cap on the pre-digest Gantt pass


class ReconcileScheduler:
    """Tasks reconcile (midday + pre-nightly) + weekly pre-digest Gantt pass."""

    def __init__(self):
        self._running = False
        self._last_slot: str | None = None

    async def start(self) -> None:
        if self._running:
            logger.warning("Reconcile scheduler already running")
            return
        self._running = True
        # Restart-safe fire-once: rebuild the last-run slot from the last
        # successful heartbeat so a Cloud Run cycle can't RE-RUN the live-sheet
        # reconcile for a slot that already ran. The guard's failure direction is
        # SKIP, which is the safe one for a sheet-writing pass. [audit P4-03]
        try:
            from schedulers.fire_once import last_ok_heartbeat
            hb = last_ok_heartbeat("reconcile")
            if hb:
                slot = (hb.get("details") or {}).get("slot")
                if slot:
                    self._last_slot = slot
                    logger.info(f"Reconcile: reconstructed last-run slot {slot} on boot")
        except Exception as e:
            logger.warning(f"Reconcile fire-once reconstruct failed (non-fatal): {e}")
        logger.info(
            f"Reconcile scheduler started (tasks={settings.RECONCILE_ENABLED} "
            f"midday={settings.RECONCILE_MIDDAY_HOUR}/pre-nightly={settings.RECONCILE_PRENIGHTLY_HOUR}; "
            f"gantt={settings.GANTT_RECONCILE_ENABLED} pre-digest={settings.GANTT_PREDIGEST_HOUR} "
            f"day={settings.WEEKLY_DIGEST_DAY} IST)"
        )
        while self._running:
            try:
                slot = await self._sleep_until_next()
                if not self._running or slot is None:
                    if slot is None:
                        await asyncio.sleep(3600)  # nothing enabled; idle
                    continue
                if slot == self._last_slot:
                    await asyncio.sleep(3600)
                    continue
                # Only mark the slot done if it succeeded, else retry — a swallowed
                # failure must not skip the live Sheet<->DB reconcile for the day
                # (audit SC-05, same class as SC-01).
                if await self._run(slot):
                    self._last_slot = slot
                else:
                    await asyncio.sleep(300)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Reconcile scheduler error: {e}")
                await asyncio.sleep(300)

    async def _notify_sync(self, tasks, meetings, decisions, projects) -> None:
        """Post a one-line 'synced N edits' to the group after an auto-sync.

        Only when something was actually applied — a no-op tick is silent, which
        keeps the 30-min cadence from becoming noise. Names any canonicalization
        the system applied to a typed value, so a correction like
        'roye -> Roye Tadmor' is visible rather than a surprise next time.
        """
        if not getattr(settings, "SYNC_NOTIFY_ENABLED", False):
            return

        def _g(d, *keys):
            return sum(int((d or {}).get(k, 0) or 0) for k in keys) if isinstance(d, dict) else 0

        t = _g(tasks, "pulled", "created")
        t_arch = _g(tasks, "archived")
        m = _g(meetings, "pulled", "created")
        m_arch = _g(meetings, "archived")
        d = _g(decisions, "pulled")
        p = _g(projects, "renamed", "created", "updated")
        if not (t or t_arch or m or m_arch or d or p):
            return  # no-op tick — stay quiet

        parts = []
        if t:
            parts.append(f"{t} task edit(s)")
        if t_arch:
            parts.append(f"{t_arch} task(s) archived")
        if m:
            parts.append(f"{m} meeting edit(s)")
        if m_arch:
            parts.append(f"{m_arch} meeting(s) to Past Meetings")
        if d:
            parts.append(f"{d} decision edit(s)")
        if p:
            parts.append(f"{p} project change(s)")
        lines = ["✅ <b>Synced from the sheet</b>: " + ", ".join(parts) + "."]

        overrides = (tasks or {}).get("overrides") if isinstance(tasks, dict) else None
        if overrides:
            lines.append("Adjusted to the standard form:")
            for o in overrides[:5]:
                lines.append(f"  • {o['field']}: “{o['from']}” → “{o['to']}”")

        try:
            from services.orchestrator.spine import comms_spine
            await comms_spine.send_to_group("\n".join(lines), parse_mode="HTML")
        except Exception as e:
            logger.warning(f"sync notify failed (non-fatal): {e}")

    def stop(self) -> None:
        self._running = False
        logger.info("Reconcile scheduler stopped")

    def _next_daily(self, now: datetime, hour: int) -> datetime:
        trig = now.replace(hour=hour, minute=0, second=0, microsecond=0)
        if now >= trig:
            trig += timedelta(days=1)
        return trig

    def _next_weekly(self, now: datetime, weekday: int, hour: int) -> datetime:
        # weekday: Mon=0 .. Sun=6 (Python). Settings WEEKLY_DIGEST_DAY uses the same.
        days_ahead = (weekday - now.weekday()) % 7
        trig = (now + timedelta(days=days_ahead)).replace(hour=hour, minute=0, second=0, microsecond=0)
        if trig <= now:
            trig += timedelta(days=7)
        return trig

    async def _sleep_until_next(self) -> str | None:
        now = datetime.now(_ISRAEL_TZ)
        candidates = []
        if settings.RECONCILE_ENABLED:
            candidates.append((self._next_daily(now, settings.RECONCILE_MIDDAY_HOUR), "midday"))
            candidates.append((self._next_daily(now, settings.RECONCILE_PRENIGHTLY_HOUR), "prenightly"))
            # Operational auto-sync: the next N-minute tick. This is what makes
            # Nechama's Sheet edits land without a human trigger. [2026-07-23]
            _iv = int(getattr(settings, "RECONCILE_INTERVAL_MINUTES", 0) or 0)
            if _iv > 0:
                candidates.append((now + timedelta(minutes=_iv), "interval"))
        if settings.GANTT_RECONCILE_ENABLED:
            candidates.append((
                self._next_weekly(now, settings.WEEKLY_DIGEST_DAY, settings.GANTT_PREDIGEST_HOUR),
                "predigest",
            ))
        if not candidates:
            return None
        trig, name = min(candidates, key=lambda c: c[0])
        sleep_s = max(0, (trig - now).total_seconds())
        logger.info(f"Next reconcile ({name}): {trig.strftime('%a %Y-%m-%d %H:%M IST')} ({sleep_s/60:.0f}m)")
        await asyncio.sleep(sleep_s)
        # Interval ticks must carry HHMM so each is a DISTINCT slot — otherwise
        # the `_last_slot` guard (built for once-a-day named slots) would run the
        # interval once and then skip it for the rest of the day.
        stamp = "%Y-%m-%d-%H%M" if name == "interval" else "%Y-%m-%d"
        return f"{trig.strftime(stamp)}:{name}"

    async def _run(self, slot: str) -> bool:
        """Run the reconcile slot. Returns True on success, False on failure (audit SC-05)."""
        name = slot.split(":", 1)[1]
        logger.info(f"Reconcile triggering ({slot})")
        from services.supabase_client import supabase_client
        try:
            # Heartbeat to scheduler_heartbeats (what the health checks read),
            # not audit_log. Both slots share the "reconcile" heartbeat row so the
            # loop is visible regardless of which slot last fired. [audit P4-01]
            if name == "predigest":
                await self._run_gantt()
                supabase_client.upsert_scheduler_heartbeat(
                    "reconcile", details={"slot": slot, "kind": "gantt"}
                )
            else:
                from processors.sheets_sync import (
                    reconcile_tasks, reconcile_decisions, reconcile_meetings,
                    reconcile_projects,
                )
                summary = await reconcile_tasks()
                # Decisions + meetings + projects reconcile self-guard on their
                # enable flags (they return {"skipped": ...} until cutover).
                dec_summary = await reconcile_decisions()
                meet_summary = await reconcile_meetings()
                proj_summary = await reconcile_projects()
                # Generated views LAST — they read the state the reconcile just
                # settled, so running them first would render stale counts.
                views = None
                if getattr(settings, "WORKSPACE_VIEWS_ENABLED", False):
                    try:
                        from processors.workspace_views import refresh_workspace_views
                        views = await refresh_workspace_views()
                    except Exception as ve:
                        logger.warning(f"Workspace views refresh failed (non-fatal): {ve}")
                supabase_client.upsert_scheduler_heartbeat(
                    "reconcile",
                    details={"slot": slot,
                             **(summary if isinstance(summary, dict) else {}),
                             "decisions": dec_summary if isinstance(dec_summary, dict) else None,
                             "meetings": meet_summary if isinstance(meet_summary, dict) else None,
                             "projects": proj_summary if isinstance(proj_summary, dict) else None,
                             "views": views},
                )
                # Close the human->machine loop: after an auto-sync that actually
                # applied edits, tell the group what landed (and any value the
                # system adjusted). Silent on a no-op tick. [2026-07-23]
                await self._notify_sync(summary, meet_summary, dec_summary, proj_summary)
            return True
        except Exception as e:
            logger.error(f"Reconcile failed ({slot}): {e}")
            try:
                supabase_client.upsert_scheduler_heartbeat(
                    "reconcile", status="error", details={"slot": slot, "error": str(e)}
                )
            except Exception:
                pass
            try:
                from core.health_monitor import check_and_alert
                await check_and_alert("reconcile", e)
            except Exception:
                pass
            return False

    async def _run_gantt(self) -> None:
        """Pre-digest Gantt pass: read-back (board -> knowledge, DB-only) + nudges. Never paints the board."""
        async def _work():
            from processors.gantt_readback import reconcile_gantt_lanes
            from processors.gantt_nudge import compute_gantt_nudges
            await reconcile_gantt_lanes()      # board -> knowledge (DB-only, manual-wins)
            compute_gantt_nudges()             # brief<->board divergence -> nudges
        await asyncio.wait_for(_work(), timeout=_GANTT_TIMEOUT_S)


# Singleton instance
reconcile_scheduler = ReconcileScheduler()
