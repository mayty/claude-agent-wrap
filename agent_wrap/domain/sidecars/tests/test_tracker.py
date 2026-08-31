# This file has been edited with the assistance of an AI tool.
"""Tests for agent_wrap/sidecars/tracker.py."""

from typing import TYPE_CHECKING

import pytest

from agent_wrap.domain.sidecars.tracker import SidecarTracker

if TYPE_CHECKING:
    from pathlib import Path

_BEDROCK = "agent-wrap-litellm-bedrock"
_DEEPSEEK = "agent-wrap-litellm-deepseek"
_TELEGRAM = "agent-wrap-telegram"


@pytest.fixture
def tracker(tmp_path: Path) -> SidecarTracker:
    return SidecarTracker(tmp_path)


def test_paths_under_agent_launches(tracker: SidecarTracker, tmp_path: Path) -> None:
    assert tracker.lock_path == tmp_path / ".agent-launches" / "sidecars.lock"
    assert tracker.start_waiters_dir == tmp_path / ".agent-launches" / "start-waiters"
    assert tracker.running_dir == tmp_path / ".agent-launches" / "running"


def test_running_dir_for_is_a_subdirectory_of_running(tracker: SidecarTracker) -> None:
    assert tracker.running_dir_for(_BEDROCK) == tracker.running_dir / _BEDROCK


def test_register_running_creates_held_file(tracker: SidecarTracker) -> None:
    handle = tracker.register_running(_BEDROCK, "inst-1")
    assert handle is not None
    assert (tracker.running_dir_for(_BEDROCK) / "inst-1").is_file()
    # Held by someone OTHER than the excluded id → live.
    assert tracker.has_live_runners(_BEDROCK, exclude_id="inst-2") is True
    tracker.clear_running(handle, _BEDROCK, "inst-1")


def test_has_live_runners_excludes_self(tracker: SidecarTracker) -> None:
    """The finishing run's own held registration does not count as a live other."""
    handle = tracker.register_running(_BEDROCK, "inst-1")
    assert tracker.has_live_runners(_BEDROCK, exclude_id="inst-1") is False
    tracker.clear_running(handle, _BEDROCK, "inst-1")


def test_registrations_are_isolated_per_container(tracker: SidecarTracker) -> None:
    """
    The core of concurrent per-provider sidecars: an agent live on one container is
    invisible to the other's probe, so each provider's sidecar is torn down on its own
    schedule.
    """
    bedrock = tracker.register_running(_BEDROCK, "inst-bedrock")
    deepseek = tracker.register_running(_DEEPSEEK, "inst-deepseek")

    # The deepseek run finishing sees its own container as idle...
    assert tracker.has_live_runners(_DEEPSEEK, exclude_id="inst-deepseek") is False
    # ...while the bedrock agent it must not disturb still registers as live.
    assert tracker.has_live_runners(_BEDROCK, exclude_id="inst-deepseek") is True

    tracker.clear_running(bedrock, _BEDROCK, "inst-bedrock")
    tracker.clear_running(deepseek, _DEEPSEEK, "inst-deepseek")


def test_shared_container_name_refcounts_together(tracker: SidecarTracker) -> None:
    """Two runs on one container (the Telegram singleton) share a single refcount."""
    first = tracker.register_running(_TELEGRAM, "inst-1")
    second = tracker.register_running(_TELEGRAM, "inst-2")

    assert tracker.has_live_runners(_TELEGRAM, exclude_id="inst-1") is True
    tracker.clear_running(second, _TELEGRAM, "inst-2")
    assert tracker.has_live_runners(_TELEGRAM, exclude_id="inst-1") is False

    tracker.clear_running(first, _TELEGRAM, "inst-1")


def test_legacy_flat_registration_file_is_invisible_to_per_container_probe(
    tracker: SidecarTracker,
) -> None:
    """
    A registration written by a pre-upgrade launch sits directly in ``running/``, beside
    the per-container directories. It must never be mistaken for a live runner.
    """
    tracker.running_dir.mkdir(parents=True, exist_ok=True)
    (tracker.running_dir / "legacy-inst").touch()
    assert tracker.has_live_runners(_BEDROCK, exclude_id="other") is False


def test_clear_running_removes_file(tracker: SidecarTracker) -> None:
    handle = tracker.register_running(_BEDROCK, "inst-1")
    tracker.clear_running(handle, _BEDROCK, "inst-1")
    assert not (tracker.running_dir_for(_BEDROCK) / "inst-1").exists()
    assert tracker.has_live_runners(_BEDROCK, exclude_id="other") is False


def test_probe_reaps_stale_file_when_owner_gone(tracker: SidecarTracker) -> None:
    """
    A registration whose lock has been released (owner 'died') is takeable, so the
    probe treats it as stale: it reaps the file and reports no live runner — without
    ever consulting a PID.
    """
    handle = tracker.register_running(_BEDROCK, "dead-inst")
    assert handle is not None
    # Simulate the owner exiting: close the handle, which drops the flock but (unlike
    # the run's clear_running) leaves the file behind, as a crash would.
    handle.close()
    assert (tracker.running_dir_for(_BEDROCK) / "dead-inst").is_file()
    assert tracker.has_live_runners(_BEDROCK, exclude_id="other") is False
    # The stale file was reaped as a side effect of the probe.
    assert not (tracker.running_dir_for(_BEDROCK) / "dead-inst").exists()


def test_probe_reaps_stale_keeps_live(tracker: SidecarTracker) -> None:
    """In one pass, a stale sibling is reaped while a live registration is reported."""
    dead = tracker.register_running(_BEDROCK, "dead-inst")
    assert dead is not None
    dead.close()  # owner gone, file lingers
    live = tracker.register_running(_BEDROCK, "live-inst")
    assert tracker.has_live_runners(_BEDROCK, exclude_id="other") is True
    assert not (tracker.running_dir_for(_BEDROCK) / "dead-inst").exists()
    assert (tracker.running_dir_for(_BEDROCK) / "live-inst").is_file()
    tracker.clear_running(live, _BEDROCK, "live-inst")


def test_probes_false_when_dirs_absent(tracker: SidecarTracker) -> None:
    """No registry directories yet (no run has started) → nothing live."""
    assert tracker.has_live_runners(_BEDROCK, exclude_id="inst-1") is False


def test_clear_tolerates_missing_handle_and_file(tracker: SidecarTracker) -> None:
    """clear_running is a safe no-op when there is nothing registered (e.g. failed start)."""
    tracker.clear_running(None, _BEDROCK, "inst-1")
