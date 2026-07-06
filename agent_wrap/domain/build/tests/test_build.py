# This file has been created with the assistance of an AI tool.
"""Tests for agent_wrap.domain.build.service.BuildService."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from agent_wrap.domain.build.models import ResolvedImage
from agent_wrap.domain.build.service import BuildService
from agent_wrap.domain.display.service import DisplayService
from agent_wrap.domain.updates.service import UpdateService

if TYPE_CHECKING:
    from pathlib import Path

    import pytest_mock


@pytest.fixture
def build_svc(mocker: pytest_mock.MockFixture) -> BuildService:
    """Return a BuildService with spec-mocked dependencies."""
    return BuildService(
        update_service=mocker.Mock(spec=UpdateService),
        display_service=mocker.Mock(spec=DisplayService),
    )


def test_from_claude_agent_image_exists(
    build_svc: BuildService, tmp_path: Path, mocker: pytest_mock.MockFixture
) -> None:
    dockerfile = tmp_path / "Dockerfile.agent"
    dockerfile.write_text("# agent-name: test\nFROM claude-agent\n")
    resolved = ResolvedImage(
        image="claude-agent-test",
        dockerfile=dockerfile,
        context=tmp_path,
    )
    mocker.patch(f"{'agent_wrap.domain.build.service'}.image_exists", return_value=True)
    assert build_svc._check_from_line(resolved) is True


def test_from_claude_agent_image_missing(
    build_svc: BuildService,
    tmp_path: Path,
    mocker: pytest_mock.MockFixture,
) -> None:
    dockerfile = tmp_path / "Dockerfile.agent"
    dockerfile.write_text("# agent-name: test\nFROM claude-agent\n")
    resolved = ResolvedImage(
        image="claude-agent-test",
        dockerfile=dockerfile,
        context=tmp_path,
    )
    mocker.patch(f"{'agent_wrap.domain.build.service'}.image_exists", return_value=False)
    assert build_svc._check_from_line(resolved) is False
    build_svc._display.error.assert_any_call(  # type: ignore[union-attr]
        f"'{resolved.dockerfile}' uses 'FROM claude-agent' but the base image is not built."
    )
    assert build_svc._display.error.call_count == 2  # type: ignore[union-attr]


def test_from_custom_image(
    build_svc: BuildService,
    tmp_path: Path,
) -> None:
    dockerfile = tmp_path / "Dockerfile.agent"
    dockerfile.write_text("# agent-name: test\nFROM ubuntu:24.04\n")
    resolved = ResolvedImage(
        image="claude-agent-test",
        dockerfile=dockerfile,
        context=tmp_path,
    )
    assert build_svc._check_from_line(resolved) is True
    build_svc._display.warning.assert_called_once_with(  # type: ignore[union-attr]
        f"'{resolved.dockerfile}' inherits from 'ubuntu:24.04' rather than"
        " 'claude-agent'. Consider migrating to 'FROM claude-agent' to reuse"
        " the base toolchain."
    )


def test_empty_dockerfile(build_svc: BuildService, tmp_path: Path) -> None:
    dockerfile = tmp_path / "Dockerfile.agent"
    dockerfile.write_text("")
    resolved = ResolvedImage(
        image="claude-agent-test",
        dockerfile=dockerfile,
        context=tmp_path,
    )
    assert build_svc._check_from_line(resolved) is True


def test_multistage_dockerfile_last_from_wins(
    build_svc: BuildService, tmp_path: Path, mocker: pytest_mock.MockerFixture
) -> None:
    """Multi-stage Dockerfile: _check_from_line uses the last FROM line."""
    mocker.patch(f"{'agent_wrap.domain.build.service'}.image_exists", return_value=True)
    dockerfile = tmp_path / "Dockerfile.agent"
    # First FROM is a builder, second is the real base
    dockerfile.write_text(
        "# agent-name: test\nFROM node:20 AS builder\nRUN npm install\nFROM claude-agent\n"
    )
    resolved = ResolvedImage(
        image="claude-agent-test",
        dockerfile=dockerfile,
        context=tmp_path,
    )
    assert build_svc._check_from_line(resolved) is True


def test_multistage_dockerfile_last_custom_base(build_svc: BuildService, tmp_path: Path) -> None:
    """Multi-stage Dockerfile where last FROM is a custom image."""
    dockerfile = tmp_path / "Dockerfile.agent"
    dockerfile.write_text("# agent-name: test\nFROM claude-agent AS base\nFROM ubuntu:24.04\n")
    resolved = ResolvedImage(
        image="claude-agent-test",
        dockerfile=dockerfile,
        context=tmp_path,
    )
    assert build_svc._check_from_line(resolved) is True
    build_svc._display.warning.assert_called_once_with(  # type: ignore[union-attr]
        f"'{resolved.dockerfile}' inherits from 'ubuntu:24.04' rather than"
        " 'claude-agent'. Consider migrating to 'FROM claude-agent' to reuse"
        " the base toolchain."
    )


# --- _do_rebuild ---


def test_do_rebuild_resolve_image_exit(
    mocker: pytest_mock.MockFixture,
) -> None:
    mocker.patch.object(
        BuildService,
        "resolve_image",
        side_effect=SystemExit("no Dockerfile.agent"),
    )
    svc = BuildService(
        update_service=mocker.Mock(spec=UpdateService),
        display_service=mocker.Mock(spec=DisplayService),
    )
    rc = svc._do_rebuild(full=False)
    assert rc == 1
    svc._display.error.assert_called_once_with("no Dockerfile.agent")  # type: ignore[union-attr]


def test_do_rebuild_full_build_fails(tmp_path: Path, mocker: pytest_mock.MockFixture) -> None:
    mock_resolve = mocker.patch.object(BuildService, "resolve_image")
    mock_resolve.return_value = ResolvedImage(
        image="claude-agent-test",
        dockerfile=tmp_path / "Dockerfile.agent",
        context=tmp_path,
    )
    mock_run = mocker.patch("agent_wrap.domain.build.service.subprocess.run")
    mock_run.return_value.returncode = 1
    svc = BuildService(
        update_service=mocker.Mock(spec=UpdateService),
        display_service=mocker.Mock(spec=DisplayService),
    )
    rc = svc._do_rebuild(full=True)
    assert rc == 1
    mock_run.assert_called_once()  # reason: subprocess was attempted


def test_do_rebuild_project_build_fails(tmp_path: Path, mocker: pytest_mock.MockFixture) -> None:
    dockerfile = tmp_path / "Dockerfile.agent"
    dockerfile.write_text("# agent-name: t\nFROM custom-image\n")
    mock_resolve = mocker.patch.object(BuildService, "resolve_image")
    mock_resolve.return_value = ResolvedImage(
        image="claude-agent-test",
        dockerfile=dockerfile,
        context=tmp_path,
    )
    mock_run = mocker.patch("agent_wrap.domain.build.service.subprocess.run")
    mock_run.return_value.returncode = 1
    svc = BuildService(
        update_service=mocker.Mock(spec=UpdateService),
        display_service=mocker.Mock(spec=DisplayService),
    )
    rc = svc._do_rebuild(full=False)
    assert rc == 1
    mock_run.assert_called_once()  # reason: subprocess was attempted


def test_do_rebuild_check_from_line_fails(tmp_path: Path, mocker: pytest_mock.MockFixture) -> None:
    mock_resolve = mocker.patch.object(BuildService, "resolve_image")
    mock_resolve.return_value = ResolvedImage(
        image="claude-agent-test",
        dockerfile=tmp_path / "Dockerfile.agent",
        context=tmp_path,
    )
    mock_run = mocker.patch("agent_wrap.domain.build.service.subprocess.run")
    mock_run.return_value.returncode = 0
    mocker.patch(f"{'agent_wrap.domain.build.service'}.image_exists", return_value=False)
    (tmp_path / "Dockerfile.agent").write_text("# agent-name: t\nFROM claude-agent\n")
    svc = BuildService(
        update_service=mocker.Mock(spec=UpdateService),
        display_service=mocker.Mock(spec=DisplayService),
    )
    rc = svc._do_rebuild(full=False)
    assert rc == 1
    mock_run.assert_called_once()  # reason: subprocess was attempted (check_from_line failed after build)


# --- run() ---


# --- _docker_build ---


def test_docker_build_returns_exit_code(
    build_svc: BuildService, tmp_path: Path, mocker: pytest_mock.MockFixture
) -> None:
    mock_run = mocker.patch("agent_wrap.domain.build.service.subprocess.run")
    mock_run.return_value.returncode = 0
    rc = build_svc._docker_build(tmp_path / "Dockerfile", "test-img", tmp_path, "1000", "1000")
    assert rc == 0


def test_docker_build_failure(
    build_svc: BuildService, tmp_path: Path, mocker: pytest_mock.MockFixture
) -> None:
    mock_run = mocker.patch("agent_wrap.domain.build.service.subprocess.run")
    mock_run.return_value.returncode = 1
    rc = build_svc._docker_build(tmp_path / "Dockerfile", "test-img", tmp_path, "1000", "1000")
    assert rc == 1
    mock_run.assert_called_once()  # reason: docker build subprocess was attempted


def test_docker_build_splices_host_network(
    build_svc: BuildService, tmp_path: Path, mocker: pytest_mock.MockFixture
) -> None:
    mock_run = mocker.patch("agent_wrap.domain.build.service.subprocess.run")
    mock_run.return_value.returncode = 0
    mocker.patch(
        f"{'agent_wrap.domain.build.service'}.host_network_build_args",
        return_value=["--network", "host"],
    )
    build_svc._docker_build(tmp_path / "Dockerfile", "test-img", tmp_path, "1000", "1000")
    argv = mock_run.call_args[0][0]
    assert "--network" in argv
    assert argv[argv.index("--network") + 1] == "host"


def test_docker_build_no_host_network_by_default(
    build_svc: BuildService, tmp_path: Path, mocker: pytest_mock.MockFixture
) -> None:
    mock_run = mocker.patch("agent_wrap.domain.build.service.subprocess.run")
    mock_run.return_value.returncode = 0
    mocker.patch(f"{'agent_wrap.domain.build.service'}.host_network_build_args", return_value=[])
    build_svc._docker_build(tmp_path / "Dockerfile", "test-img", tmp_path, "1000", "1000")
    assert "--network" not in mock_run.call_args[0][0]


# --- _do_rebuild success path ---


def test_do_rebuild_project_success(tmp_path: Path, mocker: pytest_mock.MockFixture) -> None:
    dockerfile = tmp_path / "Dockerfile.agent"
    dockerfile.write_text("# agent-name: t\nFROM custom-image\n")
    mock_resolve = mocker.patch.object(BuildService, "resolve_image")
    mock_resolve.return_value = ResolvedImage(
        image="claude-agent-test",
        dockerfile=dockerfile,
        context=tmp_path,
    )
    mock_run = mocker.patch("agent_wrap.domain.build.service.subprocess.run")
    mock_run.return_value.returncode = 0
    svc = BuildService(
        update_service=mocker.Mock(spec=UpdateService),
        display_service=mocker.Mock(spec=DisplayService),
    )
    rc = svc._do_rebuild(full=False)
    assert rc == 0


def test_do_rebuild_full_base_then_project(tmp_path: Path, mocker: pytest_mock.MockFixture) -> None:
    dockerfile = tmp_path / "Dockerfile.agent"
    dockerfile.write_text("# agent-name: t\nFROM claude-agent\n")
    mock_resolve = mocker.patch.object(BuildService, "resolve_image")
    mock_resolve.return_value = ResolvedImage(
        image="claude-agent-t",
        dockerfile=dockerfile,
        context=tmp_path,
    )
    mock_run = mocker.patch("agent_wrap.domain.build.service.subprocess.run")
    mock_run.return_value.returncode = 0
    mocker.patch(f"{'agent_wrap.domain.build.service'}.image_exists", return_value=True)
    svc = BuildService(
        update_service=mocker.Mock(spec=UpdateService),
        display_service=mocker.Mock(spec=DisplayService),
    )
    rc = svc._do_rebuild(full=True)
    assert rc == 0
    # Base build + project build + docker images ls (only at end, not after base)
    assert mock_run.call_count == 3
    # Verify docker build commands were issued
    call_args_list = [c[0][0] for c in mock_run.call_args_list if c[0]]
    docker_builds = [a for a in call_args_list if isinstance(a, list) and "build" in a]
    assert len(docker_builds) == 2  # base + project
