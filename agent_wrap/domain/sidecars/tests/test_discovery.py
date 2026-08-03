# This file has been created with the assistance of an AI tool.
"""Tests for sidecar/agent container discovery parsing."""

from __future__ import annotations

import dataclasses
import json
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

import pytest

from agent_wrap.constants import LITELLM_IMAGE, TELEGRAM_IMAGE
from agent_wrap.domain.sidecars.discovery import ContainerParsing, ContainerRows

if TYPE_CHECKING:
    from agent_wrap.domain.sidecars.models import SidecarContainer

_SEP = "\t"

# A LiteLLM sidecar's real environment: the two inspectable vars plus three secrets.
_LITELLM_ENV = json.dumps(
    [
        "PATH=/usr/bin",
        "AGENT_WRAP_SIDECAR_PORT=48620",
        "AGENT_WRAP_PROVIDER=litellm-bedrock",
        "LITELLM_MASTER_KEY=sk-master-deadbeef",
        "AWS_BEARER_TOKEN_BEDROCK=bedrock-secret-token",
    ]
)

# The Telegram sidecar records neither port nor provider — only its credentials.
_TELEGRAM_ENV = json.dumps(
    [
        "TELEGRAM_BOT_TOKEN=12345:bot-secret-token",
        "TELEGRAM_CHAT_ID=99887766",
        "LOG_LOCATION=/var/log/telegram-sidecar/x.log",
    ]
)

_NETWORKS = json.dumps({"agent-wrap-net": {"IPAddress": "172.18.0.2"}})
_NO_PORTS = json.dumps({})
_TELEGRAM_PORTS = json.dumps({"6837/tcp": [{"HostIp": "127.0.0.1", "HostPort": "6837"}]})

_AGENT_LABELS = json.dumps(
    {"agent-wrap.role": "claude-agent", "agent-wrap.instance-id": "wrap-abc123"}
)
_AGENT_MOUNTS = json.dumps(
    [
        {
            "Destination": "/home/ubuntu/.claude.json",
            "Source": "/opt/wrap/.claude_config/.claude.json",
        },
        {"Destination": "/workspace", "Source": "/home/me/projects/thing"},
        {"Destination": "/opt/agent-wrap", "Source": "/opt/wrap/ops"},
    ]
)


def _recent_timestamp(seconds_ago: int) -> str:
    started = datetime.now(tz=timezone.utc) - timedelta(seconds=seconds_ago)
    return started.isoformat().replace("+00:00", "Z")


def _sidecar_line(  # noqa: PLR0913 -- one parameter per template field, by design
    name: str = "/agent-wrap-litellm-bedrock",
    status: str = "running",
    health: str = "healthy",
    started_at: str = "",
    exit_code: str = "0",
    image: str = LITELLM_IMAGE,
    env: str = _LITELLM_ENV,
    ports: str = _NO_PORTS,
    networks: str = _NETWORKS,
) -> str:
    started = started_at or _recent_timestamp(11520)
    return _SEP.join([name, status, health, started, exit_code, image, env, ports, networks])


def _agent_line(  # noqa: PLR0913 -- one parameter per template field, by design
    name: str = "/claude-agent-wrap-abc123",
    status: str = "running",
    started_at: str = "",
    image: str = "claude-agent-wrap",
    labels: str = _AGENT_LABELS,
    mounts: str = _AGENT_MOUNTS,
) -> str:
    started = started_at or _recent_timestamp(1320)
    return _SEP.join([name, status, started, image, labels, mounts])


def _require(row: SidecarContainer | None) -> SidecarContainer:
    assert row is not None
    return row


# --- litellm sidecar rows ---


def test_sidecar_recovers_port_and_provider_from_env() -> None:
    row = _require(ContainerRows.sidecar(_sidecar_line()))
    assert row.port == 48620
    assert row.provider == "litellm-bedrock"
    assert row.role == "litellm"


def test_sidecar_strips_leading_slash_from_name() -> None:
    assert _require(ContainerRows.sidecar(_sidecar_line())).name == "agent-wrap-litellm-bedrock"


