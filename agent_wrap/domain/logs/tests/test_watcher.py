# This file has been created with the assistance of an AI tool.
"""Tests for CacheWatcher — the filesystem-event trigger for the logs cache."""

import threading
from queue import SimpleQueue
from typing import TYPE_CHECKING
from unittest.mock import Mock

import pytest
from watchdog.events import FileCreatedEvent, FileModifiedEvent

import agent_wrap.domain.logs.watcher as watcher_mod
from agent_wrap.domain.logs.cache import LogsCache
from agent_wrap.domain.logs.constants import WATCH_STOP
from agent_wrap.domain.logs.watcher import (
    CacheWatcher,
    _LogTreeHandler,
)

if TYPE_CHECKING:
    from pathlib import Path

    from pytest_mock import MockerFixture


@pytest.fixture
def queue() -> SimpleQueue[object]:
    return SimpleQueue()


def _drain(queue: SimpleQueue[object]) -> list[object]:
    items: list[object] = []
    while not queue.empty():
        items.append(queue.get_nowait())
    return items


@pytest.fixture
def observer_class(mocker: MockerFixture) -> Mock:
    """Replace the Observer class where watcher.py names it, per the mocking rules."""
    return mocker.patch.object(watcher_mod, "Observer", autospec=True)


@pytest.fixture
def cache_mock() -> Mock:
    return Mock(spec=LogsCache)


@pytest.fixture
def watcher(tmp_path: Path, cache_mock: Mock, observer_class: Mock) -> CacheWatcher:
    """Return a watcher over tmp_path whose Observer is mocked out."""
    assert observer_class is not None
    return CacheWatcher(cache_mock, logs_tree=tmp_path / "litellm-logs")


def test_log_tree_handler_queues_a_messages_file(
    queue: SimpleQueue[object], tmp_path: Path
) -> None:
    path = tmp_path / "hash" / "provider" / "session" / "messages.jsonl"
    _LogTreeHandler(queue).dispatch(FileCreatedEvent(str(path)))
    assert _drain(queue) == [path]


@pytest.mark.parametrize(
    "name",
    ["meta.json", "meta.json.a1b2.tmp", "strings.jsonl", "usage.json"],
)
def test_log_tree_handler_ignores_everything_but_the_record_file(
    queue: SimpleQueue[object], tmp_path: Path, name: str
) -> None:
    """
    Only messages.jsonl is tracked, so only messages.jsonl is forwarded.

    ``meta.json`` matters most here: the viewer writes it into the very tree it is
    watching after a slow scan, so forwarding it would make every incremental
    update schedule a redundant pass behind itself.
    """
    path = tmp_path / "hash" / "provider" / "session" / name
    _LogTreeHandler(queue).dispatch(FileModifiedEvent(str(path)))
    assert _drain(queue) == []


def test_log_tree_handler_ignores_a_directory_event(
    queue: SimpleQueue[object], tmp_path: Path
) -> None:
    """
    A directory `modified` is raised by inotify for every write inside it.

    Forwarding those would hand the cache a path it cannot attribute to a session,
    degrading every batch to a full rescan — which is the whole cost the fast path
    exists to avoid.
    """
    session_dir = tmp_path / "hash" / "provider" / "session"
    _LogTreeHandler(queue).dispatch(FileModifiedEvent(str(session_dir)))
    assert _drain(queue) == []


def test_take_batch_collapses_duplicate_paths(watcher: CacheWatcher, tmp_path: Path) -> None:
    path = tmp_path / "a" / "b" / "c" / "messages.jsonl"
    for _ in range(50):
        watcher._queue.put(path)
    assert watcher._take_batch() == {path}


def test_take_batch_takes_everything_already_queued(watcher: CacheWatcher, tmp_path: Path) -> None:
    paths = {tmp_path / f"s{i}" / "p" / "sess" / "messages.jsonl" for i in range(5)}
    for path in paths:
        watcher._queue.put(path)
    assert watcher._take_batch() == paths


def test_take_batch_returns_an_empty_batch_when_the_heartbeat_elapses(
    watcher: CacheWatcher, mocker: MockerFixture
) -> None:
    """An empty batch is the signal to reconcile rather than apply."""
    mocker.patch.object(watcher_mod, "CACHE_HEARTBEAT_INTERVAL_SEC", 0.01)
    assert watcher._take_batch() == set()


def test_take_batch_reports_stop(watcher: CacheWatcher) -> None:
    watcher._queue.put(WATCH_STOP)
    assert watcher._take_batch() is None


def test_take_batch_reports_stop_queued_behind_a_burst(
    watcher: CacheWatcher, tmp_path: Path
) -> None:
    watcher._queue.put(tmp_path / "a" / "b" / "c" / "messages.jsonl")
    watcher._queue.put(WATCH_STOP)
    assert watcher._take_batch() is None


