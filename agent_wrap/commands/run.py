# This file has been edited with the assistance of an AI tool.
"""The `run` subcommand — launches Claude Code in a Docker container."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import TYPE_CHECKING

from agent_wrap import config
from agent_wrap.lib import docker_utils
from agent_wrap.lib.flock import file_lock
from agent_wrap.lib.utils import (
    generate_uuid,
    is_truthy_env,
    parse_dockerfile_agent,
    resolve_image,
    sanitize_name,
)
from agent_wrap.providers import get_provider
from agent_wrap.sidecars.tracker import SidecarTracker

if TYPE_CHECKING:
    from typing import TextIO

    from agent_wrap.providers.base import Provider
    from agent_wrap.sidecars.base import Sidecar

USAGE = "[--base] [claude-args...]"
SUMMARY = "Launch Claude Code in Docker"

#: Expected number of agents queued behind the shared sidecar lock (the in-flight
#: launch concurrency, e.g. an external "N simultaneous jobs" semaphore). Multiplied
#: by each sidecar's hot-path walk time to size the lock timeout. Overridable via
#: AGENT_EXPECTED_QUEUE_DEPTH for very large fan-outs.
EXPECTED_QUEUE_DEPTH = 128

#: How long a releasing (stopping) run sleeps before re-acquiring the shared lock
#: when it has yielded to a live starter. Stops are low priority and may wait
#: indefinitely, so this only bounds the busy-wait granularity, not total wait.
STOP_YIELD_POLL_SEC = 0.1

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
    """
    Build WSLg-related volume mounts and env vars.

    Mounts only the X11 and Wayland runtime sockets — NOT all of /mnt/wslg, which
    on WSL2 exposes the host distro root filesystem (/mnt/wslg/distro) read-only.
    """
    if not Path("/mnt/wslg").is_dir():
        return []
    return [
        "-v",
        "/mnt/wslg/runtime-dir:/mnt/wslg/runtime-dir",
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
    args = [
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
    auto_mode_flag = os.environ.get("CLAUDE_CODE_ENABLE_AUTO_MODE", None)
    if auto_mode_flag is not None:
        args.extend(["-e", f"CLAUDE_CODE_ENABLE_AUTO_MODE={auto_mode_flag}"])
    return args


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


def collect_sidecars(provider: Provider) -> list[Sidecar]:
    """
    Gather every sidecar an agent run depends on.

    Today this is exactly the selected provider's sidecars; it is the single place a
    runner-level sidecar (e.g. a future decision-maker, independent of the model
    backend) would be appended.
    """
    return list(provider.sidecars())


def build_agent_labels(instance_id: str) -> list[str]:
    """
    Build the agent container's --label / --name flags.

    One common role label (the tracker's host-wide live-agent count filter), the
    instance id, and the container name — all belonging to the agent once. There is
    no per-sidecar label: the shared SidecarTracker counts all agents together.
    """
    if not instance_id:
        return []
    return [
        "--label",
        f"{SidecarTracker.role_label}={SidecarTracker.role_value}",
        "--label",
        f"agent-wrap.instance-id={instance_id}",
        "--name",
        f"claude-agent-{instance_id}",
    ]


def _expected_queue_depth() -> int:
    """Return the expected queue depth — EXPECTED_QUEUE_DEPTH or its env override."""
    raw = os.environ.get("AGENT_EXPECTED_QUEUE_DEPTH")
    if raw:
        try:
            value = int(raw)
        except ValueError:
            value = 0
        if value > 0:
            return value
    return EXPECTED_QUEUE_DEPTH


def sidecar_lock_timeout(sidecars: list[Sidecar], queue_depth: int) -> float:
    """
    Total seconds a launcher waits for the shared sidecar lock.

    Σ(cold_start_time + queue_depth · short_circuit_time): one cold start per sidecar
    plus the whole queue draining the hot path sequentially — the poll-inside-lock
    worst case. A launcher that waits longer is genuinely stuck, not merely queued.
    """
    return sum(sc.cold_start_time + queue_depth * sc.short_circuit_time for sc in sidecars)


def _ensure_sidecars(
    sidecars: list[Sidecar],
    tracker: SidecarTracker,
    *,
    net: tuple[bool, str | None],
    instance_id: str,
) -> tuple[list[str], TextIO | None]:
    """
    Prepare (lock-free) then ensure all sidecars under one shared lock.

    *net* is ``(use_host_net, agent_network)``. Returns ``(run_args, running_handle)``:
    the merged `docker run` connectivity flags, and the open handle of this run's
    *running* registration. That handle must stay open until the run exits — its held
    ``flock`` is what tells a stopping run an agent is still live — and is released by
    :func:`_release_sidecars` in the runner's ``finally``.

    Holds a *start-waiter* ticket from before the shared lock is taken (so a stopping
    run yields to us) until the moment the lock is acquired, then registers *running*
    as the last action under the lock, just before the agent launches.
    """
    use_host_net, agent_network = net
    for sidecar in sidecars:
        sidecar.prepare()

    run_args: list[str] = []
    running_handle: TextIO | None = None
    # Claim priority before contending for the lock: a stopping run that sees this
    # held ticket yields the lock to us.
    waiter_handle = tracker.register_waiter(instance_id)
    timeout = sidecar_lock_timeout(sidecars, _expected_queue_depth())
    try:
        with file_lock(tracker.lock_path, timeout=timeout):
            # The ticket's only job — signalling "waiting for the lock" — is done; clear
            # it first so yielding stoppers stop spinning on us (matches lock.sh).
            tracker.clear_waiter(waiter_handle, instance_id)
            waiter_handle = None
            for sidecar in sidecars:
                run_args += sidecar.ensure(use_host_net=use_host_net, agent_network=agent_network)
            # Register as running as the LAST action under the lock, on success only:
            # from here until this run exits its held flock keeps a releaser in the
            # ensure→docker-run gap from tearing down.
            running_handle = tracker.register_running(instance_id)
    finally:
        # On any early exit (lock timeout, ensure failure) the ticket may still be held.
        tracker.clear_waiter(waiter_handle, instance_id)
    return run_args, running_handle


def _release_sidecars(
    sidecars: list[Sidecar],
    tracker: SidecarTracker,
    instance_id: str,
    running_handle: TextIO | None,
) -> None:
    """
    Last-light-out teardown: release ALL declared sidecars when this is the last agent.

    Stops are low priority: this blocks on the shared lock for as long as needed, but
    always yields it to a live starter (a still-held start-waiter ticket) — starts
    keep priority. Once no starter is waiting, teardown happens only if no *other*
    run's *running* registration is still held (no agent live anywhere). The teardown
    is a host-wide decision, so it releases *every* declared sidecar (in reverse),
    reaping orphans a failed or earlier run left running; ``release()`` is a no-op when
    a container isn't running, so this is safe.
    """
    # This run is finishing — drop our own running registration first so we don't count
    # ourselves as alive.
    tracker.clear_running(running_handle, instance_id)
    if not sidecars:
        return
    while True:
        with file_lock(tracker.lock_path):
            if not tracker.has_live_waiters():
                if not tracker.has_live_runners(exclude_id=instance_id):
                    for sidecar in reversed(sidecars):
                        sidecar.release()
                return
        # A starter has priority — we released the lock by leaving the block; wait a
        # beat and retry. Low priority: this may loop indefinitely while starts arrive.
        time.sleep(STOP_YIELD_POLL_SEC)


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
    config.link_litellm_logs(cwd, tool_dir)
    config.record_project(tool_dir)

    wslg_args = _build_wslg_args(tool_dir)

    print(f"--- Agent instance: {instance_id} ---")

    # Sidecar lifecycle. One shared lock (held by the runner) makes the whole launch
    # an atomic critical section, and one SidecarTracker holds the lock-file registries
    # that drive the teardown decision. Lock-free prepare() (image pull) runs BEFORE the
    # lock. The whole block is in the try so the last-light-out teardown always runs,
    # even on a failed start — and it releases the FULL declared set, reaping orphans.
    provider = get_provider()
    sidecars = collect_sidecars(provider)
    tracker = SidecarTracker(tool_dir)
    running_handle: TextIO | None = None
    try:
        provider_run_args, running_handle = _ensure_sidecars(
            sidecars,
            tracker,
            net=(use_host_net, agent_network),
            instance_id=instance_id,
        )

        label_args = build_agent_labels(instance_id)

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
        _release_sidecars(sidecars, tracker, instance_id, running_handle)
