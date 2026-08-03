# This file has been edited with the assistance of an AI tool.
"""
Configuration file manipulation for agent-wrap.

Replaces the bash _agent_ensure_statusline, _agent_ensure_telegram_hooks,
_agent_record_project, and related config-prep helpers. Uses stdlib json
instead of jq.
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
from pathlib import Path
from typing import TYPE_CHECKING, Any

from agent_wrap.constants import (
    AGENT_LAUNCHES_DIR,
    GLOBAL_CONFIG_DIR,
    LITELLM_LOGS_DIRNAME,
    OPS_DIR,
    PROJECT_REGISTRY_FILENAME,
    TOOL_DIR,
)
from agent_wrap.domain.config.project_registry import ProjectRegistry
from agent_wrap.lib.atomic import atomic_write_json, atomic_write_text
from agent_wrap.lib.path_hash import project_path_hash

if TYPE_CHECKING:
    from agent_wrap.domain.display.service import DisplayService


def _load_json(path: Path) -> dict[str, Any] | None:
    """Load JSON from a file, returning None if malformed."""
    try:
        text = path.read_text()
        if not text.strip():
            return {}
        return json.loads(text)
    except (json.JSONDecodeError, OSError):
        return None


class ConfigService:
    """Configuration file manipulation for agent-wrap."""

    def __init__(self, display_service: DisplayService) -> None:
        self._display = display_service

    # statusline / hooks

    def _ensure_statusline(self, settings_path: Path) -> None:
        """
        Idempotently inject statusLine key into settings.json.

        If the key is absent, adds it pointing to the statusline.py script
        (invoked directly — the script has its execute bit set).
        If the file is empty or missing, creates it with {}.
        If the JSON is malformed, does nothing (don't clobber user's file).
        """
        if not settings_path.exists() or settings_path.stat().st_size == 0:
            settings_path.parent.mkdir(parents=True, exist_ok=True)
            settings_path.write_text("{}\n")

        data = _load_json(settings_path)
        if data is None:
            return  # malformed JSON — don't clobber

        if "statusLine" in data:
            return

        data["statusLine"] = {
            "type": "command",
            "command": "/opt/agent-wrap/statusline.py",
        }
        atomic_write_json(settings_path, data)

    def _ensure_telegram_hooks(self, settings_path: Path) -> None:
        """
        Idempotently inject PermissionRequest/Stop/StopFailure hooks.

        Each hook runs telegram-notify.sh directly (the script has its
        execute bit set).
        If the file is empty or missing, creates it with {}.
        If the JSON is malformed, does nothing.
        """
        if not settings_path.exists() or settings_path.stat().st_size == 0:
            settings_path.parent.mkdir(parents=True, exist_ok=True)
            settings_path.write_text("{}\n")

        data = _load_json(settings_path)
        if data is None:
            return

        cmd = "/opt/agent-wrap/telegram-notify.sh"

        self._ensure_hook(data, "PermissionRequest", cmd)
        self._ensure_hook(data, "Stop", f"{cmd} stop")
        self._ensure_hook(data, "StopFailure", f"{cmd} stopfailure")
        atomic_write_json(settings_path, data)

    def _ensure_hook(self, data: dict[str, Any], event: str, command: str) -> None:
        """Add a hook entry to an event if not already present."""
        hooks = data.setdefault("hooks", {})
        event_hooks = hooks.setdefault(event, [])

        # Check if the exact command is already present
        for entry in event_hooks:
            for hook in entry.get("hooks", []):
                if hook.get("command") == command:
                    return

        event_hooks.append(
            {
                "matcher": "",
                "hooks": [{"type": "command", "command": command}],
            }
        )

    def _ensure_claude_md(self) -> None:
        """Copy default-CLAUDE.md to the global config dir if not already present."""
        template_path = OPS_DIR / "default-CLAUDE.md"
        target = GLOBAL_CONFIG_DIR / ".claude" / "CLAUDE.md"
        if not target.exists() and template_path.exists():
            shutil.copy2(template_path, target)

    # global / per-project config

    def prepare_global_config(
        self,
        *,
        telegram_available: bool = False,
    ) -> None:
        """
        Prepare the global config directory for agent launch.

        Creates the directory structure, secures config files, injects
        statusline and telegram hooks, and copies default-CLAUDE.md.
        """
        global_config_dir = GLOBAL_CONFIG_DIR
        claude_dir = global_config_dir / ".claude"
        claude_dir.mkdir(parents=True, exist_ok=True)

        # Create and secure config files. These are bind-mounted into the
        # container, so they must exist on the host before launch. Seed them with
        # an empty JSON object rather than a zero-byte file: Claude Code parses
        # .claude.json on startup and aborts with a "Configuration error" prompt
        # ("invalid JSON … Unexpected EOF") if it finds an empty file.
        for name in (".claude.json", "settings.json"):
            path = global_config_dir / name if name == ".claude.json" else claude_dir / name
            if not path.exists() or path.stat().st_size == 0:
                path.write_text("{}\n")
            path.chmod(0o600)

        settings_path = claude_dir / "settings.json"
        self._ensure_statusline(settings_path)

        if telegram_available:
            self._ensure_telegram_hooks(settings_path)

        self._ensure_claude_md()

        # Pre-create projects dir so Docker doesn't create it as root
        (claude_dir / "projects" / "-workspace").mkdir(parents=True, exist_ok=True)

    def prepare_project_dirs(
        self,
        project_dir: Path,
        state_dirs: tuple[str, ...] | list[str],
        state_files: tuple[str, ...] | list[str],
    ) -> None:
        """
        Create per-project .claude/ directories and files.

        Pre-creating these as the host user prevents Docker from materializing
        them as root when the bind-mount targets don't yet exist.
        """
        claude_dir = project_dir / ".claude"
        for subdir in state_dirs:
            (claude_dir / subdir).mkdir(parents=True, exist_ok=True)

        for name in state_files:
            path = claude_dir / name
            if not path.exists():
                path.touch()

        gitignore = claude_dir / ".gitignore"
        if not gitignore.exists():
            gitignore.write_text("*\n")

        # One-time migration: memory was previously a subdirectory of the sessions
        # mount. Now that it has its own mount, move any old files over so they
        # are not hidden by the new (empty) bind-mount.
        old_memory_dir = claude_dir / "sessions" / "memory"
        new_memory_dir = claude_dir / "memory"
        if old_memory_dir.is_dir() and new_memory_dir.is_dir():
            for src in old_memory_dir.iterdir():
                dst = new_memory_dir / src.name
                if not dst.exists():
                    shutil.move(str(src), str(dst))

    def link_litellm_logs(self, project_dir: Path) -> None:
        """
        Point ``project_dir/.claude/litellm-logs`` at the shared per-project subtree.

        The shared sidecar writes logs to ``<tool_dir>/litellm-logs/<project_hash>/``;
        the viewer reads ``project/.claude/litellm-logs/<provider>/<session>``. This
        symlink bridges the two so the viewer needs no changes.

        Idempotent and non-destructive:
          * pre-creates the shared target so the symlink is never dangling;
          * if the link is already correct, does nothing;
          * if it is a stale symlink, repoints it;
          * if it is a REAL directory/file from the old per-project scheme, it is
            moved aside to ``litellm-logs-bkp`` (``-2``, ``-3``… on collision) rather
            than clobbered, then the symlink is created.

        Best-effort: any OSError is swallowed so logging never blocks a launch.
        """
        try:
            target = TOOL_DIR / LITELLM_LOGS_DIRNAME / project_path_hash(project_dir)
            target.mkdir(parents=True, exist_ok=True)

            link = project_dir / ".claude" / LITELLM_LOGS_DIRNAME
            link.parent.mkdir(parents=True, exist_ok=True)

            if link.is_symlink():
                if link.resolve() == target.resolve():
                    return  # already correct
                link.unlink()  # stale — repoint below
            elif link.exists():
                # A real directory/file from the old scheme — never destroy it.
                bkp = link.parent / f"{LITELLM_LOGS_DIRNAME}-bkp"
                n = 2
                while bkp.exists():
                    bkp = link.parent / f"{LITELLM_LOGS_DIRNAME}-bkp-{n}"
                    n += 1
                link.rename(bkp)
                self._display.info(f"agent-wrap: backed up pre-existing logs {link} -> {bkp}")

            link.symlink_to(target, target_is_directory=True)
        except OSError:
            pass  # non-fatal — logging must never block a launch

    # project registry -------------------------------------------------

    def read_project_paths(self) -> list[Path]:
        """Return expanded project paths from the registry file."""
        registry = AGENT_LAUNCHES_DIR / PROJECT_REGISTRY_FILENAME
        try:
            text = registry.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return []
        return [Path(p) for p in ProjectRegistry.decompress(text.splitlines())]

    def record_project(self) -> None:
        """
        Record cwd in the project registry, deduping aliases and keeping it sorted.

        Existing entries that resolve to the same canonical target as cwd are
        replaced by the current path — so a stale ``/mnt/...`` line gets overwritten
        once the user starts launching from its ``/home/.../symlink`` alias.

        Failures are non-fatal — the agent launch must not depend on this.
        """
        try:
            cwd = self._current_project_path()
            try:
                cwd_target: Path | None = Path(cwd).resolve()
            except OSError:
                cwd_target = None

            kept: list[str] = []
            for entry_path in self.read_project_paths():
                if cwd_target is not None:
                    try:
                        if entry_path.resolve() == cwd_target:
                            continue  # alias of cwd — superseded
                    except OSError:
                        pass  # keep entries we can't resolve
                kept.append(str(entry_path))
            kept.append(cwd)

            compressed = ProjectRegistry.compress(kept)
            with contextlib.suppress(OSError):
                registry = AGENT_LAUNCHES_DIR / PROJECT_REGISTRY_FILENAME
                atomic_write_text(registry, "\n".join(compressed) + "\n")
        except OSError:
            pass  # non-fatal

    def stale_project_paths(self) -> list[Path]:
        """
        Find registered project paths whose logs directory no longer exists.

        These are projects deleted or renamed after being registered — the ones
        ``agent stats`` already flags as ``(missing)``. An entry that cannot be
        stat'd counts as stale too: it is no more useful than a missing one.
        """
        return [path for path in self.read_project_paths() if not self._has_logs_dir(path)]

    def _has_logs_dir(self, path: Path) -> bool:
        """Report whether *path* still has a usable ``.claude/litellm-logs`` directory."""
        try:
            return (path / ".claude" / LITELLM_LOGS_DIRNAME).is_dir()
        except OSError:
            return False

    def prune_stale_projects(self, stale: list[Path]) -> list[Path]:
        """
        Remove *stale* from the project registry and return what was removed.

        Takes the exact list :meth:`stale_project_paths` returned rather than
        recomputing it, so the caller reports on and acts upon the same entries.
        Failures are non-fatal, matching :meth:`record_project` — the registry is
        a convenience index, not a source of truth.
        """
        drop = {str(path) for path in stale}
        kept = [str(path) for path in self.read_project_paths() if str(path) not in drop]
        compressed = ProjectRegistry.compress(kept)
        with contextlib.suppress(OSError):
            registry = AGENT_LAUNCHES_DIR / PROJECT_REGISTRY_FILENAME
            atomic_write_text(registry, "\n".join(compressed) + "\n")
        return stale

    def _current_project_path(self) -> str:
        """
        Return the project path as the user sees it.

        Mirrors bash's `$(pwd)`: when the user `cd`'d through a symlink, $PWD
        preserves it; Path.cwd() (os.getcwd()) would resolve it. Fall back to
        Path.cwd() if $PWD is missing, relative, or points somewhere else.
        """
        cwd = Path.cwd()
        pwd = os.environ.get("PWD")
        if pwd and Path(pwd).is_absolute():
            try:
                if Path(pwd).resolve() == cwd.resolve():
                    return pwd
            except OSError:
                pass
        return str(cwd)
