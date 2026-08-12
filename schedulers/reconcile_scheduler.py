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

    async def _sort_meetings_now(self) -> int:
        """Keep the meetings pool in operational order, every cycle.

        scheduled -> to schedule -> parked -> held -> dropped. Returns the
        number of rows reordered; 0 when it deliberately skipped.

        Never raises: this is cosmetic, and a sort failure must not fail a
        cycle that already wrote real data.
        """
        if not getattr(settings, "MEETING_RECONCILE_ENABLED", False):
            return 0
        try:
            from services.google_sheets import sheets_service

            # THE LAYOUT DOOR, FIRST. get_all_meetings() returns [] when the tab
            # does not declare the expected header — and an empty read made
            # `pending` empty, so the guard below PASSED and the sort ran anyway.
            # _resort_tab rewrites only the columns it knows about, so on an
            # older/taller layout it reorders the left-hand block and leaves the
            # identity column where it was: every row welded to a different
            # meeting's UUID. Silent, sheet-side, and invisible to the DB guards.
            # "Every caller treats an empty read as change nothing" has to
            # include this one. [2026-08-09 code review]
            if not await sheets_service.meetings_layout_ok():
                return 0

            rows = await sheets_service.get_all_meetings()
            if not rows:
                return 0

            pending = [r for r in rows
                       if not (r.get("id") or "").strip()
                       and (r.get("title") or "").strip()]
            if pending:
                # See the note at the call site: a row with no UUID is stamped
                # by ROW NUMBER on the next cycle, and moving it first puts that
                # stamp on somebody else's meeting.
                logger.info(f"[meetings] sort skipped — {len(pending)} row(s) "
                            "still waiting for their identity")
                return 0
            moved = await sheets_service.sort_meetings_tab()
            if moved:
                logger.info(f"[meetings] reordered {moved} row(s)")
            return moved
        except Exception as e:                              # noqa: BLE001
            logger.warning(f"[meetings] sort failed (non-fatal): {e}")
            return 0

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
            # A summary that ran in shadow mode increments its counts BEFORE the
            # write guard, so it reports non-zero while writing nothing. Never
            # announce those as applied — it would tell the team an edit landed
            # when it didn't. [review #12]
            if not isinstance(d, dict) or d.get("shadow"):
                return 0
            return sum(int(d.get(k, 0) or 0) for k in keys)

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
                # Project Status sheet. Runs AFTER reconcile_projects so a
                # project renamed on the Projects tab is already settled before
                # its blocks are merged. Self-guards on its own flag and ships
                # shadow-on. [2026-08-07]
                ps_summary = None
                if getattr(settings, "PROJECT_STATUS_RECONCILE_ENABLED", False):
                    try:
                        from processors.project_status_reconcile import (
                            reconcile_project_status,
                        )
                        # The slot decides whether structural row work is
                        # allowed — the interval tick is value-only.
                        ps_summary = await reconcile_project_status(slot=slot)
                    except Exception as pe:
                        logger.error(f"Project Status reconcile failed: {pe}")
                # Generated views LAST — they read the state the reconcile just
                # settled, so running them first would render stale counts.
                views = None
                if getattr(settings, "WORKSPACE_VIEWS_ENABLED", False):
                    try:
                        from processors.workspace_views import refresh_workspace_views
                        views = await refresh_workspace_views()
                    except Exception as ve:
                        logger.warning(f"Workspace views refresh failed (non-fatal): {ve}")
                # The Focus tab. Also last, and also non-fatal: it is a VIEW, so
                # a failure here costs a stale read and must never take down the
                # reconcile that owns the actual data. [2026-08-11]
                focus = None
                if getattr(settings, "FOCUS_VIEW_ENABLED", False):
                    try:
                        from services.focus_sheet import refresh_focus
                        focus = await refresh_focus()
                    except Exception as fe:
                        logger.warning(f"Focus view refresh failed (non-fatal): {fe}")
                # The Timeline tab. Same rule as the two above: a view, so a
                # failure costs a stale render and nothing else. [2026-08-12]
                timeline = None
                if getattr(settings, "TIMELINE_VIEW_ENABLED", False):
                    try:
                        from services.timeline_sheet import refresh_timeline
                        timeline = await refresh_timeline()
                    except Exception as te:
                        logger.warning(f"Timeline refresh failed (non-fatal): {te}")
                # The CEO tab. Same rule again: a view, so a failure costs a
                # stale render and nothing else. It carries Eyal's hand-written
                # management block across each rebuild and abandons the write
                # if it cannot read that block back. [2026-08-12]
                ceo = None
                if getattr(settings, "CEO_TAB_ENABLED", False):
                    try:
                        from services.ceo_sheet import refresh_ceo_tab
                        ceo = await refresh_ceo_tab()
                    except Exception as ce:
                        logger.warning(f"CEO tab refresh failed (non-fatal): {ce}")
                supabase_client.upsert_scheduler_heartbeat(
                    "reconcile",
                    details={"slot": slot,
                             **(summary if isinstance(summary, dict) else {}),
                             "decisions": dec_summary if isinstance(dec_summary, dict) else None,
                             "meetings": meet_summary if isinstance(meet_summary, dict) else None,
                             "projects": proj_summary if isinstance(proj_summary, dict) else None,
                             "project_status": ps_summary,
                             "views": views,
                             "focus": focus,
                             "timeline": timeline,
                             "ceo": ceo},
                )
                # Close the human->machine loop: after an auto-sync that actually
                # applied edits, tell the group what landed (and any value the
                # system adjusted). Silent on a no-op tick. [2026-07-23]
                await self._notify_sync(summary, meet_summary, dec_summary, proj_summary)

                # Daily DEFAULT order — only on the pre-nightly slot (02:00), so
                # the tabs open sorted each morning but the system never
                # re-sorts mid-day and fights a live editor. Runs AFTER the
                # reconcile has pulled the day's edits into the DB. [2026-07-23]
                if name == "prenightly" and getattr(settings, "WORKSPACE_SORT_ENABLED", False):
                    try:
                        from services.google_sheets import sheets_service
                        # Duplicates-only self-heal (safe unattended: never drops a
                        # UUID entirely) before the reorder, so any stray
                        # blind-append dup is gone by morning. [2026-07-24]
                        dd = await sheets_service.dedupe_meetings_tab(remove_orphans=False)
                        if dd.get("duplicates_removed"):
                            logger.info(f"Nightly Meetings dedupe: removed {dd['duplicates_removed']} dup row(s)")
                        nt = await sheets_service.sort_tasks_tab()
                        logger.info(f"Daily reorder: {nt} tasks")
                    except Exception as se:
                        logger.warning(f"Daily reorder failed (non-fatal): {se}")

                # MEETINGS RE-SORT ON EVERY CYCLE, not just at 02:00. Eyal:
                # "i want it to be organized all the time (every refresh) in the
                # order of scheduled, not scheduled, parked, held - operational
                # importance". The pool is the thing he scans to decide what to
                # book next, so an order that is right once a day is right at
                # the wrong time.
                #
                # AFTER the reconcile, never before: the row numbers the merge
                # just used come from a read taken at the start of the cycle,
                # and re-sorting first would address every write to a row that
                # had moved.
                #
                # SKIPPED while a hand-typed row is waiting for its UUID. Such a
                # row is created and stamped on the NEXT cycle by row number; if
                # the sort moves it in between, the stamp lands on somebody
                # else's meeting. One cycle of imperfect ordering is a fair
                # price. It also means a row cannot jump under the cursor in the
                # moments right after it is typed.
                await self._sort_meetings_now()
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
