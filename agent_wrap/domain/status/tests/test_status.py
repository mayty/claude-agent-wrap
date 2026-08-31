# This file has been created with the assistance of an AI tool.
"""Tests for the status domain service (the body of `agent inspect`)."""

import dataclasses
import json
import threading
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from agent_wrap.constants import AUTOSTART_LOGS_ENV, BASE_IMAGE_NAME
from agent_wrap.domain.build.models import ResolvedImage
from agent_wrap.domain.build.service import BuildService
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
    from unittest.mock import Mock

    import pytest_mock

_DOCKER_PROBE = "agent_wrap.domain.status.service.docker_utils.daemon_reachable"
_IMAGE_EXISTS = "agent_wrap.domain.status.service.docker_utils.image_exists"
_IMAGE_CLAUDE_VERSION = "agent_wrap.domain.status.service.docker_utils.image_claude_version"
_LATEST_CLAUDE_VERSION = "agent_wrap.domain.status.service.docker_utils.latest_claude_version"
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


#: What `resolve_image()` returns in a project that customizes its image. `agent_name`
#: being set is the "this is a project Dockerfile" predicate the service keys on.
_PROJECT_IMAGE = ResolvedImage(
    image="claude-agent-proj",
    dockerfile=Path("/proj/.claude-agent-wrap/Dockerfile"),
    context=Path("/proj"),
    agent_name="proj",
)

