# This file has been created with the assistance of an AI tool.
"""Tests for BuildService.image_cleanup_scope / remove_images — what `agent cleanup` removes."""

import json
from typing import TYPE_CHECKING

import pytest

from agent_wrap.constants import (
    AGENT_ASSETS_DIR,
    BASE_IMAGE_ID_LABEL,
    BASE_IMAGE_NAME,
    BUILD_ITERATION_LABEL,
    DOCKER_BUILD_ITERATION,
    IMAGE_NAME_LABEL,
    LITELLM_IMAGE,
    TELEGRAM_IMAGE,
)
from agent_wrap.domain.build.constants import ImageCleanupReason
from agent_wrap.domain.build.models import ImageCleanupScope, RemovableImage
from agent_wrap.domain.build.service import BuildService
from agent_wrap.domain.display.service import DisplayService
from agent_wrap.domain.updates.service import UpdateService
from agent_wrap.lib.docker_utils import ImageStamp, parse_image_ref

if TYPE_CHECKING:
    from pathlib import Path

    import pytest_mock

BASE_ID = "sha256:aaa"
CURRENT_BASE = ImageStamp(id=BASE_ID, labels={BUILD_ITERATION_LABEL: str(DOCKER_BUILD_ITERATION)})
STALE_BASE = ImageStamp(id=BASE_ID, labels={BUILD_ITERATION_LABEL: "0"})
CURRENT_PROJECT = ImageStamp(id="sha256:ccc", labels={BASE_IMAGE_ID_LABEL: BASE_ID})
FOREIGN_PROJECT = ImageStamp(id="sha256:ccc", labels={BASE_IMAGE_ID_LABEL: "sha256:zzz"})
UNSTAMPED_PROJECT = ImageStamp(id="sha256:ccc", labels={})

# Index of the digest column in a SIDECAR_IMAGE_TEMPLATE row, used to render a listing the
# way docker renders one without `--digests`.
SIDECAR_DIGEST_FIELD = 3

LITELLM_REPO = parse_image_ref(LITELLM_IMAGE).repository
LITELLM_DIGEST = parse_image_ref(LITELLM_IMAGE).digest
TELEGRAM_REPO = parse_image_ref(TELEGRAM_IMAGE).repository
TELEGRAM_DIGEST = parse_image_ref(TELEGRAM_IMAGE).digest


@pytest.fixture
def build_svc(mocker: pytest_mock.MockFixture) -> BuildService:
    """Return a BuildService with spec-mocked dependencies."""
    return BuildService(
        update_service=mocker.Mock(spec=UpdateService),
        display_service=mocker.Mock(spec=DisplayService),
    )


@pytest.fixture
def docker_up(mocker: pytest_mock.MockFixture) -> None:
    """Make the sweep believe the Docker daemon answers."""
    mocker.patch(
        "agent_wrap.domain.build.service.daemon_reachable", autospec=True, return_value=True
    )


def _project(root: Path, name: str, agent_name: str | None) -> Path:
    """
    Create a registered project directory, optionally with an `# agent-name:` Dockerfile.

    A project with no *agent_name* targets the base image, which is how the sweep's
    "declares nothing of its own" row is produced.
    """
    path = root / name
    (path / AGENT_ASSETS_DIR).mkdir(parents=True)
    if agent_name is not None:
        (path / AGENT_ASSETS_DIR / "Dockerfile").write_text(
            f"# agent-name: {agent_name}\nFROM claude-agent\n"
        )
    return path


def _stamps(
    mocker: pytest_mock.MockFixture, base: ImageStamp | None, project: ImageStamp | None
) -> None:
    """Patch ``image_stamp`` to answer per image name, as the build tests do."""

    def _answer(image: str) -> ImageStamp | None:
        return base if image == BASE_IMAGE_NAME else project

    mocker.patch("agent_wrap.domain.build.service.image_stamp", autospec=True, side_effect=_answer)


def _digest_blanked(row: str) -> str:
    """Render *row* as docker does without ``--digests``: the digest column reads `<none>`."""
    fields = row.split("\t")
    fields[SIDECAR_DIGEST_FIELD] = "<none>"
    return "\t".join(fields)


