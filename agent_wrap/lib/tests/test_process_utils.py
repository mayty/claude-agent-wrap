# This file has been created with the assistance of an AI tool.
"""Tests for lib/process_utils — driven by real processes, since the question is real."""

import subprocess
import time

import psutil

from agent_wrap.lib.process_utils import pid_alive


def test_pid_alive_true_for_a_running_process() -> None:
    assert pid_alive(psutil.Process().pid) is True


def test_pid_alive_false_for_a_zombie() -> None:
    """An exited-but-unreaped child is what every cheaper liveness test gets wrong."""
    child = subprocess.Popen(["sleep", "0"])
    try:
        deadline = time.monotonic() + 5
        while (
            time.monotonic() < deadline
            and psutil.Process(child.pid).status() != psutil.STATUS_ZOMBIE
        ):
            time.sleep(0.01)
        assert pid_alive(child.pid) is False
    finally:
        child.wait()


def test_pid_alive_false_once_the_process_is_reaped() -> None:
    child = subprocess.Popen(["sleep", "0"])
    child.wait()
    assert pid_alive(child.pid) is False
