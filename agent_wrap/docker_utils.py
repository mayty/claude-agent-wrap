# This file has been created with the assistance of an AI tool.
"""Docker-related utility functions."""

from __future__ import annotations

import subprocess


def is_rootless() -> bool:
    """
    Check if Docker is running in rootless mode.

    Returns:
        True if Docker is rootless, False otherwise.

    """
    try:
        result = subprocess.run(
            ["docker", "info"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        return "rootless" in result.stdout.lower()
    except (subprocess.TimeoutExpired, FileNotFoundError, subprocess.SubprocessError):
        # If we can't determine, assume not rootless (safer default)
        return False


def image_exists(image: str) -> bool:
    """
    Check if a Docker image exists locally.

    Args:
        image: Image name (e.g., "claude-agent" or "claude-agent-myproject").

    Returns:
        True if the image exists, False otherwise.

    """
    try:
        result = subprocess.run(
            ["docker", "image", "inspect", image],
            capture_output=True,
            timeout=10,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, subprocess.SubprocessError):
        return False
    else:
        return result.returncode == 0


def get_user_args() -> list[str]:
    """
    Get --user flags for docker run if not running rootless.

    Returns:
        List of docker run flags for user mapping, empty if rootless.

    """
    if is_rootless():
        return []
    import os

    return ["--user", f"{os.getuid()}:{os.getgid()}"]