def _docker(
    mocker: pytest_mock.MockFixture,
    *,
    tagged: list[str] | None = None,
    untagged: list[str] | None = None,
    labels: dict[str, dict[str, str]] | None = None,
    sidecars: dict[str, list[str]] | None = None,
) -> None:
    """
    Fake the three ``docker image ls`` listings and the batched inspect behind the sweep.

    *tagged* / *untagged* are rendered rows verbatim, so a test can hand over exactly what
    docker would print. *sidecars* is keyed by repository, matching the per-repository
    listing. *labels* maps a full image id to its label dict, which the inspect renders as
    JSON the way ``image_stamp`` does.

    The per-repository listing models one docker behaviour rather than just replaying rows:
    ``{{.Digest}}`` is populated only under ``--digests``, so a caller that forgets the flag
    is answered with ``<none>`` in that column exactly as docker would answer it.
    """
    tagged_rows = tagged or []
    untagged_rows = untagged or []
    sidecar_rows = sidecars or {}

    def _list(*filters: str, **kwargs: object) -> list[str]:
        if reference := str(kwargs.get("reference") or ""):
            listing = sidecar_rows.get(reference, [])
            if kwargs.get("digests"):
                return listing
            return [_digest_blanked(row) for row in listing]
        if "dangling=true" in filters:
            return untagged_rows
        return tagged_rows

    def _inspect(names: list[str], _template: str) -> list[str]:
        out: list[str] = []
        for name in names:
            full_id = f"sha256:{name}"
            out.append(f"{full_id}\t{json.dumps((labels or {}).get(full_id) or None)}")
        return out

    mocker.patch("agent_wrap.domain.build.service.list_images", side_effect=_list)
    mocker.patch("agent_wrap.domain.build.service.inspect_images", side_effect=_inspect)


def _by_reason(scope: ImageCleanupScope, reason: ImageCleanupReason) -> list[str]:
    """Return the refs the scope holds for one reason, in order."""
    return [image.ref for image in scope.images if image.reason is reason]


def test_image_cleanup_scope_is_empty_when_the_daemon_is_unreachable(
    mocker: pytest_mock.MockFixture, build_svc: BuildService
) -> None:
    """Nothing is provably outdated when nothing can be asked — and no docker call is made."""
    mocker.patch(
        "agent_wrap.domain.build.service.daemon_reachable", autospec=True, return_value=False
    )
    listed = mocker.patch("agent_wrap.domain.build.service.list_images", autospec=True)

    scope = build_svc.image_cleanup_scope([])

    assert scope.is_empty
    assert scope.unattributable == 0
    listed.assert_not_called()


@pytest.mark.usefixtures("docker_up")
def test_untagged_image_with_the_name_label_is_superseded(
    mocker: pytest_mock.MockFixture, build_svc: BuildService
) -> None:
    """An untagged image can never be the live one, so a labelled one is a superseded build."""
    _stamps(mocker, CURRENT_BASE, None)
    _docker(
        mocker,
        untagged=["w01aaaaaaaaa\t1.2GB"],
        labels={"sha256:w01aaaaaaaaa": {IMAGE_NAME_LABEL: "claude-agent-web"}},
    )

    scope = build_svc.image_cleanup_scope([])

    assert _by_reason(scope, ImageCleanupReason.SUPERSEDED) == ["w01aaaaaaaaa"]
    assert scope.images[0].detail == "claude-agent-web"
    assert scope.images[0].size == "1.2GB"


@pytest.mark.usefixtures("docker_up")
def test_superseded_image_is_swept_even_when_its_recorded_tag_is_gone(
    mocker: pytest_mock.MockFixture, build_svc: BuildService
) -> None:
    """
    Requiring the recorded name to still be a live tag would strand images forever: the
    predecessors of a tag an earlier cleanup already removed name nothing that exists.
    """
    _stamps(mocker, CURRENT_BASE, None)
    _docker(
        mocker,
        untagged=["z01aaaaaaaaa\t900MB"],
        labels={"sha256:z01aaaaaaaaa": {IMAGE_NAME_LABEL: "claude-agent-gone"}},
    )

    scope = build_svc.image_cleanup_scope([])

    assert _by_reason(scope, ImageCleanupReason.SUPERSEDED) == ["z01aaaaaaaaa"]


