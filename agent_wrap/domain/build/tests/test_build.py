# This file has been edited with the assistance of an AI tool.
"""Tests for agent_wrap.domain.build.service.BuildService."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from agent_wrap.constants import (
    AGENT_ASSETS_DIR,
    AGENT_DOCKERFILE_NAME,
    LEGACY_AGENT_DOCKERFILE_NAME,
    UpdateCheck,
)
from agent_wrap.domain.build.constants import DEFAULT_STARTUP_TIMEOUT_SECONDS
from agent_wrap.domain.build.models import ResolvedImage
from agent_wrap.domain.build.service import BuildService
from agent_wrap.domain.display.service import DisplayService
from agent_wrap.domain.updates.service import UpdateService
from agent_wrap.exceptions import DockerfileDirectiveError

if TYPE_CHECKING:
    from collections.abc import Callable

    import pytest_mock


@pytest.fixture
def build_svc(mocker: pytest_mock.MockFixture) -> BuildService:
    """Return a BuildService with spec-mocked dependencies."""
    return BuildService(
        update_service=mocker.Mock(spec=UpdateService),
        display_service=mocker.Mock(spec=DisplayService),
    )


def test_rebuild_aborts_while_containers_are_live(
    mocker: pytest_mock.MockFixture, build_svc: BuildService
) -> None:
    """Rebuilding is pointless if the update it is gated behind cannot run."""
    build_svc._updates.check_updates.return_value = UpdateCheck.BLOCKED  # pyrefly: ignore [missing-attribute]
    do_rebuild = mocker.patch.object(BuildService, "_do_rebuild", autospec=True)
    assert build_svc.rebuild(full=False) == 1
    do_rebuild.assert_not_called()


def test_rebuild_stops_after_applying_an_update(
    mocker: pytest_mock.MockFixture, build_svc: BuildService
) -> None:
    """The wrapper just changed under us, so the rebuild must be re-issued, not continued."""
    build_svc._updates.check_updates.return_value = UpdateCheck.HANDLED  # pyrefly: ignore [missing-attribute]
    do_rebuild = mocker.patch.object(BuildService, "_do_rebuild", autospec=True)
    assert build_svc.rebuild(full=False) == 0
    do_rebuild.assert_not_called()


def test_rebuild_proceeds_when_there_is_no_update(
    mocker: pytest_mock.MockFixture, build_svc: BuildService
) -> None:
    build_svc._updates.check_updates.return_value = UpdateCheck.PROCEED  # pyrefly: ignore [missing-attribute]
    do_rebuild = mocker.patch.object(BuildService, "_do_rebuild", autospec=True, return_value=0)
    assert build_svc.rebuild(full=True) == 0
    do_rebuild.assert_called_once_with(build_svc, full=True)


def _write_project_dockerfile(project_dir: Path, content: str) -> Path:
    """Write *content* to the project Dockerfile's real discovery location."""
    path = project_dir / AGENT_ASSETS_DIR / AGENT_DOCKERFILE_NAME
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    return path


def test_from_claude_agent_image_exists(
    build_svc: BuildService, tmp_path: Path, mocker: pytest_mock.MockFixture
) -> None:
    dockerfile = tmp_path / AGENT_DOCKERFILE_NAME
    dockerfile.write_text("# agent-name: test\nFROM claude-agent\n")
    resolved = ResolvedImage(
        image="claude-agent-test",
        dockerfile=dockerfile,
        context=tmp_path,
    )
    mocker.patch(
        f"{'agent_wrap.domain.build.service'}.image_exists", autospec=True, return_value=True
    )
    assert build_svc._check_from_line(resolved) is True


