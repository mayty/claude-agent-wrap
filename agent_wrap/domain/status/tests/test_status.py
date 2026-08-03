# This file has been created with the assistance of an AI tool.
"""Tests for the status domain service (the body of `agent inspect`)."""

from __future__ import annotations

import dataclasses
import json
from typing import TYPE_CHECKING

import pytest

from agent_wrap.domain.config.service import ConfigService
from agent_wrap.domain.logs.models import ViewerState
from agent_wrap.domain.logs.service import LogsService
from agent_wrap.domain.providers.service import ProviderService
from agent_wrap.domain.secrets.service import SecretsService
from agent_wrap.domain.sidecars.models import AgentContainer, RegistryState, SidecarContainer
from agent_wrap.domain.sidecars.service import SidecarService
from agent_wrap.domain.status.service import InspectService
from agent_wrap.domain.updates.models import WrapperRevision
from agent_wrap.domain.updates.service import UpdateService

if TYPE_CHECKING:
    from pathlib import Path
    from unittest.mock import Mock

    import pytest_mock

_DOCKER_PROBE = "agent_wrap.domain.status.service.docker_utils.daemon_reachable"
_IMAGE_EXISTS = "agent_wrap.domain.status.service.docker_utils.image_exists"
_IS_WSL = "agent_wrap.domain.status.service.docker_utils.is_wsl"
_DOCKER_RUN = "agent_wrap.domain.status.service.docker_utils.docker_run"
_DIR_SIZE = "agent_wrap.domain.status.service.directory_size"

_SIDECAR = SidecarContainer(
    name="agent-wrap-litellm-bedrock",
    role="litellm",
    provider="litellm-bedrock",
    status="running",
    health="healthy",
    uptime_sec=11520,
    port=48620,
    exit_code=0,
    image="ghcr.io/berriai/litellm:pinned",
    stale_image=False,
    networks=["agent-wrap-net"],
)

_AGENT = AgentContainer(
    name="claude-agent-wrap-abc",
    instance_id="wrap-abc",
    status="running",
    uptime_sec=1320,
    cwd="/home/me/agent-wrap",
    image="claude-agent-wrap",
    provider="litellm-bedrock",
    sidecars=["agent-wrap-litellm-bedrock"],
)


@pytest.fixture
def docker_probes(mocker: pytest_mock.MockFixture) -> dict[str, Mock]:
    """
    Patch every host probe to a reachable-and-present default.

    Returned as a dict so a test can retarget one probe by assigning its
    ``return_value``; re-patching an already-patched target with ``autospec=True``
    is rejected by ``mock`` (it cannot spec a Mock).
    """
    return {
        "reachable": mocker.patch(_DOCKER_PROBE, autospec=True, return_value=True),
        "image_exists": mocker.patch(_IMAGE_EXISTS, autospec=True, return_value=True),
        "is_wsl": mocker.patch(_IS_WSL, autospec=True, return_value=False),
        "docker_run": mocker.patch(_DOCKER_RUN, autospec=True, return_value=("", 0)),
        "dir_size": mocker.patch(_DIR_SIZE, autospec=True, return_value=1024),
    }


@pytest.fixture
def sidecar_mock(mocker: pytest_mock.MockFixture) -> Mock:
    mock = mocker.create_autospec(SidecarService, instance=True)
    mock.registry_state.return_value = RegistryState(
        by_container={"agent-wrap-litellm-bedrock": ["wrap-abc"]}, waiting=[]
    )
    mock.list_sidecar_containers.return_value = [_SIDECAR]
    mock.list_agent_containers.return_value = [_AGENT]
    return mock


@pytest.fixture
def provider_mock(mocker: pytest_mock.MockFixture) -> Mock:
    mock = mocker.create_autospec(ProviderService, instance=True)
    mock.get_provider.return_value.name = "litellm-bedrock"
    return mock


@pytest.fixture
def secrets_mock(mocker: pytest_mock.MockFixture) -> Mock:
    mock = mocker.create_autospec(SecretsService, instance=True)
    missing: dict[str, list[str]] = {
        "litellm-bedrock": [],
        "telegram": ["telegram:TelegramBotToken"],
    }
    mock.missing_keys_by_sidecar.return_value = missing
    return mock


