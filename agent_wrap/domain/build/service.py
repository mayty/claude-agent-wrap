# This file has been edited with the assistance of an AI tool.
import json
import os
import re
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

from agent_wrap.constants import (
    AGENT_ASSETS_DIR,
    AGENT_DOCKERFILE_NAME,
    AGENT_LAUNCHES_DIR,
    BASE_IMAGE_ID_LABEL,
    BASE_IMAGE_NAME,
    BUILD_ITERATION_LABEL,
    DOCKER_BUILD_ITERATION,
    IMAGE_NAME_LABEL,
    LEGACY_AGENT_DOCKERFILE_NAME,
    OPS_DIR,
    SPELLCHECK_BUILD_ARG,
    SPELLCHECK_LANG,
    TOOL_DIR,
    BuildForce,
    UpdateCheck,
)
from agent_wrap.domain.build.constants import (
    BASE_BUILD_CACHE_NOTE,
    BASE_FROM_RE,
    BUILD_ITERATION_BUILD_ARG,
    BUILD_LOCK_NAME,
    BUILD_REASON_TEXT,
    CLAUDE_CACHE_BUST_BUILD_ARG,
    DEFAULT_STARTUP_TIMEOUT_SECONDS,
    DOCKER_NONE,
    FROM_RE,
    PINNED_SIDECAR_IMAGES,
    PROJECT_BUILD_CACHE_NOTE,
    SHORT_IMAGE_ID_LEN,
    SIDECAR_IMAGE_FIELDS,
    SIDECAR_IMAGE_TEMPLATE,
    STARTUP_FALSY_WORDS,
    STARTUP_TRUTHY_WORDS,
    TAGGED_IMAGE_FIELDS,
    TAGGED_IMAGE_TEMPLATE,
    UNTAGGED_IMAGE_FIELDS,
    UNTAGGED_IMAGE_TEMPLATE,
    WRAPPER_IMAGE_PREFIX,
    BuildReason,
    ImageCleanupReason,
)
from agent_wrap.domain.build.models import (
    DockerfileAgentInfo,
    DockerfileLocation,
    ImageCleanupOutcome,
    ImageCleanupScope,
    ImageStaleness,
    ProjectImageVerdict,
    RemovableImage,
    ResolvedImage,
    StaleProjectImage,
)
from agent_wrap.exceptions import DockerfileDirectiveError
from agent_wrap.lib.docker_utils import (
    ImageStamp,
    daemon_reachable,
    host_network_build_args,
    image_stamp,
    inspect_images,
    list_images,
    parse_image_ref,
    remove_image,
)
from agent_wrap.lib.flock import file_lock, try_file_lock
from agent_wrap.lib.utils import generate_uuid

if TYPE_CHECKING:
    from agent_wrap.domain.display.service import DisplayService
    from agent_wrap.domain.updates.service import UpdateService