def test_from_claude_agent_image_missing(
    build_svc: BuildService,
    tmp_path: Path,
    mocker: pytest_mock.MockFixture,
) -> None:
    dockerfile = tmp_path / AGENT_DOCKERFILE_NAME
    dockerfile.write_text("# agent-name: test\nFROM claude-agent\n")
    resolved = ResolvedImage(
        image="claude-agent-test",
        dockerfile=dockerfile,
        context=tmp_path,
    )
    mocker.patch(
        f"{'agent_wrap.domain.build.service'}.image_exists", autospec=True, return_value=False
    )
    assert build_svc._check_from_line(resolved) is False
    build_svc._display.error.assert_any_call(  # pyrefly: ignore [missing-attribute]
        f"'{resolved.dockerfile}' uses 'FROM claude-agent' but the base image is not built.\n"
        "Run 'agent rebuild --full' to build the base first."
    )
    assert build_svc._display.error.call_count == 1  # pyrefly: ignore [missing-attribute]


def test_from_custom_image(
    build_svc: BuildService,
    tmp_path: Path,
) -> None:
    dockerfile = tmp_path / AGENT_DOCKERFILE_NAME
    dockerfile.write_text("# agent-name: test\nFROM ubuntu:24.04\n")
    resolved = ResolvedImage(
        image="claude-agent-test",
        dockerfile=dockerfile,
        context=tmp_path,
    )
    assert build_svc._check_from_line(resolved) is True
    build_svc._display.warning.assert_called_once_with(  # pyrefly: ignore [missing-attribute]
        f"'{resolved.dockerfile}' inherits from 'ubuntu:24.04' rather than"
        " 'claude-agent'. Consider migrating to 'FROM claude-agent' to reuse"
        " the base toolchain."
    )


def test_empty_dockerfile(build_svc: BuildService, tmp_path: Path) -> None:
    dockerfile = tmp_path / AGENT_DOCKERFILE_NAME
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
    mocker.patch(
        f"{'agent_wrap.domain.build.service'}.image_exists", autospec=True, return_value=True
    )
    dockerfile = tmp_path / AGENT_DOCKERFILE_NAME
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
    dockerfile = tmp_path / AGENT_DOCKERFILE_NAME
    dockerfile.write_text("# agent-name: test\nFROM claude-agent AS base\nFROM ubuntu:24.04\n")
    resolved = ResolvedImage(
        image="claude-agent-test",
        dockerfile=dockerfile,
        context=tmp_path,
    )
    assert build_svc._check_from_line(resolved) is True
    build_svc._display.warning.assert_called_once_with(  # pyrefly: ignore [missing-attribute]
        f"'{resolved.dockerfile}' inherits from 'ubuntu:24.04' rather than"
        " 'claude-agent'. Consider migrating to 'FROM claude-agent' to reuse"
        " the base toolchain."
    )


def test_do_rebuild_resolve_image_exit(
    mocker: pytest_mock.MockFixture,
    build_svc: BuildService,
) -> None:
    mocker.patch.object(
        BuildService,
        "resolve_image",
        autospec=True,
        side_effect=SystemExit("no project Dockerfile"),
    )
    rc = build_svc._do_rebuild(full=False)
    assert rc == 1
    build_svc._display.error.assert_called_once_with("no project Dockerfile")  # pyrefly: ignore [missing-attribute]


def test_do_rebuild_full_build_fails(
    tmp_path: Path, mocker: pytest_mock.MockFixture, build_svc: BuildService
) -> None:
    mock_resolve = mocker.patch.object(BuildService, "resolve_image", autospec=True)
    mock_resolve.return_value = ResolvedImage(
        image="claude-agent-test",
        dockerfile=tmp_path / AGENT_DOCKERFILE_NAME,
        context=tmp_path,
    )
    mock_run = mocker.patch("agent_wrap.domain.build.service.subprocess.run")
    mock_run.return_value.returncode = 1
    rc = build_svc._do_rebuild(full=True)
    assert rc == 1
    mock_run.assert_called_once()  # reason: subprocess was attempted


