# This file has been edited with the assistance of an AI tool.
"""Tests for agent_wrap/sidecars/tracker.py."""

from __future__ import annotations

from pathlib import Path

from agent_wrap.sidecars.tracker import SidecarTracker


def _tracker(tmp_path: Path) -> SidecarTracker:
    return SidecarTracker(tmp_path)


# --- paths ---


def test_paths_under_agent_launches(tmp_path: Path) -> None:
    t = _tracker(tmp_path)
    assert t.lock_path == tmp_path / ".agent-launches" / "sidecars.lock"
    assert t.start_waiters_dir == tmp_path / ".agent-launches" / "start-waiters"
    assert t.running_dir == tmp_path / ".agent-launches" / "running"


# --- register / clear waiters ---


def test_register_waiter_creates_held_file(tmp_path: Path) -> None:
    t = _tracker(tmp_path)
    handle = t.register_waiter("inst-1")
    assert handle is not None
    assert (t.start_waiters_dir / "inst-1").is_file()
    # A live (held) ticket → a stopper must yield.
    assert t.has_live_waiters() is True
    t.clear_waiter(handle, "inst-1")


def test_clear_waiter_removes_file(tmp_path: Path) -> None:
    t = _tracker(tmp_path)
    handle = t.register_waiter("inst-1")
    t.clear_waiter(handle, "inst-1")
    assert not (t.start_waiters_dir / "inst-1").exists()
    assert t.has_live_waiters() is False


# --- register / clear runners ---


def test_register_running_creates_held_file(tmp_path: Path) -> None:
    t = _tracker(tmp_path)
    handle = t.register_running("inst-1")
    assert handle is not None
    assert (t.running_dir / "inst-1").is_file()
    # Held by someone OTHER than the excluded id → live.
    assert t.has_live_runners(exclude_id="inst-2") is True
    t.clear_running(handle, "inst-1")


def test_has_live_runners_excludes_self(tmp_path: Path) -> None:
    """The finishing run's own held registration does not count as a live other."""
    t = _tracker(tmp_path)
    handle = t.register_running("inst-1")
    assert t.has_live_runners(exclude_id="inst-1") is False
    t.clear_running(handle, "inst-1")


def test_clear_running_removes_file(tmp_path: Path) -> None:
    t = _tracker(tmp_path)
    handle = t.register_running("inst-1")
    t.clear_running(handle, "inst-1")
    assert not (t.running_dir / "inst-1").exists()
    assert t.has_live_runners(exclude_id="other") is False


# --- liveness by lockability, immune to PID recycling ---


def test_probe_reaps_stale_file_when_owner_gone(tmp_path: Path) -> None:
    """
    A registration whose lock has been released (owner 'died') is takeable, so the
    probe treats it as stale: it reaps the file and reports no live runner — without
    ever consulting a PID.
    """
    t = _tracker(tmp_path)
    handle = t.register_running("dead-inst")
    assert handle is not None
    # Simulate the owner exiting: close the handle, which drops the flock but (unlike
    # the run's clear_running) leaves the file behind, as a crash would.
    handle.close()
    assert (t.running_dir / "dead-inst").is_file()
    assert t.has_live_runners(exclude_id="other") is False
    # The stale file was reaped as a side effect of the probe.
    assert not (t.running_dir / "dead-inst").exists()


def test_probe_reaps_stale_keeps_live(tmp_path: Path) -> None:
    """In one pass, a stale sibling is reaped while a live registration is reported."""
    t = _tracker(tmp_path)
    dead = t.register_running("dead-inst")
    assert dead is not None
    dead.close()  # owner gone, file lingers
    live = t.register_running("live-inst")
    assert t.has_live_runners(exclude_id="other") is True
    assert not (t.running_dir / "dead-inst").exists()
    assert (t.running_dir / "live-inst").is_file()
    t.clear_running(live, "live-inst")


def test_probes_false_when_dirs_absent(tmp_path: Path) -> None:
    """No registry directories yet (no run has started) → nothing live."""
    t = _tracker(tmp_path)
    assert t.has_live_waiters() is False
    assert t.has_live_runners(exclude_id="inst-1") is False


def test_clear_tolerates_missing_handle_and_file(tmp_path: Path) -> None:
    """clear_* is a safe no-op when there is nothing registered (e.g. failed start)."""
    t = _tracker(tmp_path)
    t.clear_waiter(None, "inst-1")
    t.clear_running(None, "inst-1")
