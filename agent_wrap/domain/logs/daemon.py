# This file has been edited with the assistance of an AI tool.
"""Background-process lifecycle for the logs viewer."""

import json
import logging
import os
import sys
import time
from pathlib import Path
from typing import TYPE_CHECKING, TextIO, override

from agent_wrap.constants import (
    AGENT_LAUNCHES_DIR,
    LOGS_TOOL_DIR_ENV,
)
from agent_wrap.domain.logs.constants import LOG_DEBUG, STATE_FILE_NAME
from agent_wrap.domain.logs.models import DaemonState

if TYPE_CHECKING:
    from datetime import timedelta
    from typing import Self


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
    except json.JSONDecodeError, ValueError:
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


class _StdoutHandler(logging.StreamHandler[TextIO]):
    """
    A ``StreamHandler`` that resolves ``sys.stdout`` per record rather than binding it.

    Late resolution is what ``print()`` did here before, and it has to stay: the daemon
    redirects its output long after this module is imported. ``StreamHandler`` flushes
    after every record, which is what keeps a line out of the block buffer a redirected
    stdout acquires — otherwise it is lost when a SIGTERM shutdown times out into a
    SIGKILL.
    """

    @override
    def emit(self, record: logging.LogRecord) -> None:
        self.stream = sys.stdout
        super().emit(record)


logger = logging.getLogger("agent_wrap.logs")
logger.setLevel(logging.DEBUG if LOG_DEBUG else logging.INFO)
logger.propagate = False
if not logger.handlers:
    _handler = _StdoutHandler()
    _handler.setFormatter(logging.Formatter("[%(asctime)s] %(message)s", "%Y-%m-%d %H:%M:%S"))
    logger.addHandler(_handler)


class _LogSpan:
    """
    Context manager returned by log_info/log_debug; logs elapsed time on exit.

    *threshold* being not-None marks this as a debug span: the "completed in Ns" line
    stays at DEBUG while elapsed time is within *threshold* and escalates to INFO once
    it is exceeded — so an unexpectedly slow debug span still surfaces without
    ``AGENT_LOG_DEBUG``.
    """

    def __init__(
        self, category: str, description: str, *, threshold: timedelta | None = None
    ) -> None:
        self._message = f"{category}: {description}"
        self._threshold = threshold
        self._start = time.monotonic()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc_info: object) -> None:
        elapsed = time.monotonic() - self._start
        within = self._threshold is not None and elapsed <= self._threshold.total_seconds()
        logger.log(
            logging.DEBUG if within else logging.INFO,
            "%s completed in %.2fs",
            self._message,
            elapsed,
        )


def log_info(category: str, description: str) -> _LogSpan:
    """
    Log a timestamped ``"<category>: <description>"`` line, always visible.

    The return value is a context manager: a bare call just logs the start line (for
    one-off markers), while ``with log_info(...):`` additionally logs a matching
    "completed in Ns" line with elapsed time on exit.
    """
    logger.info("%s: %s", category, description)
    return _LogSpan(category, description)


def log_debug(category: str, description: str, threshold: timedelta) -> _LogSpan:
    """
    Log a timestamped ``"<category>: <description>"`` line, gated by ``AGENT_LOG_DEBUG``.

    Like ``log_info``, the return value is a context manager for a matching "completed
    in Ns" line on exit. If elapsed time exceeds *threshold*, that completion line is
    logged at INFO and so appears even without ``AGENT_LOG_DEBUG`` set.
    """
    logger.debug("%s: %s", category, description)
    return _LogSpan(category, description, threshold=threshold)
