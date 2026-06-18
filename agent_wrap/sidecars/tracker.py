# This file has been created with the assistance of an AI tool.
"""
The one common per-run sidecar tracker.

Whether the shared sidecars should be torn down is a single decision about the whole
launch, not something each sidecar re-derives. ``SidecarTracker`` owns that
host-wide coordination state — one activity heartbeat, one live-agent count, one
stop decision — keyed off the install root, and the runner consults it once per run:

* ``announce()`` stamps ``{timestamp, fingerprint}`` as the last action under the
  shared lock (success only), so a releaser in the ensure→docker-run gap sees a fresh
  fingerprint and won't tear down;
* ``live_agent_count()`` counts running agents from ``docker ps`` (the single source
  of truth) by the common ``agent-wrap.role`` label;
* ``should_stop()`` is the pure decision of whether the releasing run may stop the
  sidecars, given the live count, the heartbeat, the releaser's own id, and grace.

The shared lock itself lives at ``lock_path``; the runner takes it directly via
``agent_wrap.lib.flock`` (one lock for the whole ensure-all / release-all phase).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, ClassVar

from agent_wrap.lib.atomic import atomic_write_json
from agent_wrap.lib.docker_utils import count_labeled_containers

if TYPE_CHECKING:
    from pathlib import Path


@dataclass(frozen=True)
class ActivityRecord:
    """The run's activity heartbeat: when, and by which agent instance."""

    timestamp: float
    fingerprint: str


class SidecarTracker:
    """Host-wide coordination state shared by every sidecar in a run."""

    #: Label marking an agent container (the common live-count filter).
    role_label: ClassVar[str] = "agent-wrap.role"
    role_value: ClassVar[str] = "claude-agent"

    #: Sub-directory of the install root holding host-wide launch state.
    state_dirname: ClassVar[str] = ".agent-launches"
    lock_filename: ClassVar[str] = "sidecars.lock"
    activity_filename: ClassVar[str] = "sidecars-activity.json"

    def __init__(self, tool_dir: Path, *, idle_grace_sec: float = 30.0) -> None:
        #: Grace window covering the ensure→docker-run launch gap and batch
        #: zero-crossings, before a releasing run may stop idle sidecars.
        self.idle_grace_sec = idle_grace_sec
        self._state_dir = tool_dir / self.state_dirname

    @property
    def lock_path(self) -> Path:
        return self._state_dir / self.lock_filename

    @property
    def activity_path(self) -> Path:
        return self._state_dir / self.activity_filename

    # --- activity heartbeat ---

    def announce(self, instance_id: str, *, now: float) -> None:
        """Atomically stamp the activity file with this run's time + fingerprint."""
        atomic_write_json(
            self.activity_path,
            {"timestamp": now, "fingerprint": instance_id},
        )

    def read_activity(self) -> ActivityRecord | None:
        """Read the activity heartbeat, or None if absent/unreadable/malformed."""
        path = self.activity_path
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text())
            return ActivityRecord(
                timestamp=float(data["timestamp"]),
                fingerprint=str(data["fingerprint"]),
            )
        except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError):
            return None

    # --- live agent count (docker is the source of truth) ---

    def live_agent_count(self) -> int:
        """Return the number of running agent containers, by the common role label."""
        return count_labeled_containers({self.role_label: self.role_value})

    # --- stop decision (pure) ---

    def should_stop(self, instance_id: str, *, now: float) -> bool:
        """
        Whether the run *instance_id*, on exit, may stop the shared sidecars.

        Stop only when no agents are live AND either this run was the last to
        announce a start (so nothing newer is in flight) or the heartbeat is older
        than the grace window (the batch has drained). Otherwise a newer start is
        in flight (or mid-launch) and the sidecars must stay up.
        """
        if self.live_agent_count() != 0:
            return False
        record = self.read_activity()
        if record is None:
            # No heartbeat (e.g. a start that never announced) and nothing live →
            # safe to clean up.
            return True
        if record.fingerprint == instance_id:
            return True
        return (now - record.timestamp) > self.idle_grace_sec