def test_do_rebuild_project_build_fails(
    tmp_path: Path, mocker: pytest_mock.MockFixture, build_svc: BuildService
) -> None:
    dockerfile = tmp_path / AGENT_DOCKERFILE_NAME
    dockerfile.write_text("# agent-name: t\nFROM custom-image\n")
    mock_resolve = mocker.patch.object(BuildService, "resolve_image", autospec=True)
    mock_resolve.return_value = ResolvedImage(
        image="claude-agent-test",
        dockerfile=dockerfile,
        context=tmp_path,
    )
    mock_run = mocker.patch("agent_wrap.domain.build.service.subprocess.run")
    mock_run.return_value.returncode = 1
    rc = build_svc._do_rebuild(full=False)
    assert rc == 1
    mock_run.assert_called_once()  # reason: subprocess was attempted


def test_do_rebuild_check_from_line_fails(
    tmp_path: Path, mocker: pytest_mock.MockFixture, build_svc: BuildService
) -> None:
    dockerfile = _write_project_dockerfile(tmp_path, "# agent-name: t\nFROM claude-agent\n")
    mock_resolve = mocker.patch.object(BuildService, "resolve_image", autospec=True)
    mock_resolve.return_value = ResolvedImage(
        image="claude-agent-test",
        dockerfile=dockerfile,
        context=tmp_path,
        agent_name="t",
    )
    mock_run = mocker.patch("agent_wrap.domain.build.service.subprocess.run")
    mock_run.return_value.returncode = 0
    mocker.patch(
        f"{'agent_wrap.domain.build.service'}.image_exists", autospec=True, return_value=False
    )
    rc = build_svc._do_rebuild(full=False)
    assert rc == 1
    mock_run.assert_not_called()  # reason: guard clause returns early before docker build


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
        autospec=True,
        return_value=["--network", "host"],
    )
    build_svc._docker_build(tmp_path / "Dockerfile", "test-img", tmp_path, "1000", "1000")
    argv = mock_run.call_args[0][0]
    assert "--network" in argv
    assert argv[argv.index("--network") + 1] == "host"


def test_docker_build_splices_spellcheck_lang(
    build_svc: BuildService, tmp_path: Path, mocker: pytest_mock.MockFixture
) -> None:
    # The dictionary list must reach the image build, or the language written into
    # settings.json names a dictionary that was never installed.
    mock_run = mocker.patch("agent_wrap.domain.build.service.subprocess.run")
    mock_run.return_value.returncode = 0
    build_svc._docker_build(tmp_path / "Dockerfile", "test-img", tmp_path, "1000", "1000")
    argv = mock_run.call_args[0][0]
    assert "SPELLCHECK_LANG=en_US,ru_RU" in argv
    assert argv[argv.index("SPELLCHECK_LANG=en_US,ru_RU") - 1] == "--build-arg"


def test_docker_build_no_host_network_by_default(
    build_svc: BuildService, tmp_path: Path, mocker: pytest_mock.MockFixture
) -> None:
    mock_run = mocker.patch("agent_wrap.domain.build.service.subprocess.run")
    mock_run.return_value.returncode = 0
    mocker.patch(
        f"{'agent_wrap.domain.build.service'}.host_network_build_args",
        autospec=True,
        return_value=[],
    )
    build_svc._docker_build(tmp_path / "Dockerfile", "test-img", tmp_path, "1000", "1000")
    assert "--network" not in mock_run.call_args[0][0]


def test_do_rebuild_project_success(
    tmp_path: Path, mocker: pytest_mock.MockFixture, build_svc: BuildService
) -> None:
    dockerfile = tmp_path / AGENT_DOCKERFILE_NAME
    dockerfile.write_text("# agent-name: t\nFROM custom-image\n")
    mock_resolve = mocker.patch.object(BuildService, "resolve_image", autospec=True)
    mock_resolve.return_value = ResolvedImage(
        image="claude-agent-test",
        dockerfile=dockerfile,
        context=tmp_path,
    )
    mock_run = mocker.patch("agent_wrap.domain.build.service.subprocess.run")
    mock_run.return_value.returncode = 0
    rc = build_svc._do_rebuild(full=False)
    assert rc == 0


