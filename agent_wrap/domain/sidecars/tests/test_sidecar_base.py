# This file has been created with the assistance of an AI tool.
"""
Tests for the container mechanics ``Sidecar`` implements for every sidecar.

Exercised through a minimal concrete subclass rather than through ``LiteLLMSidecar`` and
``TelegramSidecar`` in turn: these methods read nothing but ``SidecarConfig``, so running
them twice over two configs would assert the same code against the same inputs and only
disagree about which container name appeared in the docker argv.
"""

from pathlib import Path
from typing import TYPE_CHECKING, override
from unittest.mock import Mock

import pytest

from agent_wrap.constants import PollResult
from agent_wrap.domain.display.service import DisplayService
from agent_wrap.domain.sidecars.base import Sidecar
from agent_wrap.domain.sidecars.models import SidecarConfig

if TYPE_CHECKING:
    from collections.abc import Callable

    import pytest_mock

#: What ``_health_poll`` hands ``poll_until`` — named so a stub can be typed without
#: restating the whole ``poll_until`` signature.
type _Poll = Callable[[], tuple[PollResult, str]]

_DOCKER = "agent_wrap.domain.sidecars.base.docker_run"
_IMAGE_EXISTS = "agent_wrap.domain.sidecars.base.image_exists"
_NETWORK_EXISTS = "agent_wrap.domain.sidecars.base.network_exists"

_LABEL = "probe-sidecar"


class _Probe(Sidecar):
    """The abstract surface, satisfied as cheaply as possible."""

    @property
    @override
    def _label(self) -> str:
        return _LABEL

    @override
    def ensure(
        self,
        *,
        use_host_net: bool,
        agent_network: str | None,
        secrets: dict[str, str] | None = None,
    ) -> list[str]:
        return []

    @override
    def release(self) -> None:
        return


@pytest.fixture
def config() -> SidecarConfig:
    return SidecarConfig(
        image="test-image:latest",
        container_name="agent-wrap-probe",
        network_name="agent-wrap-net",
        internal_port=48620,
        health_timeout_sec=90,
        cold_start_time=120.0,
        short_circuit_time=2.0,
        pull_timeout_sec=900,
        log_dir=Path("/tmp/probe-logs"),  # noqa: S108 -- never opened; no mechanic here writes
    )


@pytest.fixture
def sidecar(config: SidecarConfig) -> _Probe:
    return _Probe(config, display_service=Mock(spec=DisplayService))


@pytest.fixture
def poll_once(sidecar: _Probe, mocker: pytest_mock.MockFixture) -> None:
    """Make ``poll_until`` run the poll callable exactly once and report its verdict."""

    def run_once(*, poll: _Poll, **_kwargs: object) -> bool:
        return poll()[0] == PollResult.SUCCESS

    mocker.patch.object(sidecar._display, "poll_until", side_effect=run_once)


def _last_error(sc: _Probe) -> str:
    return str(sc._display.error.call_args.args[0])  # pyrefly: ignore [missing-attribute]


def test_container_settings_come_from_the_config(sidecar: _Probe) -> None:
    assert sidecar.container_name == "agent-wrap-probe"
    assert sidecar.network_name == "agent-wrap-net"
    assert sidecar.image == "test-image:latest"
    assert sidecar.health_timeout_sec == 90
    assert sidecar.cold_start_time == 120.0
    assert sidecar.short_circuit_time == 2.0


def test_required_secrets_defaults_to_none(sidecar: _Probe) -> None:
    assert sidecar.required_secrets() == []


def test_prepare_and_on_exit_are_no_ops_by_default(sidecar: _Probe) -> None:
    assert sidecar.prepare() is None
    assert sidecar.on_exit() is None


def test_warning_is_prefixed_with_the_label(sidecar: _Probe) -> None:
    sidecar._warn("something to note")
    sidecar._display.warning.assert_called_once_with(  # pyrefly: ignore [missing-attribute]
        f"{_LABEL}: something to note"
    )


def test_fatal_reports_under_the_label_and_hands_back_the_exit(sidecar: _Probe) -> None:
    """Returned rather than raised, so the call site's ``raise`` stays visible."""
    exc = sidecar._fatal("it went wrong")
    assert isinstance(exc, SystemExit)
    assert exc.code == 1
    assert _last_error(sidecar) == f"{_LABEL}: it went wrong"


def test_is_running_true(sidecar: _Probe, mocker: pytest_mock.MockFixture) -> None:
    mocker.patch(_DOCKER, autospec=True, return_value=("true", 0))
    assert sidecar._is_running() is True


def test_is_running_false(sidecar: _Probe, mocker: pytest_mock.MockFixture) -> None:
    mocker.patch(_DOCKER, autospec=True, return_value=("false", 0))
    assert sidecar._is_running() is False


