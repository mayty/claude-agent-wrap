# This file has been edited with the assistance of an AI tool.
"""Docker image building domain service."""

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
    FROM_RE,
    PROJECT_BUILD_CACHE_NOTE,
    STARTUP_FALSY_WORDS,
    STARTUP_TRUTHY_WORDS,
    BuildReason,
)
from agent_wrap.domain.build.models import (
    DockerfileAgentInfo,
    DockerfileLocation,
    ImageStaleness,
    ResolvedImage,
    StaleProjectImage,
)
from agent_wrap.exceptions import DockerfileDirectiveError
from agent_wrap.lib.docker_utils import (
    ImageStamp,
    daemon_reachable,
    host_network_build_args,
    image_stamp,
)
from agent_wrap.lib.flock import file_lock, try_file_lock
from agent_wrap.lib.utils import generate_uuid

if TYPE_CHECKING:
    from agent_wrap.domain.display.service import DisplayService
    from agent_wrap.domain.updates.service import UpdateService


class BuildService:
    """Docker image building for agent-wrap."""

    def __init__(self, update_service: UpdateService, display_service: DisplayService) -> None:
        self._updates = update_service
        self._display = display_service

    def rebuild(self, *, full: bool) -> int:
        """Execute the rebuild pipeline. Returns exit code."""
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

        ``SPELLCHECK_LANG`` goes to both builds, like the UID/GID pair: a project
        Dockerfile that declares no such ARG draws the same harmless
        "build-args were not consumed" warning it already draws for those two, and one
        that does declare it gets a hook for installing further dictionaries.

        *labels* are stamped with ``--label`` rather than a ``LABEL`` instruction so no
        project Dockerfile has to cooperate to be trackable.

        The base image is the only build that uses docker's layer cache. Its recipe is
        this repo's own ``ops/Dockerfile``, split so that everything expensive and stable
        sits in the ``scaffold`` stage: ``BUILD_ITERATION`` invalidates that stage when the
        wrapper says the recipe moved, and a per-build ``CLAUDE_CACHE_BUST`` invalidates
        the final stage every time, so a base build always lands the day's Claude Code
        release without re-running apt. A project Dockerfile is somebody else's file under
        no such contract, and ``agent rebuild`` is the verb for applying edits that nothing
        hashes -- it keeps ``--no-cache``. The two cache build args are base-only for the
        same reason: on the other side there is no cache to steer, only an
        unconsumed-build-arg warning.
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

        A boolean word selects ``DEFAULT_STARTUP_TIMEOUT_SECONDS``; a positive number is
        a timeout in seconds. Numbers are *always* seconds, so ``1`` means one second
        rather than "true" -- the shell validator warns about that spelling instead of
        this parser guessing which was meant.

        Raises:
            DockerfileDirectiveError: If the value is neither a boolean word nor a
                positive number.

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

        Args:
            dockerfile_path: Path to the project Dockerfile.
            legacy: True when the file sits at the deprecated ``Dockerfile.agent`` path,
                which is not allowed to enable startup scripts.

        Returns:
            DockerfileAgentInfo with parsed directives.

        Raises:
            DockerfileDirectiveError: On a malformed or misplaced directive.

        """
        info = DockerfileAgentInfo()

        with open(dockerfile_path) as f:
            for raw_line in f:
                line = raw_line.strip()

                # Handle agent-user directive
                if match := re.match(r"^#\s*agent-user:\s*(\S+)", line):
                    info.agent_user = match.group(1)

                # Handle agent-run-args directive
                elif match := re.match(r"^#\s*agent-run-args:\s*(.+)", line):
                    info.extra_run_args.extend(match.group(1).split())

                # Handle agent-enable-startup directive -- new location only, so that a
                # project still on the deprecated path is told to migrate rather than
                # having its startup script silently ignored.
                elif match := re.match(r"^#\s*agent-enable-startup:\s*(\S+)", line):
                    if legacy:
                        msg = (
                            f"'# agent-enable-startup:' is only supported in "
                            f"'{AGENT_ASSETS_DIR}/{AGENT_DOCKERFILE_NAME}'. Move "
                            f"'{LEGACY_AGENT_DOCKERFILE_NAME}' there to use a startup script."
                        )
                        raise DockerfileDirectiveError(msg)
                    info.startup_timeout = self.parse_startup_value(match.group(1))

                # Parse EXPOSE <port> [<port>...]
                elif match := re.match(r"^[Ee][Xx][Pp][Oo][Ss][Ee]\s+(.+)", line):
                    for token in match.group(1).split():
                        port = token.split("/")[0]
                        info.expose_ports.append(port)

        return info

    def locate_dockerfile(self, cwd: Path, *, warn: bool = True) -> DockerfileLocation:
        """
        Find the project Dockerfile, preferring the current location over the legacy one.

        The single discovery point for the whole wrapper -- every caller that needs to
        know whether a project customizes its image goes through here, so the two paths
        cannot drift apart.

        *warn* off silences the legacy-location deprecation notice, for a caller sweeping
        many projects at once: the notice is addressed to whoever owns the Dockerfile, and
        one copy per registered project would bury the report it was printed alongside.

        Raises:
            SystemExit: If both locations are populated, which leaves no way to tell
                which one the author meant.

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
        here, so every caller -- launch, rebuild, and the read-only ``agent inspect`` --
        gets the same verdict from one file read.

        Args:
            use_base: If True, always use the base claude-agent image.
            project_dir: The project to resolve for; the cwd when None. Passed by the
                fleet-wide sweep, which answers this question for projects the caller is
                not standing in -- everything else asks about where it already is.
            warn: Forwarded to :meth:`locate_dockerfile`; see the note there.

        Returns:
            ResolvedImage with image name, dockerfile path, and context directory.

        Raises:
            SystemExit: If the project Dockerfile is missing the required
                '# agent-name:' comment, has an invalid name, or does not inherit from
                the base image.

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

        Raises:
            SystemExit: If no ``FROM`` is present, or the last one is not the base image.

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
                labels={BUILD_ITERATION_LABEL: str(DOCKER_BUILD_ITERATION)},
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
        """Why the project image needs building right now, or None when it is current."""
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
        the user did not ask for, and the two image kinds cost very different amounts; a
        forced ``agent rebuild`` prints no reason line and needs no such warning.
        """
        is_base = image == BASE_IMAGE_NAME
        described = f"base {image}" if is_base else image
        self._display.banner(f"Building {described} from {dockerfile}")
        if reason in BUILD_REASON_TEXT:
            note = BASE_BUILD_CACHE_NOTE if is_base else PROJECT_BUILD_CACHE_NOTE
            self._display.info(f"    reason: {self._reason_text(reason, base_stamp)}; {note}")
        return self._docker_build(dockerfile, image, context, labels=labels)

    def _reason_text(self, reason: BuildReason, base_stamp: ImageStamp | None) -> str:
        """Fill in a ``BUILD_REASON_TEXT`` template from the base image's own stamp."""
        recorded = (
            base_stamp.labels.get(BUILD_ITERATION_LABEL, "unstamped") if base_stamp else "unstamped"
        )
        return BUILD_REASON_TEXT[reason].format(
            base=BASE_IMAGE_NAME, was=recorded, now=DOCKER_BUILD_ITERATION
        )

    def stale_summary(self, resolved: ResolvedImage | None) -> ImageStaleness:
        """
        Report why each image would be rebuilt right now, without building anything.

        Strictly read-only -- ``agent inspect`` calls it, and a report that changed the
        state it describes would be worse than no report. Both fields are "" when the
        image is current, and the project field is "" when there is no project image to
        speak of.
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
        reason: ``agent inspect`` is its only caller, and a report that rebuilt what it
        describes would be worse than no report.

        Three kinds of project are absent from the result rather than reported as current:

        * one that declares no Dockerfile -- its target is the base image, and the base's
          staleness is one fact about this host, not one per project that inherits it;
        * one whose image is not built on this host -- there is nothing stale about an
          image that does not exist, and the launch that creates it is not a rebuild;
        * one whose directory or Dockerfile cannot be read. Run from inside an agent
          container, every project but the mounted one falls in here, and a Dockerfile
          that fails validation is a fault its own project's ``agent run`` will report in
          full -- neither is answerable from where this sweep stands.

        A stale base short-circuits every project image to "the base moved", which
        :meth:`_project_reason` already does without a docker call. The verdict is
        memoised per image tag, so projects sharing an ``# agent-name:`` cost one inspect
        between them rather than one each.
        """
        if not daemon_reachable():
            return []  # an unreachable daemon is not evidence that anything is stale
        base_stamp = image_stamp(BASE_IMAGE_NAME)
        base_reason = self._base_reason(base_stamp, force=BuildForce.NONE, is_target=False)

        reasons: dict[str, BuildReason | None] = {}
        rows: list[StaleProjectImage] = []
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
            reason = reasons[resolved.image]
            if reason is None or reason is BuildReason.MISSING:
                continue
            rows.append(
                StaleProjectImage(
                    project=project_dir,
                    image=resolved.image,
                    reason=self._reason_text(reason, base_stamp),
                )
            )
        return rows

    def _resolve_quietly(self, project_dir: Path) -> ResolvedImage | None:
        """
        Resolve one project's target image, or None when the project cannot be read.

        Everything :meth:`resolve_image` treats as fatal is merely a skipped row here --
        see :meth:`stale_project_images` for why none of it is actionable from a sweep.
        """
        try:
            if not project_dir.is_dir():
                return None
            return self.resolve_image(project_dir=project_dir, warn=False)
        except SystemExit, OSError:
            return None

    def _do_rebuild(self, *, full: bool) -> int:
        """Perform the actual rebuild. Returns exit code."""
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