def test_sidecar_uptime_from_started_at() -> None:
    row = _require(ContainerRows.sidecar(_sidecar_line(started_at=_recent_timestamp(3600))))
    assert row.uptime_sec is not None
    assert 3595 <= row.uptime_sec <= 3605


def test_sidecar_provider_falls_back_to_container_name() -> None:
    """A sidecar started before AGENT_WRAP_PROVIDER existed still names its provider."""
    env = json.dumps(["AGENT_WRAP_SIDECAR_PORT=48620"])
    row = _require(ContainerRows.sidecar(_sidecar_line(env=env)))
    assert row.provider == "litellm-bedrock"


def test_sidecar_reports_networks_without_ips() -> None:
    row = _require(ContainerRows.sidecar(_sidecar_line()))
    assert row.networks == ["agent-wrap-net"]


def test_sidecar_flags_stale_image() -> None:
    row = _require(ContainerRows.sidecar(_sidecar_line(image="ghcr.io/berriai/litellm:ancient")))
    assert row.stale_image is True


def test_sidecar_pinned_image_is_not_stale() -> None:
    assert _require(ContainerRows.sidecar(_sidecar_line())).stale_image is False


# --- telegram sidecar rows ---


def test_telegram_port_falls_back_to_port_binding() -> None:
    """The Telegram sidecar records no port in env — only the -p publish reveals it."""
    row = _require(
        ContainerRows.sidecar(
            _sidecar_line(
                name="/agent-wrap-telegram",
                health="none",
                image=TELEGRAM_IMAGE,
                env=_TELEGRAM_ENV,
                ports=_TELEGRAM_PORTS,
            )
        )
    )
    assert row.port == 6837
    assert row.role == "telegram"
    assert row.provider == ""


def test_telegram_no_healthcheck_reports_none() -> None:
    row = _require(
        ContainerRows.sidecar(
            _sidecar_line(name="/agent-wrap-telegram", health="none", env=_TELEGRAM_ENV)
        )
    )
    assert row.health == "none"


def test_exited_container_has_no_uptime() -> None:
    """Docker still reports StartedAt for a stopped container — its last start."""
    row = _require(
        ContainerRows.sidecar(
            _sidecar_line(status="exited", exit_code="137", started_at=_recent_timestamp(9000))
        )
    )
    assert row.status == "exited"
    assert row.exit_code == 137
    assert row.uptime_sec is None


def test_never_started_container_has_no_uptime() -> None:
    row = _require(
        ContainerRows.sidecar(_sidecar_line(status="created", started_at="0001-01-01T00:00:00Z"))
    )
    assert row.uptime_sec is None


def test_exited_agent_has_no_uptime() -> None:
    row = ContainerRows.agent(_agent_line(status="exited", started_at=_recent_timestamp(9000)), {})
    assert row is not None
    assert row.uptime_sec is None


# --- redaction ---


def test_sidecar_row_carries_no_secret_values() -> None:
    """The env array holds live credentials; none may survive into the model."""
    row = _require(ContainerRows.sidecar(_sidecar_line()))
    payload = json.dumps(dataclasses.asdict(row))
    for secret in ("sk-master-deadbeef", "bedrock-secret-token", "LITELLM_MASTER_KEY"):
        assert secret not in payload


def test_telegram_row_carries_no_bot_token() -> None:
    row = _require(
        ContainerRows.sidecar(_sidecar_line(name="/agent-wrap-telegram", env=_TELEGRAM_ENV))
    )
    payload = json.dumps(dataclasses.asdict(row))
    assert "bot-secret-token" not in payload
    assert "TELEGRAM_BOT_TOKEN" not in payload


def test_allowlisted_env_drops_everything_else() -> None:
    env = ContainerParsing.allowlisted_env(_LITELLM_ENV)
    assert set(env) == {"AGENT_WRAP_SIDECAR_PORT", "AGENT_WRAP_PROVIDER"}


