# This file has been edited with the assistance of an AI tool.
from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING

from agent_wrap.domain.logs.daemon import (
    log_event,
    read_state,
    state_file,
    write_state,
)

if TYPE_CHECKING:
    from pathlib import Path

    import pytest


# --- background server: state file + liveness ------------------------------


def test_state_file_path(tmp_path: Path):
    assert state_file() == tmp_path / ".agent-launches" / "logs-server.json"


def test_read_state_missing_returns_none():
    assert read_state() is None


def test_read_state_corrupt_returns_none(tmp_path: Path):
    (tmp_path / ".agent-launches").mkdir()
    state_file().write_text("not json {{{", encoding="utf-8")
    assert read_state() is None


def test_read_state_rejects_wrong_shape(tmp_path: Path):
    (tmp_path / ".agent-launches").mkdir()
    state_file().write_text(json.dumps({"pid": "x", "port": 8765}), encoding="utf-8")
    assert read_state() is None


def test_write_thenread_state_round_trip():
    write_state(pid=4242, port=8765)
    state = read_state()
    assert state == {"pid": 4242, "port": 8765}


def test_log_event_prints_timestamped_message(capsys: pytest.CaptureFixture[str]):
    log_event("Category", "hello")
    out = capsys.readouterr().out
    assert re.fullmatch(r"\[\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\] Category: hello\n", out)


def test_log_event_context_manager_logs_completion(capsys: pytest.CaptureFixture[str]):
    with log_event("Category", "hello"):
        pass
    lines = capsys.readouterr().out.splitlines()
    assert re.fullmatch(r"\[\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\] Category: hello", lines[0])
    assert re.fullmatch(
        r"\[\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\] Category: hello completed in \d+\.\d{2}s",
        lines[1],
    )