class BuildService:
    def __init__(self, update_service: UpdateService, display_service: DisplayService) -> None:
        self._updates = update_service
        self._display = display_service

    def rebuild(self, *, full: bool) -> int:
        outcome = self._updates.check_updates()
        if outcome is UpdateCheck.BLOCKED:
            return 1
        if outcome is UpdateCheck.HANDLED:
            return 0
        return self._do_rebuild(full=full)

    def _docker_build(
        self, dockerfile: Path, image: str, context: Path, *, labels: dict[str, str]
    ) -> int:
        """
        Run a docker build and return the exit code.

        *labels* are stamped with ``--label`` rather than a ``LABEL`` instruction, so no
        project Dockerfile has to cooperate to be trackable.

        Only the base build uses docker's layer cache, and the two cache build args are
        base-only for that reason. ``BUILD_ITERATION`` invalidates ``ops/Dockerfile``'s
        expensive ``scaffold`` stage when the recipe moves; a per-build
        ``CLAUDE_CACHE_BUST`` invalidates the final stage every time, so a base build
        always lands the day's Claude Code release without re-running apt. A project
        Dockerfile is somebody else's file under no such contract, and ``agent rebuild``
        exists to apply edits nothing hashes -- so it keeps ``--no-cache``.
        """
        label_args: list[str] = []
        for key, value in labels.items():
            label_args.extend(["--label", f"{key}={value}"])
        cache_args = (
            [
                "--build-arg",
                f"{BUILD_ITERATION_BUILD_ARG}={DOCKER_BUILD_ITERATION}",
                "--build-arg",
                f"{CLAUDE_CACHE_BUST_BUILD_ARG}={generate_uuid()}",
            ]
            if image == BASE_IMAGE_NAME
            else ["--no-cache"]
        )
        result = subprocess.run(
            [
                "docker",
                "build",
                *cache_args,
                *host_network_build_args(),
                "--build-arg",
                f"HOST_UID={os.getuid()}",
                "--build-arg",
                f"HOST_GID={os.getgid()}",
                "--build-arg",
                f"{SPELLCHECK_BUILD_ARG}={SPELLCHECK_LANG}",
                *label_args,
                "-f",
                str(dockerfile),
                "-t",
                image,
                str(context),
            ]
        )
        return result.returncode

    def parse_startup_value(self, raw: str) -> float | None:
        """
        Resolve an ``# agent-enable-startup:`` value to a timeout, or None when off.

        A boolean word selects ``DEFAULT_STARTUP_TIMEOUT_SECONDS``. Numbers are *always*
        seconds, so ``1`` means one second rather than "true" -- the shell validator
        warns about that spelling instead of this parser guessing which was meant.
        """
        value = raw.strip().lower()
        if value in STARTUP_TRUTHY_WORDS:
            return DEFAULT_STARTUP_TIMEOUT_SECONDS
        if value in STARTUP_FALSY_WORDS:
            return None
        try:
            seconds = float(value)
        except ValueError:
            seconds = 0.0
        if seconds > 0:
            return seconds
        msg = (
            f"agent-enable-startup value {raw!r} is not understood. Use "
            f"'true'/'false' or a positive number of seconds (0 is not a timeout -- "
            f"write 'false' to disable)."
        )
        raise DockerfileDirectiveError(msg)

    def parse_dockerfile_agent(
        self, dockerfile_path: Path, *, legacy: bool = False
    ) -> DockerfileAgentInfo:
        """
        Extract the ``# agent-*`` and EXPOSE directives from a project Dockerfile.

        *legacy* marks the deprecated ``Dockerfile.agent`` path, which is not allowed to
        enable startup scripts.
        """
        info = DockerfileAgentInfo()

        with open(dockerfile_path) as f:
            for raw_line in f:
                line = raw_line.strip()

                if match := re.match(r"^#\s*agent-user:\s*(\S+)", line):
                    info.agent_user = match.group(1)

                elif match := re.match(r"^#\s*agent-run-args:\s*(.+)", line):
                    info.extra_run_args.extend(match.group(1).split())

                # New location only: a project still on the deprecated path is told to
                # migrate rather than having its startup script silently ignored.
                elif match := re.match(r"^#\s*agent-enable-startup:\s*(\S+)", line):
                    if legacy:
                        msg = (
                            f"'# agent-enable-startup:' is only supported in "
                            f"'{AGENT_ASSETS_DIR}/{AGENT_DOCKERFILE_NAME}'. Move "
                            f"'{LEGACY_AGENT_DOCKERFILE_NAME}' there to use a startup script."
                        )
                        raise DockerfileDirectiveError(msg)
                    info.startup_timeout = self.parse_startup_value(match.group(1))

                elif match := re.match(r"^[Ee][Xx][Pp][Oo][Ss][Ee]\s+(.+)", line):
                    for token in match.group(1).split():
                        port = token.split("/")[0]
                        info.expose_ports.append(port)

        return info

    def locate_dockerfile(self, cwd: Path, *, warn: bool = True) -> DockerfileLocation:
        """
        Find the project Dockerfile, preferring the current location over the legacy one.

        The single discovery point for the whole wrapper, so the two paths cannot drift
        apart. Both locations populated is a ``SystemExit``: there is no way to tell which
        one the author meant.

        *warn* off silences the legacy-location deprecation notice for a caller sweeping
        many projects at once -- one copy per registered project would bury the report it
        is printed alongside.
        """
        current = cwd / AGENT_ASSETS_DIR / AGENT_DOCKERFILE_NAME
        legacy = cwd / LEGACY_AGENT_DOCKERFILE_NAME

        if current.is_file() and legacy.is_file():
            msg = (
                f"both '{AGENT_ASSETS_DIR}/{AGENT_DOCKERFILE_NAME}' and legacy "
                f"'{LEGACY_AGENT_DOCKERFILE_NAME}' exist in {cwd}. Delete "
                f"'{LEGACY_AGENT_DOCKERFILE_NAME}'."
            )
            raise SystemExit(msg)

        if current.is_file():
            return DockerfileLocation(path=current, is_legacy=False)

        if legacy.is_file():
            if warn:
                self._display.warning(
                    f"'{LEGACY_AGENT_DOCKERFILE_NAME}' is deprecated -- move it to "
                    f"'{AGENT_ASSETS_DIR}/{AGENT_DOCKERFILE_NAME}'."
                )
            return DockerfileLocation(path=legacy, is_legacy=True)

        return DockerfileLocation(path=None, is_legacy=False)

    def resolve_image(
        self, *, use_base: bool = False, project_dir: Path | None = None, warn: bool = True
    ) -> ResolvedImage:
        """
        Determine which Docker image to use, its Dockerfile, and build context.

        Also the validation point for a project Dockerfile's identity: both the
        ``# agent-name:`` directive and the mandatory ``FROM claude-agent`` are checked
        here, so launch, rebuild and the read-only ``agent inspect`` all get the same
        verdict from one file read. A Dockerfile failing either is a ``SystemExit``.

        *project_dir* is the cwd when None; only the fleet-wide sweep passes one, since
        everything else asks about where it already stands.
        """
        cwd = Path.cwd() if project_dir is None else project_dir
        location = (
            DockerfileLocation(path=None, is_legacy=False)
            if use_base
            else self.locate_dockerfile(cwd, warn=warn)
        )

        if location.path is not None:
            with open(location.path) as f:
                lines = f.readlines()

            for line in lines:
                if match := re.match(r"^#\s*agent-name:\s*(\S+)", line.strip()):
                    name = match.group(1)
                    if not re.match(r"^[a-z0-9_.\-]+$", name):
                        msg = (
                            f"agent-name '{name}' must match [a-z0-9_.-]+ "
                            f"(Docker image names are lowercase)"
                        )
                        raise SystemExit(msg)
                    self._check_inherits_base(location.path, lines)
                    # Context stays the project root, not the Dockerfile's own
                    # directory, so `COPY <project-relative-path>` keeps working.
                    return ResolvedImage(
                        image=f"{BASE_IMAGE_NAME}-{name}",
                        dockerfile=location.path,
                        context=cwd,
                        agent_name=name,
                        is_legacy=location.is_legacy,
                    )

            msg = f"{location.path} must contain '# agent-name: <name>' comment"
            raise SystemExit(msg)

        return ResolvedImage(
            image=BASE_IMAGE_NAME,
            dockerfile=OPS_DIR / AGENT_DOCKERFILE_NAME,
            context=TOOL_DIR,
        )

    def _check_inherits_base(self, dockerfile: Path, lines: list[str]) -> None:
        """
        Require the *final* ``FROM`` of a project Dockerfile to be the base image.

        The last ``FROM`` is the one that decides what the tag ends up containing, so it
        is the one that must inherit the wrapper's toolchain; earlier stages of a
        multi-stage build produce throwaway artifacts and may use any image at all.

        Inheriting from the base is what makes a project image's staleness answerable:
        the base image's id is stamped onto it at build time, and a project image built
        on something else could never be told apart from a current one.
        """
        from_image = ""
        for line in lines:
            if match := FROM_RE.match(line.strip()):
                from_image = match.group(1)

        if not from_image:
            msg = f"{dockerfile} must contain a 'FROM {BASE_IMAGE_NAME}' line"
            raise SystemExit(msg)

        if not BASE_FROM_RE.match(from_image):
            msg = (
                f"{dockerfile} must inherit from the wrapper's base image: its final "
                f"'FROM {from_image}' is not '{BASE_IMAGE_NAME}'. Change the last FROM to "
                f"'FROM {BASE_IMAGE_NAME}' (earlier stages of a multi-stage build may use "
                f"any image)."
            )
            raise SystemExit(msg)

    def ensure_images(self, resolved: ResolvedImage, *, force: BuildForce) -> int:
        """
        Build every image *resolved* needs that is missing, stale, or forced. Returns rc.

        The base image comes first and a failure there returns immediately: a project
        image must never be built on a base that is absent or known to be wrong.

        The whole of this runs under a host-global lock, and every staleness question is
        asked *inside* it. Two concurrent launches that both see a missing base therefore
        serialize -- the second re-reads, finds it current, and builds nothing. Without
        that, both would build the base, the loser's tag would overwrite the winner's,
        and every project image stamped against the winner would be stale again at once.
        """
        if not daemon_reachable():
            self._display.error(
                "the Docker daemon is not reachable, so the agent images cannot be "
                "checked or built. Start Docker and try again."
            )
            return 1

        lock_path = AGENT_LAUNCHES_DIR / BUILD_LOCK_NAME
        with try_file_lock(lock_path) as acquired:
            if acquired:
                return self._ensure_images_locked(resolved, force=force)
        self._display.info("waiting for another agent-wrap image build to finish...")
        with file_lock(lock_path):
            return self._ensure_images_locked(resolved, force=force)

    def _ensure_images_locked(self, resolved: ResolvedImage, *, force: BuildForce) -> int:
        """Body of :meth:`ensure_images`, with the build lock already held."""
        base_stamp = image_stamp(BASE_IMAGE_NAME)
        base_reason = self._base_reason(
            base_stamp, force=force, is_target=resolved.image == BASE_IMAGE_NAME
        )
        if base_reason is not None:
            rc = self._build_one(
                OPS_DIR / AGENT_DOCKERFILE_NAME,
                BASE_IMAGE_NAME,
                TOOL_DIR,
                base_reason,
                base_stamp,
                labels={
                    BUILD_ITERATION_LABEL: str(DOCKER_BUILD_ITERATION),
                    IMAGE_NAME_LABEL: BASE_IMAGE_NAME,
                },
            )
            if rc != 0:
                return rc
            # Re-read: the id that was just created is the one the project image has to
            # record. Stamping it with the pre-rebuild id would leave it stale forever.
            base_stamp = image_stamp(BASE_IMAGE_NAME)

        if resolved.image == BASE_IMAGE_NAME:
            return 0

        project_reason = self._project_reason(
            resolved, base_stamp, base_rebuilt=base_reason is not None, force=force
        )
        if project_reason is None:
            return 0
        return self._build_one(
            resolved.dockerfile,
            resolved.image,
            resolved.context,
            project_reason,
            base_stamp,
            labels={
                BUILD_ITERATION_LABEL: str(DOCKER_BUILD_ITERATION),
                BASE_IMAGE_ID_LABEL: base_stamp.id if base_stamp else "",
                IMAGE_NAME_LABEL: resolved.image,
            },
        )

    def _base_reason(
        self, stamp: ImageStamp | None, *, force: BuildForce, is_target: bool
    ) -> BuildReason | None:
        """
        Why the base image needs building right now, or None when it is current.

        *is_target* says the base is the image the caller actually asked about, which is
        the case for a project that declares no Dockerfile. ``BuildForce.PROJECT`` then
        means the base -- there is nothing else for ``agent rebuild`` to have meant.
        """
        if force is BuildForce.ALL or (is_target and force is BuildForce.PROJECT):
            return BuildReason.FORCED
        if stamp is None:
            return BuildReason.MISSING
        recorded = stamp.labels.get(BUILD_ITERATION_LABEL)
        if recorded is None:
            return BuildReason.UNSTAMPED
        # Compared as text: a garbage label value is simply a mismatch, which spares this
        # an int-parsing failure path that could only ever mean "rebuild" anyway.
        if recorded != str(DOCKER_BUILD_ITERATION):
            return BuildReason.ITERATION_CHANGED
        return None

    def _project_reason(
        self,
        resolved: ResolvedImage,
        base_stamp: ImageStamp | None,
        *,
        base_rebuilt: bool,
        force: BuildForce,
    ) -> BuildReason | None:
        if force in (BuildForce.PROJECT, BuildForce.ALL):
            return BuildReason.FORCED
        if base_rebuilt:
            # Its base moved, so the id it recorded cannot still match -- skip the inspect.
            return BuildReason.BASE_CHANGED
        stamp = image_stamp(resolved.image)
        if stamp is None:
            return BuildReason.MISSING
        recorded = stamp.labels.get(BASE_IMAGE_ID_LABEL)
        if recorded is None:
            return BuildReason.UNSTAMPED
        if base_stamp is not None and recorded != base_stamp.id:
            return BuildReason.BASE_CHANGED
        return None

    def _build_one(  # noqa: PLR0913
        self,
        dockerfile: Path,
        image: str,
        context: Path,
        reason: BuildReason,
        base_stamp: ImageStamp | None,
        *,
        labels: dict[str, str],
    ) -> int:
        """
        Announce one build, with the reason that triggered it, and run it.

        The cache note rides along on the reason line because an auto-build is wall clock
        the user did not ask for, and the two image kinds cost very differently.
        """
        is_base = image == BASE_IMAGE_NAME
        described = f"base {image}" if is_base else image
        self._display.banner(f"Building {described} from {dockerfile}")
        if reason in BUILD_REASON_TEXT:
            note = BASE_BUILD_CACHE_NOTE if is_base else PROJECT_BUILD_CACHE_NOTE
            self._display.info(f"    reason: {self._reason_text(reason, base_stamp)}; {note}")
        return self._docker_build(dockerfile, image, context, labels=labels)

    def _reason_text(self, reason: BuildReason, base_stamp: ImageStamp | None) -> str:
        recorded = (
            base_stamp.labels.get(BUILD_ITERATION_LABEL, "unstamped") if base_stamp else "unstamped"
        )
        return BUILD_REASON_TEXT[reason].format(
            base=BASE_IMAGE_NAME, was=recorded, now=DOCKER_BUILD_ITERATION
        )

    def stale_summary(self, resolved: ResolvedImage | None) -> ImageStaleness:
        """
        Report why each image would be rebuilt right now, without building anything.

        Strictly read-only: ``agent inspect`` calls it, and a report that changed the
        state it describes would be worse than no report.
        """
        if not daemon_reachable():
            return ImageStaleness(base="", project="")
        base_stamp = image_stamp(BASE_IMAGE_NAME)
        base_reason = self._base_reason(base_stamp, force=BuildForce.NONE, is_target=False)
        base = self._reason_text(base_reason, base_stamp) if base_reason else ""

        if resolved is None or resolved.image == BASE_IMAGE_NAME:
            return ImageStaleness(base=base, project="")

        # base_rebuilt reads as "the base is not the one this image was built on", which
        # is exactly what a stale base means for the report as well as for a build.
        project_reason = self._project_reason(
            resolved, base_stamp, base_rebuilt=base_reason is not None, force=BuildForce.NONE
        )
        project = self._reason_text(project_reason, base_stamp) if project_reason else ""
        return ImageStaleness(base=base, project=project)

    def stale_project_images(self, project_dirs: list[Path]) -> list[StaleProjectImage]:
        """
        Sweep *project_dirs* and report each one whose per-project image is stale.

        The fleet-wide counterpart to :meth:`stale_summary`, and read-only for the same
        reason. Three kinds of project are absent rather than reported as current:

        * one declaring no Dockerfile -- its target is the base, whose staleness is one
          fact about this host rather than one per project that inherits it;
        * one whose image is not built here -- nothing is stale about an image that does
          not exist;
        * one whose directory or Dockerfile cannot be read. Inside an agent container
          that is every project but the mounted one.
        """
        if not daemon_reachable():
            return []  # an unreachable daemon is not evidence that anything is stale
        verdicts, base_stamp = self._sweep_project_reasons(project_dirs)
        rows: list[StaleProjectImage] = []
        for verdict in verdicts:
            reason = verdict.reason
            if reason is None or reason is BuildReason.MISSING:
                continue
            rows.append(
                StaleProjectImage(
                    project=verdict.project,
                    image=verdict.image,
                    reason=self._reason_text(reason, base_stamp),
                )
            )
        return rows

    def _sweep_project_reasons(
        self, project_dirs: list[Path]
    ) -> tuple[list[ProjectImageVerdict], ImageStamp | None]:
        """
        Judge every project image in *project_dirs*, and return the base stamp used.

        Memoised per image tag, so projects sharing an ``# agent-name:`` cost one inspect
        between them. Unfiltered on purpose: the reporting caller wants only the
        non-current rows, but the cleanup caller needs the current ones too -- those are
        the *claimed* tags, and dropping them would leave a live project's image looking
        like nobody's.

        The base stamp rides along because :meth:`_reason_text` needs it and re-reading
        would cost a second inspect. Assumes a reachable daemon.
        """
        base_stamp = image_stamp(BASE_IMAGE_NAME)
        base_reason = self._base_reason(base_stamp, force=BuildForce.NONE, is_target=False)

        reasons: dict[str, BuildReason | None] = {}
        verdicts: list[ProjectImageVerdict] = []
        for project_dir in project_dirs:
            resolved = self._resolve_quietly(project_dir)
            if resolved is None or resolved.image == BASE_IMAGE_NAME:
                continue
            if resolved.image not in reasons:
                reasons[resolved.image] = self._project_reason(
                    resolved,
                    base_stamp,
                    base_rebuilt=base_reason is not None,
                    force=BuildForce.NONE,
                )
            verdicts.append(
                ProjectImageVerdict(
                    project=project_dir, image=resolved.image, reason=reasons[resolved.image]
                )
            )
        return verdicts, base_stamp

    def _resolve_quietly(self, project_dir: Path) -> ResolvedImage | None:
        """
        Resolve one project's target image, or None when the project cannot be read.

        Everything :meth:`resolve_image` treats as fatal is merely a skipped row here.
        """
        try:
            if not project_dir.is_dir():
                return None
            return self.resolve_image(project_dir=project_dir, warn=False)
        except SystemExit, OSError:
            return None

    def image_cleanup_scope(self, project_dirs: list[Path]) -> ImageCleanupScope:
        """
        Survey every image on this host that is no longer needed, removing nothing.

        Read-only: the user confirms this list before :meth:`remove_images` acts on
        exactly it.

        Ownership is decided by **name**, never by a label being present: docker merges
        ``Config.Labels`` through ``FROM``, so a user's own image built on a wrapper image
        carries the wrapper's labels too, and everything built before the wrapper started
        stamping carries none. Four kinds come back:

        * a *superseded* build -- untagged and carrying ``IMAGE_NAME_LABEL``. Untagged
          can never be live, so no id comparison is needed. It need not name a tag that
          still exists, or a predecessor of an already-removed tag would sit there forever;
        * an *orphaned* project image -- a ``claude-agent-<name>`` tag no readable
          registered project resolves to. An unreadable project contributes no claimed
          name, so its image reads as orphaned; that costs one rebuild;
        * a *stale* project image -- one a launch would rebuild anyway;
        * a *superseded sidecar* -- a pulled image in a pinned repository whose digest is
          not the pinned one. An unknown digest is left alone rather than guessed at.

        The base image is never a candidate, even when stale: every project image descends
        from it, so ``docker rmi`` would merely untag it -- reclaiming nothing, creating a
        fresh untagged image, and leaving the next launch a cold-scaffold rebuild.
        """
        if not daemon_reachable():
            # Nothing is provably outdated when nothing can be asked. This is also what
            # makes the command harmless inside an agent container, which mounts no socket.
            return ImageCleanupScope(images=[], unattributable=0)

        superseded, unattributable = self._superseded_images()
        candidates: dict[str, RemovableImage] = {}
        for image in superseded:
            candidates.setdefault(image.ref, image)
        for image in self._orphaned_and_stale_images(project_dirs):
            candidates.setdefault(image.ref, image)
        for image in self._superseded_sidecar_images():
            candidates.setdefault(image.ref, image)
        return ImageCleanupScope(images=list(candidates.values()), unattributable=unattributable)

    def _superseded_images(self) -> tuple[list[RemovableImage], int]:
        """
        Untagged wrapper builds naming the tag they were built as, and a count of the rest.

        Those without ``IMAGE_NAME_LABEL`` are only counted, never removed: a wrapper
        build from before the label existed is indistinguishable from a leftover of the
        user's own ``docker build``, so the count exists only to let the summary point at
        ``docker image prune`` instead of leaving that disk unexplained.

        Labels come back as JSON so an image with no labels renders ``null`` rather than
        tripping the template.
        """
        sizes: dict[str, str] = {}
        for row in list_images("dangling=true", template=UNTAGGED_IMAGE_TEMPLATE):
            fields = row.split("\t")
            if len(fields) == UNTAGGED_IMAGE_FIELDS:
                sizes[fields[0]] = fields[1]
        if not sizes:
            return [], 0

        # The listing renders 12-hex short ids while the inspect answers with the full
        # "sha256:..." form, so both sides are keyed on the same truncation. Order cannot be
        # relied on: an image that vanished between the two calls is skipped, not reported.
        recorded: dict[str, str] = {}
        for line in inspect_images(list(sizes), "{{.Id}}\t{{json .Config.Labels}}"):
            full_id, _, raw_labels = line.partition("\t")
            name = self._image_name_label(raw_labels)
            if name:
                recorded[full_id.removeprefix("sha256:")[:SHORT_IMAGE_ID_LEN]] = name

        rows = [
            RemovableImage(
                ref=short_id,
                display=short_id,
                image_id=short_id,
                size=size,
                reason=ImageCleanupReason.SUPERSEDED,
                detail=recorded[short_id[:SHORT_IMAGE_ID_LEN]],
            )
            for short_id, size in sizes.items()
            if short_id[:SHORT_IMAGE_ID_LEN] in recorded
        ]
        return rows, len(sizes) - len(rows)

    def _image_name_label(self, raw_labels: str) -> str:
        """
        Read ``IMAGE_NAME_LABEL`` out of a rendered ``{{json .Config.Labels}}``, or "".

        Unparseable JSON reads as "no label": either way there is no name to attribute by.
        """
        try:
            parsed = json.loads(raw_labels)
        except json.JSONDecodeError:
            return ""
        if not isinstance(parsed, dict):
            return ""
        return str(parsed.get(IMAGE_NAME_LABEL, ""))

    def _orphaned_and_stale_images(self, project_dirs: list[Path]) -> list[RemovableImage]:
        """
        Tagged wrapper images that no project claims, or that a launch would rebuild.

        Orphaned wins on overlap: saying "stale" of a deleted project's image would
        misdescribe why it is going.
        """
        verdicts, base_stamp = self._sweep_project_reasons(project_dirs)
        claimed = {BASE_IMAGE_NAME} | {verdict.image for verdict in verdicts}
        stale = {
            verdict.image: verdict
            for verdict in verdicts
            if verdict.reason is not None and verdict.reason is not BuildReason.MISSING
        }

        rows: list[RemovableImage] = []
        for line in list_images(template=TAGGED_IMAGE_TEMPLATE):
            fields = line.split("\t")
            if len(fields) != TAGGED_IMAGE_FIELDS:
                continue
            repository, tag, image_id, size = fields
            if not self._is_wrapper_image(repository) or repository == BASE_IMAGE_NAME:
                continue
            ref = repository if tag == DOCKER_NONE else f"{repository}:{tag}"
            if repository not in claimed:
                rows.append(
                    RemovableImage(
                        ref=ref,
                        display=ref,
                        image_id=image_id,
                        size=size,
                        reason=ImageCleanupReason.ORPHANED,
                        detail=repository,
                    )
                )
            elif verdict := stale.get(repository):
                reason = verdict.reason
                rows.append(
                    RemovableImage(
                        ref=ref,
                        display=ref,
                        image_id=image_id,
                        size=size,
                        reason=ImageCleanupReason.STALE,
                        # Narrowed by the `stale` comprehension above; the fallback keeps
                        # the type checker honest without inventing a second reason line.
                        detail=self._reason_text(reason, base_stamp) if reason else "",
                    )
                )
        return rows

    def _is_wrapper_image(self, repository: str) -> bool:
        """
        Whether a local repository name is one the wrapper tags into.

        The wrapper never builds into a registry, so a ``/`` rules a repository out however
        it is spelled -- and a name that merely *contains* the prefix is somebody else's.
        """
        if "/" in repository:
            return False
        return repository == BASE_IMAGE_NAME or repository.startswith(WRAPPER_IMAGE_PREFIX)

    def _superseded_sidecar_images(self) -> list[RemovableImage]:
        """
        Report pulled sidecar images that are not the digest the wrapper pins.

        ``--digests`` is required because the template names ``{{.Digest}}`` and the flag
        is what fills it in. A row whose digest docker does not know is left alone: the
        wrapper pulls by digest, so an unknown one came from somewhere else.
        """
        rows: list[RemovableImage] = []
        for pinned in PINNED_SIDECAR_IMAGES:
            ref = parse_image_ref(pinned)
            for line in list_images(
                template=SIDECAR_IMAGE_TEMPLATE, reference=ref.repository, digests=True
            ):
                fields = line.split("\t")
                if len(fields) != SIDECAR_IMAGE_FIELDS:
                    continue
                repository, _, image_id, digest, size = fields
                if repository != ref.repository:
                    continue
                if digest in (DOCKER_NONE, "", ref.digest):
                    continue
                rows.append(
                    RemovableImage(
                        ref=f"{repository}@{digest}",
                        display=f"{repository}@{digest}",
                        image_id=image_id,
                        size=size,
                        reason=ImageCleanupReason.SUPERSEDED_SIDECAR,
                        detail=pinned,
                    )
                )
        return rows

    def remove_images(self, scope: ImageCleanupScope) -> ImageCleanupOutcome:
        """
        Remove every image in *scope*, reporting what went and what docker refused.

        Takes the scope from a prior :meth:`image_cleanup_scope` call, so the list the
        user confirmed is the list acted on -- no re-survey, no TOCTOU gap.

        A refusal is not a failure to abort on: ``remove_image`` never forces, so the
        usual refusal is an image a running container still references. An image another
        row already took with it reads as removed, since docker deleting a shared parent
        is the outcome that was asked for.
        """
        removed: list[RemovableImage] = []
        skipped: list[RemovableImage] = []
        for image in scope.images:
            (removed if remove_image(image.ref) else skipped).append(image)
        return ImageCleanupOutcome(removed=removed, skipped=skipped)

    def _do_rebuild(self, *, full: bool) -> int:
        try:
            resolved = self.resolve_image()
        except SystemExit as e:
            self._display.error(str(e))
            return 1

        rc = self.ensure_images(resolved, force=BuildForce.ALL if full else BuildForce.PROJECT)
        if rc != 0:
            return rc

        if full and resolved.image == BASE_IMAGE_NAME:
            self._display.success(
                f"No {AGENT_ASSETS_DIR}/{AGENT_DOCKERFILE_NAME} in {Path.cwd()}; "
                f"base build is the only build needed"
            )
        subprocess.run(["docker", "images", "--filter", f"reference={resolved.image}"])
        return 0
