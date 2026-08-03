# This file has been created with the assistance of an AI tool.
"""Logs viewer domain service — session listing, record normalization, and daemon lifecycle."""

from __future__ import annotations

import contextlib
import os
import signal
import subprocess
import sys
import threading
import time
from typing import TYPE_CHECKING

from agent_wrap.constants import (
    LOGS_DEFAULT_PORT,
    LOGS_TOOL_DIR_ENV,
    TOOL_DIR,
)
from agent_wrap.domain.logs.cache import LogsCache
from agent_wrap.domain.logs.constants import (
    LOG_FILE_NAME,
    LOGS_VIEWER_LABEL,
    POLL_INTERVAL_SEC,
    SPAWN_TIMEOUT_SEC,
    STOP_TIMEOUT_SEC,
)
from agent_wrap.domain.logs.daemon import (
    log_info,
    read_state,
    state_dir,
    state_file,
    write_state,
)
from agent_wrap.domain.logs.models import ViewerState
from agent_wrap.domain.logs.server import bind_port, get_handler
from agent_wrap.lib.process_utils import pid_alive

if TYPE_CHECKING:
    from types import FrameType

    from agent_wrap.domain.config.service import ConfigService
    from agent_wrap.domain.display.service import DisplayService
    from agent_wrap.domain.logs.models import DaemonState
    from agent_wrap.domain.pricing.service import PricingService
    from agent_wrap.domain.stats.service import StatsService


