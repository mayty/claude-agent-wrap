# This file has been edited with the assistance of an AI tool.
"""Background-process lifecycle for the logs viewer."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import TYPE_CHECKING

from agent_wrap.constants import (
    AGENT_LAUNCHES_DIR,
    LOGS_TOOL_DIR_ENV,
)
from agent_wrap.domain.logs.constants import LOG_DEBUG, STATE_FILE_NAME
from agent_wrap.domain.logs.models import DaemonState

if TYPE_CHECKING:
    from datetime import timedelta


def state_dir() -> Path:
    env_dir = os.environ.get(LOGS_TOOL_DIR_ENV)
    if env_dir:
        return Path(env_dir) / ".agent-launches"
    return AGENT_LAUNCHES_DIR


def state_file() -> Path:
    return state_dir() / STATE_FILE_NAME


def read_state() -> DaemonState | None:
    """
    Read the viewer state file, or None when missing/corrupt.

    The result is built key by key rather than cast wholesale, so a state file written
    before ``starting`` existed still reads cleanly -- it means "was listening when
    written", which is exactly False.
    """
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
        return DaemonState(
            pid=data["pid"], port=data["port"], starting=bool(data.get("starting", False))
        )
    return None


def write_state(pid: int, port: int, *, starting: bool = False) -> None:
    """
    Write viewer state to the state file.

    *starting* marks a claim staked before the viewer is listening; the viewer clears it
    by rewriting the file once it has bound its port.
    """
    state_dir().mkdir(parents=True, exist_ok=True)
    payload = {"pid": pid, "port": port, "starting": starting}
    state_file().write_text(json.dumps(payload, indent=2), encoding="utf-8")


class _LogSpan:
    """
    Context manager returned by log_info/log_debug; logs elapsed time on exit.

    *threshold* being not-None marks this as a debug span: the "completed in
    Ns" line prints via ``_print_debug`` while elapsed time stays within
    *threshold*, and escalates to ``_print_line`` (always visible) once it's
    exceeded — so an unexpectedly slow debug span still surfaces without
    ``AGENT_LOG_DEBUG``.
    """

    def __init__(
        self, category: str, description: str, *, threshold: timedelta | None = None
    ) -> None:
        self._category = category
        self._description = description
        self._threshold = threshold
        self._start = time.monotonic()

    def __enter__(self) -> _LogSpan:  # noqa: PYI034 — `Self` needs py3.11+, target is py3.10
        return self

    def __exit__(self, *exc_info: object) -> None:
        elapsed = time.monotonic() - self._start
        line = f"{self._category}: {self._description} completed in {elapsed:.2f}s"
        if self._threshold is not None and elapsed <= self._threshold.total_seconds():
            _print_debug(line)
        else:
            _print_line(line)


def log_info(category: str, description: str) -> _LogSpan:
    """
    Print a timestamped ``"<category>: <description>"`` line, always visible.

    The return value is a context manager: a bare call just prints the start
    line (for one-off markers), while ``with log_info(...):`` additionally
    prints a matching "completed in Ns" line with elapsed time on exit.
    """
    _print_line(f"{category}: {description}")
    return _LogSpan(category, description)


def log_debug(category: str, description: str, threshold: timedelta) -> _LogSpan:
    """
    Print a timestamped ``"<category>: <description>"`` line, gated by ``AGENT_LOG_DEBUG``.

    Like ``log_info``, the return value is a context manager for a matching
    "completed in Ns" line on exit. If elapsed time exceeds *threshold*, that
    completion line always prints (even without ``AGENT_LOG_DEBUG`` set).
    """
    _print_debug(f"{category}: {description}")
    return _LogSpan(category, description, threshold=threshold)


def _print_line(message: str) -> None:
    # flush=True: stdout is block-buffered once redirected to a regular file, so
    # without an explicit flush a line can sit in the buffer and be lost if the
    # process is later killed (e.g. SIGKILL after a SIGTERM shutdown times out).
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {message}", flush=True)


def _print_debug(message: str) -> None:
    if LOG_DEBUG:
        _print_line(message)
