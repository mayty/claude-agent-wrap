# This file has been created with the assistance of an AI tool.
"""Logs viewer domain service — session listing, record normalization, and daemon lifecycle."""

import contextlib
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
from typing import TYPE_CHECKING

from agent_wrap.constants import (
    AGENT_LAUNCHES_DIR,
    LITELLM_LOGS_DIRNAME,
    LOGS_DEFAULT_PORT,
    LOGS_TOOL_DIR_ENV,
    TOOL_DIR,
)
from agent_wrap.domain.logs.cache import LogsCache
from agent_wrap.domain.logs.constants import (
    CACHE_HEARTBEAT_INTERVAL_SEC,
    EVENTLESS_FILESYSTEMS,
    INGEST_LOCK_NAME,
    LOG_FILE_NAME,
    LOGS_VIEWER_LABEL,
    MICROSECONDS_PER_SECOND,
    POLL_INTERVAL_SEC,
    RETENTION_DAYS_ENV,
    SECONDS_PER_DAY,
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
from agent_wrap.domain.logs.ingest import LogFiles, discover_sessions, read_ingest_chunks
from agent_wrap.domain.logs.models import (
    ExpiredSession,
    IndexLag,
    IndexReclaim,
    IngestReport,
    RetentionResult,
    RetentionScope,
    ViewerState,
)
from agent_wrap.domain.logs.server import bind_port, get_handler
from agent_wrap.domain.logs.stream import SessionStream
from agent_wrap.exceptions import LockTimeoutError, StorageError
from agent_wrap.infrastructure.logs.models import SessionState
from agent_wrap.lib.flock import file_lock, try_file_lock
from agent_wrap.lib.mounts import filesystem_type
from agent_wrap.lib.process_utils import pid_alive
from agent_wrap.lib.utils import directory_size

if TYPE_CHECKING:
    from pathlib import Path
    from types import FrameType

    from agent_wrap.domain.config.service import ConfigService
    from agent_wrap.domain.display.service import DisplayService
    from agent_wrap.domain.logs.models import DaemonState
    from agent_wrap.domain.pricing.service import PricingService
    from agent_wrap.domain.stats.service import StatsService
    from agent_wrap.infrastructure.logs.models import SessionKey
    from agent_wrap.infrastructure.logs.repositories.ingest import LogIngestRepository
    from agent_wrap.infrastructure.logs.repositories.requests import RequestRepository
    from agent_wrap.infrastructure.logs.repositories.sessions import SessionRepository


class LogsService:
    """Facade for the logs viewer subsystem."""

    def __init__(  # noqa: PLR0913, PLR0917 -- four collaborators and three repositories
        self,
        pricing_service: PricingService,
        stats_service: StatsService,
        config_service: ConfigService,
        display_service: DisplayService,
        log_ingest_repository: LogIngestRepository,
        log_session_repository: SessionRepository,
        log_request_repository: RequestRepository,
    ) -> None:
        self._pricing = pricing_service
        self._stats = stats_service
        self._config = config_service
        self._display = display_service
        self._ingest = log_ingest_repository
        self._sessions = log_session_repository
        self._requests = log_request_repository

    # Ingest ----------------------------------------------------------

    def index_lag(self) -> IndexLag:
        """
        Report how far behind the log files the index is, without reading one.

        Two filesystem questions and no parse: has an indexed session's messages file
        grown past the offset ingest recorded, and are there session directories the
        index has never seen? Both are answered with ``stat()`` and the same three-level
        walk ingest uses, which is why ``agent stats`` can report staleness while still
        never opening a log file.

        A session whose file has *shrunk* counts as behind too. It was replaced rather
        than appended to, so the index holds records that no longer exist -- and
        ``agent reindex`` is the fix for that as well, since it re-reads a truncated
        session from zero.

        This lives in the logs domain rather than in stats because it is a question
        about the log tree and the ingest watermarks, both of which are this domain's.
        ``agent stats`` asks for the number and prints it.
        """
        behind = 0
        indexed = self._ingest.watermarks()
        logs_root = TOOL_DIR / LITELLM_LOGS_DIRNAME
        for watermark in indexed:
            messages = LogFiles.messages(logs_root.joinpath(*watermark.key))
            try:
                size = messages.stat().st_size
            except OSError:
                # Gone, or unreadable. Not lag: there is nothing left to ingest, and
                # reconciling a deleted directory's rows is `agent cleanup`'s job.
                continue
            if size != watermark.messages_offset:
                behind += 1

        known = {watermark.key for watermark in indexed}
        unseen = sum(1 for key, _ in discover_sessions(logs_root) if key not in known)
        return IndexLag(behind=behind + unseen, total=len(indexed) + unseen)

    def ingest_tree(self) -> IngestReport | None:
        """
        Bring the logs database up to date with the whole log tree, once.

        Returns ``None`` — and does nothing at all — when another process holds the
        ingest lock. That is not a failure to retry here: the holder is either the
        viewer daemon or another ``agent reindex``, and either way it is already doing
        exactly this work. The caller decides how to report it.

        The lock is host-wide rather than per session because the writes are: every
        chunk goes through one ``rw()`` transaction on one database file, so two
        concurrent passes would contend on SQLite instead of on this, and do it after
        having already paid for the parsing.
        """
        lock_path = AGENT_LAUNCHES_DIR / INGEST_LOCK_NAME
        with try_file_lock(lock_path) as acquired:
            if not acquired:
                return None
            return self._ingest_tree_locked()

    def _ingest_tree_locked(self) -> IngestReport:
        """Walk every session in the tree and ingest what each one has gained."""
        changed = 0
        reset = 0
        records = 0
        failed: list[tuple[Path, str]] = []

        sessions = discover_sessions(TOOL_DIR / LITELLM_LOGS_DIRNAME)
        for key, session_dir in sessions:
            try:
                outcome = self._ingest_session(key, session_dir)
            except (StorageError, OSError, ValueError) as exc:
                # One unreadable or inconsistent session must not cost the other 612.
                # It is still reported, and still makes the verb exit non-zero.
                failed.append((session_dir, str(exc)))
                continue
            records += outcome.records_ingested
            changed += outcome.sessions_changed
            reset += outcome.sessions_reset

        return IngestReport(
            sessions_seen=len(sessions),
            sessions_changed=changed,
            sessions_reset=reset,
            records_ingested=records,
            failed=tuple(failed),
        )

    def _ingest_session(self, key: SessionKey, session_dir: Path) -> IngestReport:
        """
        Ingest whatever *session_dir* has gained since its stored watermarks.

        Returns a single-session report so the caller only has to sum. Truncation is
        handled first and by forgetting the session outright: both files are strictly
        append-only, so a file shorter than its own watermark was replaced, and no
        stored offset means anything after that.
        """
        state = self._ingest.session_state(key)
        was_reset = False
        if state is not None and LogFiles.is_truncated(session_dir, state):
            self._ingest.reset_session(key)
            state = None
            was_reset = True
        if state is None:
            state = SessionState.unseen()

        records = 0
        chunks = 0
        for chunk in read_ingest_chunks(session_dir, state):
            self._ingest.ingest_chunk(key, chunk)
            records += len(chunk.requests)
            chunks += 1

        # A chunk carrying no requests still counts as a change: it advanced the strings
        # watermark, which is what a session whose only new content is interned strings
        # produces, and reporting it as unchanged would make the next pass look wrong.
        return IngestReport(
            sessions_seen=1,
            sessions_changed=int(bool(chunks) or was_reset),
            sessions_reset=int(was_reset),
            records_ingested=records,
            failed=(),
        )

    # Retention and reclaim -------------------------------------------

    @staticmethod
    def retention_days() -> int:
        """
        Return ``AGENT_LOGS_RETENTION_DAYS``, or 0 when retention is switched off.

        Read here rather than at import, unlike every other ``AGENT_*`` value in this
        codebase, because this one decides whether files get deleted: the value that
        should govern a run is the one exported for that run, not whichever was in the
        environment when some module first happened to be imported.

        Unset, empty and 0 all mean off. A negative or malformed value raises, on the
        same reasoning as ``AGENT_DAY_START_UTC``: it can only be a mistake, and the
        mistake to avoid is silently substituting a default for a setting that deletes
        things. The message names the variable, because a bare ``int()`` failure two
        frames down does not say which of the ``AGENT_*`` values was mistyped.
        """
        raw = os.environ.get(RETENTION_DAYS_ENV, "").strip()
        if not raw:
            return 0
        msg = f"{RETENTION_DAYS_ENV} must be a whole number of days from 0 up, got {raw!r}"
        try:
            days = int(raw)
        except ValueError as exc:
            raise ValueError(msg) from exc
        if days < 0:
            raise ValueError(msg)
        return days

    def retention_scope(self) -> RetentionScope:
        """
        Survey the session directories retention would delete, deleting nothing.

        Two filters, and the second is the one that matters. Age comes from the index:
        a session is a candidate when its newest request is older than the cutoff. But
        a candidate is only deletable when the index has read the whole of its
        ``messages.jsonl`` -- because retention deletes the log files along with the
        rows, and a file with unread bytes at the end holds requests no consumer has
        ever seen. Comparing the live size against the stored watermark is a ``stat()``,
        not a read, so this stays on the right side of the rule that only the ingester
        opens a log file.

        A session whose file cannot be stat'd at all is left alone rather than assumed
        gone. Retention's contract is that it removes a session's files and its rows
        together; a directory that has already been deleted by hand is not that, and
        reconciling one is ``agent cleanup``'s business.

        Sizes are measured now, over exactly the directories the run will be handed, so
        the preview a user confirms describes the run that follows.
        """
        days = self.retention_days()
        if not days:
            return RetentionScope(days=0, sessions=())
        cutoff_us = round((time.time() - days * SECONDS_PER_DAY) * MICROSECONDS_PER_SECOND)
        logs_root = TOOL_DIR / LITELLM_LOGS_DIRNAME
        expired = []
        for watermark in self._ingest.expired_sessions(cutoff_us):
            session_dir = logs_root.joinpath(*watermark.key)
            if not self._fully_indexed(session_dir, watermark.messages_offset):
                continue
            expired.append(
                ExpiredSession(
                    key=watermark.key,
                    path=session_dir,
                    messages_offset=watermark.messages_offset,
                    size_bytes=directory_size(session_dir),
                )
            )
        return RetentionScope(days=days, sessions=tuple(expired))

    def reclaim_index(self, scope: RetentionScope) -> IndexReclaim | None:
        """
        Apply *scope*'s retention and then sweep the blobs, under one ingest lock.

        One method rather than two because the two are one order: retention deletes the
        session rows, and the sweep is the only thing that reclaims the content those
        rows were the last reference to. Called the other way round, retention would
        free log-tree bytes and nothing at all inside the database until the next run.

        ``None`` when another process holds the ingest lock, exactly as
        :meth:`ingest_tree` reports it -- and for a sharper reason here. The sweep
        decides what is unreachable in one pass and deletes it in another, so an ingest
        committing between the two could leave a request pointing at content that has
        just been deleted. The lock every writer of this database already takes is what
        excludes that; it is also what stops a pass from deleting the files another
        pass is halfway through reading.

        Called by ``agent cleanup`` and ``agent reindex --prune``, and by nothing else.
        Deciding what is unreachable costs a decode of every blob a request still points
        at, because the pointers that reach an interned string live inside those
        payloads and no SQL can see them -- which is a price worth paying when the user
        has asked to reclaim space, and never one to pay on a timer.
        """
        lock_path = AGENT_LAUNCHES_DIR / INGEST_LOCK_NAME
        with try_file_lock(lock_path) as acquired:
            if not acquired:
                return None
            return IndexReclaim(
                retention=self._apply_retention(scope), sweep=self._ingest.sweep_blobs()
            )

    def _apply_retention(self, scope: RetentionScope) -> RetentionResult:
        """
        Delete each expired session's directory, then forget the ones that went.

        The same order and the same reasoning as ``agent cleanup``'s orphan delete: the
        directory first, its row second, and the row only for a directory whose removal
        actually succeeded. A failed ``rmtree`` leaves a session purely live -- still
        indexed, still readable, and still expired next time -- while dropping the row
        first would blank a session's spend while its files sat waiting to be re-read.

        The watermark check is repeated here, under the lock, rather than trusted from
        the survey. It is one ``stat()`` per directory and it is what makes "retention
        never deletes an unread request" a property of the delete instead of an
        observation someone made a few seconds earlier.
        """
        freed = 0
        deleted: list[SessionKey] = []
        for session in scope.sessions:
            if not self._fully_indexed(session.path, session.messages_offset):
                continue
            try:
                shutil.rmtree(session.path)
            except OSError:
                # Still live, still indexed, still expired. Try the remaining sessions.
                continue
            self._prune_empty_parents(session.path.parent)
            deleted.append(session.key)
            freed += session.size_bytes
        self._ingest.delete_sessions(deleted)
        return RetentionResult(sessions=len(deleted), freed_bytes=freed)

    @staticmethod
    def _fully_indexed(session_dir: Path, messages_offset: int) -> bool:
        """
        Report whether the index has read *session_dir*'s records file to its end.

        False for a file that cannot be stat'd, which folds "gone" and "unreadable" in
        with "not fully read". All three mean the same thing to the only caller: this
        is not a session retention may delete.
        """
        try:
            return LogFiles.messages(session_dir).stat().st_size == messages_offset
        except OSError:
            return False

    @staticmethod
    def _prune_empty_parents(directory: Path) -> None:
        """
        Remove the provider and project directories a deleted session leaves empty.

        Walks up as far as the log root and no further. ``rmdir`` refuses a directory
        that still holds anything, so a project with sessions left keeps its directory
        and the walk stops there -- the refusal is the test, and there is no separate
        emptiness check to race against.
        """
        root = TOOL_DIR / LITELLM_LOGS_DIRNAME
        while directory != root and root in directory.parents:
            try:
                directory.rmdir()
            except OSError:
                return
            directory = directory.parent

    # Daemon lifecycle -------------------------------------------------

    def connect_line(self, port: int) -> str:
        """Return the connect line printed to the terminal."""
        return f"LiteLLM log viewer running at http://127.0.0.1:{port}"

    def install_location_warning(self) -> str | None:
        """
        Return a warning when the wrapper sits where filesystem events never arrive.

        The viewer learns about new requests from inotify, and inotify on a Windows
        drive under WSL2 or on a network share accepts a watch and then delivers
        nothing -- silently, with no error to catch. It cannot be probed for, so the
        install location is checked instead.

        Only the wrapper's own location matters: every sidecar writes into
        ``TOOL_DIR/litellm-logs``, so a *project* on such a filesystem is served
        perfectly well. None means either a filesystem that works or -- off Linux --
        no way to tell, and both are reported the same way, by saying nothing.
        """
        fs_type = filesystem_type(TOOL_DIR)
        if fs_type is None or fs_type not in EVENTLESS_FILESYSTEMS:
            return None
        return (
            f"agent-wrap is installed on a {fs_type} filesystem ({TOOL_DIR}), which does "
            f"not deliver filesystem events. The logs viewer will only notice new "
            f"requests on its periodic check, up to "
            f"{int(CACHE_HEARTBEAT_INTERVAL_SEC)}s late. Clone it onto a local "
            f"filesystem to get live updates."
        )

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
        location_warning = self.install_location_warning()
        if location_warning is not None:
            log_info("Watch", location_warning)
        logs_cache = LogsCache(self._stats, self._config, self._sessions, ingest=self.ingest_tree)
        logs_cache.start()
        # Built here rather than injected for the same reason the cache is: it is a
        # collaborator of the serve loop alone, and every command that does not serve
        # would otherwise pay for one.
        handler = get_handler(SessionStream(self._requests, self._pricing), logs_cache)
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
        except LockTimeoutError, OSError:
            return False

    def spawn_background(self, port: int) -> int:
        """Spawn a detached viewer and wait for it to start listening."""
        port = port or LOGS_DEFAULT_PORT
        location_warning = self.install_location_warning()
        if location_warning is not None:
            self._display.warning(location_warning)
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
        env = {**os.environ, LOGS_TOOL_DIR_ENV: tool_dir}
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