def test_allowlisted_env_keeps_values_containing_equals() -> None:
    """Splitting on the first '=' only, so a base64-ish value survives intact."""
    env = ContainerParsing.allowlisted_env(json.dumps(["AGENT_WRAP_PROVIDER=a=b=c"]))
    assert env["AGENT_WRAP_PROVIDER"] == "a=b=c"


# --- agent rows ---


def test_agent_row_reads_cwd_from_workspace_mount() -> None:
    row = ContainerRows.agent(_agent_line(), {})
    assert row is not None
    assert row.cwd == "/home/me/projects/thing"


def test_agent_row_retains_no_other_mount_path() -> None:
    row = ContainerRows.agent(_agent_line(), {})
    assert row is not None
    payload = json.dumps(dataclasses.asdict(row))
    assert "/opt/wrap/ops" not in payload
    assert ".claude_config" not in payload


def test_agent_row_reads_instance_id_from_label() -> None:
    row = ContainerRows.agent(_agent_line(), {})
    assert row is not None
    assert row.instance_id == "wrap-abc123"


def test_agent_row_annotated_with_its_sidecars() -> None:
    row = ContainerRows.agent(
        _agent_line(), {"wrap-abc123": ["agent-wrap-litellm-bedrock", "agent-wrap-telegram"]}
    )
    assert row is not None
    assert row.sidecars == ["agent-wrap-litellm-bedrock", "agent-wrap-telegram"]


def test_agent_row_without_registry_entry_lists_no_sidecars() -> None:
    row = ContainerRows.agent(_agent_line(), {})
    assert row is not None
    assert row.sidecars == []


def test_agent_row_carries_its_image() -> None:
    row = ContainerRows.agent(_agent_line(image="claude-agent"), {})
    assert row is not None
    assert row.image == "claude-agent"


def test_agent_provider_comes_from_the_litellm_registration() -> None:
    """The agent's own env never names its provider — only the registry does."""
    row = ContainerRows.agent(
        _agent_line(), {"wrap-abc123": ["agent-wrap-litellm-bedrock", "agent-wrap-telegram"]}
    )
    assert row is not None
    assert row.provider == "litellm-bedrock"


def test_agent_provider_blank_when_only_telegram_is_registered() -> None:
    """Telegram serves no models, so a Telegram-only agent has no provider to report."""
    row = ContainerRows.agent(_agent_line(), {"wrap-abc123": ["agent-wrap-telegram"]})
    assert row is not None
    assert row.provider == ""


def test_agent_provider_blank_without_any_registration() -> None:
    row = ContainerRows.agent(_agent_line(), {})
    assert row is not None
    assert row.provider == ""


def test_agent_row_missing_workspace_mount_is_blank() -> None:
    row = ContainerRows.agent(_agent_line(mounts=json.dumps([])), {})
    assert row is not None
    assert row.cwd == ""


# --- malformed input ---


@pytest.mark.parametrize("line", ["", "only-one-field", "a\tb\tc"])
def test_malformed_sidecar_line_is_skipped(line: str) -> None:
    assert ContainerRows.sidecar(line) is None


@pytest.mark.parametrize("line", ["", "a\tb"])
def test_malformed_agent_line_is_skipped(line: str) -> None:
    assert ContainerRows.agent(line, {}) is None


def test_unparseable_json_field_degrades_that_cell_only() -> None:
    row = _require(ContainerRows.sidecar(_sidecar_line(env="not json", networks="{{broken")))
    assert row.name == "agent-wrap-litellm-bedrock"
    assert row.port is None
    assert row.networks == []


def test_unparseable_exit_code_is_none() -> None:
    assert _require(ContainerRows.sidecar(_sidecar_line(exit_code="n/a"))).exit_code is None


def test_future_start_time_yields_no_uptime() -> None:
    row = _require(ContainerRows.sidecar(_sidecar_line(started_at=_recent_timestamp(-600))))
    assert row.uptime_sec is None


def test_unrecognised_container_name_has_unknown_role() -> None:
    row = _require(ContainerRows.sidecar(_sidecar_line(name="/something-else")))
    assert row.role == "unknown"
    assert row.stale_image is False
