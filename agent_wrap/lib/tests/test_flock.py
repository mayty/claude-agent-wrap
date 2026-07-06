# This file has been created with the assistance of an AI tool.
"""Tests for agent_wrap/lib/flock.py."""

from __future__ import annotations

import fcntl
from typing import TYPE_CHECKING

import pytest

from agent_wrap.exceptions import LockTimeoutError
from agent_wrap.lib.flock import file_lock, try_file_lock

if TYPE_CHECKING:
    from pathlib import Path

# --- file_lock ---


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
        with pytest.raises(LockTimeoutError), file_lock(lock, timeout=0.2, poll=0.05):
            pass
    finally:
        fcntl.flock(holder.fileno(), fcntl.LOCK_UN)
        holder.close()


# --- try_file_lock ---


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