def test_a_batch_of_paths_is_applied(
    watcher: CacheWatcher, cache_mock: Mock, tmp_path: Path
) -> None:
    path = tmp_path / "a" / "b" / "c" / "messages.jsonl"
    applied = threading.Event()
    cache = cache_mock
    cache.apply_paths.side_effect = lambda _paths: applied.set()  # pyrefly: ignore [implicit-any-lambda]

    watcher.start()
    try:
        watcher._queue.put(path)
        assert applied.wait(5.0)
    finally:
        watcher.stop()

    cache.apply_paths.assert_called_once_with({path})
    cache.reconcile.assert_not_called()


def test_an_empty_batch_reconciles(
    watcher: CacheWatcher, cache_mock: Mock, mocker: MockerFixture
) -> None:
    mocker.patch.object(watcher_mod, "CACHE_HEARTBEAT_INTERVAL_SEC", 0.01)
    reconciled = threading.Event()
    cache = cache_mock
    cache.reconcile.side_effect = reconciled.set

    watcher.start()
    try:
        assert reconciled.wait(5.0)
    finally:
        watcher.stop()

    cache.apply_paths.assert_not_called()


def test_a_failing_pass_does_not_kill_the_consumer(
    watcher: CacheWatcher, cache_mock: Mock, tmp_path: Path
) -> None:
    """
    Nothing restarts this thread, so one bad pass must not end it.

    A cache left with a dead consumer would keep serving whatever it last knew,
    silently, which is worse than any single failed update.
    """
    first = threading.Event()
    second = threading.Event()
    calls: list[set[Path]] = []

    def _explode_once(paths: set[Path]) -> None:
        calls.append(paths)
        if len(calls) == 1:
            first.set()
            msg = "Simulated"
            raise OSError(msg)
        second.set()

    cache_mock.apply_paths.side_effect = _explode_once

    watcher.start()
    try:
        watcher._queue.put(tmp_path / "a" / "b" / "c" / "messages.jsonl")
        # Waited on rather than queued back-to-back: the consumer drains everything
        # pending into one batch, so two immediate puts would be one call, not two.
        assert first.wait(5.0)
        watcher._queue.put(tmp_path / "d" / "e" / "f" / "messages.jsonl")
        assert second.wait(5.0)
    finally:
        watcher.stop()


def test_stop_is_idempotent(watcher: CacheWatcher) -> None:
    watcher.start()
    watcher.stop()
    watcher.stop()
    assert watcher._thread is None


def test_stop_silences_the_observer_before_joining_the_consumer(
    watcher: CacheWatcher, observer_class: Mock
) -> None:
    """Producers must be quiet first, or the sentinel can queue behind a burst."""
    watcher.start()
    watcher.stop()
    observer_class.return_value.stop.assert_called_once()


def test_start_schedules_only_the_tree_and_does_so_recursively(
    watcher: CacheWatcher, observer_class: Mock, tmp_path: Path
) -> None:
    """
    The project registry is deliberately not watched.

    Every project's logs land in this one tree, so a newly registered project's first
    session write already arrives here — and the cache cannot attribute it to a known
    group, so it falls back to a full reconcile that re-reads the registry. Until that
    write happens the project has nothing to render.
    """
    watcher.start()
    try:
        calls = observer_class.return_value.schedule.call_args_list
        scheduled = {call.args[1]: call.kwargs["recursive"] for call in calls}
        assert scheduled == {str(tmp_path / "litellm-logs"): True}
    finally:
        watcher.stop()


def test_start_creates_the_log_tree_when_no_sidecar_has_run_yet(
    watcher: CacheWatcher, tmp_path: Path
) -> None:
    """`agent run` starts the viewer before the sidecar, and schedule() needs the path."""
    assert not (tmp_path / "litellm-logs").exists()
    watcher.start()
    try:
        assert (tmp_path / "litellm-logs").is_dir()
    finally:
        watcher.stop()


def test_a_watch_that_cannot_be_established_takes_the_daemon_down(
    watcher: CacheWatcher, observer_class: Mock
) -> None:
    """
    There is no reduced mode to fall back to, so the failure must be loud.

    An exhausted fs.inotify.max_user_watches is the realistic cause; a viewer that
    silently stopped noticing new requests would be worse than one that refused to
    start.
    """
    observer_class.return_value.schedule.side_effect = OSError("inotify watch limit reached")

    with pytest.raises(OSError, match="inotify watch limit reached"):
        watcher.start()


def test_a_real_write_under_the_watched_tree_reaches_the_cache(tmp_path: Path) -> None:
    """The one test that exercises inotify itself rather than a mock."""
    session_dir = tmp_path / "litellm-logs" / "hash" / "provider" / "session"
    session_dir.mkdir(parents=True)

    applied = threading.Event()
    seen: list[set[Path]] = []

    def _record(paths: set[Path]) -> None:
        seen.append(paths)
        applied.set()

    cache = Mock(spec=LogsCache)
    cache.apply_paths.side_effect = _record

    watcher = CacheWatcher(cache, logs_tree=tmp_path / "litellm-logs")
    watcher.start()
    try:
        (session_dir / "messages.jsonl").write_text("{}\n", encoding="utf-8")
        assert applied.wait(5.0)
    finally:
        watcher.stop()

    assert session_dir / "messages.jsonl" in set().union(*seen)
