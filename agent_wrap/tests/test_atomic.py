# This file has been edited with the assistance of an AI tool.
"""Tests for agent_wrap/lib/atomic.py."""

from __future__ import annotations

import json
from pathlib import Path
from threading import Barrier, Thread

import pytest

from agent_wrap.lib import atomic
from agent_wrap.lib.atomic import atomic_write_json, atomic_write_text

# --- atomic_write_text ---


def test_write_text_creates_file(tmp_path: Path) -> None:
    target = tmp_path / "out.txt"
    atomic_write_text(target, "hello")
    assert target.read_text() == "hello"


def test_write_text_overwrites(tmp_path: Path) -> None:
    target = tmp_path / "out.txt"
    target.write_text("old")
    atomic_write_text(target, "new")
    assert target.read_text() == "new"


def test_write_text_creates_parent_dirs(tmp_path: Path) -> None:
    target = tmp_path / "nested" / "deep" / "out.txt"
    atomic_write_text(target, "x")
    assert target.read_text() == "x"


def test_write_text_leaves_no_tmp_file(tmp_path: Path) -> None:
    target = tmp_path / "out.txt"
    atomic_write_text(target, "x")
    assert list(tmp_path.iterdir()) == [target]


# --- atomic_write_json ---


def test_write_json_roundtrip(tmp_path: Path) -> None:
    target = tmp_path / "out.json"
    data = {"a": 1, "b": ["x", "y"]}
    atomic_write_json(target, data)
    assert json.loads(target.read_text()) == data


def test_write_json_trailing_newline(tmp_path: Path) -> None:
    target = tmp_path / "out.json"
    atomic_write_json(target, {"k": "v"})
    assert target.read_text().endswith("}\n")


# --- concurrency / failure handling ---


def test_concurrent_writers_do_not_crash(tmp_path: Path) -> None:
    """
    Many threads writing the same path must not raise.

    Regression for the shared-temp-name race: a fixed ``<path>.tmp`` let one
    writer's ``replace`` consume the temp file out from under another, which then
    raised ``FileNotFoundError``. With unique temp names every writer is independent.
    """
    target = tmp_path / "settings.json"
    n_threads = 12
    iterations = 25
    barrier = Barrier(n_threads)
    errors: list[BaseException] = []

    def worker(worker_id: int) -> None:
        barrier.wait()  # maximize overlap on the replace()
        try:
            for i in range(iterations):
                atomic_write_json(target, {"worker": worker_id, "i": i})
        except BaseException as exc:  # noqa: BLE001 -- record for assertion
            errors.append(exc)

    threads = [Thread(target=worker, args=(w,)) for w in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == []
    # The final file is from some single writer — complete and valid, never truncated.
    data = json.loads(target.read_text())
    assert set(data) == {"worker", "i"}
    # No stray temp files survive.
    assert list(tmp_path.iterdir()) == [target]


def test_write_text_cleans_up_tmp_on_replace_failure(tmp_path: Path, mocker) -> None:
    target = tmp_path / "out.txt"

    def boom(self: Path, _target: Path) -> None:
        raise OSError("replace failed")  # noqa: EM101, TRY003 -- test stub

    mocker.patch.object(atomic.Path, "replace", boom)

    with pytest.raises(OSError, match="replace failed"):
        atomic_write_text(target, "x")

    # No leftover temp file, and the destination was never created.
    assert list(tmp_path.iterdir()) == []
