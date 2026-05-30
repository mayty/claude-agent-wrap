# This file has been created with the assistance of an AI tool.
"""The `agent` subcommand — launches Claude Code in a Docker container."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from agent_wrap import config, docker_utils
from agent_wrap.providers import get_provider
from agent_wrap.utils import (
    DockerfileAgentInfo,
    ResolvedImage,
    generate_uuid,
    parse_dockerfile_agent,
    resolve_image,
    sanitize_name,
)

AGENT_WRAP_MOUNT = "/opt/agent-wrap"


def _is_wsl() -> bool:
    """Check if running on WSL."""
    try:
        version = Path("/proc/version").read_text()
        return "microsoft" in version.lower()
    except OSError:
        return False


def _is_truthy(value: str) -> bool:
    """Check if an env var value is truthy (not empty/0/false/no)."""
    return value.lower() not in ("", "0", "false", "no")


def _extract_network(extra_run_args: list[str]) -> str | None:
    """Extract --network value from a list of docker run flags."""
    for i, arg in enumerate(extra_run_args):
        if arg in ("--network", "--net"):
            if i + 1 < len(extra_run_args):
                return extra_run_args[i + 1]
        elif arg.startswith("--network=") or arg.startswith("--net="):
            return arg.split("=", 1)[1]
    return None


def _load_telegram_creds(secrets: dict) -> tuple[str, str]:
    """Extract Telegram credentials from secrets dict."""
    bot_token = secrets.get("TelegramBotToken", "") or ""
    chat_id = secrets.get("TelegramChatId", "") or ""
    return bot_token, chat_id


def run(args: list[str], tool_dir: Path) -> int:
    """Execute the `agent` subcommand.

    Args:
        args: Command-line arguments (after 'agent').
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
        resolved = resolve_image(tool_dir, use_base)
    except SystemExit as e:
        print(str(e), file=sys.stderr)
        return 1

    # Validate image exists
    if not docker_utils.image_exists(resolved.image):
        if use_base:
            print(
                f"Error: Base image '{resolved.image}' not found. "
                f"Run 'rebuild_agent --full' to build it.",
                file=sys.stderr,
            )
        else:
            print(
                f"Error: Image '{resolved.image}' not found. "
                f"Run 'rebuild_agent' in this directory to build it.",
                file=sys.stderr,
            )
        return 1

    # Parse Dockerfile.agent directives
    agent_user = "ubuntu"
    port_args: list[str] = []
    extra_run_args: list[str] = []
    if resolved.dockerfile.name == "Dockerfile.agent":
        info = parse_dockerfile_agent(resolved.dockerfile)
        agent_user = info.agent_user
        for port in info.expose_ports:
            port_args.extend(["-p", f"127.0.0.1:{port}:{port}"])
        extra_run_args = info.extra_run_args

    # Extract agent network from extra run args
    agent_network = _extract_network(extra_run_args)

    # Handle AGENT_USE_HOST_NETWORK
    use_host_net = False
    host_net_args: list[str] = []
    env_val = os.environ.get("AGENT_USE_HOST_NETWORK", "")
    if _is_truthy(env_val):
        if not _is_wsl():
            print(
                "Note: AGENT_USE_HOST_NETWORK ignored — only honored on WSL hosts.",
                file=sys.stderr,
            )
        elif agent_network:
            print(
                "Warning: AGENT_USE_HOST_NETWORK ignored — Dockerfile.agent already "
                "specifies --network via agent-run-args.",
                file=sys.stderr,
            )
        else:
            use_host_net = True
            host_net_args = ["--network", "host"]
            if port_args:
                print(
                    f"Warning: AGENT_USE_HOST_NETWORK is on — EXPOSE port mappings "
                    f"skipped. Services bind on the WSL distro's interfaces directly; "
                    f"ensure they listen on 127.0.0.1 to avoid LAN exposure.",
                    file=sys.stderr,
                )
                port_args = []

    claude_home = f"/home/{agent_user}"

    # Determine agent name
    cwd = Path.cwd()
    dockerfile_agent = cwd / "Dockerfile.agent"
    if not use_base and dockerfile_agent.is_file():
        # Read agent-name from Dockerfile.agent
        agent_name = ""
        with open(dockerfile_agent) as f:
            import re
            for line in f:
                if m := re.match(r"^#\s*agent-name:\s*(\S+)", line.strip()):
                    agent_name = m.group(1)
                    break
        if not agent_name:
            agent_name = sanitize_name(cwd.name) or "agent"
    else:
        agent_name = sanitize_name(cwd.name) or "agent"

    # Load secrets
    secrets_path = Path.home() / "claude_keys.json"
    if not secrets_path.exists():
        print(f"File {secrets_path} not found", file=sys.stderr)
        return 1
    try:
        secrets = json.loads(secrets_path.read_text())
    except json.JSONDecodeError:
        print(f"File {secrets_path} is not valid JSON", file=sys.stderr)
        return 1
    telegram_bot_token, telegram_chat_id = _load_telegram_creds(secrets)

    # Generate instance ID
    instance_uuid = generate_uuid()
    instance_id = f"{agent_name}-{instance_uuid}"

    # Prepare config
    global_config_dir = tool_dir / ".claude_config"
    config.prepare_global_config(
        global_config_dir, tool_dir,
        telegram_bot_token, telegram_chat_id,
    )
    config.prepare_project_dirs(cwd)
    config.record_project(tool_dir)

    # WSLg support
    wslg_args: list[str] = []
    if Path("/mnt/wslg").is_dir():
        wslg_args = [
            "-v", "/mnt/wslg:/mnt/wslg",
            "-v", "/mnt/wslg/.X11-unix:/tmp/.X11-unix",
            "-v", f"{tool_dir}/wl-paste-shim:/usr/local/bin/wl-paste:ro",
            "-e", "DISPLAY",
            "-e", "WAYLAND_DISPLAY",
            "-e", "XDG_RUNTIME_DIR=/mnt/wslg/runtime-dir",
        ]

    print(f"--- Agent instance: {instance_id} ---")

    # Provider lifecycle
    provider = get_provider()
    provider.ensure(use_host_net, instance_id, agent_network)

    try:
        label_args = provider.get_label_args(instance_id)
        provider_run_args = provider.get_run_args()

        print(f"--- Launching Claude (Image: {resolved.image}, Config: {global_config_dir}) ---")

        # Assemble docker run command
        user_args = docker_utils.get_user_args()

        cmd = [
            "docker", "run", "--rm", "-it",
            *user_args,
            # Global config mounts
            "-v", f"{global_config_dir}/.claude.json:{claude_home}/.claude.json",
            "-v", f"{global_config_dir}/.claude:{claude_home}/.claude",
            # Workspace
            "-v", f"{cwd}:/workspace",
            # Per-project state mounts
            "-v", f"{cwd}/.claude/sessions:{claude_home}/.claude/projects/-workspace",
            "-v", f"{cwd}/.claude/session-state:{claude_home}/.claude/sessions",
            "-v", f"{cwd}/.claude/daemon:{claude_home}/.claude/daemon",
            "-v", f"{cwd}/.claude/jobs:{claude_home}/.claude/jobs",
            "-v", f"{cwd}/.claude/daemon.lock:{claude_home}/.claude/daemon.lock",
            "-v", f"{cwd}/.claude/daemon.log:{claude_home}/.claude/daemon.log",
            "-v", f"{cwd}/.claude/daemon.status.json:{claude_home}/.claude/daemon.status.json",
            "-v", f"{cwd}/.claude/history.jsonl:{claude_home}/.claude/history.jsonl",
            "-v", f"{cwd}/.claude/plans:{claude_home}/.claude/plans",
            "-v", f"{cwd}/.claude/todos:{claude_home}/.claude/todos",
            "-v", f"{cwd}/.claude/tasks:{claude_home}/.claude/tasks",
            "-v", f"{cwd}/.claude/shell-snapshots:{claude_home}/.claude/shell-snapshots",
            "-v", f"{cwd}/.claude/session-env:{claude_home}/.claude/session-env",
            "-v", f"{cwd}/.claude/file-history:{claude_home}/.claude/file-history",
            "-v", f"{cwd}/.claude/paste-cache:{claude_home}/.claude/paste-cache",
            # Tool mounts (read-only)
            "-v", f"{tool_dir}/Dockerfile:{AGENT_WRAP_MOUNT}/Dockerfile:ro",
            "-v", f"{tool_dir}/agent-wrap.bashrc:{AGENT_WRAP_MOUNT}/agent-wrap.bashrc:ro",
            "-v", f"{tool_dir}/validate-dockerfile-agent:{AGENT_WRAP_MOUNT}/validate-dockerfile-agent:ro",
            "-v", f"{tool_dir}/statusline.py:{AGENT_WRAP_MOUNT}/statusline.py:ro",
            "-v", f"{tool_dir}/telegram-notify.sh:{AGENT_WRAP_MOUNT}/telegram-notify.sh:ro",
            "-v", f"{tool_dir}/md_to_html.js:{AGENT_WRAP_MOUNT}/md_to_html.js:ro",
            # Env vars
            "-e", "DISABLE_AUTOUPDATER=1",
            "-e", f"TELEGRAM_BOT_TOKEN={telegram_bot_token}",
            "-e", f"TELEGRAM_CHAT_ID={telegram_chat_id}",
            "-e", f"AGENT_NAME={agent_name}",
            "-e", f"AGENT_INSTANCE_ID={instance_id}",
            "-e", f"TERM={os.environ.get('TERM', 'xterm-256color')}",
            "-e", f"COLORTERM={os.environ.get('COLORTERM', 'truecolor')}",
            "-e", f"HOME={claude_home}",
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
