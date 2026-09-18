# This file has been edited with the assistance of an AI tool.
"""
The ``Sidecar`` base: the interface, plus the container mechanics every sidecar shares.

Locking and the start/stop decision are NOT a sidecar concern: the runner holds one
shared lock around the whole ensure-all / release-all phase and consults a single
``SidecarTracker``. So ``ensure()`` and ``release()`` are pure container mechanics --
they run with the lock already held and must not lock, announce, or decide whether to
stop. Lock-free pre-work such as the image pull goes in ``prepare()``, which the runner
runs *before* taking the lock.

The one thing a sidecar contributes to that decision is its ``container_name``: the
runner refcounts live agents **per container name**.

Everything a sidecar does to Docker that does not depend on *what the sidecar is for* --
is it running, is it healthy, is it on that network, what is its address there, is its
image pulled -- is implemented here rather than left abstract. Both subclasses had grown
their own copies, byte-identical but for the label; one implementation is what keeps the
two containers behaving the same way when only one of them is being worked on.
"""

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

from agent_wrap.constants import PollResult
from agent_wrap.lib.docker_utils import docker_run, image_exists, network_exists

if TYPE_CHECKING:
    from agent_wrap.domain.display.service import DisplayService
    from agent_wrap.domain.sidecars.models import SidecarConfig


class Sidecar(ABC):
    """
    A shared helper container an agent run depends on.

    The config is held twice, deliberately: ``_base_config`` is this class's view of it
    -- the container fields every sidecar has -- while a subclass keeps the same frozen
    object under ``config`` at its own type, for the fields only it knows about. One
    attribute cannot serve both: narrowing a mutable one in a subclass is not a sound
    override, and making the base generic in its config would force every consumer that
    merely holds a ``Sidecar`` to name a type argument it has no business knowing.
    """

    def __init__(self, config: SidecarConfig, display_service: DisplayService) -> None:
        self._base_config = config
        self._display = display_service

    @property
    @abstractmethod
    def _label(self) -> str:
        """
        What this sidecar calls itself in output — the spinner label and message prefix.

        Abstract rather than a config field because it is not always a constant: the
        LiteLLM sidecar qualifies its label with the provider, since two of them may be
        up at once.
        """

    @property
    def container_name(self) -> str:
        """
        The Docker container this sidecar manages — also its refcount identity.

        Sidecars with different container names are released independently; those sharing
        one -- the single Telegram container -- share a refcount. Docker names match
        ``[a-zA-Z0-9][a-zA-Z0-9_.-]*``, so this is safe as a path component.
        """
        return self._base_config.container_name

    @property
    def network_name(self) -> str:
        return self._base_config.network_name

    @property
    def image(self) -> str:
        return self._base_config.image

    @property
    def health_timeout_sec(self) -> int:
        return self._base_config.health_timeout_sec

    @property
    def cold_start_time(self) -> float:
        """Seconds a cold start takes (the lock winner pays this once)."""
        return self._base_config.cold_start_time

    @property
    def short_circuit_time(self) -> float:
        """Seconds one agent takes to walk the lock on the hot path (sidecar up)."""
        return self._base_config.short_circuit_time

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

        Default no-op. Both subclasses override it to pull the image: a cold pull under
        the lock would block the whole launch herd.
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

    def _warn(self, message: str) -> None:
        self._display.warning(f"{self._label}: {message}")

    def _fatal(self, message: str) -> SystemExit:
        """
        Report *message* as this sidecar's failure and return the exception to raise.

        Returned rather than raised so every call site reads ``raise self._fatal(...)``:
        a helper annotated ``NoReturn`` terminates the function as far as a type checker
        is concerned but not as far as a reader -- or ruff -- can see, which is how
        ``_recover_port`` ended up looking like it could fall off the end.
        """
        self._display.error(f"{self._label}: {message}")
        return SystemExit(1)

    def _is_running(self) -> bool:
        stdout, rc = docker_run(
            "container",
            "inspect",
            "-f",
            "{{.State.Running}}",
            self.container_name,
        )
        return rc == 0 and stdout.strip() == "true"

    def _is_on_network(self, network: str) -> bool:
        stdout, rc = docker_run(
            "inspect",
            self.container_name,
            "--format",
            "{{range $k, $_ := .NetworkSettings.Networks}}{{println $k}}{{end}}",
        )
        return rc == 0 and network in stdout.splitlines()

    def _sidecar_ip_on_network(self, network: str) -> str:
        fmt = (
            f'{{{{with index .NetworkSettings.Networks "{network}"}}}}{{{{.IPAddress}}}}{{{{end}}}}'
        )
        stdout, rc = docker_run("inspect", self.container_name, "--format", fmt)
        return stdout.strip() if rc == 0 else ""

    def _ensure_network(self) -> None:
        if network_exists(self.network_name):
            return
        _, rc = docker_run("network", "create", self.network_name)
        if rc != 0:
            msg = f"failed to create docker network {self.network_name}"
            raise self._fatal(msg)

    def _attach_to_network(self, network: str) -> None:
        """
        Connect this container to a network the agent asked for, not one we created.

        A missing network is the caller's mistake rather than something to create: the
        name came from agent-run-args, and creating an empty network of that name would
        leave the agent talking to nothing.
        """
        if not network_exists(network):
            msg = f"network '{network}' (from agent-run-args) does not exist"
            raise self._fatal(msg)
        if self._is_on_network(network):
            return
        _, rc = docker_run("network", "connect", network, self.container_name)
        if rc != 0:
            msg = f"failed to attach {self.container_name} to network '{network}'"
            raise self._fatal(msg)

    def _ensure_image(self) -> None:
        if image_exists(self.image):
            return
        self._warn(f"pulling {self.image} (first run, may take a few minutes)…")
        _, rc = docker_run(
            "pull", self.image, capture=False, timeout=self._base_config.pull_timeout_sec
        )
        if rc != 0:
            msg = f"failed to pull image {self.image}"
            raise self._fatal(msg)

    def _health_poll(self) -> bool:
        """
        Wait for docker to call the container healthy, or for it to die trying.

        A vanished container reads as FAILURE with no status: an `inspect` that cannot
        find it is not a pending health check. So does a *running* container docker calls
        unhealthy, and so does one that stopped while we were waiting -- which is why
        ``_is_running`` is consulted before reporting PENDING.
        """

        def poll() -> tuple[PollResult, str]:
            stdout, rc = docker_run(
                "inspect",
                self.container_name,
                "--format={{.State.Health.Status}}",
            )
            if rc != 0:
                return PollResult.FAILURE, ""
            status = stdout.strip()
            if status == "healthy":
                return PollResult.SUCCESS, status
            if status == "unhealthy" or not self._is_running():
                return PollResult.FAILURE, status
            return PollResult.PENDING, status

        return self._display.poll_until(
            label=self._label,
            poll=poll,
            message="waiting for healthy",
            done_message="ready",
            timeout=self.health_timeout_sec,
        )

    def _reap_stale_container(self) -> None:
        """
        Remove a same-named container left behind by an earlier run, before starting.

        Only reached on the cold path, where ``_is_running`` has already said nothing of
        this name is up -- so anything `inspect` still finds is a corpse, and
        ``docker run`` would refuse the name until it is gone.
        """
        _, rc = docker_run("container", "inspect", self.container_name)
        if rc == 0:
            docker_run("rm", "-f", self.container_name)
