# This file has been edited with the assistance of an AI tool.
"""Docker-related utility functions."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from agent_wrap.lib.utils import is_truthy_env


def is_wsl() -> bool:
    """Check if running on WSL (Microsoft kernel)."""
    try:
        return "microsoft" in Path("/proc/version").read_text().lower()
    except OSError:
        return False


def host_network_build_args() -> list[str]:
    """
    Return ["--network", "host"] for `docker build` when the WSL host-network
    workaround is active, else [].

    Honored only on WSL (see docs/configuration.md): the parallel-distro
    iptables-legacy FORWARD=DROP scenario that breaks `agent run` also breaks a
    build's `RUN` steps, which execute on Docker's default bridge.
    """
    if not is_wsl():
        return []
    if not is_truthy_env(os.environ.get("AGENT_USE_HOST_NETWORK", "")):
        return []
    return ["--network", "host"]


def docker_run(
    *args: str,
    capture: bool = True,
    check: bool = False,
    timeout: int = 30,
) -> tuple[str, int]:
    """
    Run a docker command and return (stdout, returncode).

    On timeout, missing binary, or other subprocess errors, returns ("", 1).
    With check=True, raises RuntimeError on non-zero returncode.
    """
    try:
        result = subprocess.run(
            ["docker", *args],
            capture_output=capture,
            text=True,
            timeout=timeout,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, subprocess.SubprocessError):
        return "", 1
    if check and result.returncode != 0:
        msg = f"docker {' '.join(args)} failed: {result.stderr}"
        raise RuntimeError(msg)
    stdout = result.stdout.strip() if result.stdout is not None else ""
    return stdout, result.returncode


def is_rootless() -> bool:
    """Check if Docker is running in rootless mode."""
    stdout, _ = docker_run("info", timeout=10)
    return "rootless" in stdout.lower()


def image_exists(image: str) -> bool:
    """Check if a Docker image exists locally."""
    _, rc = docker_run("image", "inspect", image, timeout=10)
    return rc == 0


def get_user_args() -> list[str]:
    """Get --user flags for docker run if not running rootless."""
    if is_rootless():
        return []
    return ["--user", f"{os.getuid()}:{os.getgid()}"]


def get_tty_args() -> list[str]:
    """
    Return docker stdin/tty flags.

    Allocate a pseudo-TTY (-t) only when our own stdin is a terminal; Docker
    rejects -t when stdin is not a TTY (e.g. launched from a subprocess with
    stdin=DEVNULL or a pipe). Always pass -i so piped stdin still reaches the
    container.
    """
    if sys.stdin.isatty():
        return ["-it"]
    return ["-i"]
