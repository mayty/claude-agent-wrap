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

from typing import TYPE_CHECKING, ClassVar, TextIO

from agent_wrap.constants import AGENT_LAUNCHES_DIR
from agent_wrap.domain.sidecars.constants import ROLE_LABEL, ROLE_VALUE
from agent_wrap.lib.flock import any_live_locks, clear_lock_handle, lock_and_hold

if TYPE_CHECKING:
    from pathlib import Path


class SidecarTracker:
    """Host-wide coordination state shared by every sidecar in a run."""

    #: Label marking an agent container (used by the runner's --label flags).
    role_label: ClassVar[str] = ROLE_LABEL
    #: Label value identifying agent containers.
    role_value: ClassVar[str] = ROLE_VALUE

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

    def register_running(self, instance_id: str) -> TextIO | None:
        """Create + lock this run's running registration; return its open handle."""
        return lock_and_hold(self.running_dir / instance_id)

    def clear_running(self, handle: TextIO | None, instance_id: str) -> None:
        """Release this run's running registration and remove it."""
        clear_lock_handle(handle, self.running_dir / instance_id)

    # --- liveness probes (lockability, reaping stale files as a side effect) ---

    def has_live_runners(self, exclude_id: str) -> bool:
        """Whether any running registration other than *exclude_id* is still held."""
        return any_live_locks(self.running_dir, exclude_id=exclude_id)
