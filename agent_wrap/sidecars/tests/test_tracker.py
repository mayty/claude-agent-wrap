# This file has been created with the assistance of an AI tool.
"""Tests for agent_wrap/sidecars/tracker.py."""

from __future__ import annotations

import json
from pathlib import Path

import pytest_mock

from agent_wrap.sidecars.tracker import ActivityRecord, SidecarTracker


def _tracker(tmp_path: Path, *, grace: float = 30.0) -> SidecarTracker:
    return SidecarTracker(tmp_path, idle_grace_sec=grace)


# --- paths ---


def test_paths_under_agent_launches(tmp_path: Path) -> None:
    t = _tracker(tmp_path)
    assert t.lock_path == tmp_path / ".agent-launches" / "sidecars.lock"
    assert t.activity_path == tmp_path / ".agent-launches" / "sidecars-activity.json"


# --- announce / read_activity ---


def test_announce_then_read(tmp_path: Path) -> None:
    t = _tracker(tmp_path)
    t.announce("inst-1", now=123.5)
    assert t.read_activity() == ActivityRecord(timestamp=123.5, fingerprint="inst-1")


def test_announce_writes_json(tmp_path: Path) -> None:
    t = _tracker(tmp_path)
    t.announce("inst-1", now=10.0)
    data = json.loads((tmp_path / ".agent-launches" / "sidecars-activity.json").read_text())
    assert data == {"timestamp": 10.0, "fingerprint": "inst-1"}


def test_read_activity_missing(tmp_path: Path) -> None:
    assert _tracker(tmp_path).read_activity() is None


def test_read_activity_malformed(tmp_path: Path) -> None:
    t = _tracker(tmp_path)
    t.activity_path.parent.mkdir(parents=True)
    t.activity_path.write_text("not json")
    assert t.read_activity() is None


def test_read_activity_missing_keys(tmp_path: Path) -> None:
    t = _tracker(tmp_path)
    t.activity_path.parent.mkdir(parents=True)
    t.activity_path.write_text('{"timestamp": 1.0}')
    assert t.read_activity() is None


# --- live_agent_count (role-only filter) ---


def test_live_agent_count_filters_by_role_only(
    tmp_path: Path, mocker: pytest_mock.MockFixture
) -> None:
    t = _tracker(tmp_path)
    mock_count = mocker.patch(
        "agent_wrap.sidecars.tracker.count_labeled_containers",
        return_value=3,
    )
    assert t.live_agent_count() == 3
    labels = mock_count.call_args.args[0]
    # One common count of all agents — no per-sidecar label.
    assert labels == {"agent-wrap.role": "claude-agent"}


# --- should_stop matrix ---


def test_should_stop_when_agents_live(tmp_path: Path, mocker: pytest_mock.MockFixture) -> None:
    t = _tracker(tmp_path)
    mocker.patch.object(t, "live_agent_count", return_value=1)
    assert t.should_stop("inst-1", now=1000.0) is False


def test_should_stop_fingerprint_is_me(tmp_path: Path, mocker: pytest_mock.MockFixture) -> None:
    """count==0 and I am the last starter → stop immediately, no grace wait."""
    t = _tracker(tmp_path)
    mocker.patch.object(t, "live_agent_count", return_value=0)
    t.announce("inst-1", now=1000.0)
    assert t.should_stop("inst-1", now=1001.0) is True


def test_should_stop_other_fingerprint_fresh(
    tmp_path: Path, mocker: pytest_mock.MockFixture
) -> None:
    """count==0 but a newer start announced within grace → keep alive."""
    t = _tracker(tmp_path, grace=30.0)
    mocker.patch.object(t, "live_agent_count", return_value=0)
    t.announce("inst-2", now=1000.0)
    assert t.should_stop("inst-1", now=1005.0) is False


def test_should_stop_other_fingerprint_stale(
    tmp_path: Path, mocker: pytest_mock.MockFixture
) -> None:
    """count==0 and last start was long ago → batch drained, stop (grace backstop)."""
    t = _tracker(tmp_path, grace=30.0)
    mocker.patch.object(t, "live_agent_count", return_value=0)
    t.announce("inst-2", now=1000.0)
    assert t.should_stop("inst-1", now=1040.0) is True


def test_should_stop_no_activity_file(tmp_path: Path, mocker: pytest_mock.MockFixture) -> None:
    """count==0 and no heartbeat (failed start that never announced) → clean up."""
    t = _tracker(tmp_path)
    mocker.patch.object(t, "live_agent_count", return_value=0)
    assert t.should_stop("inst-1", now=1000.0) is True
