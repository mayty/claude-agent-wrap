# This file has been created with the assistance of an AI tool.
"""Tests for agent_wrap.config."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from agent_wrap.config import ensure_statusline, ensure_telegram_hooks

if TYPE_CHECKING:
    from pathlib import Path


class TestEnsureStatusline:
    def test_injects_into_empty_file(self, tmp_path: Path):
        settings = tmp_path / "settings.json"
        settings.write_text("{}")
        ensure_statusline(settings)
        data = json.loads(settings.read_text())
        assert "statusLine" in data
        assert data["statusLine"]["type"] == "command"
        assert "statusline.py" in data["statusLine"]["command"]

    def test_creates_file_if_missing(self, tmp_path: Path):
        settings = tmp_path / "settings.json"
        ensure_statusline(settings)
        data = json.loads(settings.read_text())
        assert "statusLine" in data

    def test_idempotent(self, tmp_path: Path):
        settings = tmp_path / "settings.json"
        settings.write_text("{}")
        ensure_statusline(settings)
        first = json.loads(settings.read_text())
        ensure_statusline(settings)
        second = json.loads(settings.read_text())
        assert first == second

    def test_does_not_overwrite_existing(self, tmp_path: Path):
        settings = tmp_path / "settings.json"
        custom = {"statusLine": {"type": "command", "command": "/custom/script"}}
        settings.write_text(json.dumps(custom))
        ensure_statusline(settings)
        data = json.loads(settings.read_text())
        assert data["statusLine"]["command"] == "/custom/script"

    def test_preserves_other_keys(self, tmp_path: Path):
        settings = tmp_path / "settings.json"
        settings.write_text(json.dumps({"theme": "dark"}))
        ensure_statusline(settings)
        data = json.loads(settings.read_text())
        assert data["theme"] == "dark"
        assert "statusLine" in data

    def test_skips_malformed_json(self, tmp_path: Path):
        settings = tmp_path / "settings.json"
        settings.write_text("{bad json")
        ensure_statusline(settings)
        assert settings.read_text() == "{bad json"


class TestEnsureTelegramHooks:
    def test_injects_all_three_hooks(self, tmp_path: Path):
        settings = tmp_path / "settings.json"
        settings.write_text("{}")
        ensure_telegram_hooks(settings)
        data = json.loads(settings.read_text())
        hooks = data["hooks"]
        assert "PermissionRequest" in hooks
        assert "Stop" in hooks
        assert "StopFailure" in hooks

    def test_idempotent(self, tmp_path: Path):
        settings = tmp_path / "settings.json"
        settings.write_text("{}")
        ensure_telegram_hooks(settings)
        first = json.loads(settings.read_text())
        ensure_telegram_hooks(settings)
        second = json.loads(settings.read_text())
        assert first == second

    def test_stop_has_argument(self, tmp_path: Path):
        settings = tmp_path / "settings.json"
        settings.write_text("{}")
        ensure_telegram_hooks(settings)
        data = json.loads(settings.read_text())
        cmd = data["hooks"]["Stop"][0]["hooks"][0]["command"]
        assert cmd.endswith("stop")

    def test_stopfailure_has_argument(self, tmp_path: Path):
        settings = tmp_path / "settings.json"
        settings.write_text("{}")
        ensure_telegram_hooks(settings)
        data = json.loads(settings.read_text())
        cmd = data["hooks"]["StopFailure"][0]["hooks"][0]["command"]
        assert cmd.endswith("stopfailure")

    def test_preserves_existing_hooks(self, tmp_path: Path):
        settings = tmp_path / "settings.json"
        existing = {"hooks": {"PreToolUse": [{"matcher": "", "hooks": []}]}}
        settings.write_text(json.dumps(existing))
        ensure_telegram_hooks(settings)
        data = json.loads(settings.read_text())
        assert "PreToolUse" in data["hooks"]
        assert "PermissionRequest" in data["hooks"]
