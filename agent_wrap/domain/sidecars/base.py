# This file has been edited with the assistance of an AI tool.
"""
The ``Sidecar`` interface.

Locking and the start/stop decision are NOT a sidecar concern: the runner holds one
shared lock around the whole ensure-all / release-all phase and consults a single
``SidecarTracker``. So ``ensure()`` and ``release()`` are pure container mechanics --
they run with the lock already held and must not lock, announce, or decide whether to
stop. Lock-free pre-work such as the image pull goes in ``prepare()``, which the runner
runs *before* taking the lock.

The one thing a sidecar contributes to that decision is its ``container_name``: the
runner refcounts live agents **per container name**.
"""

from abc import ABC, abstractmethod


class Sidecar(ABC):
    """A shared helper container an agent run depends on."""

    @property
    @abstractmethod
    def container_name(self) -> str:
        """
        The Docker container this sidecar manages — also its refcount identity.

        Sidecars with different container names are released independently; those sharing
        one -- the single Telegram container -- share a refcount. Docker names match
        ``[a-zA-Z0-9][a-zA-Z0-9_.-]*``, so this is safe as a path component.
        """

    @property
    @abstractmethod
    def cold_start_time(self) -> float:
        """Seconds a cold start takes (the lock winner pays this once)."""

    @property
    @abstractmethod
    def short_circuit_time(self) -> float:
        """Seconds one agent takes to walk the lock on the hot path (sidecar up)."""

    @classmethod
    def required_secrets(cls) -> list[tuple[str, str]]:
        """
        Return ``(key_name, description)`` tuples for secrets this sidecar needs.

        Simple key names -- the orchestrator prepends the sidecar name for storage.
        """
        return []

    def prepare(self) -> None:  # noqa: B027 -- intentional optional no-op hook
        """
        Lock-free pre-work, run by the runner *before* the shared lock is taken.

        Default no-op. ``LiteLLMSidecar`` overrides it to pull the image: a cold pull
        under the lock would block the whole launch herd.
        """

    @abstractmethod
    def ensure(
        self,
        *,
        use_host_net: bool,
        agent_network: str | None,
        secrets: dict[str, str] | None = None,
    ) -> list[str]:
        """
        Make the sidecar running + healthy and return the agent's ``docker run`` flags.

        Runs with the runner's shared lock already held -- must not lock or announce. The
        returned flags are spliced into the agent container's launch command.

        *secrets* carries resolved key->value pairs; sidecars needing credentials take
        them from there rather than reading configuration files directly.
        """

    @abstractmethod
    def release(self) -> None:
        """
        Stop the sidecar container.

        Runs with the shared lock held, only once no other live agent holds this
        ``container_name``. Must be a no-op when the container is not running: the runner
        releases every sidecar it *began* ensuring, including one that raised mid-start.
        """

    def on_exit(self) -> None:  # noqa: B027 -- intentional optional no-op hook
        """
        Per-agent cleanup, run by the runner *before* the shared lock is taken.

        Called in reverse ensure order. Must not raise: a failure here must not stop the
        runner reaching ``release()``.
        """
