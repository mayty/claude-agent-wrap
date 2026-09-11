# This file has been created with the assistance of an AI tool.
"""
Filesystem-event trigger for the logs cache.

Replaces the interval poll the cache used to run on. Two watches cover everything the
viewer reads: one recursive watch on the shared log tree, where every sidecar writes
its request records, and one non-recursive watch on the launches directory for the
project registry.

The recursive watch on the central tree is not merely convenient, it is the only
correct scope. Each project's ``.claude/litellm-logs`` is a *symlink* into that tree
(``ConfigService.link_litellm_logs``), and inotify watches inodes -- so a write is
seen no matter which symlinked path a reader would have used to reach it. Scheduling
the per-project paths instead would watch nothing at all, because watchdog's recursive
scheduling walks with ``os.walk``, which does not follow symlinks.

Threading. Watchdog runs its own emitter and dispatcher threads, and they touch
nothing here but ``SimpleQueue.put``. One consumer thread owns every call back into
the cache, which is what keeps the cache single-writer -- the handlers are constructed
with a queue and never see the cache at all, so the invariant holds by construction
rather than by convention.

There is deliberately no debounce and no poll interval. The consumer blocks for one
path, then takes everything already queued behind it and applies the batch, so
coalescing is a consequence of the drain rather than a delay to tune: the longer a
pass takes, the more has piled up behind it, and the loop settles at exactly the rate
the work allows.
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

    A move carries two: watchdog reports an atomic ``os.replace`` as a single event
    whose *destination* is the interesting name, so a handler that read ``src_path``
    alone would miss it entirely. Paths come back as ``bytes`` when a watch was
    scheduled with a ``bytes`` path; ``os.fsdecode`` normalizes both spellings.
    """
    raw = (event.src_path, getattr(event, "dest_path", ""))
    return tuple(Path(os.fsdecode(value)) for value in raw if value)


class _LogTreeHandler(FileSystemEventHandler):
    """
    Queue writes to session record files. Runs on a watchdog emitter thread.

    Only a session's record file is forwarded, because a write to it is the one event
    that means there are records to ingest -- the ingester owns the predicate, since it
    owns what such a file is called. The filter drops the directory ``modified``
    event inotify raises for every single write, which would otherwise wake the
    consumer once per write on top of the write itself; it drops the ``strings.jsonl``
    the sidecar writes beside each record file, which is always flushed just *before*
    the record referencing it and so is never the last write of a pair; and it drops
    the sidecar's ``meta.json``, a per-session cache the request index replaced and
    that no reader opens any more.

    Nothing here writes into the watched tree, so there is no feedback loop left to
    filter out. The viewer used to seed ``meta.json`` after a slow scan and depended on
    this filter to keep that from scheduling a pass behind itself.
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

    Only the logs tree is watched. The project registry deliberately is not, even
    though a registration is a change the viewer cares about: every project's logs land
    in this one tree, so a newly registered project's first session write is already an
    event here -- one the cache cannot attribute to a known group, which makes it fall
    back to a full ``reconcile`` that re-reads the registry. Until that write happens
    the project has nothing to render, so there is nothing an earlier wakeup could show.
    A registration that is never followed by a log write is picked up by the heartbeat.
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

        Nothing here is defensive. A failure to establish a watch -- an exhausted
        ``fs.inotify.max_user_watches`` being the realistic one -- propagates out of
        ``serve_foreground`` and takes the daemon down with the reason in its logfile,
        because there is no reduced mode for the viewer to limp along in and a viewer
        that silently stopped noticing new requests would be worse than one that did
        not start.
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

        Order matters: silencing the producers first means the sentinel cannot be
        queued behind a burst that would delay shutdown. Both joins are bounded, so a
        wedged thread cannot hold up ``serve_foreground``'s teardown past the point
        where ``stop_daemon`` escalates to SIGKILL and buffered log output is lost.
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

        Collecting into a set is what makes a burst cheap -- a session written to fifty
        times while the previous batch was being applied costs one rescan, not fifty --
        and it is why there is no debounce delay anywhere in this module.
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
