# This file has been edited with the assistance of an AI tool.
"""Tests for agent_wrap.config."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent_wrap.config import (
    ensure_claude_md,
    ensure_statusline,
    ensure_telegram_hooks,
    prepare_global_config,
    prepare_project_dirs,
    record_project,
)


def test_injects_into_empty_file(tmp_path: Path):
    settings = tmp_path / "settings.json"
    settings.write_text("{}")
    ensure_statusline(settings)
    data = json.loads(settings.read_text())
    assert "statusLine" in data
    assert data["statusLine"]["type"] == "command"
    assert "statusline.py" in data["statusLine"]["command"]


def test_creates_file_if_missing(tmp_path: Path):
    settings = tmp_path / "settings.json"
    ensure_statusline(settings)
    data = json.loads(settings.read_text())
    assert "statusLine" in data


def test_idempotent(tmp_path: Path):
    settings = tmp_path / "settings.json"
    settings.write_text("{}")
    ensure_statusline(settings)
    first = json.loads(settings.read_text())
    ensure_statusline(settings)
    second = json.loads(settings.read_text())
    assert first == second


def test_does_not_overwrite_existing(tmp_path: Path):
    settings = tmp_path / "settings.json"
    custom = {"statusLine": {"type": "command", "command": "/custom/script"}}
    settings.write_text(json.dumps(custom))
    ensure_statusline(settings)
    data = json.loads(settings.read_text())
    assert data["statusLine"]["command"] == "/custom/script"


def test_preserves_other_keys(tmp_path: Path):
    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps({"theme": "dark"}))
    ensure_statusline(settings)
    data = json.loads(settings.read_text())
    assert data["theme"] == "dark"
    assert "statusLine" in data


def test_skips_malformed_json(tmp_path: Path):
    settings = tmp_path / "settings.json"
    settings.write_text("{bad json")
    ensure_statusline(settings)
    assert settings.read_text() == "{bad json"


def test_statusline_migration_bare_path(tmp_path: Path):
    """Old bare-path entry is migrated to python3-prefixed form."""
    settings = tmp_path / "settings.json"
    settings.write_text(
        json.dumps({"statusLine": {"type": "command", "command": "/opt/agent-wrap/statusline.py"}})
    )
    ensure_statusline(settings)
    data = json.loads(settings.read_text())
    assert data["statusLine"]["command"] == "python3 /opt/agent-wrap/statusline.py"


# --- telegram hooks ---


def test_injects_all_three_hooks(tmp_path: Path):
    settings = tmp_path / "settings.json"
    settings.write_text("{}")
    ensure_telegram_hooks(settings)
    data = json.loads(settings.read_text())
    hooks = data["hooks"]
    assert "PermissionRequest" in hooks
    assert "Stop" in hooks
    assert "StopFailure" in hooks


def test_telegram_hooks_idempotent(tmp_path: Path):
    settings = tmp_path / "settings.json"
    settings.write_text("{}")
    ensure_telegram_hooks(settings)
    first = json.loads(settings.read_text())
    ensure_telegram_hooks(settings)
    second = json.loads(settings.read_text())
    assert first == second


def test_stop_has_argument(tmp_path: Path):
    settings = tmp_path / "settings.json"
    settings.write_text("{}")
    ensure_telegram_hooks(settings)
    data = json.loads(settings.read_text())
    cmd = data["hooks"]["Stop"][0]["hooks"][0]["command"]
    assert cmd.endswith("stop")


def test_stopfailure_has_argument(tmp_path: Path):
    settings = tmp_path / "settings.json"
    settings.write_text("{}")
    ensure_telegram_hooks(settings)
    data = json.loads(settings.read_text())
    cmd = data["hooks"]["StopFailure"][0]["hooks"][0]["command"]
    assert cmd.endswith("stopfailure")


def test_preserves_existing_hooks(tmp_path: Path):
    settings = tmp_path / "settings.json"
    existing = {"hooks": {"PreToolUse": [{"matcher": ".*", "hooks": [{"type": "builtin"}]}]}}
    settings.write_text(json.dumps(existing))
    ensure_telegram_hooks(settings)
    data = json.loads(settings.read_text())
    assert "PreToolUse" in data["hooks"]
    assert "PermissionRequest" in data["hooks"]
    # Verify existing hook content was not mutated
    assert data["hooks"]["PreToolUse"][0]["matcher"] == ".*"
    assert data["hooks"]["PreToolUse"][0]["hooks"][0]["type"] == "builtin"


def test_telegram_hooks_skips_malformed_json(tmp_path: Path):
    settings = tmp_path / "settings.json"
    settings.write_text("{bad json")
    ensure_telegram_hooks(settings)
    assert settings.read_text() == "{bad json"


def test_telegram_hooks_migration_bare_path(tmp_path: Path):
    """Old bare-path hook entries are migrated to bash-prefixed form."""
    settings = tmp_path / "settings.json"
    existing = {
        "hooks": {
            "PermissionRequest": [
                {
                    "matcher": "",
                    "hooks": [{"type": "command", "command": "/opt/agent-wrap/telegram-notify.sh"}],
                }
            ]
        }
    }
    settings.write_text(json.dumps(existing))
    ensure_telegram_hooks(settings)
    data = json.loads(settings.read_text())
    cmd = data["hooks"]["PermissionRequest"][0]["hooks"][0]["command"]
    assert cmd.startswith("bash ")


# --- ensure_claude_md ---


def test_ensure_claude_md_copies_when_missing(tmp_path: Path) -> None:
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / ".claude").mkdir()  # ensure_claude_md expects this to exist
    template = tmp_path / "default-CLAUDE.md"
    template.write_text("# hello")
    ensure_claude_md(config_dir, template)
    target = config_dir / ".claude" / "CLAUDE.md"
    assert target.exists()
    assert target.read_text() == "# hello"


def test_ensure_claude_md_skips_when_exists(tmp_path: Path) -> None:
    config_dir = tmp_path / "config"
    target_dir = config_dir / ".claude"
    target_dir.mkdir(parents=True)
    target = target_dir / "CLAUDE.md"
    target.write_text("# user content")
    template = tmp_path / "default-CLAUDE.md"
    template.write_text("# default")
    ensure_claude_md(config_dir, template)
    assert target.read_text() == "# user content"


def test_ensure_claude_md_skips_when_no_template(tmp_path: Path) -> None:
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    ensure_claude_md(config_dir, tmp_path / "nonexistent-CLAUDE.md")
    assert not (config_dir / ".claude" / "CLAUDE.md").exists()


# --- prepare_global_config ---


def test_prepare_global_config_creates_structure(tmp_path: Path) -> None:
    prepare_global_config(tmp_path, tmp_path)
    assert (tmp_path / ".claude.json").exists()
    assert (tmp_path / ".claude" / "settings.json").exists()
    assert (tmp_path / ".claude" / "projects" / "-workspace").exists()


def test_prepare_global_config_with_telegram(tmp_path: Path) -> None:
    prepare_global_config(tmp_path, tmp_path, telegram_bot_token="abc", telegram_chat_id="123")  # noqa: S106
    settings = json.loads((tmp_path / ".claude" / "settings.json").read_text())
    assert "hooks" in settings
    assert "PermissionRequest" in settings["hooks"]


def test_prepare_global_config_without_telegram(tmp_path: Path) -> None:
    prepare_global_config(tmp_path, tmp_path)
    settings = json.loads((tmp_path / ".claude" / "settings.json").read_text())
    assert "hooks" not in settings


# --- prepare_project_dirs ---


def test_prepare_project_dirs_creates_subdirs(tmp_path: Path) -> None:
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    prepare_project_dirs(project_dir)
    claude_dir = project_dir / ".claude"
    for subdir in ("sessions", "session-state", "daemon", "jobs", "plans", "todos", "tasks"):
        assert (claude_dir / subdir).exists()


def test_prepare_project_dirs_creates_files(tmp_path: Path) -> None:
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    prepare_project_dirs(project_dir)
    claude_dir = project_dir / ".claude"
    for name in ("daemon.lock", "daemon.log", "daemon.status.json", "history.jsonl"):
        assert (claude_dir / name).exists()
    assert (claude_dir / ".gitignore").exists()
    assert (claude_dir / ".gitignore").read_text() == "*\n"


def test_prepare_project_dirs_idempotent(tmp_path: Path) -> None:
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    prepare_project_dirs(project_dir)
    prepare_project_dirs(project_dir)  # should not raise


# --- record_project ---


def test_record_project_appends_cwd(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    record_project(tmp_path)
    projects_file = tmp_path / ".agent-launches" / "projects.txt"
    assert projects_file.exists()
    assert str(tmp_path) in projects_file.read_text()


def test_record_project_deduplicates(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    record_project(tmp_path)
    record_project(tmp_path)
    projects_file = tmp_path / ".agent-launches" / "projects.txt"
    lines = projects_file.read_text().splitlines()
    assert lines.count(str(tmp_path)) == 1