#: What it returns in a project that does not — the base image, with no agent_name.
_BASE_IMAGE_ONLY = ResolvedImage(
    image=BASE_IMAGE_NAME,
    dockerfile=Path("/opt/agent-wrap/ops/Dockerfile"),
    context=Path("/opt/agent-wrap"),
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
        "image_claude_version": mocker.patch(
            _IMAGE_CLAUDE_VERSION, autospec=True, return_value="2.0.50"
        ),
        "latest_claude_version": mocker.patch(
            _LATEST_CLAUDE_VERSION, autospec=True, return_value=None
        ),
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
    # Seeded explicitly: autospec would leave this a truthy Mock, so the default-on case
    # would pass even if the flag were never read.
    mock.get_provider.return_value.autostart_logs_viewer = True
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
        running=True,
        pid=41233,
        port=8765,
        starting=False,
        log_size=42_000,
        log_mtime=1_700_000_000.0,
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
def build_mock(mocker: pytest_mock.MockFixture) -> Mock:
    mock = mocker.create_autospec(BuildService, instance=True)
    mock.resolve_image.return_value = _PROJECT_IMAGE
    return mock


@pytest.fixture
def service(  # noqa: PLR0913
    sidecar_mock: Mock,
    provider_mock: Mock,
    secrets_mock: Mock,
    logs_mock: Mock,
    updates_mock: Mock,
    config_mock: Mock,
    build_mock: Mock,
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
        build_service=build_mock,
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
        running=False, pid=None, port=None, starting=False, log_size=None, log_mtime=None
    )
    assert service.build_report().viewer.connect_line == ""


def test_viewer_row_reports_starting_without_a_connect_line(
    service: InspectService, logs_mock: Mock
) -> None:
    """A starting viewer's recorded port is provisional, so there is nothing to connect to."""
    logs_mock.viewer_state.return_value = ViewerState(
        running=True,
        pid=41233,
        port=8765,
        starting=True,
        log_size=None,
        log_mtime=None,
    )
    viewer = service.build_report().viewer
    assert (viewer.running, viewer.starting, viewer.connect_line) == (True, True, "")


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


def test_autostart_logs_is_on_when_nothing_opts_out(
    service: InspectService, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(AUTOSTART_LOGS_ENV, raising=False)
    autostart = service.build_report().logs_autostart
    assert (autostart.requested, autostart.effective) == (None, True)
    assert autostart.declining_provider == ""


def test_autostart_logs_treats_an_empty_value_as_unset(
    service: InspectService, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The opposite polarity to host networking: unset and empty both mean on."""
    monkeypatch.setenv(AUTOSTART_LOGS_ENV, "")
    autostart = service.build_report().logs_autostart
    assert (autostart.requested, autostart.effective) == (None, True)


@pytest.mark.parametrize("value", ["0", "false", "no"])
def test_autostart_logs_off_by_env(
    service: InspectService, monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    monkeypatch.setenv(AUTOSTART_LOGS_ENV, value)
    autostart = service.build_report().logs_autostart
    assert (autostart.requested, autostart.effective) == (False, False)
    assert autostart.declining_provider == ""


def test_autostart_logs_off_because_the_provider_declines(
    service: InspectService, provider_mock: Mock, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(AUTOSTART_LOGS_ENV, raising=False)
    provider_mock.get_provider.return_value.autostart_logs_viewer = False
    provider_mock.get_provider.return_value.name = "litellm-anthropic-sub"
    autostart = service.build_report().logs_autostart
    assert (autostart.requested, autostart.effective) == (None, False)
    assert autostart.declining_provider == "litellm-anthropic-sub"


def test_autostart_logs_requested_but_the_provider_declines(
    service: InspectService, provider_mock: Mock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Set-and-ignored is the case a plain on/off row would misreport."""
    monkeypatch.setenv(AUTOSTART_LOGS_ENV, "1")
    provider_mock.get_provider.return_value.autostart_logs_viewer = False
    provider_mock.get_provider.return_value.name = "litellm-anthropic-sub"
    autostart = service.build_report().logs_autostart
    assert (autostart.requested, autostart.effective) == (True, False)
    assert autostart.declining_provider == "litellm-anthropic-sub"


def test_autostart_logs_falls_back_to_the_env_when_the_provider_is_unresolvable(
    service: InspectService, provider_mock: Mock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A provider that cannot be resolved gates nothing, and must not abort the report."""
    monkeypatch.delenv(AUTOSTART_LOGS_ENV, raising=False)
    provider_mock.get_provider.side_effect = RuntimeError("bad AGENT_PROVIDER")
    autostart = service.build_report().logs_autostart
    assert (autostart.effective, autostart.declining_provider) == (True, "")


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


def test_day_start_timezone_is_reported_when_unshadowed(
    service: InspectService, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("AGENT_DAY_START_UTC", raising=False)
    monkeypatch.setenv("AGENT_TIMEZONE", "Europe/Warsaw")
    env = service.build_report().environment
    assert env.day_start_timezone == "Europe/Warsaw"
    assert env.day_start_overridden is False


def test_day_start_timezone_is_suppressed_when_day_start_utc_also_set(
    service: InspectService, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AGENT_DAY_START_UTC", "-3")
    monkeypatch.setenv("AGENT_TIMEZONE", "Europe/Warsaw")
    env = service.build_report().environment
    assert env.day_start_timezone is None
    assert env.day_start_overridden is True


def test_storage_row_counts_registry_entries(service: InspectService) -> None:
    storage = service.build_report().storage
    assert storage.projects_registered == 2
    assert storage.projects_stale == 1
    assert storage.logs_bytes == 1024


def test_report_is_json_serialisable(service: InspectService) -> None:
    """Guards against a stray Path or datetime leaking into any model."""
    payload = json.dumps(dataclasses.asdict(service.build_report()))
    assert json.loads(payload)["sidecars"][0]["port"] == 48620
    assert json.loads(payload)["environment"]["base_image_version"] == "2.0.50"


def test_environment_row_includes_base_image_version(
    service: InspectService, docker_probes: dict[str, Mock]
) -> None:
    docker_probes["image_claude_version"].return_value = "2.0.50"
    env = service.build_report().environment
    assert env.base_image_present is True
    assert env.base_image_version == "2.0.50"


def test_environment_row_skips_version_when_image_absent(
    service: InspectService, docker_probes: dict[str, Mock]
) -> None:
    docker_probes["image_exists"].return_value = False
    env = service.build_report().environment
    assert env.base_image_present is False
    assert env.base_image_version is None
    docker_probes["image_claude_version"].assert_not_called()


def test_environment_row_version_none_on_failure(
    service: InspectService, docker_probes: dict[str, Mock]
) -> None:
    """A failed version probe does not turn the image itself into a miss."""
    docker_probes["image_claude_version"].return_value = None
    env = service.build_report().environment
    assert env.base_image_present is True
    assert env.base_image_version is None


def test_environment_row_flags_update_when_latest_is_newer(
    service: InspectService, docker_probes: dict[str, Mock]
) -> None:
    docker_probes["latest_claude_version"].return_value = "2.0.51"
    env = service.build_report().environment
    assert env.latest_claude_version == "2.0.51"
    assert env.claude_update_available is True


def test_environment_row_no_update_when_latest_check_fails(
    service: InspectService, docker_probes: dict[str, Mock]
) -> None:
    """An unreachable registry must not look like 'no update' — or 'an update'."""
    docker_probes["latest_claude_version"].return_value = None
    env = service.build_report().environment
    assert env.latest_claude_version is None
    assert env.claude_update_available is False


def test_environment_row_no_update_when_same_version(
    service: InspectService, docker_probes: dict[str, Mock]
) -> None:
    docker_probes["latest_claude_version"].return_value = "2.0.50"
    env = service.build_report().environment
    assert env.latest_claude_version == "2.0.50"
    assert env.claude_update_available is False


def test_environment_row_skips_latest_check_when_image_absent(
    service: InspectService, docker_probes: dict[str, Mock]
) -> None:
    docker_probes["image_exists"].return_value = False
    env = service.build_report().environment
    assert env.base_image_present is False
    assert env.latest_claude_version is None
    assert env.claude_update_available is False
    docker_probes["latest_claude_version"].assert_not_called()


def test_report_json_carries_no_secret_values(service: InspectService, logs_mock: Mock) -> None:
    """Key names are fine (agent secrets check prints those); values never are."""
    del logs_mock  # only present so the fixture chain is explicit
    payload = json.dumps(dataclasses.asdict(service.build_report()))
    for forbidden in ("LITELLM_MASTER_KEY", "AWS_BEARER_TOKEN", "TELEGRAM_BOT_TOKEN", "sk-"):
        assert forbidden not in payload


def test_project_row_reports_the_project_image(service: InspectService) -> None:
    project = service.build_report().project
    assert project is not None
    assert project.image == "claude-agent-proj"
    assert project.present is True
    assert project.claude_version == "2.0.50"
    assert project.dockerfile == "/proj/.claude-agent-wrap/Dockerfile"


def test_project_row_is_none_without_a_project_dockerfile(
    service: InspectService, build_mock: Mock
) -> None:
    """`agent_name is None` is the predicate — the basename is shared with ops/Dockerfile."""
    build_mock.resolve_image.return_value = _BASE_IMAGE_ONLY
    assert service.build_report().project is None


def test_project_row_skips_probing_an_image_the_project_does_not_declare(
    service: InspectService, build_mock: Mock, docker_probes: dict[str, Mock]
) -> None:
    build_mock.resolve_image.return_value = _BASE_IMAGE_ONLY
    service.build_report()
    probed = {call.args[0] for call in docker_probes["image_exists"].call_args_list}
    assert probed == {BASE_IMAGE_NAME}


def test_project_row_reports_a_project_image_that_was_never_built(
    service: InspectService, docker_probes: dict[str, Mock]
) -> None:
    def base_only(image: str) -> bool:
        return image == BASE_IMAGE_NAME

    docker_probes["image_exists"].side_effect = base_only
    project = service.build_report().project
    assert project is not None
    assert project.present is False
    assert project.claude_version is None


def test_project_row_flags_an_update_from_the_shared_registry_lookup(
    service: InspectService, docker_probes: dict[str, Mock]
) -> None:
    """One `npm view` for the whole report; both images are compared against it."""
    docker_probes["latest_claude_version"].return_value = "2.0.51"
    report = service.build_report()
    assert report.project is not None
    assert report.project.claude_update_available is True
    assert report.environment.claude_update_available is True
    docker_probes["latest_claude_version"].assert_called_once_with(BASE_IMAGE_NAME)


def test_project_row_carries_the_legacy_dockerfile_flag(
    service: InspectService, build_mock: Mock
) -> None:
    build_mock.resolve_image.return_value = dataclasses.replace(_PROJECT_IMAGE, is_legacy=True)
    project = service.build_report().project
    assert project is not None
    assert project.is_legacy is True


def test_unresolvable_project_dockerfile_becomes_a_warning(
    service: InspectService, build_mock: Mock
) -> None:
    """Fatal to a launch, but a diagnostic is most useful precisely in that state."""
    build_mock.resolve_image.side_effect = SystemExit("Error: both Dockerfiles exist")
    report = service.build_report()
    assert report.project is None
    assert report.warnings == ["Error: both Dockerfiles exist"]
    assert report.environment.base_image_present is True


def test_unreadable_project_dockerfile_becomes_a_warning(
    service: InspectService, build_mock: Mock
) -> None:
    build_mock.resolve_image.side_effect = OSError("Permission denied")
    report = service.build_report()
    assert report.project is None
    assert "Permission denied" in report.warnings[0]


def test_report_has_no_warnings_when_nothing_is_wrong(service: InspectService) -> None:
    assert service.build_report().warnings == []


def test_lite_skips_the_registry_check(
    service: InspectService, docker_probes: dict[str, Mock]
) -> None:
    report = service.build_report(lite=True)
    docker_probes["latest_claude_version"].assert_not_called()
    assert report.environment.latest_claude_version is None
    assert report.environment.claude_update_available is False


def test_lite_skips_the_logs_size_walk(
    service: InspectService, docker_probes: dict[str, Mock]
) -> None:
    report = service.build_report(lite=True)
    docker_probes["dir_size"].assert_not_called()
    assert report.storage.logs_bytes is None
    assert report.storage.projects_registered == 2


def test_lite_still_reports_both_installed_versions(
    service: InspectService, docker_probes: dict[str, Mock]
) -> None:
    """The versions are the point of the command; only the registry check is dropped."""
    report = service.build_report(lite=True)
    probed = {call.args[0] for call in docker_probes["image_claude_version"].call_args_list}
    assert probed == {BASE_IMAGE_NAME, "claude-agent-proj"}
    assert report.environment.base_image_version == "2.0.50"
    assert report.project is not None
    assert report.project.claude_version == "2.0.50"


def test_lite_keeps_the_container_listings(service: InspectService, sidecar_mock: Mock) -> None:
    report = service.build_report(lite=True)
    sidecar_mock.list_sidecar_containers.assert_called_once_with()
    assert report.sidecars
    assert report.agents


def test_lite_is_recorded_on_the_report(service: InspectService) -> None:
    assert service.build_report(lite=True).lite is True
    assert service.build_report().lite is False


def test_version_probes_never_run_against_an_absent_image(
    service: InspectService, docker_probes: dict[str, Mock]
) -> None:
    """`docker run` on an image that is not present locally tries to *pull* it."""

    def project_only(image: str) -> bool:
        return image != BASE_IMAGE_NAME

    docker_probes["image_exists"].side_effect = project_only
    service.build_report()
    probed = {call.args[0] for call in docker_probes["image_claude_version"].call_args_list}
    assert probed == {"claude-agent-proj"}
    docker_probes["latest_claude_version"].assert_not_called()


def test_an_absent_project_image_does_not_gate_the_base_probes(
    service: InspectService, docker_probes: dict[str, Mock]
) -> None:
    """Each version probe waits on its own image only, never on both."""

    def base_only(image: str) -> bool:
        return image == BASE_IMAGE_NAME

    docker_probes["image_exists"].side_effect = base_only
    report = service.build_report()
    docker_probes["latest_claude_version"].assert_called_once_with(BASE_IMAGE_NAME)
    assert report.environment.base_image_version == "2.0.50"


def test_docker_unavailable_submits_no_probes(
    service: InspectService, docker_probes: dict[str, Mock]
) -> None:
    docker_probes["reachable"].return_value = False
    report = service.build_report()
    docker_probes["image_exists"].assert_not_called()
    docker_probes["image_claude_version"].assert_not_called()
    docker_probes["latest_claude_version"].assert_not_called()
    assert report.environment.network_present is False


def test_version_probes_run_concurrently(
    service: InspectService, docker_probes: dict[str, Mock]
) -> None:
    """
    Three probes, one three-party barrier: sequential execution can never clear it.

    Fails closed rather than merely slowly, so collapsing the fan-out into a loop — or
    sizing PROBE_WORKERS under the peak — breaks this test instead of quietly costing
    wall clock nobody measures.
    """
    barrier = threading.Barrier(3, timeout=10)

    def probe(image: str) -> str:
        del image
        barrier.wait()
        return "2.0.50"

    docker_probes["image_claude_version"].side_effect = probe
    docker_probes["latest_claude_version"].side_effect = probe

    report = service.build_report()
    assert report.environment.base_image_version == "2.0.50"
    assert report.project is not None
    assert report.project.claude_version == "2.0.50"
