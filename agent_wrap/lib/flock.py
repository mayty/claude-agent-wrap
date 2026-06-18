# This file has been created with the assistance of an AI tool.
"""
File-locking context managers built on ``fcntl.flock``.

Two flavours, matching the two call sites in the sidecar lifecycle:

* :func:`file_lock` — blocking with an optional timeout; raises
  :class:`LockTimeoutError` if the lock can't be taken in time. Used by the start
  path (``ensure``), which must win the lock and is given a generous timeout.
* :func:`try_file_lock` — non-blocking; yields ``True`` if the lock was taken,
  ``False`` if someone else holds it. Used by the stop path (``release``), which
  must never block a concurrent start: if it can't get the lock it simply skips.

Both open the lock file in write mode (``flock`` needs a real fd) and always
release + close on exit, even when the body raises.
"""

from __future__ import annotations

import fcntl
import time
from contextlib import contextmanager
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path


class LockTimeoutError(RuntimeError):
    """Raised by :func:`file_lock` when the lock can't be acquired in time."""


@contextmanager
def file_lock(path: Path, *, timeout: float | None = None, poll: float = 0.1) -> Iterator[None]:
    """
    Hold an exclusive ``flock`` on *path* for the duration of the block.

    With ``timeout=None`` this blocks indefinitely. With a positive *timeout* it
    polls every *poll* seconds and raises :class:`LockTimeoutError` if the deadline
    passes without acquiring the lock.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(path, "w")  # noqa: SIM115 -- fd lifetime is the context manager
    try:
        if timeout is None:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        else:
            deadline = time.monotonic() + timeout
            while True:
                try:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except OSError:
                    if time.monotonic() >= deadline:
                        msg = f"timed out waiting for lock {path}"
                        raise LockTimeoutError(msg) from None
                    time.sleep(poll)
        yield
    finally:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()


@contextmanager
def try_file_lock(path: Path) -> Iterator[bool]:
    """
    Try once to take an exclusive ``flock`` on *path*, without blocking.

    Yields ``True`` if the lock was acquired (and releases it on exit), or
    ``False`` if another holder has it. Never blocks and never raises on
    contention.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(path, "w")  # noqa: SIM115 -- fd lifetime is the context manager
    acquired = False
    try:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            acquired = True
        except OSError:
            acquired = False
        yield acquired
    finally:
        if acquired:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()