class LogsService:
    """Facade for the logs viewer subsystem."""

    def __init__(
        self,
        pricing_service: PricingService,
        stats_service: StatsService,
        config_service: ConfigService,
        display_service: DisplayService,
    ) -> None:
        self._pricing = pricing_service
        self._stats = stats_service
        self._config = config_service
        self._display = display_service

    # Daemon lifecycle -------------------------------------------------

    def connect_line(self, port: int) -> str:
        """Return the connect line printed to the terminal."""
        return f"LiteLLM log viewer running at http://127.0.0.1:{port}"

    def running_server(self) -> DaemonState | None:
        """Return the running viewer's state dict, or None when stale/dead."""
        state = read_state()
        if state is None:
            return None
        if not pid_alive(state["pid"]):
            # Stale state file — orphaned by a previous crash. Clean it up so the
            # next `agent logs` can start fresh without the port appearing "in use".
            with contextlib.suppress(OSError):
                state_file().unlink(missing_ok=True)
            return None
        return state

    def viewer_state(self) -> ViewerState:
        """
        Report the viewer's status without repairing anything.

        The read-only counterpart to :meth:`running_server`, which unlinks the state
        file when its pid is dead — correct on the launch path (so the port stops
        looking taken), wrong for a reporting caller that must not delete state it only
        looked at. The logfile's size and mtime come along because they are the cheapest
        way to tell a healthy viewer from one crash-looping.
        """
        state = read_state()
        log_path = state_dir() / LOG_FILE_NAME
        try:
            stat_result = log_path.stat()
            log_size: int | None = stat_result.st_size
            log_mtime: float | None = stat_result.st_mtime
        except OSError:
            log_size = None
            log_mtime = None

        if state is None:
            return ViewerState(
                running=False, pid=None, port=None, log_size=log_size, log_mtime=log_mtime
            )
        return ViewerState(
            running=pid_alive(state["pid"]),
            pid=state["pid"],
            port=state["port"],
            log_size=log_size,
            log_mtime=log_mtime,
        )

    def serve_foreground(self, port: int) -> int:
        """Blocking HTTP serve loop — the detached child's body. Writes state then blocks."""
        # Redirect stdout/stderr to the logfile before doing any work, so the child
        # never writes to the parent's terminal (the Popen's DEVNULL handles the
        # immediate handles, but sub-libraries might reopen; the logfile captures
        # them all) and so startup logging below actually lands somewhere —
        # Popen wired stdout/stderr to DEVNULL, so anything printed before this
        # redirect is lost.
        state_dir().mkdir(parents=True, exist_ok=True)
        logfile = state_dir() / LOG_FILE_NAME
        with logfile.open("a", encoding="utf-8") as lf:
            os.dup2(lf.fileno(), sys.stdout.fileno())
            os.dup2(lf.fileno(), sys.stderr.fileno())

        log_info("Logs server", "starting")
        logs_cache = LogsCache(self._stats, self._config, self._pricing)
        logs_cache.start()
        handler = get_handler(self._pricing, logs_cache)
        server = bind_port(port, handler)
        actual_port = server.server_address[1]
        write_state(os.getpid(), actual_port)
        log_info("Logs server", "started")

        def _handle_signal(signum: int, frame: FrameType | None) -> None:  # noqa: ARG001
            # server.shutdown() blocks on __is_shut_down.wait() which deadlocks
            # when called from a signal handler running in the same thread as
            # serve_forever().  Delegate to a daemon thread instead.
            threading.Thread(target=server.shutdown, daemon=True).start()

        signal.signal(signal.SIGTERM, _handle_signal)
        signal.signal(signal.SIGINT, _handle_signal)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            server.server_close()
            logs_cache.stop()
            log_info("Logs server", "stopped")
        return 0

    def spawn_background(self, port: int) -> int:
        """Re-exec as a detached child and wait for its state to appear."""
        port = port or LOGS_DEFAULT_PORT
        # Honour an already-set tool dir env (e.g. from test wrappers) so the child
        # resolves the same state file as the parent. Otherwise default to TOOL_DIR.
        tool_dir = os.environ.get(LOGS_TOOL_DIR_ENV, str(TOOL_DIR))
        env = {**os.environ, LOGS_TOOL_DIR_ENV: str(tool_dir)}
        child = subprocess.Popen(
            [sys.executable, "-m", "agent_wrap", "logs", "--foreground", f"--port={port}"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            env=env,
            start_new_session=True,
        )
        # Wait for child to publish its state, or timeout. Animate a spinner so
        # the user isn't staring at a blank screen during the cold start.
        captured_port: list[int | None] = [None]

        def _wait_for_child() -> None:
            deadline = time.monotonic() + SPAWN_TIMEOUT_SEC
            while time.monotonic() < deadline:
                state = self.running_server()
                if state is not None and state["pid"] == child.pid:
                    captured_port[0] = state["port"]
                    return
                time.sleep(POLL_INTERVAL_SEC)

        self._display.spin_while(
            label=LOGS_VIEWER_LABEL,
            message="starting…",
            done_message=lambda: self.connect_line(captured_port[0]) if captured_port[0] else None,
            work=_wait_for_child,
        )

        if captured_port[0] is not None:
            return 0

        # Timed out — clean up the orphaned child.
        with contextlib.suppress(OSError):
            child.send_signal(signal.SIGTERM)
        self._display.error("error: logs viewer started but did not become ready in time")
        return 1

    def stop_daemon(self) -> int:
        """Stop a running background viewer, if any."""
        state = self.running_server()
        if state is None:
            self._display.info("no viewer is running")
            return 0
        with contextlib.suppress(ProcessLookupError):
            os.kill(state["pid"], signal.SIGTERM)
        deadline = time.monotonic() + STOP_TIMEOUT_SEC
        while time.monotonic() < deadline:
            if not pid_alive(state["pid"]):
                with contextlib.suppress(OSError):
                    state_file().unlink(missing_ok=True)
                self._display.success("Logs viewer stopped.")
                return 0
            time.sleep(POLL_INTERVAL_SEC)
        # SIGTERM didn't work — try SIGKILL as a last resort.
        with contextlib.suppress(ProcessLookupError):
            os.kill(state["pid"], signal.SIGKILL)
        deadline = time.monotonic() + STOP_TIMEOUT_SEC
        while time.monotonic() < deadline:
            if not pid_alive(state["pid"]):
                with contextlib.suppress(OSError):
                    state_file().unlink(missing_ok=True)
                self._display.success("Logs viewer stopped.")
                return 0
            time.sleep(POLL_INTERVAL_SEC)
        self._display.warning(
            f"viewer (pid {state['pid']}) did not stop in time; state file left in place."
        )
        return 1
