# This file has been edited with the assistance of an AI tool.
"""Tests for agent_wrap.domain.build.service.BuildService."""

from contextlib import nullcontext
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from agent_wrap.constants import (
    AGENT_ASSETS_DIR,
    AGENT_DOCKERFILE_NAME,
    BASE_IMAGE_ID_LABEL,
    BASE_IMAGE_NAME,
    BUILD_ITERATION_LABEL,
    DOCKER_BUILD_ITERATION,
    IMAGE_NAME_LABEL,
    LEGACY_AGENT_DOCKERFILE_NAME,
    BuildForce,
    UpdateCheck,
)
from agent_wrap.domain.build.constants import (
    BASE_BUILD_CACHE_NOTE,
    BUILD_ITERATION_BUILD_ARG,
    CLAUDE_CACHE_BUST_BUILD_ARG,
    DEFAULT_STARTUP_TIMEOUT_SECONDS,
    PROJECT_BUILD_CACHE_NOTE,
)
from agent_wrap.domain.build.models import ImageStaleness, ResolvedImage
from agent_wrap.domain.build.service import BuildService
from agent_wrap.domain.display.service import DisplayService
from agent_wrap.domain.updates.service import UpdateService
from agent_wrap.exceptions import DockerfileDirectiveError
from agent_wrap.lib.docker_utils import ImageStamp

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


BASE_ID = "sha256:aaa"
NEW_BASE_ID = "sha256:bbb"


@pytest.fixture
def docker_up(mocker: pytest_mock.MockFixture) -> None:
    """Make the build path believe the Docker daemon answers."""
    mocker.patch(
        "agent_wrap.domain.build.service.daemon_reachable", autospec=True, return_value=True
    )


@pytest.fixture
def docker_build(mocker: pytest_mock.MockFixture) -> pytest_mock.MockType:
    """Fake every subprocess the build path shells out to, succeeding."""
    mock_run = mocker.patch("agent_wrap.domain.build.service.subprocess.run")
    mock_run.return_value.returncode = 0
    return mock_run


def _stamps(
    mocker: pytest_mock.MockFixture,
    base: ImageStamp | None,
    project: ImageStamp | None = None,
    *,
    after_base_build: ImageStamp | None = None,
) -> pytest_mock.MockType:
    """
    Patch ``image_stamp`` to answer per image name.

    *after_base_build* is what the base reads as once it has been rebuilt, which is how
    the "stamp the project with the id that was just created" path gets exercised.
    """
    state = {"base": base}

    def _answer(image: str) -> ImageStamp | None:
        if image == BASE_IMAGE_NAME:
            current = state["base"]
            if after_base_build is not None:
                state["base"] = after_base_build
            return current
        return project

    return mocker.patch(
        "agent_wrap.domain.build.service.image_stamp", autospec=True, side_effect=_answer
    )


def _built_images(mock_run: pytest_mock.MockType) -> list[str]:
    """Return the ``-t`` argument of every ``docker build`` the run mock saw, in order."""
    images: list[str] = []
    for call in mock_run.call_args_list:
        argv: list[str] = call[0][0] if call[0] else []
        if isinstance(argv, list) and "build" in argv:
            images.append(argv[argv.index("-t") + 1])
    return images


def _labels_for(mock_run: pytest_mock.MockType, image: str) -> dict[str, str]:
    """Return the ``--label`` pairs the build of *image* was given."""
    for call in mock_run.call_args_list:
        argv: list[str] = call[0][0] if call[0] else []
        if not isinstance(argv, list) or "build" not in argv or argv[argv.index("-t") + 1] != image:
            continue
        return dict(argv[i + 1].split("=", 1) for i, arg in enumerate(argv) if arg == "--label")
    return {}


def _build_args_in(argv: list[str]) -> dict[str, str]:
    """Return the ``--build-arg`` pairs in one ``docker build`` argv."""
    return dict(argv[i + 1].split("=", 1) for i, arg in enumerate(argv) if arg == "--build-arg")


