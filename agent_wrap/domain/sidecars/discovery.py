# This file has been created with the assistance of an AI tool.
"""
Parsing of ``docker container inspect`` output into sidecar/agent report rows.

The templates in ``constants.py`` render one tab-separated line per container. This
module turns one such line into a scalar-only model. It is the **only** place raw
container environment and mount arrays exist, and both are reduced here:

* env is allowlisted to :data:`INSPECTABLE_ENV_KEYS` — every other variable in a
  sidecar's environment is a live credential (``LITELLM_MASTER_KEY``, the upstream
  provider token, ``TELEGRAM_BOT_TOKEN``);
* mounts are reduced to the single ``/workspace`` source, discarding the dozen-odd
  other host paths an agent container binds.

Allowlisting happens here in Python rather than in the Go template because docker's
template functions have no ``hasPrefix``, and splitting an ``VAR=value`` entry on ``=``
breaks for values that themselves contain one.

Every parser is total: a field docker renders differently than expected degrades that
one cell (``None`` / ``""``) instead of raising, because a diagnostic command must
still print the rest of the report.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

from agent_wrap.constants import (
    CONTAINER_NAME_PREFIX,
    LITELLM_IMAGE,
    RUNNING_STATUS,
    SIDECAR_PORT_ENV,
    SIDECAR_PROVIDER_ENV,
    TELEGRAM_IMAGE,
    TELEGRAM_SIDECAR_NAME,
)
from agent_wrap.domain.sidecars.constants import (
    AGENT_INSPECT_TEMPLATE,
    INSPECT_FIELD_SEP,
    INSPECTABLE_ENV_KEYS,
    INSTANCE_ID_LABEL,
    LITELLM_ROLE,
    SIDECAR_INSPECT_TEMPLATE,
    TELEGRAM_ROLE,
    UNKNOWN_ROLE,
    WORKSPACE_MOUNT_DEST,
)
from agent_wrap.domain.sidecars.models import AgentContainer, SidecarContainer
from agent_wrap.lib.docker_utils import parse_docker_timestamp

#: Field counts the two templates render. A line with fewer fields is malformed and
#: skipped; comparing against the template keeps the two from drifting apart.
SIDECAR_FIELD_COUNT = SIDECAR_INSPECT_TEMPLATE.count(INSPECT_FIELD_SEP) + 1
AGENT_FIELD_COUNT = AGENT_INSPECT_TEMPLATE.count(INSPECT_FIELD_SEP) + 1

#: The single Telegram sidecar's container name.
TELEGRAM_CONTAINER_NAME = f"{CONTAINER_NAME_PREFIX}-{TELEGRAM_SIDECAR_NAME}"


class ContainerParsing:
    """Field-level parsers shared by the sidecar and agent row builders."""

    @staticmethod
    def json_object(raw: str) -> dict[str, object]:
        """Parse a ``{{json}}`` field expected to be an object; {} on anything else."""
        try:
            parsed: object = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            return {}
        if not isinstance(parsed, dict):
            return {}
        return {str(key): value for key, value in parsed.items()}

    @staticmethod
    def json_array(raw: str) -> list[object]:
        """Parse a ``{{json}}`` field expected to be an array; [] on anything else."""
        try:
            parsed: object = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            return []
        if not isinstance(parsed, list):
            return []
        return list(parsed)

    @staticmethod
    def container_name(raw: str) -> str:
        """Strip the leading slash docker puts on ``.Name``."""
        return raw.strip().removeprefix("/")

    @staticmethod
    def uptime_sec(started_at: str, status: str) -> int | None:
        """
        Seconds a container has been up, or None when it is not up.

        *status* is required because docker keeps reporting ``StartedAt`` for a stopped
        container — its last start. Measuring from that would present the age of a
        corpse as its uptime, so anything other than ``running`` has no uptime at all.
        """
        if status != RUNNING_STATUS:
            return None
        started = parse_docker_timestamp(started_at)
        if started is None:
            return None
        elapsed = (datetime.now(tz=timezone.utc) - started).total_seconds()
        if elapsed < 0:
            return None
        return int(elapsed)

    @staticmethod
    def exit_code(raw: str) -> int | None:
        """Parse ``.State.ExitCode``; None when docker rendered something unexpected."""
        try:
            return int(raw.strip())
        except ValueError:
            return None

    @staticmethod
    def allowlisted_env(raw: str) -> dict[str, str]:
        """
        Reduce a ``{{json .Config.Env}}`` array to the allowlisted keys only.

        The raw array holds live credentials. It exists as a local here and nowhere
        else; the return value is the widest view of a container's environment any
        caller gets.
        """
        result: dict[str, str] = {}
        for entry in ContainerParsing.json_array(raw):
            if not isinstance(entry, str) or "=" not in entry:
                continue
            key, value = entry.split("=", 1)
            if key in INSPECTABLE_ENV_KEYS:
                result[key] = value
        return result

    @staticmethod
    def published_port(raw: str) -> int | None:
        """
        Lowest container port from ``{{json .NetworkSettings.Ports}}``.

        The fallback for a sidecar that records no port in its environment — the
        Telegram sidecar passes only its token, chat id, and log path, so its port is
        legible solely from the ``-p 127.0.0.1:6837:6837`` publish. Keys look like
        ``"6837/tcp"``; the container-side number is the listening port.
        """
        ports: list[int] = []
        for key in ContainerParsing.json_object(raw):
            candidate = key.split("/", 1)[0]
            if candidate.isdigit():
                ports.append(int(candidate))
        return min(ports) if ports else None

    @staticmethod
    def network_names(raw: str) -> list[str]:
        """Network names from ``{{json .NetworkSettings.Networks}}`` — never the IPs."""
        return sorted(ContainerParsing.json_object(raw))

    @staticmethod
    def workspace_source(raw: str) -> str:
        """
        Host path bound at ``/workspace`` from ``{{json .Mounts}}``, or "".

        An agent container binds a dozen-odd host paths (global config, per-project
        state files, the ops directory); only the project directory is wanted, and the
        rest must not survive this call.
        """
        for entry in ContainerParsing.json_array(raw):
            if not isinstance(entry, dict):
                continue
            if entry.get("Destination") != WORKSPACE_MOUNT_DEST:
                continue
            source = entry.get("Source")
            if isinstance(source, str):
                return source
        return ""


class ContainerRows:
    """Builders turning one inspect line into a report model."""

    @staticmethod
    def sidecar_role(name: str) -> str:
        """Classify a sidecar container by name: Telegram is the one singleton."""
        if name == TELEGRAM_CONTAINER_NAME:
            return TELEGRAM_ROLE
        if name.startswith(f"{CONTAINER_NAME_PREFIX}-"):
            return LITELLM_ROLE
        return UNKNOWN_ROLE

    @staticmethod
    def pinned_image(role: str) -> str:
        """Return the image agent-wrap would start for *role* today."""
        if role == TELEGRAM_ROLE:
            return TELEGRAM_IMAGE
        if role == LITELLM_ROLE:
            return LITELLM_IMAGE
        return ""

    @staticmethod
    def provider_name(role: str, name: str, env: dict[str, str]) -> str:
        """
        Return the provider a LiteLLM sidecar serves; "" for Telegram, which serves none.

        Prefers the recorded env var, falling back to the container name — a sidecar
        started before that var existed still names its provider.
        """
        if role != LITELLM_ROLE:
            return ""
        recorded = env.get(SIDECAR_PROVIDER_ENV, "")
        if recorded:
            return recorded
        return name.removeprefix(f"{CONTAINER_NAME_PREFIX}-")

    @staticmethod
    def sidecar(line: str) -> SidecarContainer | None:
        """Build a :class:`SidecarContainer` from one inspect line; None if malformed."""
        fields = line.split(INSPECT_FIELD_SEP)
        if len(fields) < SIDECAR_FIELD_COUNT:
            return None
        raw_name, status, health, started_at, exit_code, image, env_raw, ports, networks = fields[
            :SIDECAR_FIELD_COUNT
        ]

        name = ContainerParsing.container_name(raw_name)
        role = ContainerRows.sidecar_role(name)
        env = ContainerParsing.allowlisted_env(env_raw)

        recorded_port = env.get(SIDECAR_PORT_ENV, "")
        port = (
            int(recorded_port)
            if recorded_port.isdigit()
            else ContainerParsing.published_port(ports)
        )
        pinned = ContainerRows.pinned_image(role)

        return SidecarContainer(
            name=name,
            role=role,
            provider=ContainerRows.provider_name(role, name, env),
            status=status.strip(),
            health=health.strip(),
            uptime_sec=ContainerParsing.uptime_sec(started_at, status.strip()),
            port=port,
            exit_code=ContainerParsing.exit_code(exit_code),
            image=image.strip(),
            stale_image=bool(pinned) and image.strip() != pinned,
            networks=ContainerParsing.network_names(networks),
        )

    @staticmethod
    def agent_provider(sidecars: list[str]) -> str:
        """
        Name the provider an agent's model traffic goes through, or "" if unknowable.

        Read off the flock registrations rather than the container: an agent's env holds
        the provider's *credentials* but never its name, so the registry is the only
        source. An agent registers on ``agent-wrap-<provider>`` and, with hooks on, also
        on the single ``agent-wrap-telegram``; dropping the latter leaves the provider.

        "" is a real answer, not a failure: a headless run declares its sidecars without
        starting them, and teardown drops registrations before stopping the container.
        """
        for name in sidecars:
            if name != TELEGRAM_CONTAINER_NAME:
                return name.removeprefix(f"{CONTAINER_NAME_PREFIX}-")
        return ""

    @staticmethod
    def agent(line: str, sidecars_by_instance: dict[str, list[str]]) -> AgentContainer | None:
        """Build an :class:`AgentContainer` from one inspect line; None if malformed."""
        fields = line.split(INSPECT_FIELD_SEP)
        if len(fields) < AGENT_FIELD_COUNT:
            return None
        raw_name, status, started_at, image, labels_raw, mounts_raw = fields[:AGENT_FIELD_COUNT]

        labels = ContainerParsing.json_object(labels_raw)
        raw_instance = labels.get(INSTANCE_ID_LABEL)
        instance_id = raw_instance if isinstance(raw_instance, str) else ""
        sidecars = sidecars_by_instance.get(instance_id, [])

        return AgentContainer(
            name=ContainerParsing.container_name(raw_name),
            instance_id=instance_id,
            status=status.strip(),
            uptime_sec=ContainerParsing.uptime_sec(started_at, status.strip()),
            cwd=ContainerParsing.workspace_source(mounts_raw),
            image=image.strip(),
            provider=ContainerRows.agent_provider(sidecars),
            sidecars=sidecars,
        )