@pytest.fixture
def logs_mock(mocker: pytest_mock.MockFixture) -> Mock:
    mock = mocker.create_autospec(LogsService, instance=True)
    mock.viewer_state.return_value = ViewerState(
        running=True, pid=41233, port=8765, log_size=42_000, log_mtime=1_700_000_000.0
    )
    mock.connect_line.return_value = "LiteLLM log viewer running at http://127.0.0.1:8765"
    return mock


@pytest.fixture
def updates_mock(mocker: pytest_mock.MockFixture) -> Mock:
    mock = mocker.create_autospec(UpdateService, instance=True)
    mock.current_revision.return_value = WrapperRevision(
        branch="master", commit="7e8ef2f", describe="0.8.0", dirty=False
    )
    return mock


@pytest.fixture
def config_mock(mocker: pytest_mock.MockFixture, tmp_path: Path) -> Mock:
    mock = mocker.create_autospec(ConfigService, instance=True)
    mock.read_project_paths.return_value = [tmp_path / "a", tmp_path / "b"]
    mock.stale_project_paths.return_value = [tmp_path / "b"]
    return mock


@pytest.fixture
def service(  # noqa: PLR0913
    sidecar_mock: Mock,
    provider_mock: Mock,
    secrets_mock: Mock,
    logs_mock: Mock,
    updates_mock: Mock,
    config_mock: Mock,
    docker_probes: dict[str, Mock],
) -> InspectService:
    del docker_probes  # patches must be active for every test using this service
    return InspectService(
        sidecar_service=sidecar_mock,
        provider_service=provider_mock,
        secrets_service=secrets_mock,
        logs_service=logs_mock,
        updates_service=updates_mock,
        config_service=config_mock,
    )


def test_report_includes_sidecar_row(service: InspectService) -> None:
    row = service.build_report().sidecars[0]
    assert row.name == "agent-wrap-litellm-bedrock"
    assert row.port == 48620
    assert row.health == "healthy"


def test_sidecar_row_counts_attached_agents(service: InspectService) -> None:
    assert service.build_report().sidecars[0].attached_agents == 1


def test_sidecar_with_no_registrations_reports_zero_agents(
    service: InspectService, sidecar_mock: Mock
) -> None:
    """Zero attached agents is a legitimate transient state, not an orphan."""
    sidecar_mock.registry_state.return_value = RegistryState(by_container={}, waiting=[])
    row = service.build_report().sidecars[0]
    assert row.attached_agents == 0
    assert row.status == "running"


def test_report_includes_agent_row(service: InspectService) -> None:
    row = service.build_report().agents[0]
    assert row.instance_id == "wrap-abc"
    assert row.cwd == "/home/me/agent-wrap"
    assert row.sidecars == ["agent-wrap-litellm-bedrock"]


def test_agent_row_carries_image_and_provider(service: InspectService) -> None:
    row = service.build_report().agents[0]
    assert row.image == "claude-agent-wrap"
    assert row.provider == "litellm-bedrock"


def test_queued_launches_come_from_the_registry(
    service: InspectService, sidecar_mock: Mock
) -> None:
    sidecar_mock.registry_state.return_value = RegistryState(by_container={}, waiting=["other-xyz"])
    assert service.build_report().queued_launches == ["other-xyz"]


def test_docker_unavailable_empties_container_lists(
    service: InspectService, docker_probes: dict[str, Mock]
) -> None:
    docker_probes["reachable"].return_value = False
    report = service.build_report()
    assert report.docker.available is False
    assert report.docker.error
    assert report.sidecars == []
    assert report.agents == []


def test_docker_unavailable_keeps_filesystem_sections(
    service: InspectService, docker_probes: dict[str, Mock]
) -> None:
    """The sections that do not need docker are the whole point of degrading."""
    docker_probes["reachable"].return_value = False
    report = service.build_report()
    assert report.viewer.running is True
    assert report.wrapper.commit == "7e8ef2f"
    assert report.storage.projects_registered == 2
    assert report.providers


