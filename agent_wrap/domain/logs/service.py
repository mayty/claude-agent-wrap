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
    SPAWN_LOCK_NAME,
    SPAWN_LOCK_TIMEOUT_SEC,
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
from agent_wrap.exceptions import LockTimeoutError
from agent_wrap.lib.flock import file_lock
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

    def starting_line(self, port: int) -> str:
        """
        Return the line printed for a viewer that is claimed but not yet listening.

        The port is hedged rather than stated: a starting viewer has not reached
        ``bind_port`` yet, and that scans upward if the requested port is taken.
        """
        return (
            f"LiteLLM log viewer is starting; it will serve at "
            f"http://127.0.0.1:{port} (or the next free port) once ready"
        )

    def running_server(self) -> DaemonState | None:
        """
        Return the state of the viewer that holds this host, or None when none does.

        "Holds" covers a viewer that has been claimed but is not listening yet -- check
        ``starting`` on the result to tell the two apart. Callers use this to decide
        whether to spawn, and a claim is exactly as good a reason not to as a bound port.
        """
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
                running=False,
                pid=None,
                port=None,
                starting=False,
                log_size=log_size,
                log_mtime=log_mtime,
            )
        alive = pid_alive(state["pid"])
        return ViewerState(
            running=alive,
            pid=state["pid"],
            port=state["port"],
            # Only a live process can be starting: a dead pid marked "starting" is a claim
            # whose spawn never made it, which is a report of "not running", not "coming up".
            starting=alive and state["starting"],
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

    def autostart(self) -> bool:
        """
        Start a detached viewer without waiting for it to become ready.

        The `agent run` path. Idempotent -- a viewer already running or already coming up
        is adopted -- and deliberately without a teardown counterpart: the viewer is a
        host-level singleton that outlives whichever agent happened to start it.

        Returns whether a viewer is running or on its way. Failure is reported rather
        than raised: the caller is a launch that must proceed regardless.
        """
        try:
            return self._claim_or_spawn(LOGS_DEFAULT_PORT) is not None
        except (LockTimeoutError, OSError):
            return False

    def spawn_background(self, port: int) -> int:
        """Spawn a detached viewer and wait for it to start listening."""
        port = port or LOGS_DEFAULT_PORT
        try:
            claimed = self._claim_or_spawn(port)
        except (LockTimeoutError, OSError) as e:
            self._display.error(f"could not start the logs viewer: {e}")
            return 1
        if claimed is None:
            self._display.error("could not start the logs viewer")
            return 1
        if not claimed["starting"]:
            # Already listening: either a concurrent launcher won the lock and its viewer
            # is up, or our own child bound its port before we got here.
            self._display.info(self.connect_line(claimed["port"]))
            return 0

        # Wait for the viewer to publish its listening state, or timeout. Animate a
        # spinner so the user isn't staring at a blank screen during the cold start.
        pid = claimed["pid"]
        captured_port: list[int | None] = [None]

        def _wait_for_child() -> None:
            deadline = time.monotonic() + SPAWN_TIMEOUT_SEC
            while time.monotonic() < deadline:
                state = self.running_server()
                if state is not None and state["pid"] == pid and not state["starting"]:
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

        # Timed out — clean up the orphaned viewer and the claim it never honoured.
        with contextlib.suppress(ProcessLookupError, OSError):
            os.kill(pid, signal.SIGTERM)
        with contextlib.suppress(OSError):
            state_file().unlink(missing_ok=True)
        self._display.error("logs viewer started but did not become ready in time")
        return 1

    def _claim_or_spawn(self, port: int) -> DaemonState | None:
        """
        Return the viewer that is up or coming up, spawning one under the lock if none is.

        The lock is what makes this safe to call concurrently: reading the state and
        acting on it is one critical section, so two launchers cannot both conclude that
        nothing is running and each start a viewer. It also keeps
        :meth:`running_server`'s repair of a stale state file from racing a fresh claim.

        None means the spawn produced no claim -- the caller decides whether that is an
        error or a warning.
        """
        with file_lock(state_dir() / SPAWN_LOCK_NAME, timeout=SPAWN_LOCK_TIMEOUT_SEC):
            existing = self.running_server()
            if existing is not None:
                return existing
            self._fork_spawn(port)
            return read_state()

    def _fork_spawn(self, port: int) -> None:
        """
        Start the viewer through an intermediate fork, and claim the state file for it.

        The intermediate exists to hand the viewer off to init. Spawning it directly from
        a caller that then lives for hours -- `agent run` -- would leave a zombie for the
        rest of that run every time the viewer exited first, because nobody would ever
        wait on it. Forking first means the viewer's parent dies immediately, so the
        viewer is reparented to init and reaped there, while this process waits only on
        the intermediate and does so within a millisecond.

        The intermediate also writes the claim, because it is the only side that learns
        the viewer's pid. That keeps the claim inside the caller's lock: ``waitpid``
        returns only after the state file is on disk.
        """
        # Honour an already-set tool dir env (e.g. from test wrappers) so the child
        # resolves the same state file as the parent. Otherwise default to TOOL_DIR.
        tool_dir = os.environ.get(LOGS_TOOL_DIR_ENV, str(TOOL_DIR))
        env = {**os.environ, LOGS_TOOL_DIR_ENV: str(tool_dir)}
        argv = [sys.executable, "-m", "agent_wrap", "logs", "--foreground", f"--port={port}"]

        pid = os.fork()
        if pid == 0:
            try:
                # close_fds stays at its default True: an flock belongs to the open file
                # description and survives fork, so a viewer inheriting the spawn lock's
                # fd would hold that lock for its entire lifetime and wedge every later
                # spawn. The intermediate's own inherited copy is harmless because it
                # exits at once and the caller's fd keeps the description alive.
                child = subprocess.Popen(
                    argv,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    stdin=subprocess.DEVNULL,
                    env=env,
                    start_new_session=True,
                )
                write_state(child.pid, port, starting=True)
            finally:
                # os._exit, never sys.exit: it skips both the atexit/stdout flush that
                # would duplicate the caller's buffered output, and -- load-bearing --
                # the enclosing file_lock's release. That release would unlock the shared
                # open file description, dropping the caller's lock while it still
                # believes it holds it.
                os._exit(0)
        os.waitpid(pid, 0)

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
