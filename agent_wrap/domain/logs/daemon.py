# This file has been edited with the assistance of an AI tool.
"""Background-process lifecycle for the logs viewer."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import TYPE_CHECKING, cast

from agent_wrap.constants import (
    AGENT_LAUNCHES_DIR,
    LOGS_TOOL_DIR_ENV,
)
from agent_wrap.domain.logs.constants import STATE_FILE_NAME

if TYPE_CHECKING:
    from agent_wrap.domain.logs.models import DaemonState


def state_dir() -> Path:
    env_dir = os.environ.get(LOGS_TOOL_DIR_ENV)
    if env_dir:
        return Path(env_dir) / ".agent-launches"
    return AGENT_LAUNCHES_DIR


def state_file() -> Path:
    return state_dir() / STATE_FILE_NAME


def read_state() -> DaemonState | None:
    """Read the viewer state file, or None when missing/corrupt."""
    try:
        raw = state_file().read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return None
    if (
        isinstance(data, dict)
        and isinstance(data.get("pid"), int)
        and isinstance(data.get("port"), int)
    ):
        return cast("DaemonState", data)
    return None


def write_state(pid: int, port: int) -> None:
    """Write viewer state to the state file."""
    state_dir().mkdir(parents=True, exist_ok=True)
    state_file().write_text(json.dumps({"pid": pid, "port": port}, indent=2), encoding="utf-8")


class _LogSpan:
    """Context manager returned by log_event; logs elapsed time on exit."""

    def __init__(self, category: str, description: str) -> None:
        self._category = category
        self._description = description
        self._start = time.monotonic()

    def __enter__(self) -> _LogSpan:  # noqa: PYI034 — `Self` needs py3.11+, target is py3.10
        return self

    def __exit__(self, *exc_info: object) -> None:
        elapsed = time.monotonic() - self._start
        _print_line(f"{self._category}: {self._description} completed in {elapsed:.2f}s")


def log_event(category: str, description: str) -> _LogSpan:
    """
    Print a timestamped ``"<category>: <description>"`` line.

    The return value is a context manager: a bare call just prints the start
    line (for one-off markers), while ``with log_event(...):`` additionally
    prints a matching "completed in Ns" line with elapsed time on exit.
    """
    _print_line(f"{category}: {description}")
    return _LogSpan(category, description)


def _print_line(message: str) -> None:
    # flush=True: stdout is block-buffered once redirected to a regular file, so
    # without an explicit flush a line can sit in the buffer and be lost if the
    # process is later killed (e.g. SIGKILL after a SIGTERM shutdown times out).
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {message}", flush=True)
