# This file has been edited with the assistance of an AI tool.
"""Tests for agent_wrap.config."""

import itertools
import json
import os
import stat
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import Mock

import pytest

import agent_wrap.domain.config.service as config_mod
from agent_wrap.constants import STATE_FILES
from agent_wrap.domain.config.service import ConfigService
from agent_wrap.domain.display.service import DisplayService
from agent_wrap.domain.launch.constants import EXTERNAL_STATE_MOUNTS, STATE_MOUNTS
from agent_wrap.exceptions import HostMountError, StorageError
from agent_wrap.infrastructure.projects.repositories.projects import ProjectsRepository
from agent_wrap.lib.path_hash import project_path_hash

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    import pytest_mock

    from agent_wrap.containers import Core


@pytest.fixture
def svc(projects_repository: ProjectsRepository) -> ConfigService:
    return ConfigService(
        display_service=Mock(spec=DisplayService), projects_repository=projects_repository
    )


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


def test_injects_all_four_hooks(svc: ConfigService, tmp_path: Path):
    settings = tmp_path / "settings.json"
    settings.write_text("{}")
    svc._ensure_telegram_hooks(settings)
    data = json.loads(settings.read_text())
    hooks = data["hooks"]
    assert "PermissionRequest" in hooks
    assert "Stop" in hooks
    assert "StopFailure" in hooks
    assert "SessionEnd" in hooks


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


def test_sessionend_has_argument(svc: ConfigService, tmp_path: Path):
    settings = tmp_path / "settings.json"
    settings.write_text("{}")
    svc._ensure_telegram_hooks(settings)
    data = json.loads(settings.read_text())
    cmd = data["hooks"]["SessionEnd"][0]["hooks"][0]["command"]
    assert cmd.endswith("sessionend")


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


def test_spellcheck_injects_block(svc: ConfigService, tmp_path: Path) -> None:
    settings = tmp_path / "settings.json"
    settings.write_text("{}")
    svc._ensure_spellcheck(settings)
    block = json.loads(settings.read_text())["spellcheck"]
    assert block["enabled"] is True
    assert block["checker"] == "hunspell"
    assert block["language"] == "en_US,ru_RU"


def test_spellcheck_creates_file_if_missing(svc: ConfigService, tmp_path: Path) -> None:
    settings = tmp_path / "settings.json"
    svc._ensure_spellcheck(settings)
    assert "spellcheck" in json.loads(settings.read_text())


def test_spellcheck_idempotent(svc: ConfigService, tmp_path: Path) -> None:
    settings = tmp_path / "settings.json"
    settings.write_text("{}")
    svc._ensure_spellcheck(settings)
    first = json.loads(settings.read_text())
    svc._ensure_spellcheck(settings)
    assert json.loads(settings.read_text()) == first


def test_spellcheck_leaves_existing_block_alone(svc: ConfigService, tmp_path: Path) -> None:
    # With neither env var set, whatever is in the file wins -- including "off".
    custom = {"spellcheck": {"enabled": False, "language": "fr_FR"}}
    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps(custom))
    svc._ensure_spellcheck(settings)
    assert json.loads(settings.read_text()) == custom


def test_spellcheck_disabled_by_env_on_injection(
    svc: ConfigService, tmp_path: Path, mocker: pytest_mock.MockFixture
) -> None:
    mocker.patch.object(config_mod, "SPELLCHECK_ENABLED_OVERRIDE", new=False)
    settings = tmp_path / "settings.json"
    settings.write_text("{}")
    svc._ensure_spellcheck(settings)
    assert json.loads(settings.read_text())["spellcheck"]["enabled"] is False


def test_spellcheck_env_overrides_existing_enabled(
    svc: ConfigService, tmp_path: Path, mocker: pytest_mock.MockFixture
) -> None:
    # An explicit AGENT_SPELLCHECK must not be inert once a block already exists,
    # which is the whole point of it being an override rather than a seed.
    mocker.patch.object(config_mod, "SPELLCHECK_ENABLED_OVERRIDE", new=False)
    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps({"spellcheck": {"enabled": True, "language": "en_US"}}))
    svc._ensure_spellcheck(settings)
    block = json.loads(settings.read_text())["spellcheck"]
    assert block["enabled"] is False
    assert block["language"] == "en_US"