CURRENT_BASE = ImageStamp(id=BASE_ID, labels={BUILD_ITERATION_LABEL: str(DOCKER_BUILD_ITERATION)})
STALE_BASE = ImageStamp(id=BASE_ID, labels={BUILD_ITERATION_LABEL: "0"})
UNSTAMPED_BASE = ImageStamp(id=BASE_ID, labels={})
CURRENT_PROJECT = ImageStamp(id="sha256:ccc", labels={BASE_IMAGE_ID_LABEL: BASE_ID})
FOREIGN_PROJECT = ImageStamp(id="sha256:ccc", labels={BASE_IMAGE_ID_LABEL: "sha256:zzz"})
UNSTAMPED_PROJECT = ImageStamp(id="sha256:ccc", labels={})


@pytest.fixture
def project_resolved(tmp_path: Path) -> ResolvedImage:
    """Return a resolved project image whose Dockerfile inherits from the base."""
    dockerfile = _write_project_dockerfile(tmp_path, "# agent-name: t\nFROM claude-agent\n")
    return ResolvedImage(
        image="claude-agent-t", dockerfile=dockerfile, context=tmp_path, agent_name="t"
    )


@pytest.fixture
def base_resolved(tmp_path: Path) -> ResolvedImage:
    """Return the base image as ``resolve_image`` returns it."""
    return ResolvedImage(
        image=BASE_IMAGE_NAME, dockerfile=tmp_path / "ops" / AGENT_DOCKERFILE_NAME, context=tmp_path
    )


def test_ensure_images_refuses_when_docker_is_down(
    mocker: pytest_mock.MockFixture, build_svc: BuildService, base_resolved: ResolvedImage
) -> None:
    """A build against a dead daemon would fail with docker's error, not a useful one."""
    mocker.patch(
        "agent_wrap.domain.build.service.daemon_reachable", autospec=True, return_value=False
    )
    mock_run = mocker.patch("agent_wrap.domain.build.service.subprocess.run")

    assert build_svc.ensure_images(base_resolved, force=BuildForce.NONE) == 1

    mock_run.assert_not_called()
    message = build_svc._display.error.call_args[0][0]  # pyrefly: ignore [missing-attribute]
    assert "Docker daemon is not reachable" in message


@pytest.mark.parametrize(
    ("base_stamp", "expected"),
    [
        (None, [BASE_IMAGE_NAME]),
        (UNSTAMPED_BASE, [BASE_IMAGE_NAME]),
        (STALE_BASE, [BASE_IMAGE_NAME]),
        (CURRENT_BASE, []),
    ],
    ids=["missing", "unstamped", "iteration-changed", "current"],
)
@pytest.mark.usefixtures("docker_up")
def test_ensure_images_base_only(  # noqa: PLR0913
    mocker: pytest_mock.MockFixture,
    build_svc: BuildService,
    base_resolved: ResolvedImage,
    docker_build: pytest_mock.MockType,
    base_stamp: ImageStamp | None,
    expected: list[str],
) -> None:
    """With no project image in play, only the base's own state decides."""
    _stamps(mocker, base_stamp)

    assert build_svc.ensure_images(base_resolved, force=BuildForce.NONE) == 0
    assert _built_images(docker_build) == expected


@pytest.mark.parametrize(
    ("project_stamp", "expected"),
    [
        (None, ["claude-agent-t"]),
        (UNSTAMPED_PROJECT, ["claude-agent-t"]),
        (FOREIGN_PROJECT, ["claude-agent-t"]),
        (CURRENT_PROJECT, []),
    ],
    ids=["missing", "unstamped", "base-changed", "current"],
)
@pytest.mark.usefixtures("docker_up")
def test_ensure_images_project_against_a_current_base(  # noqa: PLR0913
    mocker: pytest_mock.MockFixture,
    build_svc: BuildService,
    project_resolved: ResolvedImage,
    docker_build: pytest_mock.MockType,
    project_stamp: ImageStamp | None,
    expected: list[str],
) -> None:
    """The base is current, so the project image's recorded base id is the whole question."""
    _stamps(mocker, CURRENT_BASE, project_stamp)

    assert build_svc.ensure_images(project_resolved, force=BuildForce.NONE) == 0
    assert _built_images(docker_build) == expected


