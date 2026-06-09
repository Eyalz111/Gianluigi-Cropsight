"""Tests for PR1: restart-safe intelligence-signal distribution.

Covers the double-send guard, the bounded Drive-readiness poll, the restart-safe
finalize worker (status guard + reject-during-finalize), and the reconstruct /
periodic re-pickup. Mirrors the mocking style of test_intelligence_signal_agent.py
(patch the module-level supabase_client; patch settings flag on the singleton).
"""
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

from config.settings import settings
from processors.intelligence_signal_agent import (
    _wait_for_drive_video_ready,
    distribute_intelligence_signal,
    finalize_and_distribute_intelligence_signal,
    reconstruct_intelligence_finalize_jobs,
)

AGENT = "processors.intelligence_signal_agent"


# --------------------------------------------------------------------------- #
# Double-send guard                                                           #
# --------------------------------------------------------------------------- #
async def test_safe_distribute_double_send_guard_skips_resend():
    """On the safe path, a signal whose distributed_at marker is set must NOT be
    re-sent to the team — the guard returns before any email machinery."""
    with patch(f"{AGENT}.supabase_client") as sc, patch.object(
        settings, "INTELLIGENCE_SIGNAL_SAFE_DISTRIBUTE", True
    ):
        sc.get_intelligence_signal.return_value = {
            "signal_id": "signal-w14-2026",
            "signal_content": "x",
            "distributed_at": "2026-06-09T10:00:00+00:00",
            "status": "distributed",
            "recipients": ["a@b.com"],
        }
        res = await distribute_intelligence_signal("signal-w14-2026")

    assert res["already_distributed"] is True
    assert res["status"] == "distributed"
    # No status rewrite was needed (already distributed) and no send happened.
    sc.update_intelligence_signal.assert_not_called()


async def test_safe_distribute_guard_flips_status_when_marker_but_not_terminal():
    """Crash-after-send/before-status-flip recovery: marker present, status still
    approved_finalizing → guard flips to distributed without re-sending."""
    with patch(f"{AGENT}.supabase_client") as sc, patch.object(
        settings, "INTELLIGENCE_SIGNAL_SAFE_DISTRIBUTE", True
    ):
        sc.get_intelligence_signal.return_value = {
            "signal_id": "s",
            "signal_content": "x",
            "distributed_at": "2026-06-09T10:00:00+00:00",
            "status": "approved_finalizing",
        }
        res = await distribute_intelligence_signal("s")

    assert res["already_distributed"] is True
    sc.update_intelligence_signal.assert_called_once_with("s", {"status": "distributed"})


# --------------------------------------------------------------------------- #
# Bounded Drive-readiness poll                                                 #
# --------------------------------------------------------------------------- #
async def test_wait_ready_true_when_metadata_present():
    drive = MagicMock()
    drive.get_file_metadata = AsyncMock(
        return_value={"videoMediaMetadata": {"durationMillis": "90000"}}
    )
    with patch("services.google_drive.drive_service", drive):
        ok = await _wait_for_drive_video_ready(
            "file1", datetime.now(timezone.utc) + timedelta(minutes=5)
        )
    assert ok is True


async def test_wait_ready_proceeds_at_deadline():
    drive = MagicMock()
    drive.get_file_metadata = AsyncMock(return_value={})  # never ready
    with patch("services.google_drive.drive_service", drive):
        ok = await _wait_for_drive_video_ready(
            "file1", datetime.now(timezone.utc) - timedelta(seconds=1)
        )
    assert ok is False  # deadline already passed → proceed anyway


# --------------------------------------------------------------------------- #
# Restart-safe finalize worker                                                 #
# --------------------------------------------------------------------------- #
async def test_finalize_skips_when_not_approved_finalizing():
    with patch(f"{AGENT}.supabase_client") as sc:
        sc.get_intelligence_signal.return_value = {
            "signal_id": "s",
            "status": "pending_approval",
        }
        res = await finalize_and_distribute_intelligence_signal("s")
    assert res["skipped"] is True


async def test_finalize_aborts_when_cancelled_during():
    with patch(f"{AGENT}.supabase_client") as sc, patch(
        f"{AGENT}.distribute_intelligence_signal", AsyncMock()
    ) as dist:
        sc.get_intelligence_signal.side_effect = [
            {"signal_id": "s", "status": "approved_finalizing"},  # entry (no video)
            {"signal_id": "s", "status": "cancelled"},  # re-read before send
        ]
        res = await finalize_and_distribute_intelligence_signal("s")
    assert res["status"] == "cancelled"
    dist.assert_not_awaited()


async def test_finalize_distributes_when_ready():
    with patch(f"{AGENT}.supabase_client") as sc, patch(
        f"{AGENT}.distribute_intelligence_signal",
        AsyncMock(return_value={"signal_id": "s", "status": "distributed"}),
    ) as dist:
        sc.get_intelligence_signal.side_effect = [
            {"signal_id": "s", "status": "approved_finalizing"},  # entry, no video
            {"signal_id": "s", "status": "approved_finalizing"},  # re-read
        ]
        res = await finalize_and_distribute_intelligence_signal("s")
    dist.assert_awaited_once_with("s")
    assert res["status"] == "distributed"


# --------------------------------------------------------------------------- #
# Reconstruct / periodic re-pickup                                            #
# --------------------------------------------------------------------------- #
async def test_reconstruct_creates_task_per_row():
    with patch(f"{AGENT}.supabase_client") as sc, patch(
        f"{AGENT}.finalize_and_distribute_intelligence_signal", MagicMock()
    ), patch(f"{AGENT}.asyncio.create_task") as ct, patch(
        f"{AGENT}._attach_finalize_done_callback"
    ):
        sc.get_signals_by_status.return_value = [
            {"signal_id": "a"},
            {"signal_id": "b"},
        ]
        n = await reconstruct_intelligence_finalize_jobs()
    assert n == 2
    assert ct.call_count == 2


async def test_reconstruct_stale_filter_skips_recent_rows():
    recent = datetime.now(timezone.utc).isoformat()
    with patch(f"{AGENT}.supabase_client") as sc, patch(
        f"{AGENT}.finalize_and_distribute_intelligence_signal", MagicMock()
    ), patch(f"{AGENT}.asyncio.create_task") as ct, patch(
        f"{AGENT}._attach_finalize_done_callback"
    ):
        sc.get_signals_by_status.return_value = [
            {"signal_id": "a", "finalize_started_at": recent},
        ]
        n = await reconstruct_intelligence_finalize_jobs(stale_after_minutes=60)
    assert n == 0
    ct.assert_not_called()


async def test_reconstruct_stale_filter_picks_old_rows():
    old = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    with patch(f"{AGENT}.supabase_client") as sc, patch(
        f"{AGENT}.finalize_and_distribute_intelligence_signal", MagicMock()
    ), patch(f"{AGENT}.asyncio.create_task") as ct, patch(
        f"{AGENT}._attach_finalize_done_callback"
    ):
        sc.get_signals_by_status.return_value = [
            {"signal_id": "a", "finalize_started_at": old},
        ]
        n = await reconstruct_intelligence_finalize_jobs(stale_after_minutes=60)
    assert n == 1
    ct.assert_called_once()