@pytest.mark.usefixtures("docker_up")
def test_untagged_image_on_the_current_iteration_is_still_superseded(
    mocker: pytest_mock.MockFixture, build_svc: BuildService
) -> None:
    """A manual `agent rebuild` leaves a leftover stamped with the *current* iteration."""
    _stamps(mocker, CURRENT_BASE, None)
    _docker(
        mocker,
        untagged=["w01aaaaaaaaa\t1.2GB"],
        labels={
            "sha256:w01aaaaaaaaa": {
                IMAGE_NAME_LABEL: "claude-agent-web",
                BUILD_ITERATION_LABEL: str(DOCKER_BUILD_ITERATION),
            }
        },
    )

    scope = build_svc.image_cleanup_scope([])

    assert _by_reason(scope, ImageCleanupReason.SUPERSEDED) == ["w01aaaaaaaaa"]


@pytest.mark.usefixtures("docker_up")
def test_unlabelled_untagged_image_is_counted_and_never_removed(
    mocker: pytest_mock.MockFixture, build_svc: BuildService
) -> None:
    """A pre-label wrapper build and the user's own leftover are indistinguishable here."""
    _stamps(mocker, CURRENT_BASE, None)
    _docker(
        mocker,
        untagged=["u01aaaaaaaaa\t800MB", "n02aaaaaaaaa\t120MB"],
        labels={"sha256:u01aaaaaaaaa": {}},
    )

    scope = build_svc.image_cleanup_scope([])

    assert scope.is_empty
    assert scope.unattributable == 2


@pytest.mark.usefixtures("docker_up")
def test_unattributable_count_excludes_the_labelled_ones(
    mocker: pytest_mock.MockFixture, build_svc: BuildService
) -> None:
    """The count is the remainder of one partition, not a separate listing."""
    _stamps(mocker, CURRENT_BASE, None)
    _docker(
        mocker,
        untagged=["a01aaaaaaaaa\t1GB", "u01aaaaaaaaa\t800MB"],
        labels={"sha256:a01aaaaaaaaa": {IMAGE_NAME_LABEL: BASE_IMAGE_NAME}},
    )

    scope = build_svc.image_cleanup_scope([])

    assert _by_reason(scope, ImageCleanupReason.SUPERSEDED) == ["a01aaaaaaaaa"]
    assert scope.unattributable == 1


@pytest.mark.usefixtures("docker_up")
def test_project_image_no_registered_project_claims_is_orphaned(
    mocker: pytest_mock.MockFixture, build_svc: BuildService, tmp_path: Path
) -> None:
    """The `gone` project's tag survives its directory; nothing builds it any more."""
    web = _project(tmp_path, "proj-web", "web")
    _stamps(mocker, CURRENT_BASE, CURRENT_PROJECT)
    _docker(
        mocker,
        tagged=[
            f"{BASE_IMAGE_NAME}\tlatest\taaa111111111\t2GB",
            "claude-agent-web\tlatest\tw02aaaaaaaaa\t2.1GB",
            "claude-agent-gone\tlatest\tg01aaaaaaaaa\t2.1GB",
        ],
    )

    scope = build_svc.image_cleanup_scope([web])

    assert _by_reason(scope, ImageCleanupReason.ORPHANED) == ["claude-agent-gone:latest"]


@pytest.mark.usefixtures("docker_up")
def test_project_dir_that_cannot_be_read_leaves_its_image_orphaned(
    mocker: pytest_mock.MockFixture, build_svc: BuildService, tmp_path: Path
) -> None:
    """
    An unreadable project contributes no claimed name, so its image reads as orphaned.

    The documented risk of the sweep, and the reason every row is previewed before the
    prompt: the cost is one rebuild, not lost data.
    """
    _stamps(mocker, CURRENT_BASE, CURRENT_PROJECT)
    _docker(mocker, tagged=["claude-agent-nfs\tlatest\tn01aaaaaaaaa\t2.1GB"])

    scope = build_svc.image_cleanup_scope([tmp_path / "never-existed"])

    assert _by_reason(scope, ImageCleanupReason.ORPHANED) == ["claude-agent-nfs:latest"]


