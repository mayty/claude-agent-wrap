# This file has been created with the assistance of an AI tool.
"""Generic process-related utilities."""

from __future__ import annotations

import os


def pid_alive(pid: int) -> bool:
    """Return True if a process with *pid* exists (best-effort, via ``os.kill(pid, 0)``)."""
    try:
        os.kill(pid, 0)
    except PermissionError:
        return True
    except OSError:
        return False
    return True
