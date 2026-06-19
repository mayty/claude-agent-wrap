# This file has been edited with the assistance of an AI tool.
"""
File-locking helpers built on ``fcntl.flock``.

Three flavours, matching the call sites in the sidecar lifecycle:

* :func:`file_lock` — blocking context manager with an optional timeout; raises
  :class:`LockTimeoutError` if the lock can't be taken in time. Used by the start
  path (``ensure``), which must win the lock and is given a generous timeout.
* :func:`try_file_lock` — non-blocking context manager; yields ``True`` if the lock
  was taken, ``False`` if someone else holds it. Used to probe registration files:
  acquiring the lock means the owner is gone (stale, reap it).
* :func:`lock_and_hold` — non-blocking; takes the lock and returns the *open handle*
  so the caller can hold it open across an arbitrary span (e.g. a whole agent run)
  rather than just one ``with`` block. The kernel drops the lock automatically when
  the holding process dies, which is what makes liveness immune to PID recycling.

All open the lock file in write mode (``flock`` needs a real fd). The two context
managers release + close on exit, even when the body raises; :func:`lock_and_hold`
hands ownership of the handle to the caller.
"""

from __future__ import annotations

import fcntl
import time
from contextlib import contextmanager
from typing import TYPE_CHECKING, TextIO

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


def lock_and_hold(path: Path) -> TextIO | None:
    """
    Take an exclusive ``flock`` on *path* and return the open handle, without blocking.

    Returns the open file handle if the lock was acquired — the caller must keep it
    open for as long as the lock should be held, and close it (releasing the lock) to
    let go. Returns ``None`` if another holder already has the lock.

    Unlike the context managers, the lock outlives this call: it is released only when
    the handle is closed or the owning process exits (the kernel reclaims ``flock``s on
    process death). This is the primitive behind crash-safe, PID-recycle-immune
    liveness — a still-locked file means its owner is alive.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(path, "w")  # noqa: SIM115 -- handle ownership is handed to the caller
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        return None
    return handle