@pytest.mark.usefixtures("docker_up")
def test_stale_project_image_is_removable_with_its_reason(
    mocker: pytest_mock.MockFixture, build_svc: BuildService, tmp_path: Path
) -> None:
    """A claimed image whose base moved would be rebuilt anyway, so removing it defers nothing."""
    api = _project(tmp_path, "proj-api", "api")
    _stamps(mocker, CURRENT_BASE, FOREIGN_PROJECT)
    _docker(mocker, tagged=["claude-agent-api\tlatest\tp01aaaaaaaaa\t2.1GB"])

    scope = build_svc.image_cleanup_scope([api])

    assert _by_reason(scope, ImageCleanupReason.STALE) == ["claude-agent-api:latest"]
    assert "is not the one it was built on" in scope.images[0].detail


@pytest.mark.usefixtures("docker_up")
def test_pre_stamping_project_image_is_found_by_name_not_by_label(
    mocker: pytest_mock.MockFixture, build_svc: BuildService, tmp_path: Path
) -> None:
    """
    Everything built before stamping carries no labels at all, which is exactly why the
    tagged half matches on the repository name rather than filtering on a label.
    """
    legacy = _project(tmp_path, "proj-legacy", "legacy")
    _stamps(mocker, CURRENT_BASE, UNSTAMPED_PROJECT)
    _docker(mocker, tagged=["claude-agent-legacy\tlatest\tl01aaaaaaaaa\t2.1GB"])

    scope = build_svc.image_cleanup_scope([legacy])

    assert _by_reason(scope, ImageCleanupReason.STALE) == ["claude-agent-legacy:latest"]
    assert "before agent-wrap stamped its images" in scope.images[0].detail


@pytest.mark.usefixtures("docker_up")
def test_current_project_image_is_left_alone(
    mocker: pytest_mock.MockFixture, build_svc: BuildService, tmp_path: Path
) -> None:
    """An active tag on the current base is nothing to reclaim."""
    web = _project(tmp_path, "proj-web", "web")
    _stamps(mocker, CURRENT_BASE, CURRENT_PROJECT)
    _docker(mocker, tagged=["claude-agent-web\tlatest\tw02aaaaaaaaa\t2.1GB"])

    assert build_svc.image_cleanup_scope([web]).is_empty


@pytest.mark.usefixtures("docker_up")
def test_orphaned_wins_over_stale_on_the_same_image(
    mocker: pytest_mock.MockFixture, build_svc: BuildService, tmp_path: Path
) -> None:
    """
    Nobody building an image is a stronger statement than it being behind, and calling a
    deleted project's image "stale" would misdescribe why it is going.
    """
    _stamps(mocker, STALE_BASE, FOREIGN_PROJECT)
    _docker(mocker, tagged=["claude-agent-gone\tlatest\tg01aaaaaaaaa\t2.1GB"])

    scope = build_svc.image_cleanup_scope([tmp_path / "gone"])

    assert [image.reason for image in scope.images] == [ImageCleanupReason.ORPHANED]


@pytest.mark.usefixtures("docker_up")
def test_base_image_is_never_removable_even_when_stale(
    mocker: pytest_mock.MockFixture, build_svc: BuildService, tmp_path: Path
) -> None:
    """
    Removing the base would only untag it — every project image descends from it — so it
    reclaims nothing while costing the next launch a cold-scaffold rebuild.
    """
    plain = _project(tmp_path, "proj-plain", None)
    _stamps(mocker, STALE_BASE, CURRENT_PROJECT)
    _docker(mocker, tagged=[f"{BASE_IMAGE_NAME}\tlatest\taaa111111111\t2GB"])

    assert build_svc.image_cleanup_scope([plain]).is_empty


