# This file has been created with the assistance of an AI tool.
"""
Filesystem-event trigger for the logs cache.

The recursive watch on the *central* tree is not merely convenient, it is the only
correct scope. Each project's ``.claude/litellm-logs`` is a symlink into that tree, and
inotify watches inodes -- so a write is seen whichever symlinked path a reader would
have used. Scheduling the per-project paths would watch nothing at all, because
watchdog's recursive scheduling walks with ``os.walk``, which does not follow symlinks.

Threading: watchdog's emitter and dispatcher threads touch nothing here but
``SimpleQueue.put``, and one consumer thread owns every call back into the cache. The
handlers are constructed with a queue and never see the cache, so the cache's
single-writer invariant holds by construction.

No debounce and no poll interval: the consumer blocks for one path, then applies
everything already queued behind it, so coalescing falls out of the drain and the loop
settles at exactly the rate the work allows.
"""

import os
import threading
from datetime import timedelta
from pathlib import Path
from queue import Empty, SimpleQueue
from typing import TYPE_CHECKING, override

from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

from agent_wrap.domain.logs.constants import (
    CACHE_HEARTBEAT_INTERVAL_SEC,
    CACHE_STOP_TIMEOUT_SEC,
    WATCH_STOP,
)
from agent_wrap.domain.logs.daemon import log_debug, log_info
from agent_wrap.domain.logs.ingest import LogFiles

if TYPE_CHECKING:
    from watchdog.events import FileSystemEvent
    from watchdog.observers.api import BaseObserver

    from agent_wrap.domain.logs.cache import LogsCache


def event_paths(event: FileSystemEvent) -> tuple[Path, ...]:
    """
    Return the filesystem paths an event refers to, decoded.

    A move carries two: watchdog reports an atomic ``os.replace`` as one event whose
    *destination* is the interesting name, so reading ``src_path`` alone would miss it.
    ``os.fsdecode`` normalizes the ``bytes`` spelling a ``bytes`` watch produces.
    """
    raw = (event.src_path, getattr(event, "dest_path", ""))
    return tuple(Path(os.fsdecode(value)) for value in raw if value)


class _LogTreeHandler(FileSystemEventHandler):
    """
    Queue writes to session record files. Runs on a watchdog emitter thread.

    Only a session's record file is forwarded -- the ingester owns that predicate, since
    it owns what such a file is called. The filter drops the directory ``modified`` event
    inotify raises for every write, which would otherwise double every wakeup, and the
    ``strings.jsonl`` written beside each record file, which is always flushed just
    *before* the record referencing it and so is never the last write of a pair.

    Nothing here writes into the watched tree, so there is no feedback loop to filter.
    """

    def __init__(self, queue: SimpleQueue[object]) -> None:
        self._queue = queue

    @override
    def dispatch(self, event: FileSystemEvent) -> None:
        # `dispatch`, not one of the `on_*` hooks the base class routes to: every event
        # is treated identically here, so the routing would be pure overhead.
        for path in event_paths(event):
            if LogFiles.is_messages(path):
                self._queue.put(path)


class CacheWatcher:
    """
    Drives :class:`~agent_wrap.domain.logs.cache.LogsCache` from filesystem events.

    Owns the observer and the single consumer thread. Every call into the cache
    happens on that thread, so the cache needs no lock at all.

    Only the logs tree is watched; the project registry deliberately is not. A newly
    registered project's first session write is already an event here -- one the cache
    cannot attribute to a known group, so it falls back to a full ``reconcile`` that
    re-reads the registry. Until that write there is nothing to render anyway, and a
    registration never followed by one is picked up by the heartbeat.
    """

    def __init__(self, cache: LogsCache, logs_tree: Path) -> None:
        self._cache = cache
        self._logs_tree = logs_tree
        self._queue: SimpleQueue[object] = SimpleQueue()
        self._observer: BaseObserver | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        """
        Schedule the watch, start the observer, then start the consumer.

        Nothing here is defensive: a failure to establish a watch -- realistically an
        exhausted ``fs.inotify.max_user_watches`` -- takes the daemon down with the
        reason in its logfile. A viewer that silently stopped noticing new requests
        would be worse than one that did not start.
        """
        # The tree does not exist yet on a host where no sidecar has ever run, and
        # `agent run` starts the viewer before the sidecar comes up. Observer.schedule
        # raises on a missing path, so create it the way link_litellm_logs does.
        self._logs_tree.mkdir(parents=True, exist_ok=True)

        observer = Observer()
        observer.schedule(_LogTreeHandler(self._queue), str(self._logs_tree), recursive=True)
        observer.start()
        self._observer = observer

        self._thread = threading.Thread(target=self._run, daemon=True, name="logs-cache-watch")
        self._thread.start()
        log_info("Watch", f"watching {self._logs_tree}")

    def stop(self) -> None:
        """
        Stop the observer, then the consumer. Idempotent -- callers stop twice.

        Order matters: silencing the producers first keeps the sentinel from queueing
        behind a burst. Both joins are bounded, so a wedged thread cannot hold teardown
        past the point where ``stop_daemon`` escalates to SIGKILL.
        """
        observer = self._observer
        if observer is not None:
            self._observer = None
            observer.stop()
            observer.join(timeout=CACHE_STOP_TIMEOUT_SEC)

        thread = self._thread
        if thread is not None:
            self._thread = None
            self._queue.put(WATCH_STOP)
            thread.join(timeout=CACHE_STOP_TIMEOUT_SEC)

    def _run(self) -> None:
        while True:
            batch = self._take_batch()
            if batch is None:
                return
            # One bad pass must never take the consumer thread down with it, because
            # nothing would restart it and the viewer would go silently stale. Logged
            # rather than merely suppressed: this is the only place a defect in the cache
            # can surface, and a swallowed one leaves a viewer that looks healthy.
            try:
                with log_debug("Update", "applying changes", threshold=timedelta(seconds=2)):
                    if batch:
                        self._cache.apply_paths(batch)
                    else:
                        self._cache.reconcile()
            except Exception as exc:  # noqa: BLE001
                log_info("Update", f"pass failed, cache may be stale: {exc!r}")

    def _take_batch(self) -> set[Path] | None:
        """
        Block for one path, then take everything already queued behind it.

        An empty set means the heartbeat elapsed with nothing to do; None means stop.
        Collecting into a set is what makes a burst cheap, and why this module needs no
        debounce delay.
        """
        try:
            item = self._queue.get(timeout=CACHE_HEARTBEAT_INTERVAL_SEC)
        except Empty:
            return set()

        batch: set[Path] = set()
        while True:
            if item is WATCH_STOP:
                return None
            if isinstance(item, Path):
                batch.add(item)
            try:
                item = self._queue.get_nowait()
            except Empty:
                return batch