def test_do_rebuild_full_base_then_project(
    tmp_path: Path, mocker: pytest_mock.MockFixture, build_svc: BuildService
) -> None:
    dockerfile = tmp_path / AGENT_DOCKERFILE_NAME
    dockerfile.write_text("# agent-name: t\nFROM claude-agent\n")
    mock_resolve = mocker.patch.object(BuildService, "resolve_image", autospec=True)
    mock_resolve.return_value = ResolvedImage(
        image="claude-agent-t",
        dockerfile=dockerfile,
        context=tmp_path,
    )
    mock_run = mocker.patch("agent_wrap.domain.build.service.subprocess.run")
    mock_run.return_value.returncode = 0
    mocker.patch(
        f"{'agent_wrap.domain.build.service'}.image_exists", autospec=True, return_value=True
    )
    rc = build_svc._do_rebuild(full=True)
    assert rc == 0
    # Base build + project build + docker images ls (only at end, not after base)
    assert mock_run.call_count == 3
    # Verify docker build commands were issued
    call_args_list = [c[0][0] for c in mock_run.call_args_list if c[0]]
    docker_builds = [a for a in call_args_list if isinstance(a, list) and "build" in a]
    assert len(docker_builds) == 2  # base + project


def test_agent_user(write_dockerfile: Callable[[str], Path], build_svc: BuildService) -> None:
    p = write_dockerfile("# agent-name: test\n# agent-user: customuser\nFROM claude-agent\n")
    info = build_svc.parse_dockerfile_agent(p)
    assert info.agent_user == "customuser"


def test_default_agent_user(
    write_dockerfile: Callable[[str], Path], build_svc: BuildService
) -> None:
    p = write_dockerfile("# agent-name: test\nFROM claude-agent\n")
    info = build_svc.parse_dockerfile_agent(p)
    assert info.agent_user == "ubuntu"


def test_expose_ports(write_dockerfile: Callable[[str], Path], build_svc: BuildService) -> None:
    p = write_dockerfile("FROM claude-agent\nEXPOSE 8080 3000/tcp\n")
    info = build_svc.parse_dockerfile_agent(p)
    assert info.expose_ports == ["8080", "3000"]


def test_agent_run_args(write_dockerfile: Callable[[str], Path], build_svc: BuildService) -> None:
    p = write_dockerfile(
        "FROM claude-agent\n# agent-run-args: --device /dev/fuse --cap-add SYS_ADMIN\n"
    )
    info = build_svc.parse_dockerfile_agent(p)
    assert info.extra_run_args == ["--device", "/dev/fuse", "--cap-add", "SYS_ADMIN"]


def test_multiple_run_args_lines(
    write_dockerfile: Callable[[str], Path], build_svc: BuildService
) -> None:
    p = write_dockerfile(
        "FROM claude-agent\n"
        "# agent-run-args: --device /dev/fuse\n"
        "# agent-run-args: --cap-add SYS_ADMIN\n"
    )
    info = build_svc.parse_dockerfile_agent(p)
    assert info.extra_run_args == ["--device", "/dev/fuse", "--cap-add", "SYS_ADMIN"]


def test_empty_dockerfile_agent(
    write_dockerfile: Callable[[str], Path], build_svc: BuildService
) -> None:
    p = write_dockerfile("FROM claude-agent\n")
    info = build_svc.parse_dockerfile_agent(p)
    assert info.agent_user == "ubuntu"
    assert info.expose_ports == []
    assert info.extra_run_args == []


def test_parse_nonexistent_file(build_svc: BuildService) -> None:
    with pytest.raises(FileNotFoundError):
        build_svc.parse_dockerfile_agent(Path("/nonexistent/Dockerfile"))


def test_resolve_base_image(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, build_svc: BuildService
) -> None:
    monkeypatch.chdir(tmp_path)
    result = build_svc.resolve_image(use_base=True)
    assert result.image == "claude-agent"
    assert result.dockerfile == tmp_path / "ops" / "Dockerfile"
    assert result.context == tmp_path


