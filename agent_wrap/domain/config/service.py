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
    SPELLCHECK_CHECKER,
    SPELLCHECK_ENABLED_OVERRIDE,
    SPELLCHECK_LANG,
    SPELLCHECK_LANG_OVERRIDE,
    TOOL_DIR,
    WORKSPACE_MOUNT,
)
from agent_wrap.domain.config.project_registry import ProjectRegistry
from agent_wrap.exceptions import HostMountError
from agent_wrap.lib.atomic import atomic_write_json, atomic_write_text
from agent_wrap.lib.docker_utils import parse_mount_specs
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
    except json.JSONDecodeError, OSError:
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

    def _ensure_spellcheck(self, settings_path: Path) -> None:
        """
        Idempotently configure Claude Code's prompt spell checking.

        The feature only reads this tier -- a ``spellcheck`` block in a project's
        ``.claude/settings.json`` is ignored outright -- so the wrapper-global user
        settings are the only place it can be turned on from.

        With no block present, one is written: on, ``hunspell``, and the dictionary list
        in force. With a block already there, the file wins and it is left alone, except
        that an explicitly set ``AGENT_SPELLCHECK`` / ``AGENT_SPELLCHECK_LANG`` overrides
        the corresponding key on every launch -- otherwise the env vars would be inert
        the moment a previous launch had written the block. Keys the user added by hand
        (``color``, a different ``checker``) are always preserved, and the file is
        rewritten only when a value actually changed.

        If the file is empty or missing, creates it with {}.
        If the JSON is malformed, does nothing.
        """
        if not settings_path.exists() or settings_path.stat().st_size == 0:
            settings_path.parent.mkdir(parents=True, exist_ok=True)
            settings_path.write_text("{}\n")

        data = _load_json(settings_path)
        if data is None:
            return  # malformed JSON -- don't clobber

        block = data.get("spellcheck")
        if block is None:
            data["spellcheck"] = {
                "enabled": SPELLCHECK_ENABLED_OVERRIDE is not False,
                "checker": SPELLCHECK_CHECKER,
                "language": SPELLCHECK_LANG,
            }
            atomic_write_json(settings_path, data)
            return

        if not isinstance(block, dict):
            return  # someone's hand-written value, of a shape we won't second-guess

        updated = dict(block)
        if SPELLCHECK_ENABLED_OVERRIDE is not None:
            updated["enabled"] = SPELLCHECK_ENABLED_OVERRIDE
        if SPELLCHECK_LANG_OVERRIDE is not None:
            updated["language"] = SPELLCHECK_LANG_OVERRIDE
        if updated == block:
            return

        data["spellcheck"] = updated
        atomic_write_json(settings_path, data)

    def _ensure_telegram_hooks(self, settings_path: Path) -> None:
        """
        Idempotently inject PermissionRequest/Stop/StopFailure/SessionEnd hooks.

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
        self._ensure_hook(data, "SessionEnd", f"{cmd} sessionend")
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
        statusline, spell checking and telegram hooks, and copies default-CLAUDE.md.
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
        self._ensure_spellcheck(settings_path)

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
        them as root when the bind-mount targets don't yet exist -- and, for a
        file source, from creating a *directory* where a file was meant.

        Both arguments accept nested, `/`-separated paths (per-container state
        arrives as ``instances/<id>/...``); missing parents are created for
        files as well as directories, so neither list depends on the other
        having built the intervening directories first.
        """
        claude_dir = project_dir / ".claude"
        for subdir in state_dirs:
            (claude_dir / subdir).mkdir(parents=True, exist_ok=True)

        for name in state_files:
            path = claude_dir / name
            path.parent.mkdir(parents=True, exist_ok=True)
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

    def prepare_declared_mounts(self, run_args: list[str], project_dir: Path) -> None:
        """
        Pre-create the host side of every mount a project Dockerfile declares.

        Same rationale as :meth:`prepare_project_dirs`, applied to the mounts an image
        author asked for via ``agent-run-args``: whatever docker has to materialize
        itself lands as ``root:root`` and the agent cannot write it. One spec can call for
        two *different* host paths: the bind *source*, and -- when the container-side
        target sits under ``/workspace`` -- the mountpoint docker needs inside the bind
        mount of the project, which is what makes an anonymous
        ``-v /workspace/node_modules`` volume leave a root-owned directory behind. A spec
        whose source already exists can still need that second path created.

        A read-only source is never invented: an author who wrote ``:ro`` asked for
        content that already exists, so a missing one fails the launch instead of
        silently mounting an empty directory. Everything is reported at once, and
        nothing is created until the whole declaration checks out.

        Author-supplied tokens are passed to docker untouched, so this resolves paths
        exactly the way docker will: absolute as written, relative against the directory
        the launch runs from. ``~`` is left to fail on docker's side -- no shell is
        involved to expand it -- with a warning that says so.
        """
        specs = parse_mount_specs(run_args)

        missing = [
            spec
            for spec in specs
            if spec.read_only
            and (source := self._mount_source_path(spec.source, project_dir)) is not None
            and not source.exists()
        ]
        if missing:
            listed = "\n".join(f"  {spec.source} -> {spec.target}" for spec in missing)
            msg = (
                "the project Dockerfile declares read-only mounts whose host source"
                " does not exist:\n"
                f"{listed}\n"
                "Create each path on the host, or drop ':ro' to have it created automatically."
            )
            raise HostMountError(msg)

        for spec in specs:
            if spec.source is not None and spec.source.startswith("~"):
                self._display.warning(
                    f"Mount source '{spec.source}' in agent-run-args is passed to docker verbatim"
                    " — '~' is not expanded because no shell is involved. Use an absolute path."
                )
                continue
            source = self._mount_source_path(spec.source, project_dir)
            if source is not None and not source.exists():
                self._make_mount_path(source, as_file=False)
            self._prepare_mountpoint(spec.target, source, project_dir)

    def _mount_source_path(self, source: str | None, project_dir: Path) -> Path | None:
        """Resolve a declared bind source, or None when the spec has no usable host path."""
        if source is None or source.startswith("~"):
            return None
        return (project_dir / source).resolve()

    def _prepare_mountpoint(self, target: str, source: Path | None, project_dir: Path) -> None:
        """Create the host-side mountpoint for a target nested under ``/workspace``."""
        prefix = f"{WORKSPACE_MOUNT}/"
        stripped = target.rstrip("/")
        if not stripped.startswith(prefix):
            return

        root = project_dir.resolve()
        point = (root / stripped[len(prefix) :]).resolve()
        if not point.is_relative_to(root) or point.exists():
            return
        # Node type is read off the source, never guessed from the path's shape: docker
        # refuses to mount a file onto a directory ("are you trying to mount a directory
        # onto a file (or vice-versa)?"), so a file source needs a file here. Volumes,
        # tmpfs and directory binds all need a directory -- as does a source the caller
        # just created, since those are always created as directories.
        self._make_mount_path(point, as_file=source is not None and source.is_file())

    def _make_mount_path(self, path: Path, *, as_file: bool) -> None:
        """Create *path* as the host user, translating failures into HostMountError."""
        try:
            if as_file:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.touch()
            else:
                path.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            msg = f"cannot create host mount path '{path}' from the project Dockerfile: {e}"
            raise HostMountError(msg) from e

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
