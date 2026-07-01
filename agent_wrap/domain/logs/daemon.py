# This file has been edited with the assistance of an AI tool.
"""Background-process lifecycle for the logs viewer."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from agent_wrap.constants import (
    AGENT_LAUNCHES_DIR,
    LOGS_TOOL_DIR_ENV,
)
from agent_wrap.domain.logs.constants import STATE_FILE_NAME


def state_dir() -> Path:
    env_dir = os.environ.get(LOGS_TOOL_DIR_ENV)
    if env_dir:
        return Path(env_dir) / ".agent-launches"
    return AGENT_LAUNCHES_DIR


def state_file() -> Path:
    return state_dir() / STATE_FILE_NAME


def read_state() -> dict[str, Any] | None:
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
        return data
    return None


def write_state(pid: int, port: int) -> None:
    """Write viewer state to the state file."""
    state_dir().mkdir(parents=True, exist_ok=True)
    state_file().write_text(json.dumps({"pid": pid, "port": port}, indent=2), encoding="utf-8")