def test_spellcheck_env_overrides_language_preserving_user_keys(
    svc: ConfigService, tmp_path: Path, mocker: pytest_mock.MockFixture
) -> None:
    mocker.patch.object(config_mod, "SPELLCHECK_LANG_OVERRIDE", "de_DE")
    settings = tmp_path / "settings.json"
    settings.write_text(
        json.dumps({"spellcheck": {"enabled": True, "language": "en_US", "color": "magenta"}})
    )
    svc._ensure_spellcheck(settings)
    block = json.loads(settings.read_text())["spellcheck"]
    assert block["language"] == "de_DE"
    assert block["color"] == "magenta"
    assert block["enabled"] is True


def test_spellcheck_preserves_other_keys(svc: ConfigService, tmp_path: Path) -> None:
    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps({"theme": "dark"}))
    svc._ensure_spellcheck(settings)
    data = json.loads(settings.read_text())
    assert data["theme"] == "dark"
    assert "spellcheck" in data


def test_spellcheck_skips_non_dict_block(svc: ConfigService, tmp_path: Path) -> None:
    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps({"spellcheck": "yes please"}))
    svc._ensure_spellcheck(settings)
    assert json.loads(settings.read_text())["spellcheck"] == "yes please"


def test_spellcheck_skips_malformed_json(svc: ConfigService, tmp_path: Path) -> None:
    settings = tmp_path / "settings.json"
    settings.write_text("{bad json")
    svc._ensure_spellcheck(settings)
    assert settings.read_text() == "{bad json"


def test_ensure_claude_md_copies_when_missing(svc: ConfigService, tmp_path: Path) -> None:
    (tmp_path / ".claude").mkdir()
    (tmp_path / "ops").mkdir()
    (tmp_path / "ops" / "default-CLAUDE.md").write_text("# hello")
    svc._ensure_claude_md()
    target = tmp_path / ".claude" / "CLAUDE.md"
    assert target.exists()
    assert target.read_text() == "# hello"


def test_ensure_claude_md_preserves_template_metadata(svc: ConfigService, tmp_path: Path) -> None:
    """The copy keeps the template's mtime and mode (Path.copy drops both by default)."""
    (tmp_path / ".claude").mkdir()
    (tmp_path / "ops").mkdir()
    template = tmp_path / "ops" / "default-CLAUDE.md"
    template.write_text("# hello")
    template.chmod(0o640)
    os.utime(template, (1_000_000, 1_000_000))
    before = template.stat()

    svc._ensure_claude_md()

    after = (tmp_path / ".claude" / "CLAUDE.md").stat()
    assert after.st_mtime_ns == before.st_mtime_ns
    assert stat.S_IMODE(after.st_mode) == 0o640


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


def test_prepare_global_config_enables_spellcheck(svc: ConfigService, tmp_path: Path) -> None:
    svc.prepare_global_config(telegram_available=False)
    settings = json.loads((tmp_path / ".claude" / "settings.json").read_text())
    assert settings["spellcheck"]["enabled"] is True
    assert settings["spellcheck"]["checker"] == "hunspell"


# Derived from the production mount tables rather than hand-copied: the previous
# literal had silently drifted out of date, so every directory added to a mount table
# went untested.
_STATE_DIRS = (*STATE_MOUNTS, *EXTERNAL_STATE_MOUNTS)


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


def test_prepare_project_dirs_creates_nested_instance_dirs(
    svc: ConfigService, tmp_path: Path
) -> None:
    """Per-container state is passed in as instance-relative paths."""
    project_dir = tmp_path / "project"
    project_dir.mkdir()

    svc.prepare_project_dirs(project_dir, ["instances/agent-abc/daemon"], [])

    assert (project_dir / ".claude" / "instances" / "agent-abc" / "daemon").is_dir()