@pytest.mark.usefixtures("docker_up")
def test_ensure_images_stale_base_rebuilds_the_project_too(
    mocker: pytest_mock.MockFixture,
    build_svc: BuildService,
    project_resolved: ResolvedImage,
    docker_build: pytest_mock.MockType,
) -> None:
    """A project image is only ever as current as the base it was built on."""
    _stamps(mocker, STALE_BASE, CURRENT_PROJECT, after_base_build=CURRENT_BASE)

    assert build_svc.ensure_images(project_resolved, force=BuildForce.NONE) == 0
    assert _built_images(docker_build) == [BASE_IMAGE_NAME, "claude-agent-t"]


@pytest.mark.usefixtures("docker_up")
def test_ensure_images_stamps_the_project_with_the_rebuilt_base_id(
    mocker: pytest_mock.MockFixture,
    build_svc: BuildService,
    project_resolved: ResolvedImage,
    docker_build: pytest_mock.MockType,
) -> None:
    """Recording the pre-rebuild id would leave the project image stale forever."""
    rebuilt = ImageStamp(
        id=NEW_BASE_ID, labels={BUILD_ITERATION_LABEL: str(DOCKER_BUILD_ITERATION)}
    )
    _stamps(mocker, STALE_BASE, CURRENT_PROJECT, after_base_build=rebuilt)

    build_svc.ensure_images(project_resolved, force=BuildForce.NONE)

    assert _labels_for(docker_build, "claude-agent-t")[BASE_IMAGE_ID_LABEL] == NEW_BASE_ID
    assert _labels_for(docker_build, BASE_IMAGE_NAME) == {
        BUILD_ITERATION_LABEL: str(DOCKER_BUILD_ITERATION),
        IMAGE_NAME_LABEL: BASE_IMAGE_NAME,
    }


@pytest.mark.usefixtures("docker_up")
def test_ensure_images_stamps_each_image_with_its_own_name(
    mocker: pytest_mock.MockFixture,
    build_svc: BuildService,
    project_resolved: ResolvedImage,
    docker_build: pytest_mock.MockType,
) -> None:
    """
    Every build records the tag it was built as, which is the only handle a superseded
    image keeps: docker takes the repository away with the tag, so `agent cleanup` has
    nothing else to match an untagged leftover on.
    """
    _stamps(mocker, STALE_BASE, CURRENT_PROJECT)

    build_svc.ensure_images(project_resolved, force=BuildForce.NONE)

    assert _labels_for(docker_build, BASE_IMAGE_NAME)[IMAGE_NAME_LABEL] == BASE_IMAGE_NAME
    assert _labels_for(docker_build, "claude-agent-t")[IMAGE_NAME_LABEL] == "claude-agent-t"


@pytest.mark.usefixtures("docker_up")
def test_ensure_images_stops_when_the_base_build_fails(
    mocker: pytest_mock.MockFixture,
    build_svc: BuildService,
    project_resolved: ResolvedImage,
) -> None:
    """A project image must never be built on a base that is absent or known to be wrong."""
    mock_run = mocker.patch("agent_wrap.domain.build.service.subprocess.run")
    mock_run.return_value.returncode = 1
    _stamps(mocker, None, CURRENT_PROJECT)

    assert build_svc.ensure_images(project_resolved, force=BuildForce.NONE) == 1
    assert _built_images(mock_run) == [BASE_IMAGE_NAME]


@pytest.mark.parametrize(
    ("force", "expected"),
    [
        (BuildForce.NONE, []),
        (BuildForce.PROJECT, ["claude-agent-t"]),
        (BuildForce.ALL, [BASE_IMAGE_NAME, "claude-agent-t"]),
    ],
    ids=["none", "project", "all"],
)
@pytest.mark.usefixtures("docker_up")
def test_ensure_images_honours_force(  # noqa: PLR0913
    mocker: pytest_mock.MockFixture,
    build_svc: BuildService,
    project_resolved: ResolvedImage,
    docker_build: pytest_mock.MockType,
    force: BuildForce,
    expected: list[str],
) -> None:
    """Everything is current, so only the caller's insistence produces a build."""
    _stamps(mocker, CURRENT_BASE, CURRENT_PROJECT)

    assert build_svc.ensure_images(project_resolved, force=force) == 0
    assert _built_images(docker_build) == expected


