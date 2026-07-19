# This file has been edited with the assistance of an AI tool.
"""Generic process-related utilities."""

from __future__ import annotations

import contextlib
import os
from pathlib import Path


def pid_alive(pid: int) -> bool:
    """
    Return True if a process with *pid* exists and is not a zombie.

    Best-effort: uses ``os.kill(pid, 0)`` first, then checks
    ``/proc/<pid>/stat`` on Linux to rule out zombie processes
    (``os.kill(0)`` succeeds even for zombies).
    """
    try:
        os.kill(pid, 0)
    except PermissionError:
        return True
    except OSError:
        return False

    # os.kill(0) returns success for zombie processes.  Check /proc/<pid>/stat
    # to detect zombies: the state character is the third whitespace-separated
    # token, immediately after the closing parenthesis of the comm field.
    with contextlib.suppress(OSError):
        stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
        # Format: "pid (comm) state ..." — comm cannot contain ')' per the
        # kernel's get_task_comm(), so rfind(')') reliably finds the boundary.
        paren = stat.rfind(")")
        if paren != -1 and paren + 2 < len(stat) and stat[paren + 2] == "Z":
            return False

    return True
