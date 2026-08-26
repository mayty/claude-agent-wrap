# This file has been created with the assistance of an AI tool.
"""Docker image building domain service."""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

from agent_wrap.constants import (
    BASE_IMAGE_NAME,
    OPS_DIR,
    SPELLCHECK_BUILD_ARG,
    SPELLCHECK_LANG,
    TOOL_DIR,
)
from agent_wrap.domain.build.models import DockerfileAgentInfo, ResolvedImage
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

        ``SPELLCHECK_LANG`` goes to both builds, like the UID/GID pair: a
        ``Dockerfile.agent`` that declares no such ARG draws the same harmless
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
        """Validate FROM line of Dockerfile.agent. Returns False on error."""
        from_line = ""
        with open(resolved.dockerfile) as f:
            for line in f:
                if m := re.match(r"^[Ff][Rr][Oo][Mm]\s+(\S+)", line):
                    from_line = m.group(1)

        if re.match(rf"^{BASE_IMAGE_NAME}(:.*)?$", from_line) and not image_exists(BASE_IMAGE_NAME):
            self._display.error(
                f"'{resolved.dockerfile}' uses 'FROM claude-agent' but the base image is not built."
            )
            self._display.error("       Run 'agent rebuild --full' to build the base first.")
            return False

        if from_line and not re.match(rf"^{BASE_IMAGE_NAME}(:.*)?$", from_line):
            self._display.warning(
                f"'{resolved.dockerfile}' inherits from '{from_line}' rather than"
                " 'claude-agent'. Consider migrating to 'FROM claude-agent' to reuse"
                " the base toolchain."
            )

        return True

    def parse_dockerfile_agent(self, dockerfile_path: Path) -> DockerfileAgentInfo:
        """
        Extract EXPOSE, agent-user, and agent-run-args directives from a Dockerfile.agent.

        Args:
            dockerfile_path: Path to the Dockerfile.agent file.

        Returns:
            DockerfileAgentInfo with parsed directives.

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

                # Parse EXPOSE <port> [<port>...]
                elif match := re.match(r"^[Ee][Xx][Pp][Oo][Ss][Ee]\s+(.+)", line):
                    for token in match.group(1).split():
                        port = token.split("/")[0]
                        info.expose_ports.append(port)

        return info

    def resolve_image(self, *, use_base: bool = False) -> ResolvedImage:
        """
        Determine which Docker image to use, its Dockerfile, and build context.

        Args:
            use_base: If True, always use the base claude-agent image.

        Returns:
            ResolvedImage with image name, dockerfile path, and context directory.

        Raises:
            SystemExit: If Dockerfile.agent exists but is missing the required
                '# agent-name:' comment or has an invalid name.

        """
        cwd = Path.cwd()
        dockerfile_agent = cwd / "Dockerfile.agent"

        if not use_base and dockerfile_agent.is_file():
            with open(dockerfile_agent) as f:
                for line in f:
                    if match := re.match(r"^#\s*agent-name:\s*(\S+)", line.strip()):
                        name = match.group(1)
                        if not re.match(r"^[a-z0-9_.\-]+$", name):
                            msg = (
                                f"Error: agent-name '{name}' must match [a-z0-9_.-]+ "
                                f"(Docker image names are lowercase)"
                            )
                            raise SystemExit(msg)
                        return ResolvedImage(
                            image=f"claude-agent-{name}",
                            dockerfile=dockerfile_agent,
                            context=cwd,
                        )

            msg = "Error: Dockerfile.agent must contain '# agent-name: <name>' comment"
            raise SystemExit(msg)

        return ResolvedImage(
            image=BASE_IMAGE_NAME,
            dockerfile=OPS_DIR / "Dockerfile",
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
            self._display.banner(f"Building base claude-agent from {OPS_DIR / 'Dockerfile'}")
            rc = self._docker_build(OPS_DIR / "Dockerfile", BASE_IMAGE_NAME, TOOL_DIR, uid, gid)
            if rc != 0:
                return rc

            if resolved.image == BASE_IMAGE_NAME:
                self._display.success(
                    f"No Dockerfile.agent in {Path.cwd()}; base build is the only build needed"
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
