# This file has been edited with the assistance of an AI tool.
"""
The one common per-run sidecar tracker.

Whether the shared sidecars should be torn down is a single decision about the whole
launch, not something each sidecar re-derives. ``SidecarTracker`` owns that
host-wide coordination state — keyed off the install root — as two directories of
**lock-held registration files**, one file per run named by its ``instance_id``:

* ``start-waiters/`` — a ticket a starting run holds while it waits for (and briefly
  after taking) the shared lock. A still-locked ticket is the signal that makes a
  stopping run yield: starts have priority.
* ``running/`` — a registration a run holds for its whole lifetime, from just before
  it launches the agent until it exits. A still-locked entry means an agent is live.

Liveness is tested by **lockability**, never by PID: a file whose ``flock`` can be
taken has lost its owner (the kernel drops the lock on process death), so it is
stale and gets reaped; a file that cannot be locked has a live owner. This is immune
to PID recycling and needs no explicit crash cleanup.

The shared lock itself lives at ``lock_path``; the runner takes it directly via
``agent_wrap.lib.flock`` (one lock for the whole ensure-all / release-all phase).
"""

from __future__ import annotations

import contextlib
from typing import TYPE_CHECKING, ClassVar, TextIO

from agent_wrap.constants import AGENT_LAUNCHES_DIR, BASE_IMAGE_NAME
from agent_wrap.lib.flock import lock_and_hold, try_file_lock

if TYPE_CHECKING:
    from pathlib import Path


class SidecarTracker:
    """Host-wide coordination state shared by every sidecar in a run."""

    #: Label marking an agent container (used by the runner's --label flags).
    role_label: ClassVar[str] = "agent-wrap.role"
    role_value: ClassVar[str] = BASE_IMAGE_NAME

    #: Sub-directory of the install root holding host-wide launch state.
    state_dirname: ClassVar[str] = AGENT_LAUNCHES_DIR.name
    lock_filename: ClassVar[str] = "sidecars.lock"
    #: Directories of lock-held per-run registration files (named by instance id).
    waiters_dirname: ClassVar[str] = "start-waiters"
    running_dirname: ClassVar[str] = "running"

    def __init__(self, tool_dir: Path) -> None:
        self._state_dir = tool_dir / self.state_dirname

    @property
    def lock_path(self) -> Path:
        return self._state_dir / self.lock_filename

    @property
    def start_waiters_dir(self) -> Path:
        return self._state_dir / self.waiters_dirname

    @property
    def running_dir(self) -> Path:
        return self._state_dir / self.running_dirname

    # --- registration (the caller holds the returned handle for the lock's life) ---

    def register_waiter(self, instance_id: str) -> TextIO | None:
        """Create + lock this run's start-waiter ticket; return its open handle."""
        return lock_and_hold(self.start_waiters_dir / instance_id)

    def register_running(self, instance_id: str) -> TextIO | None:
        """Create + lock this run's running registration; return its open handle."""
        return lock_and_hold(self.running_dir / instance_id)

    @staticmethod
    def clear(handle: TextIO | None, path: Path) -> None:
        """Release (close) a held registration handle and remove its file."""
        if handle is not None:
            handle.close()
        with contextlib.suppress(OSError):
            path.unlink()

    def clear_waiter(self, handle: TextIO | None, instance_id: str) -> None:
        """Release this run's start-waiter ticket and remove it."""
        self.clear(handle, self.start_waiters_dir / instance_id)

    def clear_running(self, handle: TextIO | None, instance_id: str) -> None:
        """Release this run's running registration and remove it."""
        self.clear(handle, self.running_dir / instance_id)

    # --- liveness probes (lockability, reaping stale files as a side effect) ---

    def has_live_waiters(self) -> bool:
        """Whether any start-waiter ticket is still held (its owner alive)."""
        return self._any_live(self.start_waiters_dir, exclude_id=None)

    def has_live_runners(self, exclude_id: str) -> bool:
        """Whether any running registration other than *exclude_id* is still held."""
        return self._any_live(self.running_dir, exclude_id=exclude_id)

    def _any_live(self, directory: Path, *, exclude_id: str | None) -> bool:
        """
        Walk *directory*, reaping registration files whose lock is free (owner gone),
        and report whether any remaining file is still locked (owner alive).

        Files are probed with a non-blocking lock: acquiring it proves the owner has
        exited, so the stale file is unlinked while the lock is held. The whole walk
        runs each file once; a single live holder is enough to answer ``True``, but we
        keep going so stale siblings are cleaned up in the same pass.
        """
        if not directory.is_dir():
            return False
        live = False
        for path in directory.iterdir():
            if not path.is_file():
                continue
            if exclude_id is not None and path.name == exclude_id:
                continue
            with try_file_lock(path) as acquired:
                if acquired:
                    # Lock was free → owner gone → stale; reap while holding the lock.
                    with contextlib.suppress(OSError):
                        path.unlink()
                else:
                    live = True
        return live