@pytest.mark.usefixtures("docker_up")
def test_ensure_images_forces_the_base_when_it_is_the_only_image(
    mocker: pytest_mock.MockFixture,
    build_svc: BuildService,
    base_resolved: ResolvedImage,
    docker_build: pytest_mock.MockType,
) -> None:
    """`agent rebuild` in a project with no Dockerfile can only have meant the base."""
    _stamps(mocker, CURRENT_BASE, after_base_build=CURRENT_BASE)

    assert build_svc.ensure_images(base_resolved, force=BuildForce.PROJECT) == 0
    assert _built_images(docker_build) == [BASE_IMAGE_NAME]


@pytest.mark.usefixtures("docker_up")
def test_ensure_images_explains_an_automatic_rebuild(
    mocker: pytest_mock.MockFixture,
    build_svc: BuildService,
    base_resolved: ResolvedImage,
    docker_build: pytest_mock.MockType,
) -> None:
    """An auto-build spends wall clock the user did not ask for; it has to say why."""
    _stamps(mocker, STALE_BASE)

    build_svc.ensure_images(base_resolved, force=BuildForce.NONE)

    assert _built_images(docker_build) == [BASE_IMAGE_NAME]
    reason = build_svc._display.info.call_args[0][0]  # pyrefly: ignore [missing-attribute]
    assert "build iteration" in reason
    assert BASE_BUILD_CACHE_NOTE in reason


@pytest.mark.usefixtures("docker_up")
def test_ensure_images_explains_an_automatic_project_rebuild(
    mocker: pytest_mock.MockFixture,
    build_svc: BuildService,
    project_resolved: ResolvedImage,
    docker_build: pytest_mock.MockType,
) -> None:
    """A project image is still uncached, so it must not inherit the base's cheaper note."""
    _stamps(mocker, CURRENT_BASE, FOREIGN_PROJECT)

    build_svc.ensure_images(project_resolved, force=BuildForce.NONE)

    assert _built_images(docker_build) == ["claude-agent-t"]
    reason = build_svc._display.info.call_args[0][0]  # pyrefly: ignore [missing-attribute]
    assert PROJECT_BUILD_CACHE_NOTE in reason
    assert "--no-cache" in reason


@pytest.mark.usefixtures("docker_up")
def test_ensure_images_states_no_reason_for_a_forced_rebuild(
    mocker: pytest_mock.MockFixture,
    build_svc: BuildService,
    base_resolved: ResolvedImage,
    docker_build: pytest_mock.MockType,
) -> None:
    """`agent rebuild` was the user's own idea and needs no justification."""
    _stamps(mocker, CURRENT_BASE)

    build_svc.ensure_images(base_resolved, force=BuildForce.ALL)

    assert _built_images(docker_build) == [BASE_IMAGE_NAME]
    build_svc._display.info.assert_not_called()  # pyrefly: ignore [missing-attribute]


@pytest.mark.usefixtures("docker_up")
def test_ensure_images_waits_for_a_concurrent_build(
    mocker: pytest_mock.MockFixture,
    build_svc: BuildService,
    base_resolved: ResolvedImage,
    docker_build: pytest_mock.MockType,
) -> None:
    """A launcher queued behind another build must say so rather than hang silently."""
    mocker.patch(
        "agent_wrap.domain.build.service.try_file_lock",
        autospec=True,
        return_value=nullcontext(enter_result=False),
    )
    _stamps(mocker, CURRENT_BASE)

    assert build_svc.ensure_images(base_resolved, force=BuildForce.NONE) == 0

    # Nothing to do once the lock is free -- the holder built it while this one waited.
    assert _built_images(docker_build) == []
    build_svc._display.info.assert_called_once()  # pyrefly: ignore [missing-attribute]
    message = build_svc._display.info.call_args[0][0]  # pyrefly: ignore [missing-attribute]
    assert "waiting for another agent-wrap image build" in message


