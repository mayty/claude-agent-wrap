# This file has been created with the assistance of an AI tool.
"""Tests for agent_wrap/lib/flock.py."""

from __future__ import annotations

import fcntl
from typing import TYPE_CHECKING

import pytest

from agent_wrap.exceptions import LockTimeoutError
from agent_wrap.lib.flock import file_lock, live_lock_ids, lock_and_hold, try_file_lock

if TYPE_CHECKING:
    from pathlib import Path


def test_file_lock_runs_body(tmp_path: Path) -> None:
    lock = tmp_path / "lock"
    ran = []
    with file_lock(lock):
        ran.append(True)
    assert ran == [True]


def test_file_lock_creates_parent(tmp_path: Path) -> None:
    lock = tmp_path / "nested" / "lock"
    with file_lock(lock):
        pass
    assert lock.exists()


def test_file_lock_released_after_block(tmp_path: Path) -> None:
    lock = tmp_path / "lock"
    with file_lock(lock):
        pass
    # If the lock were still held, a non-blocking acquire on a fresh fd would fail.
    with try_file_lock(lock) as acquired:
        assert acquired is True


def test_file_lock_released_on_exception(tmp_path: Path) -> None:
    lock = tmp_path / "lock"

    def boom() -> None:
        with file_lock(lock):
            msg = "boom"
            raise ValueError(msg)

    with pytest.raises(ValueError, match="boom"):
        boom()
    with try_file_lock(lock) as acquired:
        assert acquired is True


def test_file_lock_timeout_when_held(tmp_path: Path) -> None:
    lock = tmp_path / "lock"
    # Hold the lock on an independent fd, then assert a timed acquire gives up.
    holder = open(lock, "w")  # noqa: SIM115
    try:
        fcntl.flock(holder.fileno(), fcntl.LOCK_EX)
        with pytest.raises(LockTimeoutError), file_lock(lock, timeout=0.2):
            pass
    finally:
        fcntl.flock(holder.fileno(), fcntl.LOCK_UN)
        holder.close()


def test_try_file_lock_acquires_when_free(tmp_path: Path) -> None:
    lock = tmp_path / "lock"
    with try_file_lock(lock) as acquired:
        assert acquired is True


def test_try_file_lock_skips_when_held(tmp_path: Path) -> None:
    lock = tmp_path / "lock"
    holder = open(lock, "w")  # noqa: SIM115
    try:
        fcntl.flock(holder.fileno(), fcntl.LOCK_EX)
        with try_file_lock(lock) as acquired:
            assert acquired is False
    finally:
        fcntl.flock(holder.fileno(), fcntl.LOCK_UN)
        holder.close()


def test_try_file_lock_releases_after_block(tmp_path: Path) -> None:
    lock = tmp_path / "lock"
    with try_file_lock(lock) as acquired:
        assert acquired is True
    # A subsequent acquire should succeed since the first was released.
    with try_file_lock(lock) as acquired:
        assert acquired is True


def test_live_lock_ids_reports_held_entries(tmp_path: Path) -> None:
    handle = lock_and_hold(tmp_path / "held")
    assert handle is not None
    try:
        assert live_lock_ids(tmp_path) == ["held"]
    finally:
        handle.close()


def test_live_lock_ids_omits_stale_entry(tmp_path: Path) -> None:
    stale = tmp_path / "stale"
    handle = lock_and_hold(stale)
    assert handle is not None
    handle.close()  # owner "exited" — the lock is takeable again
    assert live_lock_ids(tmp_path) == []


def test_live_lock_ids_leaves_stale_file_on_disk(tmp_path: Path) -> None:
    """The whole point vs any_live_locks: reporting must not reap."""
    stale = tmp_path / "stale"
    handle = lock_and_hold(stale)
    assert handle is not None
    handle.close()
    live_lock_ids(tmp_path)
    assert stale.exists()


def test_live_lock_ids_does_not_truncate(tmp_path: Path) -> None:
    entry = tmp_path / "entry"
    entry.write_text("payload")
    live_lock_ids(tmp_path)
    assert entry.read_text() == "payload"


def test_live_lock_ids_sorted(tmp_path: Path) -> None:
    handles = [lock_and_hold(tmp_path / name) for name in ("c", "a", "b")]
    try:
        assert live_lock_ids(tmp_path) == ["a", "b", "c"]
    finally:
        for handle in handles:
            assert handle is not None
            handle.close()


def test_live_lock_ids_ignores_subdirectories(tmp_path: Path) -> None:
    (tmp_path / "subdir").mkdir()
    assert live_lock_ids(tmp_path) == []


def test_live_lock_ids_missing_directory(tmp_path: Path) -> None:
    assert live_lock_ids(tmp_path / "absent") == []
