# This file has been edited with the assistance of an AI tool.
"""
File-locking helpers over ``filelock``'s ``UnixFileLock`` — an ``fcntl.flock``.

Three flavours, matching the call sites in the sidecar lifecycle:

* :func:`file_lock` — blocking context manager with an optional timeout; raises
  :class:`LockTimeoutError` if the lock can't be taken in time. Used by the start
  path (``ensure``), which must win the lock and is given a generous timeout.
* :func:`try_file_lock` — non-blocking context manager; yields ``True`` if the lock
  was taken, ``False`` if someone else holds it. Used to probe registration files:
  acquiring the lock means the owner is gone (stale, reap it).
* :func:`lock_and_hold` — non-blocking; takes the lock and returns the held
  :class:`~filelock.BaseFileLock` so the caller can hold it across an arbitrary span (e.g.
  a whole agent run) rather than just one ``with`` block. The kernel drops the lock
  automatically when the holding process dies, which is what makes liveness immune to
  PID recycling.

Two directory-level probes read a whole directory of such files:

* :func:`any_live_locks` — the teardown predicate. Reaps stale files as it goes.
* :func:`live_lock_ids` — the reporting variant. Names every live holder and unlinks
  nothing, so a read-only caller cannot reap another process's state.

A lock file carries no content: its *name* is the instance id, and the lock is the
kernel's. So the truncation ``filelock`` performs once it owns a file is a no-op here,
and nothing may start writing a payload into one of these.
"""

import contextlib
from contextlib import contextmanager
from typing import TYPE_CHECKING

from filelock import BaseFileLock, FileLock, Timeout

from agent_wrap.exceptions import LockTimeoutError

if TYPE_CHECKING:
    from collections.abc import Generator
    from pathlib import Path


def _lock(path: Path, timeout: float) -> BaseFileLock:
    """
    Build a lock on *path*, with the two defaults this project cannot take.

    ``fallback_to_soft=False`` because the alternative is a silent downgrade: on a
    filesystem without ``flock`` the default swaps in a ``SoftFileLock``, whose lock is
    the file's *existence*, so a killed process leaves it held forever and every
    liveness answer below turns into a lie. Failing loudly is the only honest response.

    ``thread_local=False`` because :func:`lock_and_hold` hands its lock to a caller that
    need not release it from the acquiring thread, and the kernel lock underneath was
    never thread-scoped.

    ``is_singleton`` stays off (its default), and must: the probes deliberately build a
    *second* lock object for a path this process may already hold, and a shared instance
    would hand back the held one and report its owner's own lock as takeable.
    """
    return FileLock(path, timeout=timeout, thread_local=False, fallback_to_soft=False)


@contextmanager
def file_lock(path: Path, *, timeout: float | None = None) -> Generator[None]:
    """
    Hold an exclusive ``flock`` on *path* for the duration of the block.

    ``timeout=None`` blocks indefinitely; a positive *timeout* gives up after that long
    and raises :class:`LockTimeoutError`.
    """
    lock = _lock(path, -1 if timeout is None else timeout)
    try:
        lock.acquire()
    except Timeout:
        msg = f"timed out waiting for lock {path}"
        raise LockTimeoutError(msg) from None
    try:
        yield
    finally:
        lock.release()


@contextmanager
def try_file_lock(path: Path) -> Generator[bool]:
    """
    Try once to take an exclusive ``flock`` on *path*, without blocking.

    Yields ``True`` if the lock was acquired (and releases it on exit), else ``False``.
    """
    lock = _lock(path, 0)
    try:
        lock.acquire()
    except Timeout:
        yield False
        return
    try:
        yield True
    finally:
        lock.release()


def lock_and_hold(path: Path) -> BaseFileLock | None:
    """
    Take an exclusive ``flock`` on *path* and return the held lock, without blocking.

    The caller must keep the returned lock referenced for as long as it should be held.
    ``None`` when another holder already has it.

    Unlike the context managers, the lock outlives this call: it is released only by
    :func:`clear_lock_handle` or when the owning process exits (the kernel reclaims
    ``flock``s on process death). This is the primitive behind crash-safe,
    PID-recycle-immune liveness — a still-locked file means its owner is alive.
    """
    lock = _lock(path, 0)
    try:
        lock.acquire()
    except Timeout:
        return None
    return lock


def clear_lock_handle(handle: BaseFileLock | None, path: Path) -> None:
    if handle is not None:
        handle.release()
    with contextlib.suppress(OSError):
        path.unlink()


def live_lock_ids(directory: Path) -> list[str]:
    """
    List the names of files in *directory* whose locks are still held, sorted.

    The read-only twin of :func:`any_live_locks`, and the reason it exists: it **never
    unlinks** anything, so a reporting caller cannot reap another process's state as a
    side effect of looking at it.

    A stale file — one whose owner exited, so the lock is takeable — is simply omitted;
    it stays on disk for :func:`any_live_locks` to reap on the next real launch.
    """
    if not directory.is_dir():
        return []
    live: list[str] = []
    for path in sorted(directory.iterdir()):
        if not path.is_file():
            continue
        with try_file_lock(path) as acquired:
            if not acquired:
                live.append(path.name)  # someone holds it — the owner is alive
    return live


def any_live_locks(directory: Path, *, exclude_id: str | None = None) -> bool:
    """
    Walk *directory* of lock-held files, reaping stale entries and reporting liveness.

    Acquiring a file's lock proves its owner exited, so it is unlinked while the lock is
    held. The walk continues past the first live holder to reap siblings in one pass.
    """
    if not directory.is_dir():
        return False
    live = False
    for path in directory.iterdir():
        if not path.is_file():
            continue
        if exclude_id is not None and path.name == exclude_id:
            continue
        with try_file_lock(path) as acquired:
            if acquired:
                with contextlib.suppress(OSError):
                    path.unlink()
            else:
                live = True
    return live