@pytest.mark.parametrize(
    ("base_stamp", "project_stamp", "expected_base", "expected_project"),
    [
        (CURRENT_BASE, CURRENT_PROJECT, "", ""),
        (STALE_BASE, CURRENT_PROJECT, "build iteration", "is not the one it was built on"),
        (CURRENT_BASE, None, "", "not built on this host"),
    ],
    ids=["both-current", "stale-base", "missing-project"],
)
@pytest.mark.usefixtures("docker_up")
def test_stale_summary_reports_without_building(  # noqa: PLR0913
    mocker: pytest_mock.MockFixture,
    build_svc: BuildService,
    project_resolved: ResolvedImage,
    base_stamp: ImageStamp,
    project_stamp: ImageStamp | None,
    expected_base: str,
    expected_project: str,
) -> None:
    mock_run = mocker.patch("agent_wrap.domain.build.service.subprocess.run")
    _stamps(mocker, base_stamp, project_stamp)

    summary = build_svc.stale_summary(project_resolved)

    mock_run.assert_not_called()
    assert expected_base in summary.base
    assert bool(summary.base) is bool(expected_base)
    assert expected_project in summary.project
    assert bool(summary.project) is bool(expected_project)


def test_stale_summary_is_silent_when_docker_is_down(
    mocker: pytest_mock.MockFixture, build_svc: BuildService, project_resolved: ResolvedImage
) -> None:
    """An unreachable daemon is not evidence that anything is stale."""
    mocker.patch(
        "agent_wrap.domain.build.service.daemon_reachable", autospec=True, return_value=False
    )

    assert build_svc.stale_summary(project_resolved) == ImageStaleness(base="", project="")


@pytest.mark.parametrize(
    "content",
    [
        "# agent-name: t\nFROM ubuntu:24.04\n",
        "# agent-name: t\nFROM claude-agent AS base\nFROM ubuntu:24.04\n",
    ],
    ids=["single-stage", "multi-stage-last-foreign"],
)
def test_resolve_rejects_a_foreign_final_from(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    build_svc: BuildService,
    content: str,
) -> None:
    """The last FROM is what the tag contains, so it is the one that must be the base."""
    monkeypatch.chdir(tmp_path)
    _write_project_dockerfile(tmp_path, content)

    with pytest.raises(SystemExit, match="must inherit from the wrapper's base image"):
        build_svc.resolve_image(use_base=False)


def test_resolve_rejects_a_dockerfile_with_no_from(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, build_svc: BuildService
) -> None:
    monkeypatch.chdir(tmp_path)
    _write_project_dockerfile(tmp_path, "# agent-name: t\n")

    with pytest.raises(SystemExit, match="must contain a 'FROM claude-agent' line"):
        build_svc.resolve_image(use_base=False)


@pytest.mark.parametrize(
    "content",
    [
        "# agent-name: t\nFROM claude-agent\n",
        "# agent-name: t\nFROM claude-agent:latest\n",
        "# agent-name: t\nFROM node:20 AS builder\nRUN npm install\nFROM claude-agent\n",
    ],
    ids=["plain", "tagged", "multi-stage-last-base"],
)
def test_resolve_accepts_a_base_final_from(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, build_svc: BuildService, content: str
) -> None:
    """Earlier stages produce throwaway artifacts and may use any image at all."""
    monkeypatch.chdir(tmp_path)
    _write_project_dockerfile(tmp_path, content)

    assert build_svc.resolve_image(use_base=False).image == "claude-agent-t"


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


@pytest.mark.usefixtures("docker_up")
def test_do_rebuild_propagates_a_build_failure(
    mocker: pytest_mock.MockFixture,
    build_svc: BuildService,
    project_resolved: ResolvedImage,
) -> None:
    mocker.patch.object(BuildService, "resolve_image", autospec=True, return_value=project_resolved)
    mock_run = mocker.patch("agent_wrap.domain.build.service.subprocess.run")
    mock_run.return_value.returncode = 1
    _stamps(mocker, CURRENT_BASE, CURRENT_PROJECT)

    assert build_svc._do_rebuild(full=False) == 1
    assert _built_images(mock_run) == ["claude-agent-t"]


