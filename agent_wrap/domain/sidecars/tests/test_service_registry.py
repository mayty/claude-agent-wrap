# This file has been created with the assistance of an AI tool.
"""Tests for SidecarService.registry_state — the read-only view of the flock registry."""

from typing import TYPE_CHECKING
from unittest.mock import Mock

import pytest

from agent_wrap.domain.display.service import DisplayService
from agent_wrap.domain.sidecars.service import SidecarService
from agent_wrap.domain.sidecars.tracker import SidecarTracker
from agent_wrap.lib.flock import lock_and_hold

if TYPE_CHECKING:
    from pathlib import Path

_BEDROCK = "agent-wrap-litellm-bedrock"
_TELEGRAM = "agent-wrap-telegram"


@pytest.fixture
def svc() -> SidecarService:
    return SidecarService(display_service=Mock(spec=DisplayService))


@pytest.fixture
def tracker(tmp_path: Path) -> SidecarTracker:
    """Write the registry that the service then reads back."""
    return SidecarTracker(tmp_path)


def test_registry_state_maps_containers_to_live_ids(
    tracker: SidecarTracker, svc: SidecarService, tmp_path: Path
) -> None:
    bedrock = tracker.register_running(_BEDROCK, "inst-1")
    telegram = tracker.register_running(_TELEGRAM, "inst-1")
    try:
        state = svc.registry_state(tmp_path)
        assert state.by_container == {_BEDROCK: ["inst-1"], _TELEGRAM: ["inst-1"]}
    finally:
        tracker.clear_running(bedrock, _BEDROCK, "inst-1")
        tracker.clear_running(telegram, _TELEGRAM, "inst-1")


def test_registry_state_reports_known_container_with_nobody_attached(
    tracker: SidecarTracker, svc: SidecarService, tmp_path: Path
) -> None:
    """Registration dirs are never removed, so an empty one means 'nobody attached'."""
    tracker.running_dir_for(_BEDROCK).mkdir(parents=True, exist_ok=True)
    assert svc.registry_state(tmp_path).by_container == {_BEDROCK: []}


def test_registry_state_omits_dead_owner_but_keeps_its_file(
    tracker: SidecarTracker, svc: SidecarService, tmp_path: Path
) -> None:
    """A reporting read must not reap — unlike has_live_runners, which does."""
    handle = tracker.register_running(_BEDROCK, "dead-inst")
    assert handle is not None
    handle.close()  # owner "crashed": lock dropped, file left behind

    assert svc.registry_state(tmp_path).by_container == {_BEDROCK: []}
    assert (tracker.running_dir_for(_BEDROCK) / "dead-inst").exists()


def test_registry_state_ignores_legacy_flat_file(
    tracker: SidecarTracker, svc: SidecarService, tmp_path: Path
) -> None:
    tracker.running_dir.mkdir(parents=True, exist_ok=True)
    (tracker.running_dir / "legacy-inst").touch()
    assert svc.registry_state(tmp_path).by_container == {}


def test_registry_state_reports_start_queue_separately(
    tracker: SidecarTracker, svc: SidecarService, tmp_path: Path
) -> None:
    running = tracker.register_running(_BEDROCK, "inst-1")
    waiter = lock_and_hold(tracker.start_waiters_dir / "inst-2")
    assert waiter is not None
    try:
        state = svc.registry_state(tmp_path)
        assert state.waiting == ["inst-2"]
        assert state.by_container == {_BEDROCK: ["inst-1"]}
    finally:
        waiter.close()
        tracker.clear_running(running, _BEDROCK, "inst-1")


def test_registry_state_empty_when_nothing_registered(svc: SidecarService, tmp_path: Path) -> None:
    state = svc.registry_state(tmp_path)
    assert state.by_container == {}
    assert state.waiting == []
