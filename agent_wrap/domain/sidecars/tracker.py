# This file has been edited with the assistance of an AI tool.
"""
The one common per-run sidecar tracker.

``SidecarTracker`` owns the host-wide coordination state behind the teardown decision —
keyed off the install root — as two directories of **lock-held registration files**:

* ``start-waiters/`` — a ticket a starting run holds while it waits for (and briefly
  after taking) the shared lock, one file per run named by its ``instance_id``. A
  still-locked ticket is the signal that makes a stopping run yield: starts have
  priority.
* ``running/<container_name>/`` — a registration a run holds **per sidecar container**
  it depends on, from just before it launches the agent until it exits. A still-locked
  entry means an agent is live on that container.

Teardown is therefore decided **per container**, not per launch: an agent on one
provider exiting stops that provider's sidecar even while agents on other providers
keep running, and never touches theirs. Sidecars that genuinely share one container —
the single Telegram container, whatever the provider — share one refcount, which is the
same question asked correctly.

Liveness is tested by **lockability**, never by PID: a file whose ``flock`` can be
taken has lost its owner (the kernel drops the lock on process death), so it is
stale and gets reaped; a file that cannot be locked has a live owner. This is immune
to PID recycling and needs no explicit crash cleanup.

``lock_path`` and ``start_waiters_dir`` stay **global**, deliberately unpartitioned —
partitioning them per container would break three things:

* the master-key approval hooks (``on_started`` / ``on_stopping``) read-modify-write a
  shared ``.claude.json``; concurrent providers would lose an approval;
* a cold start picks a free port and then starts the container non-atomically, so
  without one global lock two providers could choose the same port;
* ``start-waiters/`` is the priority signal for that one lock — split it, and a stop
  for one provider could enter the critical section while another's start is queued.

The lock itself is taken by the runner directly via ``agent_wrap.lib.flock`` (one lock
for the whole ensure-all / release-all phase).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar, TextIO

from agent_wrap.constants import AGENT_LAUNCHES_DIR
from agent_wrap.domain.sidecars.constants import ROLE_LABEL, ROLE_VALUE
from agent_wrap.domain.sidecars.models import RegistryState
from agent_wrap.lib.flock import any_live_locks, clear_lock_handle, live_lock_ids, lock_and_hold

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
    #: Directory of lock-held start tickets, one file per run (named by instance id).
    waiters_dirname: ClassVar[str] = "start-waiters"
    #: Parent of the per-container registration directories; the files inside each are
    #: named by instance id.
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

    def running_dir_for(self, container_name: str) -> Path:
        """
        Directory of registrations for one sidecar container.

        *container_name* is used verbatim as the path component — Docker names match
        ``[a-zA-Z0-9][a-zA-Z0-9_.-]*``, so no sanitizing is needed.
        """
        return self.running_dir / container_name

    # --- registration (the caller holds the returned handle for the lock's life) ---

    def register_running(self, container_name: str, instance_id: str) -> TextIO | None:
        """Create + lock this run's registration on *container_name*; return its handle."""
        return lock_and_hold(self.running_dir_for(container_name) / instance_id)

    def clear_running(self, handle: TextIO | None, container_name: str, instance_id: str) -> None:
        """Release this run's registration on *container_name* and remove it."""
        clear_lock_handle(handle, self.running_dir_for(container_name) / instance_id)

    # --- liveness probes (lockability, reaping stale files as a side effect) ---

    def has_live_runners(self, container_name: str, *, exclude_id: str) -> bool:
        """
        Whether another run than *exclude_id* still holds a registration on the container.

        This is the teardown predicate: ``False`` means the caller is the last agent out
        on *container_name* and may stop it. Registrations under other container names
        are invisible here, which is what lets providers be torn down independently.
        """
        return any_live_locks(self.running_dir_for(container_name), exclude_id=exclude_id)

    def registry_state(self) -> RegistryState:
        """
        Read the whole registry — every container's live runners plus the start queue.

        The reporting counterpart to :meth:`has_live_runners`, which answers one
        container's question and reaps as it goes. This walks the entire tree, keys the
        result by container name, and mutates nothing: a reader must not reap another
        run's state, and a stale file left behind here is reaped by the next real launch.

        Containers whose registration directory exists but holds no live entry are
        reported with an empty list rather than omitted — the directories are never
        removed, so their presence is history, but the distinction between "known
        container, nobody attached" and "never seen" is worth keeping.
        """
        by_container: dict[str, list[str]] = {}
        if self.running_dir.is_dir():
            for entry in sorted(self.running_dir.iterdir()):
                # Files directly under running/ predate the per-container layout.
                if entry.is_dir():
                    by_container[entry.name] = live_lock_ids(entry)
        return RegistryState(
            by_container=by_container, waiting=live_lock_ids(self.start_waiters_dir)
        )
