# This file has been edited with the assistance of an AI tool.
"""The `run` subcommand — launches Claude Code in a Docker container."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

from agent_wrap import config
from agent_wrap.lib import docker_utils
from agent_wrap.lib.utils import (
    generate_uuid,
    is_truthy_env,
    parse_dockerfile_agent,
    resolve_image,
    sanitize_name,
)
from agent_wrap.providers import get_provider

USAGE = "[--base] [claude-args...]"
SUMMARY = "Launch Claude Code in Docker"

AGENT_WRAP_MOUNT = "/opt/agent-wrap"

# Per-project state directories mounted into the container.
# Keys are the host subdirectory names under $(pwd)/.claude/;
# values are the in-container destination relative to ~/.claude/.
_STATE_MOUNTS = {
    "sessions": "projects/-workspace",
    "session-state": "sessions",
    "daemon": "daemon",
    "jobs": "jobs",
    "plans": "plans",
    "todos": "todos",
    "tasks": "tasks",
    "shell-snapshots": "shell-snapshots",
    "session-env": "session-env",
    "file-history": "file-history",
    "paste-cache": "paste-cache",
    "image-cache": "image-cache",
}

# Per-project files mounted into the container.
_STATE_FILES = (
    "daemon.lock",
    "daemon.log",
    "daemon.status.json",
    "history.jsonl",
)


def _is_wsl() -> bool:
    """Check if running on WSL."""
    try:
        version = Path("/proc/version").read_text()
        return "microsoft" in version.lower()
    except OSError:
        return False


def _extract_network(extra_run_args: list[str]) -> str | None:
    """Extract --network value from a list of docker run flags."""
    for i, arg in enumerate(extra_run_args):
        if arg in ("--network", "--net"):
            if i + 1 < len(extra_run_args):
                return extra_run_args[i + 1]
        elif arg.startswith(("--network=", "--net=")):
            return arg.split("=", 1)[1]
    return None


def _load_telegram_creds(secrets: dict) -> tuple[str, str]:
    """Extract Telegram credentials from secrets dict."""
    bot_token = secrets.get("TelegramBotToken", "") or ""
    chat_id = secrets.get("TelegramChatId", "") or ""
    return bot_token, chat_id


def _resolve_host_network(
    agent_network: str | None,
    port_args: list[str],
) -> tuple[bool, list[str], list[str]]:
    """
    Resolve AGENT_USE_HOST_NETWORK env var.

    Returns (use_host_net, host_net_args, port_args) — port_args may be
    cleared if host networking is enabled.
    """
    env_val = os.environ.get("AGENT_USE_HOST_NETWORK", "")
    if not is_truthy_env(env_val):
        return False, [], port_args

    if not _is_wsl():
        print(
            "Note: AGENT_USE_HOST_NETWORK ignored — only honored on WSL hosts.",
            file=sys.stderr,
        )
        return False, [], port_args

    if agent_network:
        print(
            "Warning: AGENT_USE_HOST_NETWORK ignored — Dockerfile.agent already "
            "specifies --network via agent-run-args.",
            file=sys.stderr,
        )
        return False, [], port_args

    if port_args:
        print(
            "Warning: AGENT_USE_HOST_NETWORK is on — EXPOSE port mappings "
            "skipped. Services bind on the WSL distro's interfaces directly; "
            "ensure they listen on 127.0.0.1 to avoid LAN exposure.",
            file=sys.stderr,
        )
    return True, ["--network", "host"], []


def _resolve_agent_name(*, use_base: bool, cwd: Path) -> str:
    """Determine agent name from Dockerfile.agent or directory name."""
    if use_base:
        return sanitize_name(cwd.name) or "agent"

    dockerfile_agent = cwd / "Dockerfile.agent"
    if not dockerfile_agent.is_file():
        return sanitize_name(cwd.name) or "agent"

    with open(dockerfile_agent) as f:
        for line in f:
            if match := re.match(r"^#\s*agent-name:\s*(\S+)", line.strip()):
                return match.group(1)

    return sanitize_name(cwd.name) or "agent"


def _load_secrets() -> tuple[str, str]:
    """Load and validate claude_keys.json. Exits on error."""
    secrets_path = Path.home() / "claude_keys.json"
    if not secrets_path.exists():
        print(f"File {secrets_path} not found", file=sys.stderr)
        raise SystemExit(1)
    try:
        secrets = json.loads(secrets_path.read_text())
    except json.JSONDecodeError:
        print(f"File {secrets_path} is not valid JSON", file=sys.stderr)
        raise SystemExit(1) from None
    return _load_telegram_creds(secrets)


def _build_wslg_args(tool_dir: Path) -> list[str]:
    """Build WSLg-related volume mounts and env vars."""
    if not Path("/mnt/wslg").is_dir():
        return []
    return [
        "-v",
        "/mnt/wslg:/mnt/wslg",
        "-v",
        "/mnt/wslg/.X11-unix:/tmp/.X11-unix",
        "-v",
        f"{tool_dir}/ops/wl-paste-shim:/usr/local/bin/wl-paste:ro",
        "-e",
        "DISPLAY",
        "-e",
        "WAYLAND_DISPLAY",
        "-e",
        "XDG_RUNTIME_DIR=/mnt/wslg/runtime-dir",
    ]


def _parse_dockerfile_directives(
    resolved_dockerfile: Path,
) -> tuple[str, list[str], list[str]]:
    """Parse Dockerfile.agent directives. Returns (agent_user, port_args, extra_run_args)."""
    agent_user = "ubuntu"
    port_args: list[str] = []
    extra_run_args: list[str] = []
    if resolved_dockerfile.name == "Dockerfile.agent":
        info = parse_dockerfile_agent(resolved_dockerfile)
        agent_user = info.agent_user
        for port in info.expose_ports:
            port_args.extend(["-p", f"127.0.0.1:{port}:{port}"])
        extra_run_args = info.extra_run_args
    return agent_user, port_args, extra_run_args


def _build_env_args(
    telegram_bot_token: str,
    telegram_chat_id: str,
    agent_name: str,
    instance_id: str,
    claude_home: str,
) -> list[str]:
    """Build -e flags for the docker run command."""
    return [
        "-e",
        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1",
        "-e",
        f"TELEGRAM_BOT_TOKEN={telegram_bot_token}",
        "-e",
        f"TELEGRAM_CHAT_ID={telegram_chat_id}",
        "-e",
        f"AGENT_NAME={agent_name}",
        "-e",
        f"AGENT_INSTANCE_ID={instance_id}",
        "-e",
        f"TERM={os.environ.get('TERM', 'xterm-256color')}",
        "-e",
        f"COLORTERM={os.environ.get('COLORTERM', 'truecolor')}",
        "-e",
        f"HOME={claude_home}",
    ]


def _build_volume_mounts(
    global_config_dir: Path,
    cwd: Path,
    tool_dir: Path,
    claude_home: str,
) -> list[str]:
    """Build all -v mount flags for the docker run command."""
    mounts: list[str] = []

    # Global config mounts
    mounts.extend(
        [
            "-v",
            f"{global_config_dir}/.claude.json:{claude_home}/.claude.json",
            "-v",
            f"{global_config_dir}/.claude:{claude_home}/.claude",
            # Workspace
            "-v",
            f"{cwd}:/workspace",
        ]
    )

    # Per-project state directory mounts
    for name, dest in _STATE_MOUNTS.items():
        mounts.extend(["-v", f"{cwd}/.claude/{name}:{claude_home}/.claude/{dest}"])

    # Per-project state file mounts
    for name in _STATE_FILES:
        mounts.extend(["-v", f"{cwd}/.claude/{name}:{claude_home}/.claude/{name}"])

    # Tool directory mounted read-only into the container.
    mounts.extend(["-v", f"{tool_dir}/ops:{AGENT_WRAP_MOUNT}:ro"])

    return mounts


def run(args: list[str], tool_dir: Path) -> int:
    """
    Execute the `run` subcommand.

    Args:
        args: Command-line arguments (after 'run').
        tool_dir: Path to the agent-wrap tool directory.

    Returns:
        Exit code from docker run.

    """
    # Parse --base flag
    use_base = False
    claude_args: list[str] = []
    for arg in args:
        if arg == "--base":
            use_base = True
        else:
            claude_args.append(arg)

    # Check for updates (best-effort, non-blocking)
    from agent_wrap.commands.update import check as check_updates

    if check_updates(tool_dir):
        return 0  # Update applied, abort original operation

    # Resolve image
    try:
        resolved = resolve_image(tool_dir, use_base=use_base)
    except SystemExit as e:
        print(str(e), file=sys.stderr)
        return 1

    # Validate image exists
    if not docker_utils.image_exists(resolved.image):
        if use_base:
            print(
                f"Error: Base image '{resolved.image}' not found. "
                f"Run 'agent rebuild --full' to build it.",
                file=sys.stderr,
            )
        else:
            print(
                f"Error: Image '{resolved.image}' not found. "
                f"Run 'agent rebuild' in this directory to build it.",
                file=sys.stderr,
            )
        return 1

    # Parse Dockerfile.agent directives
    agent_user, port_args, extra_run_args = _parse_dockerfile_directives(resolved.dockerfile)

    agent_network = _extract_network(extra_run_args)
    use_host_net, host_net_args, port_args = _resolve_host_network(agent_network, port_args)
    claude_home = f"/home/{agent_user}"
    agent_name = _resolve_agent_name(use_base=use_base, cwd=Path.cwd())
    telegram_bot_token, telegram_chat_id = _load_secrets()

    instance_uuid = generate_uuid()
    instance_id = f"{agent_name}-{instance_uuid}"

    # Prepare config
    cwd = Path.cwd()
    global_config_dir = tool_dir / ".claude_config"
    config.prepare_global_config(
        global_config_dir,
        tool_dir,
        telegram_bot_token,
        telegram_chat_id,
    )
    config.prepare_project_dirs(cwd, tuple(_STATE_MOUNTS.keys()), _STATE_FILES)
    config.record_project(tool_dir)

    wslg_args = _build_wslg_args(tool_dir)

    print(f"--- Agent instance: {instance_id} ---")

    # Provider lifecycle
    provider = get_provider()
    provider.ensure(
        use_host_net=use_host_net,
        instance_id=instance_id,
        agent_network=agent_network,
    )

    try:
        label_args = provider.get_label_args(instance_id)
        provider_run_args = provider.get_run_args()

        print(f"--- Launching Claude (Image: {resolved.image}, Config: {global_config_dir}) ---")

        volume_mounts = _build_volume_mounts(global_config_dir, cwd, tool_dir, claude_home)
        user_args = docker_utils.get_user_args()

        cmd = [
            "docker",
            "run",
            "--rm",
            "-it",
            *user_args,
            *volume_mounts,
            *_build_env_args(
                telegram_bot_token,
                telegram_chat_id,
                agent_name,
                instance_id,
                claude_home,
            ),
            # Spliced arrays
            *label_args,
            *provider_run_args,
            *port_args,
            *wslg_args,
            *host_net_args,
            *extra_run_args,
            # Image and passthrough args
            resolved.image,
            *claude_args,
        ]

        result = subprocess.run(cmd)
        return result.returncode
    finally:
        provider.release(instance_id)
