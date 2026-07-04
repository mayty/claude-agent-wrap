# This file has been edited with the assistance of an AI tool.
"""Tests for agent_wrap.config."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent_wrap.constants import STATE_FILES
from agent_wrap.domain.config.service import ConfigService
from agent_wrap.lib.path_hash import project_path_hash


@pytest.fixture
def svc() -> ConfigService:
    return ConfigService()


def test_injects_into_empty_file(svc: ConfigService, tmp_path: Path):
    settings = tmp_path / "settings.json"
    settings.write_text("{}")
    svc._ensure_statusline(settings)
    data = json.loads(settings.read_text())
    assert "statusLine" in data
    assert data["statusLine"]["type"] == "command"
    assert "statusline.py" in data["statusLine"]["command"]


def test_creates_file_if_missing(svc: ConfigService, tmp_path: Path):
    settings = tmp_path / "settings.json"
    svc._ensure_statusline(settings)
    data = json.loads(settings.read_text())
    assert "statusLine" in data


def test_idempotent(svc: ConfigService, tmp_path: Path):
    settings = tmp_path / "settings.json"
    settings.write_text("{}")
    svc._ensure_statusline(settings)
    first = json.loads(settings.read_text())
    svc._ensure_statusline(settings)
    second = json.loads(settings.read_text())
    assert first == second


def test_does_not_overwrite_existing(svc: ConfigService, tmp_path: Path):
    settings = tmp_path / "settings.json"
    custom = {"statusLine": {"type": "command", "command": "/custom/script"}}
    settings.write_text(json.dumps(custom))
    svc._ensure_statusline(settings)
    data = json.loads(settings.read_text())
    assert data["statusLine"]["command"] == "/custom/script"


def test_preserves_other_keys(svc: ConfigService, tmp_path: Path):
    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps({"theme": "dark"}))
    svc._ensure_statusline(settings)
    data = json.loads(settings.read_text())
    assert data["theme"] == "dark"
    assert "statusLine" in data


def test_skips_malformed_json(svc: ConfigService, tmp_path: Path):
    settings = tmp_path / "settings.json"
    settings.write_text("{bad json")
    svc._ensure_statusline(settings)
    assert settings.read_text() == "{bad json"


# --- telegram hooks ---


def test_injects_all_three_hooks(svc: ConfigService, tmp_path: Path):
    settings = tmp_path / "settings.json"
    settings.write_text("{}")
    svc._ensure_telegram_hooks(settings)
    data = json.loads(settings.read_text())
    hooks = data["hooks"]
    assert "PermissionRequest" in hooks
    assert "Stop" in hooks
    assert "StopFailure" in hooks


def test_telegram_hooks_idempotent(svc: ConfigService, tmp_path: Path):
    settings = tmp_path / "settings.json"
    settings.write_text("{}")
    svc._ensure_telegram_hooks(settings)
    first = json.loads(settings.read_text())
    svc._ensure_telegram_hooks(settings)
    second = json.loads(settings.read_text())
    assert first == second


def test_stop_has_argument(svc: ConfigService, tmp_path: Path):
    settings = tmp_path / "settings.json"
    settings.write_text("{}")
    svc._ensure_telegram_hooks(settings)
    data = json.loads(settings.read_text())
    cmd = data["hooks"]["Stop"][0]["hooks"][0]["command"]
    assert cmd.endswith("stop")


def test_stopfailure_has_argument(svc: ConfigService, tmp_path: Path):
    settings = tmp_path / "settings.json"
    settings.write_text("{}")
    svc._ensure_telegram_hooks(settings)
    data = json.loads(settings.read_text())
    cmd = data["hooks"]["StopFailure"][0]["hooks"][0]["command"]
    assert cmd.endswith("stopfailure")


def test_preserves_existing_hooks(svc: ConfigService, tmp_path: Path):
    settings = tmp_path / "settings.json"
    existing = {"hooks": {"PreToolUse": [{"matcher": ".*", "hooks": [{"type": "builtin"}]}]}}
    settings.write_text(json.dumps(existing))
    svc._ensure_telegram_hooks(settings)
    data = json.loads(settings.read_text())
    assert "PreToolUse" in data["hooks"]
    assert "PermissionRequest" in data["hooks"]
    # Verify existing hook content was not mutated
    assert data["hooks"]["PreToolUse"][0]["matcher"] == ".*"
    assert data["hooks"]["PreToolUse"][0]["hooks"][0]["type"] == "builtin"


def test_telegram_hooks_skips_malformed_json(svc: ConfigService, tmp_path: Path):
    settings = tmp_path / "settings.json"
    settings.write_text("{bad json")
    svc._ensure_telegram_hooks(settings)
    assert settings.read_text() == "{bad json"


# --- ensure_claude_md ---


def test_ensure_claude_md_copies_when_missing(svc: ConfigService, tmp_path: Path) -> None:
    (tmp_path / ".claude").mkdir()
    (tmp_path / "ops").mkdir()
    (tmp_path / "ops" / "default-CLAUDE.md").write_text("# hello")
    svc._ensure_claude_md()
    target = tmp_path / ".claude" / "CLAUDE.md"
    assert target.exists()
    assert target.read_text() == "# hello"


def test_ensure_claude_md_skips_when_exists(svc: ConfigService, tmp_path: Path) -> None:
    target_dir = tmp_path / ".claude"
    target_dir.mkdir(parents=True)
    target = target_dir / "CLAUDE.md"
    target.write_text("# user content")
    (tmp_path / "ops").mkdir()
    (tmp_path / "ops" / "default-CLAUDE.md").write_text("# default")
    svc._ensure_claude_md()
    assert target.read_text() == "# user content"


def test_ensure_claude_md_skips_when_no_template(svc: ConfigService, tmp_path: Path) -> None:
    (tmp_path / ".claude").mkdir()
    svc._ensure_claude_md()
    assert not (tmp_path / ".claude" / "CLAUDE.md").exists()


# --- prepare_global_config ---


def test_prepare_global_config_creates_structure(svc: ConfigService, tmp_path: Path) -> None:
    svc.prepare_global_config(telegram_available=False)
    assert (tmp_path / ".claude.json").exists()
    assert (tmp_path / ".claude" / "settings.json").exists()
    assert (tmp_path / ".claude" / "projects" / "-workspace").exists()


def test_prepare_global_config_seeds_valid_json(svc: ConfigService, tmp_path: Path) -> None:
    # Claude Code aborts on startup if .claude.json is empty/invalid, so the
    # seeded files must be parseable JSON, not zero-byte touch() output.
    svc.prepare_global_config(telegram_available=False)
    assert json.loads((tmp_path / ".claude.json").read_text()) == {}


def test_prepare_global_config_repairs_empty_claude_json(
    svc: ConfigService, tmp_path: Path
) -> None:
    # A pre-existing zero-byte .claude.json (e.g. from an older wrapper that
    # touch()ed it) must be repaired, not left to crash Claude Code.
    empty = tmp_path / ".claude.json"
    empty.touch()
    svc.prepare_global_config(telegram_available=False)
    assert json.loads(empty.read_text()) == {}


def test_prepare_global_config_preserves_existing_claude_json(
    svc: ConfigService, tmp_path: Path
) -> None:
    existing = tmp_path / ".claude.json"
    existing.write_text('{"foo": "bar"}\n')
    svc.prepare_global_config(telegram_available=False)
    assert json.loads(existing.read_text()) == {"foo": "bar"}


def test_prepare_global_config_with_telegram(svc: ConfigService, tmp_path: Path) -> None:
    svc.prepare_global_config(telegram_available=True)
    settings = json.loads((tmp_path / ".claude" / "settings.json").read_text())
    assert "hooks" in settings
    assert "PermissionRequest" in settings["hooks"]


def test_prepare_global_config_without_telegram(svc: ConfigService, tmp_path: Path) -> None:
    svc.prepare_global_config(telegram_available=False)
    settings = json.loads((tmp_path / ".claude" / "settings.json").read_text())
    assert "hooks" not in settings


# --- prepare_project_dirs ---


_STATE_DIRS = ("sessions", "session-state", "daemon", "jobs", "plans", "todos", "tasks")


@pytest.mark.parametrize("subdir", _STATE_DIRS)
def test_prepare_project_dirs_creates_subdirs(
    svc: ConfigService, tmp_path: Path, subdir: str
) -> None:
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    svc.prepare_project_dirs(project_dir, _STATE_DIRS, STATE_FILES)
    assert (project_dir / ".claude" / subdir).exists()


@pytest.mark.parametrize("filename", STATE_FILES)
def test_prepare_project_dirs_creates_state_files(
    svc: ConfigService, tmp_path: Path, filename: str
) -> None:
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    svc.prepare_project_dirs(project_dir, _STATE_DIRS, STATE_FILES)
    assert (project_dir / ".claude" / filename).exists()


def test_prepare_project_dirs_creates_gitignore(svc: ConfigService, tmp_path: Path) -> None:
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    svc.prepare_project_dirs(project_dir, _STATE_DIRS, STATE_FILES)
    claude_dir = project_dir / ".claude"
    assert (claude_dir / ".gitignore").exists()
    assert (claude_dir / ".gitignore").read_text() == "*\n"


def test_prepare_project_dirs_idempotent(svc: ConfigService, tmp_path: Path) -> None:
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    svc.prepare_project_dirs(project_dir, _STATE_DIRS, STATE_FILES)
    svc.prepare_project_dirs(project_dir, _STATE_DIRS, STATE_FILES)  # should not raise


_STATE_DIRS_WITH_MEMORY = (*_STATE_DIRS, "memory")


def test_prepare_project_dirs_migrates_old_memory_files(svc: ConfigService, tmp_path: Path) -> None:
    """Old memory files under sessions/memory/ are moved to memory/."""
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    claude_dir = project_dir / ".claude"

    # Simulate pre-migration state: sessions/memory/ exists with old files,
    # and prepare_project_dirs has already created the empty memory/ dir.
    old_memory_dir = claude_dir / "sessions" / "memory"
    old_memory_dir.mkdir(parents=True)
    new_memory_dir = claude_dir / "memory"
    new_memory_dir.mkdir(parents=True)
    (old_memory_dir / "MEMORY.md").write_text("old index")
    (old_memory_dir / "some-fact.md").write_text("old fact")

    svc.prepare_project_dirs(project_dir, _STATE_DIRS_WITH_MEMORY, STATE_FILES)

    # Files should be moved to the new location.
    assert (new_memory_dir / "MEMORY.md").read_text() == "old index"
    assert (new_memory_dir / "some-fact.md").read_text() == "old fact"
    # Old location should be empty.
    assert list(old_memory_dir.iterdir()) == []


def test_prepare_project_dirs_migration_skips_existing_destination_files(
    svc: ConfigService, tmp_path: Path
) -> None:
    """Files already present at the destination are not overwritten."""
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    claude_dir = project_dir / ".claude"

    old_memory_dir = claude_dir / "sessions" / "memory"
    old_memory_dir.mkdir(parents=True)
    new_memory_dir = claude_dir / "memory"
    new_memory_dir.mkdir(parents=True)

    (old_memory_dir / "old-fact.md").write_text("old version")
    (new_memory_dir / "old-fact.md").write_text("newer version")

    svc.prepare_project_dirs(project_dir, _STATE_DIRS_WITH_MEMORY, STATE_FILES)

    # Destination file should not be overwritten.
    assert (new_memory_dir / "old-fact.md").read_text() == "newer version"
    # Old file should remain.
    assert (old_memory_dir / "old-fact.md").read_text() == "old version"


def test_prepare_project_dirs_migration_idempotent(svc: ConfigService, tmp_path: Path) -> None:
    """Running the migration twice is safe."""
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    claude_dir = project_dir / ".claude"

    old_memory_dir = claude_dir / "sessions" / "memory"
    old_memory_dir.mkdir(parents=True)
    new_memory_dir = claude_dir / "memory"
    new_memory_dir.mkdir(parents=True)
    (old_memory_dir / "fact.md").write_text("fact")

    svc.prepare_project_dirs(project_dir, _STATE_DIRS_WITH_MEMORY, STATE_FILES)
    svc.prepare_project_dirs(project_dir, _STATE_DIRS_WITH_MEMORY, STATE_FILES)

    assert (new_memory_dir / "fact.md").read_text() == "fact"
    assert list(old_memory_dir.iterdir()) == []


# --- record_project ---


def test_record_project_appends_cwd(
    svc: ConfigService, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    svc.record_project()
    projects_file = tmp_path / ".agent-launches" / "projects.txt"
    assert projects_file.exists()
    assert str(tmp_path) in projects_file.read_text()


def test_record_project_deduplicates(
    svc: ConfigService, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    svc.record_project()
    svc.record_project()
    projects_file = tmp_path / ".agent-launches" / "projects.txt"
    lines = projects_file.read_text().splitlines()
    assert lines.count(str(tmp_path)) == 1


def test_record_project_uses_pwd_env_when_consistent(
    svc: ConfigService, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real)

    monkeypatch.chdir(link)
    monkeypatch.setenv("PWD", str(link))
    svc.record_project()

    lines = (tmp_path / ".agent-launches" / "projects.txt").read_text().splitlines()
    assert str(link) in lines
    assert str(real) not in lines


def test_record_project_replaces_alias_pointing_to_same_target(
    svc: ConfigService, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
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
    svc.record_project()

    lines = projects_file.read_text().splitlines()
    assert lines == [str(link)]


def test_record_project_keeps_file_sorted(
    svc: ConfigService, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    launches = tmp_path / ".agent-launches"
    launches.mkdir()
    projects_file = launches / "projects.txt"
    projects_file.write_text("/z\n/a\n/m\n")

    extra = tmp_path / "extra"
    extra.mkdir()
    monkeypatch.chdir(extra)
    monkeypatch.delenv("PWD", raising=False)
    svc.record_project()

    lines = projects_file.read_text().splitlines()
    assert lines == sorted(lines)
    assert str(extra) in lines


# --- link_litellm_logs ---


def test_link_litellm_logs_creates_symlink(svc: ConfigService, tmp_path: Path) -> None:
    project = tmp_path / "project"
    (project / ".claude").mkdir(parents=True)

    svc.link_litellm_logs(project)

    link = project / ".claude" / "litellm-logs"
    target = tmp_path / "litellm-logs" / project_path_hash(project)
    assert link.is_symlink()
    assert link.resolve() == target.resolve()
    assert target.is_dir()


def test_link_litellm_logs_idempotent(svc: ConfigService, tmp_path: Path) -> None:
    project = tmp_path / "project"
    (project / ".claude").mkdir(parents=True)

    svc.link_litellm_logs(project)
    svc.link_litellm_logs(project)  # second call must not raise or change state

    link = project / ".claude" / "litellm-logs"
    target = tmp_path / "litellm-logs" / project_path_hash(project)
    assert link.is_symlink()
    assert link.resolve() == target.resolve()


def test_link_litellm_logs_repoints_stale_symlink(svc: ConfigService, tmp_path: Path) -> None:
    project = tmp_path / "project"
    (project / ".claude").mkdir(parents=True)

    bogus = tmp_path / "bogus"
    bogus.mkdir()
    link = project / ".claude" / "litellm-logs"
    link.symlink_to(bogus)

    svc.link_litellm_logs(project)

    target = tmp_path / "litellm-logs" / project_path_hash(project)
    assert link.is_symlink()
    assert link.resolve() == target.resolve()


def test_link_litellm_logs_backs_up_real_directory(
    svc: ConfigService, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project = tmp_path / "project"
    old = project / ".claude" / "litellm-logs"
    old.mkdir(parents=True)
    (old / "litellm-bedrock").mkdir()
    (old / "litellm-bedrock" / "keep.txt").write_text("old data")

    svc.link_litellm_logs(project)

    link = project / ".claude" / "litellm-logs"
    bkp = project / ".claude" / "litellm-logs-bkp"
    # Old data preserved in the backup, link now points at the shared target.
    assert (bkp / "litellm-bedrock" / "keep.txt").read_text() == "old data"
    assert link.is_symlink()
    assert link.resolve() == (tmp_path / "litellm-logs" / project_path_hash(project)).resolve()
    assert "backed up" in capsys.readouterr().err


def test_link_litellm_logs_backup_suffix_on_collision(svc: ConfigService, tmp_path: Path) -> None:
    project = tmp_path / "project"
    old = project / ".claude" / "litellm-logs"
    old.mkdir(parents=True)
    (old / "marker.txt").write_text("data")
    # A previous backup already occupies the default name.
    (project / ".claude" / "litellm-logs-bkp").mkdir()

    svc.link_litellm_logs(project)

    # The new backup gets a numeric suffix rather than clobbering the old one.
    assert (project / ".claude" / "litellm-logs-bkp-2" / "marker.txt").read_text() == "data"
    assert (project / ".claude" / "litellm-logs").is_symlink()
