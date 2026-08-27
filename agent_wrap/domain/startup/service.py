# This file has been created with the assistance of an AI tool.
"""Per-project startup script domain service."""

from __future__ import annotations

import os
import subprocess
from typing import TYPE_CHECKING

from agent_wrap.constants import (
    AGENT_ASSETS_DIR,
    AGENT_BINARY_PATH,
    AGENT_DOCKERFILE_NAME,
    AGENT_STARTUP_SCRIPT_NAME,
    LEGACY_AGENT_DOCKERFILE_NAME,
    SIDECAR_NETWORK_NAME,
)
from agent_wrap.domain.startup.constants import DEFAULT_STARTUP_RUNNER, SHEBANG_PROBE_BYTES
from agent_wrap.exceptions import StartupScriptError

if TYPE_CHECKING:
    from pathlib import Path

    from agent_wrap.domain.display.service import DisplayService


class StartupService:
    """Runs a project's optional host-side startup script before launch."""

    def __init__(self, display_service: DisplayService) -> None:
        self._display = display_service

    def script_path(self, project_dir: Path) -> Path:
        """Where a project's startup script lives, whether or not it exists."""
        return project_dir / AGENT_ASSETS_DIR / AGENT_STARTUP_SCRIPT_NAME

    def runner_argv(self, script: Path) -> list[str]:
        r"""
        Resolve the interpreter for *script* from its shebang.

        The script is handed to an explicit interpreter rather than executed directly, so
        no execute bit is needed -- exec bits are routinely lost on Windows/WSL checkouts,
        and a silently skipped startup script is exactly the failure this feature must not
        have. A trailing ``\\r`` is stripped for the same reason: the checkouts that lose
        exec bits are the ones that rewrite line endings.

        The shebang remainder is split on whitespace rather than treated as the kernel's
        single argument, because the common ``#!/usr/bin/env python3`` form needs the
        split to work at all.
        """
        try:
            with open(script, "rb") as f:
                first_line = f.read(SHEBANG_PROBE_BYTES).split(b"\n", 1)[0]
        except OSError:
            return [DEFAULT_STARTUP_RUNNER]

        decoded = first_line.decode("utf-8", errors="ignore").rstrip("\r").strip()
        if decoded.startswith("#!"):
            argv = decoded[2:].split()
            if argv:
                return argv
        return [DEFAULT_STARTUP_RUNNER]

    def run(
        self,
        project_dir: Path,
        *,
        timeout: float,
        agent_name: str,
        instance_id: str,
    ) -> None:
        """
        Run the project's startup script to completion, from the project directory.

        Raises:
            StartupScriptError: If the script exits non-zero, exceeds *timeout*, or its
                interpreter cannot be executed. The launch is aborted in every case --
                an agent whose prerequisites failed to materialize is worse than no agent.

        """
        script = self.script_path(project_dir)
        rel = f"{AGENT_ASSETS_DIR}/{AGENT_STARTUP_SCRIPT_NAME}"
        if not script.is_file():
            self._display.warning(f"'# agent-enable-startup' is set but '{rel}' does not exist.")
            return

        self._display.banner(f"Running {rel} (timeout {timeout:g}s)")

        env = {
            **os.environ,
            "AGENT_NAME": agent_name,
            "AGENT_INSTANCE_ID": instance_id,
            "AGENT_SIDECAR_NETWORK": SIDECAR_NETWORK_NAME,
            "AGENT_BINARY": str(AGENT_BINARY_PATH),
        }

        try:
            # stdin is closed: the script holds the host-global sidecar lock while it
            # runs, so one that stopped to prompt would stall every other launcher.
            result = subprocess.run(
                [*self.runner_argv(script), str(script)],
                cwd=project_dir,
                env=env,
                stdin=subprocess.DEVNULL,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired as e:
            msg = f"Error: '{rel}' exceeded its {timeout:g}s timeout; aborting launch."
            raise StartupScriptError(msg) from e
        except OSError as e:
            msg = f"Error: could not execute '{rel}': {e}"
            raise StartupScriptError(msg) from e

        if result.returncode != 0:
            msg = f"Error: '{rel}' failed with exit code {result.returncode}; aborting launch."
            raise StartupScriptError(msg)

    def warn_if_unused(self, project_dir: Path, *, is_legacy: bool) -> None:
        """
        Warn when a startup script is present but nothing enables it.

        Without this a user who adds only the script gets no feedback at all -- the
        script simply never runs.
        """
        if not self.script_path(project_dir).is_file():
            return

        rel = f"{AGENT_ASSETS_DIR}/{AGENT_STARTUP_SCRIPT_NAME}"
        if is_legacy:
            self._display.warning(
                f"'{rel}' exists but startup scripts need "
                f"'# agent-enable-startup: true' in "
                f"'{AGENT_ASSETS_DIR}/{AGENT_DOCKERFILE_NAME}' -- move "
                f"'{LEGACY_AGENT_DOCKERFILE_NAME}' there to use it."
            )
            return
        self._display.warning(
            f"'{rel}' exists but is not enabled -- add "
            f"'# agent-enable-startup: true' to "
            f"'{AGENT_ASSETS_DIR}/{AGENT_DOCKERFILE_NAME}'."
        )