def test_is_running_error(sidecar: _Probe, mocker: pytest_mock.MockFixture) -> None:
    """A container docker cannot inspect is not running, whatever it printed."""
    mocker.patch(_DOCKER, autospec=True, return_value=("true", 1))
    assert sidecar._is_running() is False


def test_is_on_network_true(sidecar: _Probe, mocker: pytest_mock.MockFixture) -> None:
    mocker.patch(_DOCKER, autospec=True, return_value=("agent-wrap-net\nhost\n", 0))
    assert sidecar._is_on_network("host") is True


def test_is_on_network_false(sidecar: _Probe, mocker: pytest_mock.MockFixture) -> None:
    mocker.patch(_DOCKER, autospec=True, return_value=("agent-wrap-net\n", 0))
    assert sidecar._is_on_network("host") is False


def test_is_on_network_error(sidecar: _Probe, mocker: pytest_mock.MockFixture) -> None:
    mocker.patch(_DOCKER, autospec=True, return_value=("", 1))
    assert sidecar._is_on_network("agent-wrap-net") is False


def test_sidecar_ip_on_network(sidecar: _Probe, mocker: pytest_mock.MockFixture) -> None:
    mock_docker = mocker.patch(_DOCKER, autospec=True, return_value=("172.18.0.2\n", 0))
    assert sidecar._sidecar_ip_on_network("agent-wrap-net") == "172.18.0.2"
    # The network is interpolated into the Go template, so it has to reach docker there.
    assert "agent-wrap-net" in mock_docker.call_args.args[-1]


def test_sidecar_ip_on_network_failure(sidecar: _Probe, mocker: pytest_mock.MockFixture) -> None:
    mocker.patch(_DOCKER, autospec=True, return_value=("", 1))
    assert sidecar._sidecar_ip_on_network("agent-wrap-net") == ""


def test_ensure_network_leaves_an_existing_one_alone(
    sidecar: _Probe, mocker: pytest_mock.MockFixture
) -> None:
    mocker.patch(_NETWORK_EXISTS, autospec=True, return_value=True)
    mock_docker = mocker.patch(_DOCKER, autospec=True, return_value=("", 0))
    sidecar._ensure_network()
    assert mock_docker.call_args_list == []


def test_ensure_network_creates_a_missing_one(
    sidecar: _Probe, mocker: pytest_mock.MockFixture
) -> None:
    mocker.patch(_NETWORK_EXISTS, autospec=True, return_value=False)
    mock_docker = mocker.patch(_DOCKER, autospec=True, return_value=("", 0))
    sidecar._ensure_network()
    assert mock_docker.call_args.args == ("network", "create", "agent-wrap-net")


def test_ensure_network_create_failure_is_fatal(
    sidecar: _Probe, mocker: pytest_mock.MockFixture
) -> None:
    mocker.patch(_NETWORK_EXISTS, autospec=True, return_value=False)
    mocker.patch(_DOCKER, autospec=True, return_value=("", 1))
    with pytest.raises(SystemExit):
        sidecar._ensure_network()
    assert "failed to create docker network" in _last_error(sidecar)


def test_attach_to_network_refuses_a_network_that_does_not_exist(
    sidecar: _Probe, mocker: pytest_mock.MockFixture
) -> None:
    """It came from agent-run-args, so creating an empty one would hide the typo."""
    mocker.patch(_NETWORK_EXISTS, autospec=True, return_value=False)
    mock_docker = mocker.patch(_DOCKER, autospec=True, return_value=("", 0))
    with pytest.raises(SystemExit):
        sidecar._attach_to_network("missing-net")
    assert mock_docker.call_args_list == []
    assert "does not exist" in _last_error(sidecar)


def test_attach_to_network_is_a_no_op_when_already_connected(
    sidecar: _Probe, mocker: pytest_mock.MockFixture
) -> None:
    mocker.patch(_NETWORK_EXISTS, autospec=True, return_value=True)
    mocker.patch.object(_Probe, "_is_on_network", autospec=True, return_value=True)
    mock_docker = mocker.patch(_DOCKER, autospec=True, return_value=("", 0))
    sidecar._attach_to_network("custom-net")
    assert mock_docker.call_args_list == []


def test_attach_to_network_connects(sidecar: _Probe, mocker: pytest_mock.MockFixture) -> None:
    mocker.patch(_NETWORK_EXISTS, autospec=True, return_value=True)
    mocker.patch.object(_Probe, "_is_on_network", autospec=True, return_value=False)
    mock_docker = mocker.patch(_DOCKER, autospec=True, return_value=("", 0))
    sidecar._attach_to_network("custom-net")
    assert mock_docker.call_args.args == (
        "network",
        "connect",
        "custom-net",
        "agent-wrap-probe",
    )