@pytest.mark.usefixtures("docker_up")
def test_do_rebuild_full_builds_base_then_project(
    mocker: pytest_mock.MockFixture,
    build_svc: BuildService,
    project_resolved: ResolvedImage,
    docker_build: pytest_mock.MockType,
) -> None:
    mocker.patch.object(BuildService, "resolve_image", autospec=True, return_value=project_resolved)
    _stamps(mocker, CURRENT_BASE, CURRENT_PROJECT, after_base_build=CURRENT_BASE)

    assert build_svc._do_rebuild(full=True) == 0
    assert _built_images(docker_build) == [BASE_IMAGE_NAME, "claude-agent-t"]
    # Base build + project build + the closing `docker images` listing.
    assert docker_build.call_count == 3


@pytest.mark.usefixtures("docker_up")
def test_do_rebuild_full_says_the_base_was_the_only_build_needed(
    mocker: pytest_mock.MockFixture,
    build_svc: BuildService,
    base_resolved: ResolvedImage,
    docker_build: pytest_mock.MockType,
) -> None:
    mocker.patch.object(BuildService, "resolve_image", autospec=True, return_value=base_resolved)
    _stamps(mocker, CURRENT_BASE, after_base_build=CURRENT_BASE)

    assert build_svc._do_rebuild(full=True) == 0
    assert _built_images(docker_build) == [BASE_IMAGE_NAME]
    message = build_svc._display.success.call_args[0][0]  # pyrefly: ignore [missing-attribute]
    assert "base build is the only build needed" in message


def test_docker_build_returns_exit_code(
    build_svc: BuildService, tmp_path: Path, mocker: pytest_mock.MockFixture
) -> None:
    mock_run = mocker.patch("agent_wrap.domain.build.service.subprocess.run")
    mock_run.return_value.returncode = 0
    rc = build_svc._docker_build(tmp_path / "Dockerfile", "test-img", tmp_path, labels={})
    assert rc == 0


def test_docker_build_failure(
    build_svc: BuildService, tmp_path: Path, mocker: pytest_mock.MockFixture
) -> None:
    mock_run = mocker.patch("agent_wrap.domain.build.service.subprocess.run")
    mock_run.return_value.returncode = 1
    rc = build_svc._docker_build(tmp_path / "Dockerfile", "test-img", tmp_path, labels={})
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
    build_svc._docker_build(tmp_path / "Dockerfile", "test-img", tmp_path, labels={})
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
    build_svc._docker_build(tmp_path / "Dockerfile", "test-img", tmp_path, labels={})
    argv = mock_run.call_args[0][0]
    assert "SPELLCHECK_LANG=en_US,ru_RU" in argv
    assert argv[argv.index("SPELLCHECK_LANG=en_US,ru_RU") - 1] == "--build-arg"


def test_docker_build_caches_the_base(
    build_svc: BuildService, tmp_path: Path, mocker: pytest_mock.MockFixture
) -> None:
    # The whole point of the two-stage ops/Dockerfile: the scaffold must be allowed to
    # come from the layer cache, steered by the iteration rather than by --no-cache.
    mock_run = mocker.patch("agent_wrap.domain.build.service.subprocess.run")
    mock_run.return_value.returncode = 0
    mocker.patch("agent_wrap.domain.build.service.generate_uuid", autospec=True, return_value="tok")

    build_svc._docker_build(tmp_path / "Dockerfile", BASE_IMAGE_NAME, tmp_path, labels={})

    argv = mock_run.call_args[0][0]
    assert "--no-cache" not in argv
    build_args = _build_args_in(argv)
    assert build_args[BUILD_ITERATION_BUILD_ARG] == str(DOCKER_BUILD_ITERATION)
    assert build_args[CLAUDE_CACHE_BUST_BUILD_ARG] == "tok"