@pytest.mark.usefixtures("docker_up")
def test_project_declaring_no_dockerfile_claims_the_base(
    mocker: pytest_mock.MockFixture, build_svc: BuildService, tmp_path: Path
) -> None:
    """
    Such a project targets the base, so it contributes no `claude-agent-<name>` claim —
    and a stray tag of that shape is still nobody's.
    """
    plain = _project(tmp_path, "proj-plain", None)
    _stamps(mocker, CURRENT_BASE, CURRENT_PROJECT)
    _docker(mocker, tagged=["claude-agent-plain\tlatest\tq01aaaaaaaaa\t2.1GB"])

    scope = build_svc.image_cleanup_scope([plain])

    assert _by_reason(scope, ImageCleanupReason.ORPHANED) == ["claude-agent-plain:latest"]


@pytest.mark.usefixtures("docker_up")
@pytest.mark.parametrize(
    "repository",
    ["ubuntu", "my-claude-agent-thing", "registry.example.com/claude-agent-web", "claudeagent-web"],
    ids=["unrelated", "contains-the-prefix", "registry-qualified", "no-separator"],
)
def test_images_outside_the_wrapper_namespace_are_left_alone(
    mocker: pytest_mock.MockFixture, build_svc: BuildService, repository: str
) -> None:
    """The wrapper builds only `claude-agent` and `claude-agent-<name>`, never into a registry."""
    _stamps(mocker, CURRENT_BASE, None)
    _docker(mocker, tagged=[f"{repository}\tlatest\tub1aaaaaaaaa\t80MB"])

    assert build_svc.image_cleanup_scope([]).is_empty


@pytest.mark.usefixtures("docker_up")
def test_sidecar_image_off_the_pinned_digest_is_superseded(
    mocker: pytest_mock.MockFixture, build_svc: BuildService
) -> None:
    """A pin bump leaves the previously pulled image resident under the same repository."""
    _stamps(mocker, CURRENT_BASE, None)
    _docker(
        mocker,
        sidecars={
            LITELLM_REPO: [f"{LITELLM_REPO}\t<none>\ts01aaaaaaaaa\tsha256:99aa\t1.4GB"],
            TELEGRAM_REPO: [f"{TELEGRAM_REPO}\t0.1.0\tt01aaaaaaaaa\tsha256:11bb\t180MB"],
        },
    )

    scope = build_svc.image_cleanup_scope([])

    assert _by_reason(scope, ImageCleanupReason.SUPERSEDED_SIDECAR) == [
        f"{LITELLM_REPO}@sha256:99aa",
        f"{TELEGRAM_REPO}@sha256:11bb",
    ]


@pytest.mark.usefixtures("docker_up")
def test_sidecar_image_on_the_pinned_digest_is_left_alone(
    mocker: pytest_mock.MockFixture, build_svc: BuildService
) -> None:
    """That is the live sidecar image."""
    _stamps(mocker, CURRENT_BASE, None)
    _docker(
        mocker,
        sidecars={
            LITELLM_REPO: [f"{LITELLM_REPO}\t<none>\ts02aaaaaaaaa\t{LITELLM_DIGEST}\t1.4GB"],
            TELEGRAM_REPO: [f"{TELEGRAM_REPO}\t0.2.0\tt02aaaaaaaaa\t{TELEGRAM_DIGEST}\t180MB"],
        },
    )

    assert build_svc.image_cleanup_scope([]).is_empty


@pytest.mark.usefixtures("docker_up")
def test_sidecar_listing_asks_docker_for_digests(
    mocker: pytest_mock.MockFixture, build_svc: BuildService
) -> None:
    """
    Without `--digests` docker renders `<none>` for every digest, so the sweep would judge
    nothing and silently offer no sidecar image at all.
    """
    _stamps(mocker, CURRENT_BASE, None)
    listed = mocker.patch("agent_wrap.domain.build.service.list_images", return_value=[])

    build_svc.image_cleanup_scope([])

    per_repository = [call for call in listed.call_args_list if call.kwargs.get("reference")]
    assert [call.kwargs["reference"] for call in per_repository] == [LITELLM_REPO, TELEGRAM_REPO]
    assert all(call.kwargs.get("digests") for call in per_repository)