def test_attach_to_network_connect_failure_is_fatal(
    sidecar: _Probe, mocker: pytest_mock.MockFixture
) -> None:
    mocker.patch(_NETWORK_EXISTS, autospec=True, return_value=True)
    mocker.patch.object(_Probe, "_is_on_network", autospec=True, return_value=False)
    mocker.patch(_DOCKER, autospec=True, return_value=("", 1))
    with pytest.raises(SystemExit):
        sidecar._attach_to_network("custom-net")
    assert "failed to attach" in _last_error(sidecar)


def test_ensure_image_skips_the_pull_when_it_is_present(
    sidecar: _Probe, mocker: pytest_mock.MockFixture
) -> None:
    mocker.patch(_IMAGE_EXISTS, autospec=True, return_value=True)
    mock_docker = mocker.patch(_DOCKER, autospec=True, return_value=("", 0))
    sidecar._ensure_image()
    assert mock_docker.call_args_list == []


def test_ensure_image_pulls_a_missing_one_under_the_configured_timeout(
    sidecar: _Probe, mocker: pytest_mock.MockFixture
) -> None:
    """capture=False so a pull's progress reaches the terminal instead of a return value."""
    mocker.patch(_IMAGE_EXISTS, autospec=True, return_value=False)
    mock_docker = mocker.patch(_DOCKER, autospec=True, return_value=("", 0))
    sidecar._ensure_image()
    assert mock_docker.call_args.args == ("pull", "test-image:latest")
    assert mock_docker.call_args.kwargs == {"capture": False, "timeout": 900}


def test_ensure_image_pull_failure_is_fatal(
    sidecar: _Probe, mocker: pytest_mock.MockFixture
) -> None:
    mocker.patch(_IMAGE_EXISTS, autospec=True, return_value=False)
    mocker.patch(_DOCKER, autospec=True, return_value=("", 1))
    with pytest.raises(SystemExit):
        sidecar._ensure_image()
    assert "failed to pull image" in _last_error(sidecar)


@pytest.mark.usefixtures("poll_once")
def test_health_poll_succeeds_on_healthy(sidecar: _Probe, mocker: pytest_mock.MockFixture) -> None:
    mocker.patch(_DOCKER, autospec=True, return_value=("healthy", 0))
    assert sidecar._health_poll() is True


@pytest.mark.usefixtures("poll_once")
def test_health_poll_fails_on_unhealthy(sidecar: _Probe, mocker: pytest_mock.MockFixture) -> None:
    mocker.patch(_DOCKER, autospec=True, return_value=("unhealthy", 0))
    mocker.patch.object(_Probe, "_is_running", autospec=True, return_value=True)
    assert sidecar._health_poll() is False


@pytest.mark.usefixtures("poll_once")
def test_health_poll_fails_when_the_container_is_gone(
    sidecar: _Probe, mocker: pytest_mock.MockFixture
) -> None:
    """An inspect that cannot find the container is not a pending health check."""
    mocker.patch(_DOCKER, autospec=True, return_value=("", 1))
    assert sidecar._health_poll() is False


@pytest.mark.usefixtures("poll_once")
def test_health_poll_fails_when_the_container_stopped_mid_wait(
    sidecar: _Probe, mocker: pytest_mock.MockFixture
) -> None:
    """A "starting" status on a container that has since exited will never become healthy."""
    mocker.patch(_DOCKER, autospec=True, return_value=("starting", 0))
    mocker.patch.object(_Probe, "_is_running", autospec=True, return_value=False)
    assert sidecar._health_poll() is False


def test_health_poll_stays_pending_while_starting(
    sidecar: _Probe, mocker: pytest_mock.MockFixture
) -> None:
    mocker.patch(_DOCKER, autospec=True, return_value=("starting", 0))
    mocker.patch.object(_Probe, "_is_running", autospec=True, return_value=True)
    verdicts: list[tuple[PollResult, str]] = []

    def capture(*, poll: _Poll, **_kwargs: object) -> bool:
        verdicts.append(poll())
        return False

    mocker.patch.object(sidecar._display, "poll_until", side_effect=capture)
    sidecar._health_poll()
    assert verdicts == [(PollResult.PENDING, "starting")]


def test_reap_stale_container_removes_a_corpse(
    sidecar: _Probe, mocker: pytest_mock.MockFixture
) -> None:
    mock_docker = mocker.patch(_DOCKER, autospec=True, return_value=("", 0))
    sidecar._reap_stale_container()
    assert [c.args for c in mock_docker.call_args_list] == [
        ("container", "inspect", "agent-wrap-probe"),
        ("rm", "-f", "agent-wrap-probe"),
    ]


def test_reap_stale_container_removes_nothing_when_the_name_is_free(
    sidecar: _Probe, mocker: pytest_mock.MockFixture
) -> None:
    mock_docker = mocker.patch(_DOCKER, autospec=True, return_value=("", 1))
    sidecar._reap_stale_container()
    assert [c.args for c in mock_docker.call_args_list] == [
        ("container", "inspect", "agent-wrap-probe")
    ]
