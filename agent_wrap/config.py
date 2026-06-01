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
from pathlib import Path


def _load_json(path: Path) -> dict | None:
    """Load JSON from a file, returning None if malformed."""
    try:
        text = path.read_text()
        if not text.strip():
            return {}
        return json.loads(text)
    except (json.JSONDecodeError, OSError):
        return None


def _save_json(path: Path, data: dict) -> None:
    """Atomically write JSON to a file."""
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n")
    tmp.replace(path)


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
    _save_json(settings_path, data)


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
    _save_json(settings_path, data)


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

    # Create and secure config files
    for name in (".claude.json", "settings.json"):
        path = global_config_dir / name if name == ".claude.json" else claude_dir / name
        if not path.exists():
            path.touch()
        path.chmod(0o600)

    settings_path = claude_dir / "settings.json"
    ensure_statusline(settings_path)

    if telegram_bot_token and telegram_chat_id:
        ensure_telegram_hooks(settings_path)

    ensure_claude_md(global_config_dir, tool_dir / "ops" / "default-CLAUDE.md")

    # Pre-create projects dir so Docker doesn't create it as root
    (claude_dir / "projects" / "-workspace").mkdir(parents=True, exist_ok=True)


def prepare_project_dirs(project_dir: Path) -> None:
    """Create per-project .claude/ directories and files."""
    claude_dir = project_dir / ".claude"
    for subdir in (
        "sessions",
        "session-state",
        "daemon",
        "jobs",
        "plans",
        "todos",
        "tasks",
        "shell-snapshots",
        "session-env",
        "file-history",
        "paste-cache",
    ):
        (claude_dir / subdir).mkdir(parents=True, exist_ok=True)

    for name in ("daemon.lock", "daemon.log", "daemon.status.json", "history.jsonl"):
        path = claude_dir / name
        if not path.exists():
            path.touch()

    gitignore = claude_dir / ".gitignore"
    if not gitignore.exists():
        gitignore.write_text("*\n")


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
        tmp = projects_file.with_suffix(projects_file.suffix + ".tmp")
        tmp.write_text("\n".join(merged) + "\n")
        tmp.replace(projects_file)
    except OSError:
        pass  # non-fatal