def test_resolve_with_dockerfile_agent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, build_svc: BuildService
) -> None:
    monkeypatch.chdir(tmp_path)
    _write_project_dockerfile(tmp_path, "# agent-name: myproj\nFROM claude-agent\n")
    result = build_svc.resolve_image(use_base=False)
    assert result.image == "claude-agent-myproj"
    assert result.dockerfile == tmp_path / AGENT_ASSETS_DIR / AGENT_DOCKERFILE_NAME
    assert result.context == tmp_path


def test_resolve_base_ignores_dockerfile_agent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, build_svc: BuildService
) -> None:
    monkeypatch.chdir(tmp_path)
    _write_project_dockerfile(tmp_path, "# agent-name: myproj\nFROM claude-agent\n")
    result = build_svc.resolve_image(use_base=True)
    assert result.image == "claude-agent"


def test_resolve_no_agent_name_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, build_svc: BuildService
) -> None:
    monkeypatch.chdir(tmp_path)
    _write_project_dockerfile(tmp_path, "FROM claude-agent\n")
    with pytest.raises(SystemExit, match="must contain '# agent-name:"):
        build_svc.resolve_image(use_base=False)


def test_resolve_invalid_agent_name_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, build_svc: BuildService
) -> None:
    monkeypatch.chdir(tmp_path)
    _write_project_dockerfile(tmp_path, "# agent-name: UPPER CASE\nFROM claude-agent\n")
    with pytest.raises(SystemExit, match="must match"):
        build_svc.resolve_image(use_base=False)


def test_resolve_no_dockerfile_uses_base(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, build_svc: BuildService
) -> None:
    monkeypatch.chdir(tmp_path)
    result = build_svc.resolve_image(use_base=False)
    assert result.image == "claude-agent"


# --- locate_dockerfile: the single discovery point -----------------------------------


def test_locate_prefers_the_current_location(tmp_path: Path, build_svc: BuildService) -> None:
    current = _write_project_dockerfile(tmp_path, "# agent-name: new\n")

    location = build_svc.locate_dockerfile(tmp_path)

    assert location.path == current
    assert location.is_legacy is False
    build_svc._display.warning.assert_not_called()  # pyrefly: ignore [missing-attribute]


def test_locate_falls_back_to_the_legacy_location_with_a_warning(
    tmp_path: Path, build_svc: BuildService
) -> None:
    legacy = tmp_path / LEGACY_AGENT_DOCKERFILE_NAME
    legacy.write_text("# agent-name: old\n")

    location = build_svc.locate_dockerfile(tmp_path)

    assert location.path == legacy
    assert location.is_legacy is True
    build_svc._display.warning.assert_called_once_with(  # pyrefly: ignore [missing-attribute]
        f"'{LEGACY_AGENT_DOCKERFILE_NAME}' is deprecated -- move it to "
        f"'{AGENT_ASSETS_DIR}/{AGENT_DOCKERFILE_NAME}'."
    )


def test_locate_refuses_when_both_locations_exist(tmp_path: Path, build_svc: BuildService) -> None:
    _write_project_dockerfile(tmp_path, "# agent-name: new\n")
    (tmp_path / LEGACY_AGENT_DOCKERFILE_NAME).write_text("# agent-name: old\n")

    with pytest.raises(SystemExit, match=f"Delete '{LEGACY_AGENT_DOCKERFILE_NAME}'"):
        build_svc.locate_dockerfile(tmp_path)


def test_locate_reports_nothing_when_the_project_declares_no_dockerfile(
    tmp_path: Path, build_svc: BuildService
) -> None:
    location = build_svc.locate_dockerfile(tmp_path)
    assert location.path is None
    assert location.is_legacy is False


def test_resolve_with_legacy_dockerfile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, build_svc: BuildService
) -> None:
    monkeypatch.chdir(tmp_path)
    legacy = tmp_path / LEGACY_AGENT_DOCKERFILE_NAME
    legacy.write_text("# agent-name: myproj\nFROM claude-agent\n")

    result = build_svc.resolve_image(use_base=False)

    assert result.image == "claude-agent-myproj"
    assert result.dockerfile == legacy
    assert result.agent_name == "myproj"
    assert result.is_legacy is True