def test_docker_unavailable_skips_container_discovery(
    service: InspectService, sidecar_mock: Mock, docker_probes: dict[str, Mock]
) -> None:
    docker_probes["reachable"].return_value = False
    service.build_report()
    sidecar_mock.list_sidecar_containers.assert_not_called()
    sidecar_mock.list_agent_containers.assert_not_called()


def test_viewer_row_takes_connect_line_verbatim(service: InspectService) -> None:
    assert service.build_report().viewer.connect_line.endswith("http://127.0.0.1:8765")


def test_viewer_row_has_no_connect_line_when_down(service: InspectService, logs_mock: Mock) -> None:
    logs_mock.viewer_state.return_value = ViewerState(
        running=False, pid=None, port=None, log_size=None, log_mtime=None
    )
    assert service.build_report().viewer.connect_line == ""


def test_report_uses_read_only_viewer_probe(service: InspectService, logs_mock: Mock) -> None:
    """running_server() unlinks a stale state file, so it must not be used here."""
    service.build_report()
    logs_mock.viewer_state.assert_called_once_with()
    logs_mock.running_server.assert_not_called()


def test_report_uses_read_only_secrets_probe(service: InspectService, secrets_mock: Mock) -> None:
    """check_secrets() runs the legacy-keyfile migration, so it must not be used here."""
    service.build_report()
    secrets_mock.missing_keys_by_sidecar.assert_called_once_with()
    secrets_mock.check_secrets.assert_not_called()
    secrets_mock.read.assert_not_called()


def test_report_never_fetches_for_the_revision(service: InspectService, updates_mock: Mock) -> None:
    service.build_report()
    updates_mock.current_revision.assert_called_once_with()
    updates_mock.check_updates.assert_not_called()


def test_provider_row_flags_the_default(service: InspectService) -> None:
    rows = {row.name: row for row in service.build_report().providers}
    assert rows["litellm-bedrock"].is_default is True
    assert rows["telegram"].is_default is False


def test_provider_row_reports_missing_secrets(service: InspectService) -> None:
    rows = {row.name: row for row in service.build_report().providers}
    assert rows["litellm-bedrock"].secrets_ok is True
    assert rows["telegram"].secrets_ok is False
    assert rows["telegram"].missing_keys == ["telegram:TelegramBotToken"]


def test_unresolvable_default_provider_does_not_abort(
    service: InspectService, provider_mock: Mock
) -> None:
    provider_mock.get_provider.side_effect = RuntimeError("bad AGENT_PROVIDER")
    report = service.build_report()
    assert [row.is_default for row in report.providers] == [False, False]


def test_host_network_requested_but_ignored_off_wsl(
    service: InspectService, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AGENT_USE_HOST_NETWORK", "1")
    env = service.build_report().environment
    assert env.host_network_requested is True
    assert env.host_network_effective is False


def test_host_network_effective_on_wsl(
    service: InspectService, monkeypatch: pytest.MonkeyPatch, docker_probes: dict[str, Mock]
) -> None:
    monkeypatch.setenv("AGENT_USE_HOST_NETWORK", "1")
    docker_probes["is_wsl"].return_value = True
    assert service.build_report().environment.host_network_effective is True


def test_day_start_override_is_reported(
    service: InspectService, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AGENT_DAY_START_UTC", "-3")
    assert service.build_report().environment.day_start_overridden is True


def test_storage_row_counts_registry_entries(service: InspectService) -> None:
    storage = service.build_report().storage
    assert storage.projects_registered == 2
    assert storage.projects_stale == 1
    assert storage.logs_bytes == 1024


def test_report_is_json_serialisable(service: InspectService) -> None:
    """Guards against a stray Path or datetime leaking into any model."""
    payload = json.dumps(dataclasses.asdict(service.build_report()))
    assert json.loads(payload)["sidecars"][0]["port"] == 48620


def test_report_json_carries_no_secret_values(service: InspectService, logs_mock: Mock) -> None:
    """Key names are fine (agent secrets check prints those); values never are."""
    del logs_mock  # only present so the fixture chain is explicit
    payload = json.dumps(dataclasses.asdict(service.build_report()))
    for forbidden in ("LITELLM_MASTER_KEY", "AWS_BEARER_TOKEN", "TELEGRAM_BOT_TOKEN", "sk-"):
        assert forbidden not in payload