def test_docker_build_never_caches_a_project_image(
    build_svc: BuildService, tmp_path: Path, mocker: pytest_mock.MockFixture
) -> None:
    # A project Dockerfile is under no such contract, and nothing hashes it: --no-cache is
    # the only thing that makes `agent rebuild` apply an edit the user just made.
    mock_run = mocker.patch("agent_wrap.domain.build.service.subprocess.run")
    mock_run.return_value.returncode = 0

    build_svc._docker_build(tmp_path / "Dockerfile", "claude-agent-t", tmp_path, labels={})

    argv = mock_run.call_args[0][0]
    assert "--no-cache" in argv
    build_args = _build_args_in(argv)
    assert BUILD_ITERATION_BUILD_ARG not in build_args
    assert CLAUDE_CACHE_BUST_BUILD_ARG not in build_args


def test_docker_build_token_differs_between_base_builds(
    build_svc: BuildService, tmp_path: Path, mocker: pytest_mock.MockFixture
) -> None:
    # This is the assertion that actually protects the goal: a token that repeated would
    # let the CLI layer cache, and a base rebuild would stop picking up the day's release.
    mock_run = mocker.patch("agent_wrap.domain.build.service.subprocess.run")
    mock_run.return_value.returncode = 0

    build_svc._docker_build(tmp_path / "Dockerfile", BASE_IMAGE_NAME, tmp_path, labels={})
    build_svc._docker_build(tmp_path / "Dockerfile", BASE_IMAGE_NAME, tmp_path, labels={})

    tokens = [
        _build_args_in(call[0][0])[CLAUDE_CACHE_BUST_BUILD_ARG] for call in mock_run.call_args_list
    ]
    assert tokens[0] != tokens[1]


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
    build_svc._docker_build(tmp_path / "Dockerfile", "test-img", tmp_path, labels={})
    assert "--network" not in mock_run.call_args[0][0]


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


@pytest.fixture
def sweep_projects(tmp_path: Path) -> dict[str, Path]:
    """
    Build a small fleet of registered project directories under one root.

    ``twin_a``/``twin_b`` deliberately declare the same ``# agent-name:``, which is what
    makes them one image and two rows. ``plain`` customizes nothing, ``broken`` declares a
    Dockerfile that cannot be resolved, and ``gone`` never existed on disk at all.
    """
    dirs = {name: tmp_path / name for name in ("twin_a", "twin_b", "plain", "broken")}
    for path in dirs.values():
        path.mkdir()
    _write_project_dockerfile(dirs["twin_a"], "# agent-name: t\nFROM claude-agent\n")
    _write_project_dockerfile(dirs["twin_b"], "# agent-name: t\nFROM claude-agent\n")
    _write_project_dockerfile(dirs["broken"], "FROM claude-agent\n")
    dirs["gone"] = tmp_path / "gone"
    return dirs


@pytest.mark.usefixtures("docker_up")
def test_stale_project_images_reports_one_row_per_project_sharing_an_image(
    mocker: pytest_mock.MockFixture, build_svc: BuildService, sweep_projects: dict[str, Path]
) -> None:
    """Two projects on one `# agent-name:` are two rows, but only one docker inspect."""
    mock_run = mocker.patch("agent_wrap.domain.build.service.subprocess.run")
    stamps = _stamps(mocker, CURRENT_BASE, FOREIGN_PROJECT)

    rows = build_svc.stale_project_images([sweep_projects["twin_a"], sweep_projects["twin_b"]])

    mock_run.assert_not_called()
    assert [(row.project, row.image) for row in rows] == [
        (sweep_projects["twin_a"], "claude-agent-t"),
        (sweep_projects["twin_b"], "claude-agent-t"),
    ]
    assert all("is not the one it was built on" in row.reason for row in rows)
    assert [call.args[0] for call in stamps.call_args_list].count("claude-agent-t") == 1


@pytest.mark.usefixtures("docker_up")
def test_stale_project_images_omits_an_image_that_is_not_built(
    mocker: pytest_mock.MockFixture, build_svc: BuildService, sweep_projects: dict[str, Path]
) -> None:
    """Nothing is stale about an image that does not exist -- its launch builds it."""
    _stamps(mocker, CURRENT_BASE, None)
    assert build_svc.stale_project_images([sweep_projects["twin_a"]]) == []


