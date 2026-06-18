# This file has been created with the assistance of an AI tool.
"""Docker-related utility functions."""

from __future__ import annotations

import os
import subprocess


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
