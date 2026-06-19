# This file has been created with the assistance of an AI tool.
"""
Configuration file manipulation for agent-wrap.

Replaces the bash _agent_ensure_statusline, _agent_ensure_telegram_hooks,
_agent_record_project, and related config-prep helpers. Uses stdlib json
instead of jq.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path

from agent_wrap.lib.atomic import atomic_write_json, atomic_write_text
from agent_wrap.lib.utils import project_path_hash


def _load_json(path: Path) -> dict | None:
    """Load JSON from a file, returning None if malformed."""
    try:
        text = path.read_text()
        if not text.strip():
            return {}
        return json.loads(text)
    except (json.JSONDecodeError, OSError):
        return None


def ensure_statusline(settings_path: Path) -> None:
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


def _ensure_hook(data: dict, event: str, command: str) -> None:
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


def ensure_telegram_hooks(settings_path: Path) -> None:
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

    _ensure_hook(data, "PermissionRequest", cmd)
    _ensure_hook(data, "Stop", f"{cmd} stop")
    _ensure_hook(data, "StopFailure", f"{cmd} stopfailure")
    atomic_write_json(settings_path, data)


def ensure_claude_md(config_dir: Path, template_path: Path) -> None:
    """Copy default-CLAUDE.md to the global config dir if not already present."""
    target = config_dir / ".claude" / "CLAUDE.md"
    if not target.exists() and template_path.exists():
        shutil.copy2(template_path, target)


def prepare_global_config(
    global_config_dir: Path,
    tool_dir: Path,
    telegram_bot_token: str = "",
    telegram_chat_id: str = "",
) -> None:
    """
    Prepare the global config directory for agent launch.

    Creates the directory structure, secures config files, injects
    statusline and telegram hooks, and copies default-CLAUDE.md.
    """
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
    ensure_statusline(settings_path)

    if telegram_bot_token and telegram_chat_id:
        ensure_telegram_hooks(settings_path)

    ensure_claude_md(global_config_dir, tool_dir / "ops" / "default-CLAUDE.md")

    # Pre-create projects dir so Docker doesn't create it as root
    (claude_dir / "projects" / "-workspace").mkdir(parents=True, exist_ok=True)


def prepare_project_dirs(
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


def link_litellm_logs(project_dir: Path, tool_dir: Path) -> None:
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
        target = tool_dir / "litellm-logs" / project_path_hash(project_dir)
        target.mkdir(parents=True, exist_ok=True)

        link = project_dir / ".claude" / "litellm-logs"
        link.parent.mkdir(parents=True, exist_ok=True)

        if link.is_symlink():
            if link.resolve() == target.resolve():
                return  # already correct
            link.unlink()  # stale — repoint below
        elif link.exists():
            # A real directory/file from the old scheme — never destroy it.
            bkp = link.parent / "litellm-logs-bkp"
            n = 2
            while bkp.exists():
                bkp = link.parent / f"litellm-logs-bkp-{n}"
                n += 1
            link.rename(bkp)
            print(f"agent-wrap: backed up pre-existing logs {link} -> {bkp}", file=sys.stderr)

        link.symlink_to(target, target_is_directory=True)
    except OSError:
        pass  # non-fatal — logging must never block a launch


def _current_project_path() -> str:
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


def record_project(tool_dir: Path) -> None:
    """
    Record cwd in the project registry, deduping aliases and keeping it sorted.

    Existing entries that resolve to the same canonical target as cwd are
    replaced by the current path — so a stale `/mnt/...` line gets overwritten
    once the user starts launching from its `/home/.../symlink` alias.

    Failures are non-fatal — the agent launch must not depend on this.
    """
    try:
        launches_dir = tool_dir / ".agent-launches"
        launches_dir.mkdir(parents=True, exist_ok=True)
        projects_file = launches_dir / "projects.txt"

        cwd = _current_project_path()
        try:
            cwd_target: Path | None = Path(cwd).resolve()
        except OSError:
            cwd_target = None

        existing: list[str] = []
        if projects_file.exists():
            existing = [
                line.strip() for line in projects_file.read_text().splitlines() if line.strip()
            ]

        kept: list[str] = []
        for entry in existing:
            if cwd_target is not None:
                try:
                    if Path(entry).resolve() == cwd_target:
                        continue  # alias of cwd — superseded
                except OSError:
                    pass  # keep entries we can't resolve
            kept.append(entry)
        kept.append(cwd)

        merged = sorted(set(kept))
        atomic_write_text(projects_file, "\n".join(merged) + "\n")
    except OSError:
        pass  # non-fatal
