# This file has been created with the assistance of an AI tool.
"""The `rebuild` subcommand — builds Docker images."""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

from agent_wrap.lib.console import Ansi
from agent_wrap.lib.docker_utils import image_exists
from agent_wrap.lib.utils import ResolvedImage, resolve_image


def _parse_rebuild_args(args: list[str]) -> tuple[bool, bool]:
    """Parse rebuild arguments. Returns (full, abort) — abort if --help was printed."""
    full = False
    for arg in args:
        if arg == "--full":
            full = True
        elif arg in ("-h", "--help"):
            print("Usage: agent rebuild [--full]")
            print("  --full  Rebuild the base 'claude-agent' image first, then the project image.")
            return full, True
        else:
            print(f"Error: unknown argument '{arg}' (expected --full)", file=sys.stderr)
            return full, True
    return full, False


def _docker_build(dockerfile: Path, image: str, context: Path, uid: str, gid: str) -> int:
    """Run a docker build and return the exit code."""
    result = subprocess.run(
        [
            "docker",
            "build",
            "--no-cache",
            "--build-arg",
            f"HOST_UID={uid}",
            "--build-arg",
            f"HOST_GID={gid}",
            "-f",
            str(dockerfile),
            "-t",
            image,
            str(context),
        ]
    )
    return result.returncode


def _check_from_line(resolved: ResolvedImage) -> bool:
    """Validate FROM line of Dockerfile.agent. Returns False on error."""
    from_line = ""
    with open(resolved.dockerfile) as f:
        for line in f:
            if m := re.match(r"^[Ff][Rr][Oo][Mm]\s+(\S+)", line):
                from_line = m.group(1)

    if re.match(r"^claude-agent(:.*)?$", from_line) and not image_exists("claude-agent"):
        print(
            f"Error: '{resolved.dockerfile}' uses 'FROM claude-agent' but the "
            f"base image is not built.",
            file=sys.stderr,
        )
        print("       Run 'agent rebuild --full' to build the base first.", file=sys.stderr)
        return False

    if from_line and not re.match(r"^claude-agent(:.*)?$", from_line):
        print(
            f"{Ansi.BOLD_YELLOW}Note:{Ansi.RESET} '{resolved.dockerfile}' inherits from "
            f"'{from_line}' rather than 'claude-agent'. Consider migrating to "
            f"'FROM claude-agent' to reuse the base toolchain.",
            file=sys.stderr,
        )

    return True


def run(args: list[str], tool_dir: Path) -> int:
    """Execute the `rebuild` subcommand."""
    full, abort = _parse_rebuild_args(args)
    if abort:
        return 1 if args and args[0] not in ("-h", "--help") else 0

    # Check for updates
    from agent_wrap.commands.update import check as check_updates

    if check_updates(tool_dir):
        return 0

    return _do_rebuild(tool_dir, full=full)


def _do_rebuild(tool_dir: Path, *, full: bool) -> int:
    """Perform the actual rebuild. Returns exit code."""
    try:
        resolved = resolve_image(tool_dir)
    except SystemExit as e:
        print(str(e), file=sys.stderr)
        return 1

    uid = str(os.getuid())
    gid = str(os.getgid())

    if full:
        print(f"--- Building base claude-agent from {tool_dir}/ops/Dockerfile ---")
        rc = _docker_build(tool_dir / "ops" / "Dockerfile", "claude-agent", tool_dir, uid, gid)
        if rc != 0:
            return rc

        if resolved.image == "claude-agent":
            print(
                f"--- No Dockerfile.agent in {Path.cwd()}; base build is the only build needed ---"
            )
            subprocess.run(["docker", "images", "--filter", f"reference={resolved.image}"])
            return 0

    if not full and resolved.image != "claude-agent" and not _check_from_line(resolved):
        return 1

    print(f"--- Building {resolved.image} from {resolved.dockerfile} ---")
    rc = _docker_build(resolved.dockerfile, resolved.image, resolved.context, uid, gid)
    if rc == 0:
        subprocess.run(["docker", "images", "--filter", f"reference={resolved.image}"])
    return rc
