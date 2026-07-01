# This file has been edited with the assistance of an AI tool.
"""Tests for agent_wrap/sidecars/tracker.py."""

from __future__ import annotations

from pathlib import Path

import pytest

from agent_wrap.domain.sidecars.tracker import SidecarTracker


@pytest.fixture
def tracker(tmp_path: Path) -> SidecarTracker:
    return SidecarTracker(tmp_path)


# --- paths ---


def test_paths_under_agent_launches(tracker: SidecarTracker, tmp_path: Path) -> None:
    assert tracker.lock_path == tmp_path / ".agent-launches" / "sidecars.lock"
    assert tracker.start_waiters_dir == tmp_path / ".agent-launches" / "start-waiters"
    assert tracker.running_dir == tmp_path / ".agent-launches" / "running"


# --- register / clear runners ---


def test_register_running_creates_held_file(tracker: SidecarTracker) -> None:
    handle = tracker.register_running("inst-1")
    assert handle is not None
    assert (tracker.running_dir / "inst-1").is_file()
    # Held by someone OTHER than the excluded id → live.
    assert tracker.has_live_runners(exclude_id="inst-2") is True
    tracker.clear_running(handle, "inst-1")


def test_has_live_runners_excludes_self(tracker: SidecarTracker) -> None:
    """The finishing run's own held registration does not count as a live other."""
    handle = tracker.register_running("inst-1")
    assert tracker.has_live_runners(exclude_id="inst-1") is False
    tracker.clear_running(handle, "inst-1")


def test_clear_running_removes_file(tracker: SidecarTracker) -> None:
    handle = tracker.register_running("inst-1")
    tracker.clear_running(handle, "inst-1")
    assert not (tracker.running_dir / "inst-1").exists()
    assert tracker.has_live_runners(exclude_id="other") is False


# --- liveness by lockability, immune to PID recycling ---


def test_probe_reaps_stale_file_when_owner_gone(tracker: SidecarTracker) -> None:
    """
    A registration whose lock has been released (owner 'died') is takeable, so the
    probe treats it as stale: it reaps the file and reports no live runner — without
    ever consulting a PID.
    """
    handle = tracker.register_running("dead-inst")
    assert handle is not None
    # Simulate the owner exiting: close the handle, which drops the flock but (unlike
    # the run's clear_running) leaves the file behind, as a crash would.
    handle.close()
    assert (tracker.running_dir / "dead-inst").is_file()
    assert tracker.has_live_runners(exclude_id="other") is False
    # The stale file was reaped as a side effect of the probe.
    assert not (tracker.running_dir / "dead-inst").exists()


def test_probe_reaps_stale_keeps_live(tracker: SidecarTracker) -> None:
    """In one pass, a stale sibling is reaped while a live registration is reported."""
    dead = tracker.register_running("dead-inst")
    assert dead is not None
    dead.close()  # owner gone, file lingers
    live = tracker.register_running("live-inst")
    assert tracker.has_live_runners(exclude_id="other") is True
    assert not (tracker.running_dir / "dead-inst").exists()
    assert (tracker.running_dir / "live-inst").is_file()
    tracker.clear_running(live, "live-inst")


def test_probes_false_when_dirs_absent(tracker: SidecarTracker) -> None:
    """No registry directories yet (no run has started) → nothing live."""
    assert tracker.has_live_runners(exclude_id="inst-1") is False


def test_clear_tolerates_missing_handle_and_file(tracker: SidecarTracker) -> None:
    """clear_running is a safe no-op when there is nothing registered (e.g. failed start)."""
    tracker.clear_running(None, "inst-1")
