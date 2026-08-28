# This file has been edited with the assistance of an AI tool.
"""Docker image building domain service."""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

from agent_wrap.constants import (
    AGENT_ASSETS_DIR,
    AGENT_DOCKERFILE_NAME,
    BASE_IMAGE_NAME,
    LEGACY_AGENT_DOCKERFILE_NAME,
    OPS_DIR,
    SPELLCHECK_BUILD_ARG,
    SPELLCHECK_LANG,
    TOOL_DIR,
)
from agent_wrap.domain.build.constants import (
    DEFAULT_STARTUP_TIMEOUT_SECONDS,
    STARTUP_FALSY_WORDS,
    STARTUP_TRUTHY_WORDS,
)
from agent_wrap.domain.build.models import (
    DockerfileAgentInfo,
    DockerfileLocation,
    ResolvedImage,
)
from agent_wrap.exceptions import DockerfileDirectiveError
from agent_wrap.lib.docker_utils import host_network_build_args, image_exists

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
        if self._updates.check_updates():
            return 0
        return self._do_rebuild(full=full)

    def _docker_build(self, dockerfile: Path, image: str, context: Path, uid: str, gid: str) -> int:
        """
        Run a docker build and return the exit code.

        ``SPELLCHECK_LANG`` goes to both builds, like the UID/GID pair: a project
        Dockerfile that declares no such ARG draws the same harmless
        "build-args were not consumed" warning it already draws for those two, and one
        that does declare it gets a hook for installing further dictionaries.
        """
        result = subprocess.run(
            [
                "docker",
                "build",
                "--no-cache",
                *host_network_build_args(),
                "--build-arg",
                f"HOST_UID={uid}",
                "--build-arg",
                f"HOST_GID={gid}",
                "--build-arg",
                f"{SPELLCHECK_BUILD_ARG}={SPELLCHECK_LANG}",
                "-f",
                str(dockerfile),
                "-t",
                image,
                str(context),
            ]
        )
        return result.returncode

    def _check_from_line(self, resolved: ResolvedImage) -> bool:
        """Validate FROM line of the project Dockerfile. Returns False on error."""
        from_line = ""
        with open(resolved.dockerfile) as f:
            for line in f:
                if m := re.match(r"^[Ff][Rr][Oo][Mm]\s+(\S+)", line):
                    from_line = m.group(1)

        if re.match(rf"^{BASE_IMAGE_NAME}(:.*)?$", from_line) and not image_exists(BASE_IMAGE_NAME):
            self._display.error(
                f"'{resolved.dockerfile}' uses 'FROM claude-agent' but the base image is "
                "not built.\n"
                "Run 'agent rebuild --full' to build the base first."
            )
            return False

        if from_line and not re.match(rf"^{BASE_IMAGE_NAME}(:.*)?$", from_line):
            self._display.warning(
                f"'{resolved.dockerfile}' inherits from '{from_line}' rather than"
                " 'claude-agent'. Consider migrating to 'FROM claude-agent' to reuse"
                " the base toolchain."
            )

        return True

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

    def locate_dockerfile(self, cwd: Path) -> DockerfileLocation:
        """
        Find the project Dockerfile, preferring the current location over the legacy one.

        The single discovery point for the whole wrapper -- every caller that needs to
        know whether a project customizes its image goes through here, so the two paths
        cannot drift apart.

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
            self._display.warning(
                f"'{LEGACY_AGENT_DOCKERFILE_NAME}' is deprecated -- move it to "
                f"'{AGENT_ASSETS_DIR}/{AGENT_DOCKERFILE_NAME}'."
            )
            return DockerfileLocation(path=legacy, is_legacy=True)

        return DockerfileLocation(path=None, is_legacy=False)

    def resolve_image(self, *, use_base: bool = False) -> ResolvedImage:
        """
        Determine which Docker image to use, its Dockerfile, and build context.

        Args:
            use_base: If True, always use the base claude-agent image.

        Returns:
            ResolvedImage with image name, dockerfile path, and context directory.

        Raises:
            SystemExit: If the project Dockerfile exists but is missing the required
                '# agent-name:' comment or has an invalid name.

        """
        cwd = Path.cwd()
        location = (
            DockerfileLocation(path=None, is_legacy=False)
            if use_base
            else self.locate_dockerfile(cwd)
        )

        if location.path is not None:
            with open(location.path) as f:
                for line in f:
                    if match := re.match(r"^#\s*agent-name:\s*(\S+)", line.strip()):
                        name = match.group(1)
                        if not re.match(r"^[a-z0-9_.\-]+$", name):
                            msg = (
                                f"agent-name '{name}' must match [a-z0-9_.-]+ "
                                f"(Docker image names are lowercase)"
                            )
                            raise SystemExit(msg)
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

    def _do_rebuild(self, *, full: bool) -> int:
        """Perform the actual rebuild. Returns exit code."""
        try:
            resolved = self.resolve_image()
        except SystemExit as e:
            self._display.error(str(e))
            return 1

        uid = str(os.getuid())
        gid = str(os.getgid())

        if full:
            self._display.banner(
                f"Building base {BASE_IMAGE_NAME} from {OPS_DIR / AGENT_DOCKERFILE_NAME}"
            )
            rc = self._docker_build(
                OPS_DIR / AGENT_DOCKERFILE_NAME, BASE_IMAGE_NAME, TOOL_DIR, uid, gid
            )
            if rc != 0:
                return rc

            if resolved.image == BASE_IMAGE_NAME:
                self._display.success(
                    f"No {AGENT_ASSETS_DIR}/{AGENT_DOCKERFILE_NAME} in {Path.cwd()}; "
                    f"base build is the only build needed"
                )
                subprocess.run(["docker", "images", "--filter", f"reference={resolved.image}"])
                return 0

        if not full and resolved.image != BASE_IMAGE_NAME and not self._check_from_line(resolved):
            return 1

        self._display.banner(f"Building {resolved.image} from {resolved.dockerfile}")
        rc = self._docker_build(resolved.dockerfile, resolved.image, resolved.context, uid, gid)
        if rc == 0:
            subprocess.run(["docker", "images", "--filter", f"reference={resolved.image}"])
        return rc
