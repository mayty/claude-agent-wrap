# This file has been edited with the assistance of an AI tool.
"""Generic process-related utilities."""

import psutil


def pid_alive(pid: int) -> bool:
    """
    Return True if a process with *pid* exists and is not a zombie.

    The zombie check is the whole point: a caller asking this wants to know whether the
    daemon it started is still serving, and an exited-but-unreaped child answers every
    cheaper liveness test in the affirmative.

    A process that exits between the two calls raises ``NoSuchProcess`` and is dead,
    which is the same answer the race-free order would have given.
    """
    try:
        return psutil.pid_exists(pid) and psutil.Process(pid).status() != psutil.STATUS_ZOMBIE
    except psutil.NoSuchProcess:
        return False
