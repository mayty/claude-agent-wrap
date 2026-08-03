# This file has been created with the assistance of an AI tool.
"""Tests for SidecarService's container-discovery methods."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

import pytest

from agent_wrap.domain.sidecars.service import SidecarService

if TYPE_CHECKING:
    from pathlib import Path

    import pytest_mock

    from agent_wrap.domain.display.service import DisplayService

_LIST = "agent_wrap.domain.sidecars.service.list_container_names"
_INSPECT = "agent_wrap.domain.sidecars.service.inspect_containers"

_ENV = json.dumps(["AGENT_WRAP_SIDECAR_PORT=48620", "AGENT_WRAP_PROVIDER=litellm-bedrock"])
_NETWORKS = json.dumps({"agent-wrap-net": {}})


def _started_now() -> str:
    return (
        (datetime.now(tz=timezone.utc) - timedelta(seconds=60)).isoformat().replace("+00:00", "Z")
    )


def _sidecar_line(name: str) -> str:
    return "\t".join(
        [f"/{name}", "running", "healthy", _started_now(), "0", "img", _ENV, "{}", _NETWORKS]
    )


def _agent_line(name: str, image: str = "claude-agent-wrap", cwd: str = "/home/me/thing") -> str:
    mounts = json.dumps([{"Destination": "/workspace", "Source": cwd}])
    labels = json.dumps({"agent-wrap.instance-id": name.removeprefix("claude-agent-")})
    return "\t".join([f"/{name}", "running", _started_now(), image, labels, mounts])


@pytest.fixture
def service(display_mock: DisplayService) -> SidecarService:
    return SidecarService(display_service=display_mock)


def test_list_sidecars_filters_by_name_prefix(
    service: SidecarService, mocker: pytest_mock.MockFixture
) -> None:
    """Sidecar containers carry no agent-wrap labels, so the name prefix is the marker."""
    listing = mocker.patch(_LIST, autospec=True, return_value=[])
    mocker.patch(_INSPECT, autospec=True, return_value=([], 0))
    service.list_sidecar_containers()
    listing.assert_called_once_with("name=^agent-wrap-")


def test_list_sidecars_parses_and_sorts_rows(
    service: SidecarService, mocker: pytest_mock.MockFixture
) -> None:
    mocker.patch(_LIST, autospec=True, return_value=["b", "a"])
    mocker.patch(
        _INSPECT,
        autospec=True,
        return_value=([_sidecar_line("agent-wrap-b"), _sidecar_line("agent-wrap-a")], 0),
    )
    assert [row.name for row in service.list_sidecar_containers()] == [
        "agent-wrap-a",
        "agent-wrap-b",
    ]


def test_list_sidecars_keeps_rows_despite_nonzero_rc(
    service: SidecarService, mocker: pytest_mock.MockFixture
) -> None:
    """One container vanishing mid-inspect must not discard the others."""
    mocker.patch(_LIST, autospec=True, return_value=["agent-wrap-a", "gone"])
    mocker.patch(_INSPECT, autospec=True, return_value=([_sidecar_line("agent-wrap-a")], 1))
    assert len(service.list_sidecar_containers()) == 1


def test_list_sidecars_drops_malformed_rows(
    service: SidecarService, mocker: pytest_mock.MockFixture
) -> None:
    mocker.patch(_LIST, autospec=True, return_value=["agent-wrap-a"])
    mocker.patch(_INSPECT, autospec=True, return_value=(["truncated\trow"], 0))
    assert service.list_sidecar_containers() == []


def test_list_agents_filters_by_role_label(
    service: SidecarService, mocker: pytest_mock.MockFixture, tmp_path: Path
) -> None:
    listing = mocker.patch(_LIST, autospec=True, return_value=[])
    mocker.patch(_INSPECT, autospec=True, return_value=([], 0))
    service.list_agent_containers(tmp_path)
    listing.assert_called_once_with("label=agent-wrap.role=claude-agent")


def test_list_agents_annotates_sidecars_from_the_registry(
    service: SidecarService, mocker: pytest_mock.MockFixture, tmp_path: Path
) -> None:
    """No docker label links an agent to its sidecars — the flock registry does."""
    for container in ("agent-wrap-telegram", "agent-wrap-litellm-bedrock"):
        registration = tmp_path / ".agent-launches" / "running" / container / "wrap-abc"
        registration.parent.mkdir(parents=True, exist_ok=True)
        registration.touch()

    registry = mocker.patch.object(service, "registry_state", autospec=True)
    registry.return_value = mocker.Mock(
        by_container={
            "agent-wrap-telegram": ["wrap-abc"],
            "agent-wrap-litellm-bedrock": ["wrap-abc"],
        },
        waiting=[],
    )
    mocker.patch(_LIST, autospec=True, return_value=["claude-agent-wrap-abc"])
    mocker.patch(_INSPECT, autospec=True, return_value=([_agent_line("claude-agent-wrap-abc")], 0))

    rows = service.list_agent_containers(tmp_path)
    assert rows[0].sidecars == ["agent-wrap-litellm-bedrock", "agent-wrap-telegram"]
    assert rows[0].provider == "litellm-bedrock"


def test_list_agents_sorts_by_image_then_cwd(
    service: SidecarService, mocker: pytest_mock.MockFixture, tmp_path: Path
) -> None:
    """Sorting by container name would order by instance id, i.e. randomly."""
    lines = [
        _agent_line("claude-agent-aaa", image="claude-agent-wrap", cwd="/home/me/b"),
        _agent_line("claude-agent-bbb", image="claude-agent", cwd="/home/me/z"),
        _agent_line("claude-agent-ccc", image="claude-agent-wrap", cwd="/home/me/a"),
    ]
    mocker.patch(_LIST, autospec=True, return_value=["a", "b", "c"])
    mocker.patch(_INSPECT, autospec=True, return_value=(lines, 0))

    rows = service.list_agent_containers(tmp_path)
    assert [(row.image, row.cwd) for row in rows] == [
        ("claude-agent", "/home/me/z"),
        ("claude-agent-wrap", "/home/me/a"),
        ("claude-agent-wrap", "/home/me/b"),
    ]


def test_registry_state_reads_the_tool_dir_given(service: SidecarService, tmp_path: Path) -> None:
    registration = tmp_path / ".agent-launches" / "running" / "agent-wrap-x" / "inst-1"
    registration.parent.mkdir(parents=True, exist_ok=True)
    registration.touch()
    # The file is unlocked (no live owner), so it is reported as attached-to-nobody —
    # and, being a reporting read, it must survive the call.
    state = service.registry_state(tmp_path)
    assert state.by_container == {"agent-wrap-x": []}
    assert registration.exists()