@pytest.mark.usefixtures("docker_up")
def test_stale_project_images_omits_a_current_image(
    mocker: pytest_mock.MockFixture, build_svc: BuildService, sweep_projects: dict[str, Path]
) -> None:
    _stamps(mocker, CURRENT_BASE, CURRENT_PROJECT)
    assert build_svc.stale_project_images([sweep_projects["twin_a"]]) == []


@pytest.mark.usefixtures("docker_up")
def test_stale_project_images_short_circuits_on_a_stale_base(
    mocker: pytest_mock.MockFixture, build_svc: BuildService, sweep_projects: dict[str, Path]
) -> None:
    """A moved base condemns every project image, and no per-image inspect can change it."""
    stamps = _stamps(mocker, STALE_BASE, CURRENT_PROJECT)

    rows = build_svc.stale_project_images([sweep_projects["twin_a"], sweep_projects["twin_b"]])

    assert [row.image for row in rows] == ["claude-agent-t", "claude-agent-t"]
    assert all("is not the one it was built on" in row.reason for row in rows)
    assert [call.args[0] for call in stamps.call_args_list] == [BASE_IMAGE_NAME]


@pytest.mark.usefixtures("docker_up")
def test_stale_project_images_reports_an_unstamped_image(
    mocker: pytest_mock.MockFixture, build_svc: BuildService, sweep_projects: dict[str, Path]
) -> None:
    """Built before stamping, so it cannot be checked -- a one-time rebuild is due."""
    _stamps(mocker, CURRENT_BASE, UNSTAMPED_PROJECT)
    rows = build_svc.stale_project_images([sweep_projects["twin_a"]])
    assert len(rows) == 1
    assert "before agent-wrap stamped its images" in rows[0].reason


@pytest.mark.parametrize("project", ["plain", "broken", "gone"])
@pytest.mark.usefixtures("docker_up")
def test_stale_project_images_skips_what_it_cannot_answer_for(
    mocker: pytest_mock.MockFixture,
    build_svc: BuildService,
    sweep_projects: dict[str, Path],
    project: str,
) -> None:
    """
    No Dockerfile, an unresolvable one, and a vanished directory are all skipped.

    The base image's own staleness is reported once elsewhere, and neither a foreign
    project's broken Dockerfile nor a path this host cannot see is answerable from here --
    all three would otherwise be reported against the base image by accident.
    """
    _stamps(mocker, STALE_BASE, FOREIGN_PROJECT)
    assert build_svc.stale_project_images([sweep_projects[project]]) == []


@pytest.mark.usefixtures("docker_up")
def test_stale_project_images_does_not_warn_about_a_legacy_dockerfile(
    mocker: pytest_mock.MockFixture, build_svc: BuildService, tmp_path: Path
) -> None:
    """The deprecation notice belongs to that project's own launch, not to this sweep."""
    project = tmp_path / "legacy"
    project.mkdir()
    (project / LEGACY_AGENT_DOCKERFILE_NAME).write_text("# agent-name: l\nFROM claude-agent\n")
    _stamps(mocker, CURRENT_BASE, FOREIGN_PROJECT)

    rows = build_svc.stale_project_images([project])

    assert [row.image for row in rows] == ["claude-agent-l"]
    build_svc._display.warning.assert_not_called()  # pyrefly: ignore [missing-attribute]


def test_stale_project_images_is_silent_when_docker_is_down(
    mocker: pytest_mock.MockFixture, build_svc: BuildService, sweep_projects: dict[str, Path]
) -> None:
    """An unreachable daemon is not evidence that anything is stale."""
    mocker.patch(
        "agent_wrap.domain.build.service.daemon_reachable", autospec=True, return_value=False
    )
    stamps = _stamps(mocker, STALE_BASE, FOREIGN_PROJECT)

    assert build_svc.stale_project_images([sweep_projects["twin_a"]]) == []
    stamps.assert_not_called()
