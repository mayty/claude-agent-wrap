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
    link_litellm_logs,
    prepare_global_config,
    prepare_project_dirs,
    record_project,
)
from agent_wrap.lib.utils import project_path_hash


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


_STATE_DIRS = ("sessions", "session-state", "daemon", "jobs", "plans", "todos", "tasks")
_STATE_FILES = ("daemon.lock", "daemon.log", "daemon.status.json", "history.jsonl")


def test_prepare_project_dirs_creates_subdirs(tmp_path: Path) -> None:
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    prepare_project_dirs(project_dir, _STATE_DIRS, _STATE_FILES)
    claude_dir = project_dir / ".claude"
    for subdir in _STATE_DIRS:
        assert (claude_dir / subdir).exists()


def test_prepare_project_dirs_creates_files(tmp_path: Path) -> None:
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    prepare_project_dirs(project_dir, _STATE_DIRS, _STATE_FILES)
    claude_dir = project_dir / ".claude"
    for name in _STATE_FILES:
        assert (claude_dir / name).exists()
    assert (claude_dir / ".gitignore").exists()
    assert (claude_dir / ".gitignore").read_text() == "*\n"


def test_prepare_project_dirs_idempotent(tmp_path: Path) -> None:
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    prepare_project_dirs(project_dir, _STATE_DIRS, _STATE_FILES)
    prepare_project_dirs(project_dir, _STATE_DIRS, _STATE_FILES)  # should not raise


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


def test_record_project_uses_pwd_env_when_consistent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real)

    monkeypatch.chdir(link)
    monkeypatch.setenv("PWD", str(link))
    record_project(tmp_path)

    lines = (tmp_path / ".agent-launches" / "projects.txt").read_text().splitlines()
    assert str(link) in lines
    assert str(real) not in lines


def test_record_project_replaces_alias_pointing_to_same_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real)

    launches = tmp_path / ".agent-launches"
    launches.mkdir()
    projects_file = launches / "projects.txt"
    projects_file.write_text(f"{real}\n")

    monkeypatch.chdir(link)
    monkeypatch.setenv("PWD", str(link))
    record_project(tmp_path)

    lines = projects_file.read_text().splitlines()
    assert lines == [str(link)]


def test_record_project_keeps_file_sorted(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    launches = tmp_path / ".agent-launches"
    launches.mkdir()
    projects_file = launches / "projects.txt"
    projects_file.write_text("/z\n/a\n/m\n")

    extra = tmp_path / "extra"
    extra.mkdir()
    monkeypatch.chdir(extra)
    monkeypatch.delenv("PWD", raising=False)
    record_project(tmp_path)

    lines = projects_file.read_text().splitlines()
    assert lines == sorted(lines)
    assert str(extra) in lines


# --- link_litellm_logs ---


def test_link_litellm_logs_creates_symlink(tmp_path: Path) -> None:
    project = tmp_path / "project"
    tool = tmp_path / "tool"
    (project / ".claude").mkdir(parents=True)
    tool.mkdir()

    link_litellm_logs(project, tool)

    link = project / ".claude" / "litellm-logs"
    target = tool / "litellm-logs" / project_path_hash(project)
    assert link.is_symlink()
    assert link.resolve() == target.resolve()
    assert target.is_dir()


def test_link_litellm_logs_idempotent(tmp_path: Path) -> None:
    project = tmp_path / "project"
    tool = tmp_path / "tool"
    (project / ".claude").mkdir(parents=True)
    tool.mkdir()

    link_litellm_logs(project, tool)
    link_litellm_logs(project, tool)  # second call must not raise or change state

    link = project / ".claude" / "litellm-logs"
    target = tool / "litellm-logs" / project_path_hash(project)
    assert link.is_symlink()
    assert link.resolve() == target.resolve()


def test_link_litellm_logs_repoints_stale_symlink(tmp_path: Path) -> None:
    project = tmp_path / "project"
    tool = tmp_path / "tool"
    (project / ".claude").mkdir(parents=True)
    tool.mkdir()

    bogus = tmp_path / "bogus"
    bogus.mkdir()
    link = project / ".claude" / "litellm-logs"
    link.symlink_to(bogus)

    link_litellm_logs(project, tool)

    target = tool / "litellm-logs" / project_path_hash(project)
    assert link.is_symlink()
    assert link.resolve() == target.resolve()


def test_link_litellm_logs_backs_up_real_directory(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    project = tmp_path / "project"
    tool = tmp_path / "tool"
    old = project / ".claude" / "litellm-logs"
    old.mkdir(parents=True)
    (old / "litellm-bedrock").mkdir()
    (old / "litellm-bedrock" / "keep.txt").write_text("old data")
    tool.mkdir()

    link_litellm_logs(project, tool)

    link = project / ".claude" / "litellm-logs"
    bkp = project / ".claude" / "litellm-logs-bkp"
    # Old data preserved in the backup, link now points at the shared target.
    assert (bkp / "litellm-bedrock" / "keep.txt").read_text() == "old data"
    assert link.is_symlink()
    assert link.resolve() == (tool / "litellm-logs" / project_path_hash(project)).resolve()
    assert "backed up" in capsys.readouterr().err


def test_link_litellm_logs_backup_suffix_on_collision(tmp_path: Path) -> None:
    project = tmp_path / "project"
    tool = tmp_path / "tool"
    old = project / ".claude" / "litellm-logs"
    old.mkdir(parents=True)
    (old / "marker.txt").write_text("data")
    # A previous backup already occupies the default name.
    (project / ".claude" / "litellm-logs-bkp").mkdir()
    tool.mkdir()

    link_litellm_logs(project, tool)

    # The new backup gets a numeric suffix rather than clobbering the old one.
    assert (project / ".claude" / "litellm-logs-bkp-2" / "marker.txt").read_text() == "data"
    assert (project / ".claude" / "litellm-logs").is_symlink()