@pytest.mark.usefixtures("docker_up")
@pytest.mark.parametrize("digest", ["<none>", ""], ids=["rendered-none", "empty"])
def test_sidecar_image_with_an_unknown_digest_is_never_guessed_at(
    mocker: pytest_mock.MockFixture, build_svc: BuildService, digest: str
) -> None:
    """The wrapper pulls by digest, so an unknown one means the image came from elsewhere."""
    _stamps(mocker, CURRENT_BASE, None)
    _docker(
        mocker,
        sidecars={LITELLM_REPO: [f"{LITELLM_REPO}\tdev\ts03aaaaaaaaa\t{digest}\t1.4GB"]},
    )

    assert build_svc.image_cleanup_scope([]).is_empty


@pytest.mark.usefixtures("docker_up")
def test_two_projects_on_one_image_yield_one_removal(
    mocker: pytest_mock.MockFixture, build_svc: BuildService, tmp_path: Path
) -> None:
    """The tag is what gets removed, and there is one of it however many projects share it."""
    twin_a = _project(tmp_path, "twin_a", "shared")
    twin_b = _project(tmp_path, "twin_b", "shared")
    _stamps(mocker, CURRENT_BASE, FOREIGN_PROJECT)
    _docker(mocker, tagged=["claude-agent-shared\tlatest\tsh1aaaaaaaaa\t2.1GB"])

    scope = build_svc.image_cleanup_scope([twin_a, twin_b])

    assert _by_reason(scope, ImageCleanupReason.STALE) == ["claude-agent-shared:latest"]


@pytest.mark.usefixtures("docker_up")
def test_malformed_listing_rows_are_dropped_rather_than_raising(
    mocker: pytest_mock.MockFixture, build_svc: BuildService
) -> None:
    """A docker version rendering something unexpected must not abort the whole survey."""
    _stamps(mocker, CURRENT_BASE, None)
    _docker(
        mocker,
        tagged=["claude-agent-gone\tlatest", "claude-agent-gone2\tlatest\tg02aaaaaaaaa\t2GB"],
        untagged=["w01aaaaaaaaa"],
    )

    scope = build_svc.image_cleanup_scope([])

    assert [image.ref for image in scope.images] == ["claude-agent-gone2:latest"]
    assert scope.unattributable == 0


def _removable(ref: str) -> RemovableImage:
    """Return a minimal removable image, enough for the removal loop."""
    return RemovableImage(
        ref=ref,
        display=ref,
        image_id=ref,
        size="1GB",
        reason=ImageCleanupReason.SUPERSEDED,
        detail=BASE_IMAGE_NAME,
    )


def test_remove_images_partitions_refusals_without_stopping(
    mocker: pytest_mock.MockFixture, build_svc: BuildService
) -> None:
    """A refusal is the safety net working, so the run continues and reports it."""

    def _refuse_busy(ref: str) -> bool:
        return ref != "busy"

    mocker.patch(
        "agent_wrap.domain.build.service.remove_image", autospec=True, side_effect=_refuse_busy
    )
    scope = ImageCleanupScope(
        images=[_removable("first"), _removable("busy"), _removable("last")], unattributable=0
    )

    outcome = build_svc.remove_images(scope)

    assert [image.ref for image in outcome.removed] == ["first", "last"]
    assert [image.ref for image in outcome.skipped] == ["busy"]


def test_remove_images_acts_on_exactly_the_surveyed_list(
    mocker: pytest_mock.MockFixture, build_svc: BuildService
) -> None:
    """No re-survey between the preview a user confirmed and the removal that follows."""
    remove = mocker.patch(
        "agent_wrap.domain.build.service.remove_image", autospec=True, return_value=True
    )

    build_svc.remove_images(ImageCleanupScope(images=[_removable("only")], unattributable=0))

    assert [call.args[0] for call in remove.call_args_list] == ["only"]


def test_remove_images_on_an_empty_scope_calls_docker_not_at_all(
    mocker: pytest_mock.MockFixture, build_svc: BuildService
) -> None:
    remove = mocker.patch("agent_wrap.domain.build.service.remove_image", autospec=True)

    outcome = build_svc.remove_images(ImageCleanupScope(images=[], unattributable=3))

    assert outcome.removed == []
    assert outcome.skipped == []
    remove.assert_not_called()
