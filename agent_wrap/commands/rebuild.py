# This file has been created with the assistance of an AI tool.
"""The `rebuild` subcommand — builds Docker images."""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

from agent_wrap.docker_utils import image_exists
from agent_wrap.utils import resolve_image


def run(args: list[str], tool_dir: Path) -> int:
    """Execute the `rebuild` subcommand."""
    full = False
    for arg in args:
        if arg == "--full":
            full = True
        elif arg in ("-h", "--help"):
            print("Usage: rebuild_agent [--full]")
            print("  --full  Rebuild the base 'claude-agent' image first, then the project image.")
            return 0
        else:
            print(f"Error: unknown argument '{arg}' (expected --full)", file=sys.stderr)
            return 1

    # Check for updates
    from agent_wrap.commands.update import check as check_updates
    if check_updates(tool_dir):
        return 0

    try:
        resolved = resolve_image(tool_dir)
    except SystemExit as e:
        print(str(e), file=sys.stderr)
        return 1

    uid = str(os.getuid())
    gid = str(os.getgid())

    if full:
        print(f"--- Building base claude-agent from {tool_dir}/Dockerfile ---")
        result = subprocess.run([
            "docker", "build", "--no-cache",
            "--build-arg", f"HOST_UID={uid}",
            "--build-arg", f"HOST_GID={gid}",
            "-f", str(tool_dir / "Dockerfile"),
            "-t", "claude-agent",
            str(tool_dir),
        ])
        if result.returncode != 0:
            return result.returncode

        if resolved.image == "claude-agent":
            print(f"--- No Dockerfile.agent in {Path.cwd()}; base build is the only build needed ---")
            subprocess.run(["docker", "images", "--filter", f"reference={resolved.image}"])
            return 0

    if not full and resolved.image != "claude-agent":
        # Check FROM line
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
            print("       Run 'rebuild_agent --full' to build the base first.", file=sys.stderr)
            return 1

        if from_line and not re.match(r"^claude-agent(:.*)?$", from_line):
            print(
                f"\033[1;33mNote:\033[0m '{resolved.dockerfile}' inherits from "
                f"'{from_line}' rather than 'claude-agent'. Consider migrating to "
                f"'FROM claude-agent' to reuse the base toolchain.",
                file=sys.stderr,
            )

    print(f"--- Building {resolved.image} from {resolved.dockerfile} ---")
    result = subprocess.run([
        "docker", "build", "--no-cache",
        "--build-arg", f"HOST_UID={uid}",
        "--build-arg", f"HOST_GID={gid}",
        "-f", str(resolved.dockerfile),
        "-t", resolved.image,
        str(resolved.context),
    ])
    if result.returncode != 0:
        return result.returncode

    subprocess.run(["docker", "images", "--filter", f"reference={resolved.image}"])
    return 0