def test_prepare_project_dirs_creates_nested_file_without_sibling_dir(
    svc: ConfigService, tmp_path: Path
) -> None:
    """
    A nested state file must not depend on a state *dir* having built its parent.

    INSTANCE_STATE_MOUNTS and INSTANCE_STATE_FILES read as independent tables, so
    emptying the former must not turn every launch into a FileNotFoundError here.
    """
    project_dir = tmp_path / "project"
    project_dir.mkdir()

    svc.prepare_project_dirs(project_dir, [], ["instances/agent-abc/daemon.lock"])

    assert (project_dir / ".claude" / "instances" / "agent-abc" / "daemon.lock").is_file()


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

    svc.prepare_project_dirs(project_dir, _STATE_DIRS, STATE_FILES)

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

    svc.prepare_project_dirs(project_dir, _STATE_DIRS, STATE_FILES)

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

    svc.prepare_project_dirs(project_dir, _STATE_DIRS, STATE_FILES)
    svc.prepare_project_dirs(project_dir, _STATE_DIRS, STATE_FILES)

    assert (new_memory_dir / "fact.md").read_text() == "fact"
    assert list(old_memory_dir.iterdir()) == []


@pytest.fixture
def make_live_project(tmp_path: Path) -> Callable[[str], Path]:
    """Return a factory creating a project whose ``.claude/litellm-logs`` exists."""

    def _make(name: str) -> Path:
        project = tmp_path / name
        (project / ".claude" / "litellm-logs").mkdir(parents=True)
        return project

    return _make


@pytest.fixture
def write_legacy_registry(tmp_path: Path) -> Callable[..., Path]:
    """
    Return a factory writing a pre-SQLite projects.txt, one argument per line.

    Lines are written verbatim, so a caller passes plain paths when the encoding is
    beside the point and the encoded form (``/a/{x,y}``, ``{2}/rest``) when it is not.
    """

    def _write(*lines: Path | str) -> Path:
        launches = tmp_path / ".agent-launches"
        launches.mkdir(parents=True, exist_ok=True)
        legacy = launches / "projects.txt"
        legacy.write_text("\n".join(str(line) for line in lines) + "\n", encoding="utf-8")
        return legacy

    return _write


