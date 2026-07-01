# This file has been created with the assistance of an AI tool.
"""
Priority-based file lock with waiter registration.

Two acquisition strategies:

* **HI** — start path: registers a start-waiter ticket before contending for
  the lock (so a stopping run yields), acquires the lock with a computed
  timeout, clears the waiter inside, and yields the critical section. The
  waiter is cleaned up defensively on any early exit.
* **LO** — stop path: loops internally acquiring the lock and yielding to
  any live start-waiter (sleep + retry). Only yields the critical section
  once the lock is held and no starters are waiting. The caller performs
  its own domain checks inside the block.

The public entry point is :func:`priority_lock`, dispatched on
:class:`Priority`.
"""

from __future__ import annotations

import time
from contextlib import AbstractContextManager, contextmanager
from enum import Enum, auto
from typing import TYPE_CHECKING

from agent_wrap.lib.flock import (
    any_live_locks,
    clear_lock_handle,
    file_lock,
    lock_and_hold,
)

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

#: How long a releasing (stopping) run sleeps before re-acquiring the shared lock
#: when it has yielded to a live starter. Stops are low priority and may wait
#: indefinitely, so this only bounds the busy-wait granularity, not total wait.
STOP_YIELD_POLL_SEC = 0.1


class Priority(Enum):
    """
    Acquisition priority for :func:`priority_lock`.

    ``HI`` — start path: register a waiter ticket, acquire with timeout,
    clear the waiter inside the lock. Defensive cleanup on failure.

    ``LO`` — stop path: loop internally yielding to live start-waiters;
    only enters the critical section once the lock is held and no
    starters are waiting. The caller performs domain checks inside.
    """

    HI = auto()
    LO = auto()


@contextmanager
def _high_priority_lock(
    lock_path: Path,
    waiters_dir: Path,
    instance_id: str,
    timeout: float,
) -> Iterator[None]:
    """HI-priority: register a start-waiter ticket, acquire the lock, clear waiter."""
    waiter_handle = lock_and_hold(waiters_dir / instance_id)
    try:
        with file_lock(lock_path, timeout=timeout):
            clear_lock_handle(waiter_handle, waiters_dir / instance_id)
            waiter_handle = None
            yield
    finally:
        # Defensive cleanup: if lock acquisition failed (timeout), the waiter
        # handle is still set; if everything succeeded, waiter_handle is None.
        if waiter_handle is not None:
            clear_lock_handle(waiter_handle, waiters_dir / instance_id)


@contextmanager
def _low_priority_lock(
    lock_path: Path,
    waiters_dir: Path,
    instance_id: str,  # noqa: ARG001 — consistent interface
) -> Iterator[None]:
    """LO-priority: yield-and-retry loop — only enters when no starters are waiting."""
    while True:
        with file_lock(lock_path):
            if not any_live_locks(waiters_dir):
                yield
                return
        time.sleep(STOP_YIELD_POLL_SEC)


def priority_lock(
    priority: Priority,
    *,
    lock_path: Path,
    waiters_dir: Path,
    instance_id: str,
    timeout: float | None = None,
) -> AbstractContextManager[None]:
    """
    Acquire a shared file lock with the given *priority*.

    ``HI`` (start path): requires *timeout*. Registers a start-waiter ticket,
    acquires the lock, clears the ticket, and yields. The waiter is cleaned up
    defensively on any early exit.

    ``LO`` (stop path): ignores *timeout*. Loops internally acquiring the lock
    and yielding to live start-waiters (the lock is released and retried after
    a sleep). Only yields the critical section once the lock is held and no
    starters are waiting. The caller performs domain checks inside the block.
    """
    if priority == Priority.HI:
        if timeout is None:
            msg = "timeout is required for HI priority"
            raise ValueError(msg)
        return _high_priority_lock(lock_path, waiters_dir, instance_id, timeout)
    if priority == Priority.LO:
        return _low_priority_lock(lock_path, waiters_dir, instance_id)
    msg = f"unknown priority: {priority!r}"
    raise ValueError(msg)
