# This file has been created with the assistance of an AI tool.
"""
The ``Sidecar`` interface.

A sidecar is a shared container an agent run depends on. The interface is kept as
narrow as possible: the runner only needs to ensure each sidecar (getting back the
``docker run`` flags the agent needs to reach it), release each on exit, and read
the timing knobs that size the concurrency lock.

``ensure()`` returns a flat ``list[str]`` of ``docker run`` flags (env + connectivity)
— the same shape the runner splices into the agent's launch command — so a sidecar is
free to emit whatever ``-e`` / ``--network`` / ``--add-host`` flags its connectivity
needs without a wider contract.

Locking and the start/stop decision are NOT a sidecar concern: the runner holds one
shared lock around the whole ensure-all / release-all phase and consults a single
``SidecarTracker`` once. So ``ensure()`` and ``release()`` are pure container
mechanics — they run with the lock already held and must not lock, announce, or
decide whether to stop. Lock-free pre-work (e.g. the image pull) goes in ``prepare()``,
which the runner runs *before* taking the lock.
"""

from __future__ import annotations

from abc import ABC, abstractmethod


class Sidecar(ABC):
    """A shared helper container an agent run depends on."""

    #: Stable identity. Labels the sidecar and names provider-specific resources.
    key: str

    @property
    @abstractmethod
    def cold_start_time(self) -> float:
        """Seconds a cold start takes (the lock winner pays this once)."""

    @property
    @abstractmethod
    def short_circuit_time(self) -> float:
        """Seconds one agent takes to walk the lock on the hot path (sidecar up)."""

    def prepare(self) -> None:  # noqa: B027 -- intentional optional no-op hook
        """
        Lock-free pre-work, run by the runner *before* the shared lock is taken.

        Default no-op. ``LiteLLMSidecar`` overrides it to pull the image — a cold
        pull must never happen under the lock, or the whole launch herd blocks on it.
        """

    @abstractmethod
    def ensure(
        self,
        *,
        use_host_net: bool,
        agent_network: str | None,
    ) -> list[str]:
        """
        Make the sidecar running + healthy and return the agent's ``docker run`` flags.

        Runs with the runner's shared lock already held — must not lock or announce.
        The returned flags (env vars + connectivity such as ``--network`` /
        ``--add-host``) are spliced into the agent container's launch command so it
        can reach this sidecar. Raises on failure.
        """

    @abstractmethod
    def release(self) -> None:
        """
        Stop the sidecar container.

        Runs with the shared lock held and only after the runner's ``SidecarTracker``
        has decided the run may stop. Must be idempotent / a no-op when the container
        is not running (the runner releases every sidecar it began ensuring, in
        reverse order, including one whose ``ensure()`` raised mid-start).
        """