def test_record_project_records_cwd(
    svc: ConfigService, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    svc.record_project()
    assert svc.read_project_paths() == [tmp_path]


def test_record_project_deduplicates(
    svc: ConfigService, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    svc.record_project()
    svc.record_project()
    assert svc.read_project_paths() == [tmp_path]


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

    assert svc.read_project_paths() == [link]


def test_record_project_replaces_alias_pointing_to_same_target(
    svc: ConfigService,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    register_projects: Callable[..., None],
) -> None:
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real)
    register_projects(real)

    monkeypatch.chdir(link)
    monkeypatch.setenv("PWD", str(link))
    svc.record_project()

    assert svc.read_project_paths() == [link]


def test_record_project_keeps_the_registry_sorted(
    svc: ConfigService,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    register_projects: Callable[..., None],
) -> None:
    register_projects("/z", "/a", "/m")

    extra = tmp_path / "extra"
    extra.mkdir()
    monkeypatch.chdir(extra)
    monkeypatch.delenv("PWD", raising=False)
    svc.record_project()

    paths = svc.read_project_paths()
    assert paths == sorted(paths)
    assert extra in paths


def test_record_project_survives_a_storage_failure(
    svc: ConfigService,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mocker: pytest_mock.MockerFixture,
) -> None:
    """A launch must not die because the registry was locked — it is a convenience index."""
    monkeypatch.chdir(tmp_path)
    mocker.patch.object(
        svc._projects, "record", autospec=True, side_effect=StorageError("database is locked")
    )

    svc.record_project()  # must not raise


def test_read_project_paths_survives_a_storage_failure(
    svc: ConfigService, mocker: pytest_mock.MockerFixture
) -> None:
    mocker.patch.object(
        svc._projects, "list_projects", autospec=True, side_effect=StorageError("corrupt")
    )

    assert svc.read_project_paths() == []


def test_stale_project_paths_finds_projects_without_logs_dir(
    svc: ConfigService,
    tmp_path: Path,
    make_live_project: Callable[[str], Path],
    register_projects: Callable[..., None],
) -> None:
    live = make_live_project("live")
    gone = tmp_path / "gone"
    no_logs = tmp_path / "no_logs"
    (no_logs / ".claude").mkdir(parents=True)
    register_projects(live, gone, no_logs)

    assert svc.stale_project_paths() == [gone, no_logs]


def test_stale_project_paths_accepts_symlinked_logs_dir(
    svc: ConfigService, tmp_path: Path, register_projects: Callable[..., None]
) -> None:
    """The real layout is a symlink into the central store — it must count as live."""
    central = tmp_path / "central" / "hashA"
    central.mkdir(parents=True)
    project = tmp_path / "project"
    (project / ".claude").mkdir(parents=True)
    (project / ".claude" / "litellm-logs").symlink_to(central, target_is_directory=True)
    register_projects(project)

    assert svc.stale_project_paths() == []


def test_stale_project_paths_flags_broken_symlink(
    svc: ConfigService, tmp_path: Path, register_projects: Callable[..., None]
) -> None:
    project = tmp_path / "project"
    (project / ".claude").mkdir(parents=True)
    (project / ".claude" / "litellm-logs").symlink_to(tmp_path / "missing")
    register_projects(project)

    assert svc.stale_project_paths() == [project]


def test_stale_project_paths_empty_when_no_registry(svc: ConfigService) -> None:
    assert svc.stale_project_paths() == []


def test_prune_stale_projects_removes_only_given_paths(
    svc: ConfigService,
    tmp_path: Path,
    make_live_project: Callable[[str], Path],
    register_projects: Callable[..., None],
) -> None:
    live = make_live_project("live")
    other = make_live_project("other")
    gone = tmp_path / "gone"
    register_projects(live, other, gone)

    removed = svc.prune_stale_projects([gone])

    assert removed == [gone]
    remaining = svc.read_project_paths()
    assert gone not in remaining
    assert set(remaining) == {live, other}


def test_prune_stale_projects_keeps_the_registry_sorted(
    svc: ConfigService,
    tmp_path: Path,
    make_live_project: Callable[[str], Path],
    register_projects: Callable[..., None],
) -> None:
    kept_z = make_live_project("z_project")
    kept_a = make_live_project("a_project")
    gone = tmp_path / "gone"
    register_projects(kept_z, gone, kept_a)

    svc.prune_stale_projects([gone])

    paths = svc.read_project_paths()
    assert paths == sorted(paths)


def test_prune_stale_projects_empty_list_is_noop(
    svc: ConfigService,
    make_live_project: Callable[[str], Path],
    register_projects: Callable[..., None],
) -> None:
    live = make_live_project("live")
    register_projects(live)

    assert svc.prune_stale_projects([]) == []
    assert svc.read_project_paths() == [live]


def test_prune_stale_projects_can_empty_the_registry(
    svc: ConfigService, tmp_path: Path, register_projects: Callable[..., None]
) -> None:
    gone = tmp_path / "gone"
    register_projects(gone)

    svc.prune_stale_projects([gone])

    assert svc.read_project_paths() == []


def test_prune_then_stale_paths_is_clean(
    svc: ConfigService,
    tmp_path: Path,
    make_live_project: Callable[[str], Path],
    register_projects: Callable[..., None],
) -> None:
    """Pruning what stale_project_paths() reported leaves nothing stale behind."""
    live = make_live_project("live")
    register_projects(live, tmp_path / "gone1", tmp_path / "gone2")

    svc.prune_stale_projects(svc.stale_project_paths())

    assert svc.stale_project_paths() == []
    assert svc.read_project_paths() == [live]


def test_registry_fingerprint_is_empty_without_projects(svc: ConfigService) -> None:
    assert svc.registry_fingerprint() == (None, 0)


def test_registry_fingerprint_moves_on_every_record(
    svc: ConfigService, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    The viewer's ETag reads this, so re-recording an unchanged project must move it.

    The registry file's mtime moved on every `agent run` for the same reason.
    """
    monkeypatch.chdir(tmp_path)
    svc.record_project()
    before = svc.registry_fingerprint()

    svc.record_project()

    assert svc.registry_fingerprint() != before


@pytest.fixture
def read_only_svc(read_only_core: Core, display_mock: Mock) -> ConfigService:
    """
    Return a service over a factory holding no write grant.

    This is the logs daemon's position: a process that reads the registry and must not
    be able to mutate it, however deep the call goes. Takes the shared ``display_mock``
    so a test can assert on what it did or did not report.
    """
    repository = ProjectsRepository(connection_factory=read_only_core.projects_db)
    return ConfigService(display_service=display_mock, projects_repository=repository)


def test_a_read_without_a_write_grant_cannot_import_the_legacy_registry(
    read_only_svc: ConfigService,
    read_only_core: Core,
    tmp_path: Path,
    write_legacy_registry: Callable[..., Path],
) -> None:
    """
    The defect this gate exists for.

    ``read_project_paths`` reaches the legacy import, which is a write. A process holding
    no grant must come away with an empty list and an untouched database rather than
    having migrated host state from inside a read.
    """
    legacy = write_legacy_registry(tmp_path / "a", tmp_path / "b")
    before = legacy.read_bytes()

    assert read_only_svc.read_project_paths() == []

    assert legacy.read_bytes() == before
    with read_only_core.projects_db.ro() as connection:
        assert connection.execute("SELECT COUNT(*) AS n FROM projects").fetchone()["n"] == 0


def test_the_refused_import_advises_running_agent_run(
    read_only_svc: ConfigService,
    display_mock: Mock,
    tmp_path: Path,
    write_legacy_registry: Callable[..., Path],
) -> None:
    """An empty list beside an untouched projects.txt is inexplicable without this."""
    write_legacy_registry(tmp_path / "a")

    read_only_svc.read_project_paths()

    (message,) = [call.args[0] for call in display_mock.error.call_args_list]
    assert "agent run" in message


def test_a_genuine_storage_failure_is_not_told_to_run_agent_run(
    read_only_svc: ConfigService, display_mock: Mock, mocker: pytest_mock.MockerFixture
) -> None:
    """A corrupt or locked database is not fixed by launching, so it gets no advice."""
    mocker.patch.object(
        read_only_svc._projects, "list_projects", autospec=True, side_effect=StorageError("corrupt")
    )

    assert read_only_svc.read_project_paths() == []
    display_mock.error.assert_not_called()


def test_an_empty_registry_with_no_legacy_file_is_quiet(
    read_only_svc: ConfigService, display_mock: Mock
) -> None:
    """A fresh install has nothing to import, so it must not be advised to import."""
    assert read_only_svc.read_project_paths() == []
    display_mock.error.assert_not_called()


def test_prune_stale_projects_without_a_write_grant_silently_removes_nothing(
    read_only_svc: ConfigService,
    read_only_core: Core,
    register_projects: Callable[..., None],
    tmp_path: Path,
) -> None:
    """
    Why the cleanup command needs a grant around its clean phase, not just its survey.

    The refusal is a ``StorageError``, and this method suppresses those by design, so it
    returns the paths as though it had removed them. Both halves are asserted: the
    best-effort contract is unchanged, *and* nothing was actually deleted. A caller that
    reports on the returned list — ``run_cleanup`` does — would claim a prune that never
    happened.
    """
    gone = tmp_path / "gone"
    register_projects(gone)

    assert read_only_svc.prune_stale_projects([gone]) == [gone]

    with read_only_core.projects_db.ro() as connection:
        assert connection.execute("SELECT COUNT(*) AS n FROM projects").fetchone()["n"] == 1


def test_a_granted_process_imports_the_same_registry(
    svc: ConfigService, tmp_path: Path, write_legacy_registry: Callable[..., Path]
) -> None:
    """The other half of the gate: the refusal is about permission, not about the data."""
    write_legacy_registry(tmp_path / "a", tmp_path / "b")

    assert svc.read_project_paths() == [tmp_path / "a", tmp_path / "b"]


def test_legacy_registry_is_imported(
    svc: ConfigService, tmp_path: Path, write_legacy_registry: Callable[..., Path]
) -> None:
    """The file itself is left alone — it is the only copy of the pre-SQLite registry."""
    legacy = write_legacy_registry(tmp_path / "a", tmp_path / "b", tmp_path / "c")
    before = legacy.read_bytes()

    assert svc.read_project_paths() == [tmp_path / "a", tmp_path / "b", tmp_path / "c"]
    assert legacy.read_bytes() == before


def test_legacy_registry_import_expands_the_compressed_encoding(
    svc: ConfigService, write_legacy_registry: Callable[..., Path]
) -> None:
    """Sibling groups and prefix borrows have to survive the trip into the database."""
    write_legacy_registry("/home/dev/{x,y,z}", "{1}/other/thing")

    assert [str(p) for p in svc.read_project_paths()] == [
        "/home/dev/x",
        "/home/dev/y",
        "/home/dev/z",
        "/home/other/thing",
    ]


def test_legacy_registry_import_runs_once(
    svc: ConfigService, tmp_path: Path, write_legacy_registry: Callable[..., Path]
) -> None:
    """A path pruned after the import must not be resurrected by a later read."""
    write_legacy_registry(tmp_path / "a", tmp_path / "b")
    svc.read_project_paths()

    svc.prune_stale_projects([tmp_path / "a"])

    assert svc.read_project_paths() == [tmp_path / "b"]


def test_a_fully_pruned_registry_reimports_the_legacy_file(
    svc: ConfigService, tmp_path: Path, write_legacy_registry: Callable[..., Path]
) -> None:
    """
    The accepted cost of gating the import on an empty table.

    Pruning every row puts the registry back in the state a fresh install is in, and the
    legacy file is still there, so the next read imports it again. Self-correcting —
    those paths were pruned for being stale and will be flagged stale again — and the
    alternative was either a marker file or deleting the user's only copy of the old
    registry.
    """
    write_legacy_registry(tmp_path / "a")
    svc.read_project_paths()

    svc.prune_stale_projects([tmp_path / "a"])

    assert svc.read_project_paths() == [tmp_path / "a"]


def test_legacy_registry_import_is_idempotent_across_instances(
    svc: ConfigService,
    tmp_path: Path,
    projects_repository: ProjectsRepository,
    write_legacy_registry: Callable[..., Path],
) -> None:
    """
    Two processes can race here — a launch and the logs daemon.

    Both write the same paths through an upsert keyed on the path, so neither can
    double-insert; and whichever reads second finds a non-empty table and imports
    nothing.
    """
    write_legacy_registry(tmp_path / "a")
    other = ConfigService(
        display_service=Mock(spec=DisplayService), projects_repository=projects_repository
    )

    svc.read_project_paths()
    other.read_project_paths()

    assert svc.read_project_paths() == [tmp_path / "a"]


def test_record_project_imports_the_legacy_registry_too(
    svc: ConfigService,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    write_legacy_registry: Callable[..., Path],
) -> None:
    """A launch is the likeliest first touch, so it must not lose the old registry."""
    write_legacy_registry(tmp_path / "old")
    cwd = tmp_path / "new"
    cwd.mkdir()
    monkeypatch.chdir(cwd)
    monkeypatch.delenv("PWD", raising=False)

    svc.record_project()

    assert set(svc.read_project_paths()) == {tmp_path / "old", cwd}


def test_legacy_registry_import_retries_after_a_storage_failure(
    svc: ConfigService,
    tmp_path: Path,
    write_legacy_registry: Callable[..., Path],
    mocker: pytest_mock.MockerFixture,
) -> None:
    """A failed import writes nothing, so the next read finds an empty table and retries."""
    write_legacy_registry(tmp_path / "a")
    real_record_many = svc._projects.record_many
    attempts = itertools.count()

    def flaky(paths: Sequence[str]) -> None:
        if next(attempts) == 0:
            msg = "locked"
            raise StorageError(msg)
        real_record_many(paths)

    mocker.patch.object(svc._projects, "record_many", autospec=True, side_effect=flaky)

    assert svc.read_project_paths() == []

    assert svc.read_project_paths() == [tmp_path / "a"]


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


def test_link_litellm_logs_backs_up_real_directory(svc: ConfigService, tmp_path: Path) -> None:
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
    svc._display.info.assert_called_once_with(  # pyrefly: ignore [missing-attribute]
        f"agent-wrap: backed up pre-existing logs {link} -> {bkp}"
    )


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


def test_declared_mounts_creates_missing_absolute_source(svc: ConfigService, tmp_path: Path):
    source = tmp_path / "srv" / "data"
    svc.prepare_declared_mounts(["-v", f"{source}:/data"], tmp_path)
    assert source.is_dir()


def test_declared_mounts_resolves_relative_source_against_project(
    svc: ConfigService, tmp_path: Path
):
    project = tmp_path / "project"
    project.mkdir()
    svc.prepare_declared_mounts(["-v", "./scratch:/scratch"], project)
    assert (project / "scratch").is_dir()


def test_declared_mounts_leaves_existing_source_untouched(svc: ConfigService, tmp_path: Path):
    source = tmp_path / "data"
    source.mkdir()
    (source / "keep.txt").write_text("kept")
    svc.prepare_declared_mounts(["-v", f"{source}:/data"], tmp_path)
    assert (source / "keep.txt").read_text() == "kept"


def test_declared_mounts_rejects_missing_read_only_source(svc: ConfigService, tmp_path: Path):
    missing = tmp_path / "models"
    writable = tmp_path / "data"
    with pytest.raises(HostMountError) as excinfo:
        svc.prepare_declared_mounts(
            ["-v", f"{missing}:/models:ro", "-v", f"{writable}:/data"], tmp_path
        )
    assert f"{missing} -> /models" in str(excinfo.value)
    assert not missing.exists()
    # Nothing is created until the whole declaration checks out.
    assert not writable.exists()


def test_declared_mounts_rejects_missing_relative_read_only_source(
    svc: ConfigService, tmp_path: Path
):
    with pytest.raises(HostMountError, match=r"\./models -> /models"):
        svc.prepare_declared_mounts(["--mount", "type=bind,src=./models,dst=/models,ro"], tmp_path)


def test_declared_mounts_accepts_existing_read_only_source(svc: ConfigService, tmp_path: Path):
    source = tmp_path / "models"
    source.mkdir()
    svc.prepare_declared_mounts(["-v", f"{source}:/models:ro"], tmp_path)
    assert source.is_dir()


def test_declared_mounts_creates_workspace_mountpoint_directory(svc: ConfigService, tmp_path: Path):
    svc.prepare_declared_mounts(["-v", "/workspace/node_modules"], tmp_path)
    assert (tmp_path / "node_modules").is_dir()


def test_declared_mounts_creates_workspace_mountpoint_for_nested_bind(
    svc: ConfigService, tmp_path: Path
):
    source = tmp_path / "shared"
    svc.prepare_declared_mounts(["-v", f"{source}:/workspace/vendor/shared"], tmp_path)
    assert source.is_dir()
    assert (tmp_path / "vendor" / "shared").is_dir()


def test_declared_mounts_creates_workspace_mountpoint_as_file_for_file_source(
    svc: ConfigService, tmp_path: Path
):
    source = tmp_path / "config.toml"
    source.write_text("x = 1\n")
    svc.prepare_declared_mounts(["-v", f"{source}:/workspace/.tool/config.toml"], tmp_path)
    assert (tmp_path / ".tool" / "config.toml").is_file()


def test_declared_mounts_skips_workspace_root_target(svc: ConfigService, tmp_path: Path):
    project = tmp_path / "project"
    project.mkdir()
    svc.prepare_declared_mounts(["-v", f"{project}:/workspace"], project)
    assert sorted(p.name for p in project.iterdir()) == []


def test_declared_mounts_ignores_workspace_target_escaping_the_project(
    svc: ConfigService, tmp_path: Path
):
    project = tmp_path / "project"
    project.mkdir()
    svc.prepare_declared_mounts(["-v", "/workspace/../escaped"], project)
    assert not (tmp_path / "escaped").exists()


def test_declared_mounts_warns_and_skips_tilde_source(svc: ConfigService, tmp_path: Path):
    svc.prepare_declared_mounts(["-v", "~/cache:/cache"], tmp_path)
    assert not (tmp_path / "~").exists()
    warning = svc._display.warning.call_args[0][0]  # pyrefly: ignore [missing-attribute]
    assert "'~' is not expanded" in warning


def test_declared_mounts_ignores_named_volumes(svc: ConfigService, tmp_path: Path):
    project = tmp_path / "project"
    project.mkdir()
    svc.prepare_declared_mounts(["-v", "cache:/cache", "--cap-add", "SYS_ADMIN"], project)
    assert sorted(p.name for p in project.iterdir()) == []


def test_declared_mounts_reports_unwritable_source(
    svc: ConfigService, tmp_path: Path, mocker: pytest_mock.MockFixture
):
    mocker.patch.object(Path, "mkdir", autospec=True, side_effect=OSError("Permission denied"))
    with pytest.raises(HostMountError, match="Permission denied"):
        svc.prepare_declared_mounts(["-v", f"{tmp_path / 'data'}:/data"], tmp_path)
