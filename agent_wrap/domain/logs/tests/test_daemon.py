# This file has been edited with the assistance of an AI tool.
from __future__ import annotations

import json
import re
from datetime import timedelta
from typing import TYPE_CHECKING

from agent_wrap.domain.logs.daemon import (
    log_debug,
    log_info,
    read_state,
    state_file,
    write_state,
)

if TYPE_CHECKING:
    from pathlib import Path

    import pytest


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


def test_log_info_prints_timestamped_message(capsys: pytest.CaptureFixture[str]):
    log_info("Category", "hello")
    out = capsys.readouterr().out
    assert re.fullmatch(r"\[\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\] Category: hello\n", out)


def test_log_info_context_manager_logs_completion(capsys: pytest.CaptureFixture[str]):
    with log_info("Category", "hello"):
        pass
    lines = capsys.readouterr().out.splitlines()
    assert re.fullmatch(r"\[\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\] Category: hello", lines[0])
    assert re.fullmatch(
        r"\[\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\] Category: hello completed in \d+\.\d{2}s",
        lines[1],
    )


def test_log_debug_silent_when_log_debug_disabled(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
):
    monkeypatch.setattr("agent_wrap.domain.logs.daemon.LOG_DEBUG", False)
    with log_debug("Category", "hello", threshold=timedelta(seconds=60)):
        pass
    assert capsys.readouterr().out == ""


def test_log_debug_prints_when_log_debug_enabled(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
):
    monkeypatch.setattr("agent_wrap.domain.logs.daemon.LOG_DEBUG", True)
    with log_debug("Category", "hello", threshold=timedelta(seconds=60)):
        pass
    lines = capsys.readouterr().out.splitlines()
    assert re.fullmatch(r"\[\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\] Category: hello", lines[0])
    assert re.fullmatch(
        r"\[\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\] Category: hello completed in \d+\.\d{2}s",
        lines[1],
    )


def test_log_debug_escalates_completion_line_past_threshold(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
):
    monkeypatch.setattr("agent_wrap.domain.logs.daemon.LOG_DEBUG", False)
    with log_debug("Category", "hello", threshold=timedelta(seconds=-1)):
        pass
    lines = capsys.readouterr().out.splitlines()
    assert len(lines) == 1
    assert re.fullmatch(
        r"\[\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\] Category: hello completed in \d+\.\d{2}s",
        lines[0],
    )