def test_resolve_populates_agent_name_and_location(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, build_svc: BuildService
) -> None:
    monkeypatch.chdir(tmp_path)
    _write_project_dockerfile(tmp_path, "# agent-name: myproj\nFROM claude-agent\n")

    result = build_svc.resolve_image(use_base=False)

    assert result.agent_name == "myproj"
    assert result.is_legacy is False


def test_resolve_base_image_carries_no_agent_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, build_svc: BuildService
) -> None:
    """``agent_name is None`` is what keeps ops/Dockerfile from being read for directives."""
    monkeypatch.chdir(tmp_path)
    _write_project_dockerfile(tmp_path, "# agent-name: myproj\nFROM claude-agent\n")

    result = build_svc.resolve_image(use_base=True)

    assert result.agent_name is None
    assert result.dockerfile.name == AGENT_DOCKERFILE_NAME


def test_resolve_base_skips_discovery_entirely(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, build_svc: BuildService
) -> None:
    """--base must not error on a conflict it is about to ignore."""
    monkeypatch.chdir(tmp_path)
    _write_project_dockerfile(tmp_path, "# agent-name: new\n")
    (tmp_path / LEGACY_AGENT_DOCKERFILE_NAME).write_text("# agent-name: old\n")

    assert build_svc.resolve_image(use_base=True).image == "claude-agent"


# --- agent-enable-startup ------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("true", DEFAULT_STARTUP_TIMEOUT_SECONDS),
        ("TRUE", DEFAULT_STARTUP_TIMEOUT_SECONDS),
        ("yes", DEFAULT_STARTUP_TIMEOUT_SECONDS),
        ("on", DEFAULT_STARTUP_TIMEOUT_SECONDS),
        ("false", None),
        ("no", None),
        ("off", None),
        ("30", 30.0),
        ("2.5", 2.5),
        ("1", 1.0),
    ],
)
def test_parse_startup_value_accepted(
    build_svc: BuildService, value: str, expected: float | None
) -> None:
    assert build_svc.parse_startup_value(value) == expected


@pytest.mark.parametrize("value", ["0", "-5", "garbage", "10s", "true-ish"])
def test_parse_startup_value_rejected(build_svc: BuildService, value: str) -> None:
    with pytest.raises(DockerfileDirectiveError, match="agent-enable-startup"):
        build_svc.parse_startup_value(value)


def test_parse_startup_directive_absent_means_off(
    build_svc: BuildService, write_dockerfile: Callable[[str], Path]
) -> None:
    info = build_svc.parse_dockerfile_agent(write_dockerfile("FROM claude-agent\n"))
    assert info.startup_timeout is None


def test_parse_startup_directive_enabled(
    build_svc: BuildService, write_dockerfile: Callable[[str], Path]
) -> None:
    dockerfile = write_dockerfile("FROM claude-agent\n#  agent-enable-startup:  45 \n")
    info = build_svc.parse_dockerfile_agent(dockerfile)
    assert info.startup_timeout == 45.0


def test_parse_startup_directive_rejected_in_legacy_location(
    build_svc: BuildService, write_legacy_dockerfile: Callable[[str], Path]
) -> None:
    """A project on the old path is told to migrate, not silently denied its script."""
    dockerfile = write_legacy_dockerfile("FROM claude-agent\n# agent-enable-startup: true\n")
    with pytest.raises(DockerfileDirectiveError, match=AGENT_DOCKERFILE_NAME):
        build_svc.parse_dockerfile_agent(dockerfile, legacy=True)


def test_parse_other_directives_still_work_in_legacy_location(
    build_svc: BuildService, write_legacy_dockerfile: Callable[[str], Path]
) -> None:
    dockerfile = write_legacy_dockerfile(
        "FROM claude-agent\n# agent-user: dev\n# agent-run-args: --cap-add SYS_ADMIN\n"
    )
    info = build_svc.parse_dockerfile_agent(dockerfile, legacy=True)
    assert info.agent_user == "dev"
    assert info.extra_run_args == ["--cap-add", "SYS_ADMIN"]
