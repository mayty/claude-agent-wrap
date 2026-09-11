# This file has been created with the assistance of an AI tool.
"""Spinner collaborator for DisplayService."""

import random
import sys
import time
from concurrent.futures import ThreadPoolExecutor, wait
from typing import TYPE_CHECKING

from agent_wrap.constants import PollResult
from agent_wrap.domain.display.constants import SPINNERS, Ansi

if TYPE_CHECKING:
    from collections.abc import Callable


class Spinner:
    def __init__(self, label: str) -> None:
        self._label = label

    def _frame(self, frames: tuple[str, ...], n: int, message: str) -> str:
        glyph = frames[n % len(frames)]
        return f"{Ansi.CR}{Ansi.ERASE_LINE}{self._label}: {glyph} {message}"

    def _final(self, message: str) -> str:
        return f"{Ansi.CR}{Ansi.ERASE_LINE}{self._label}: {message}"

    def _choose_spinner(self) -> tuple[tuple[str, ...], float]:
        frames, duration = random.choice(list(SPINNERS.values()))  # noqa: S311
        duration = duration or 1.0
        sleep_time = duration / len(frames)
        return frames, sleep_time

    def _done_line[T](
        self, done_message: str | Callable[[T], str | None] | None, result: T
    ) -> str | None:
        """Collapse the three ``done_message`` forms into a final line, None meaning none."""
        if done_message is None or isinstance(done_message, str):
            return done_message
        return done_message(result)

    def spin_while[T](
        self,
        *,
        message: str | Callable[[], str],
        work: Callable[[], T],
        done_message: str | Callable[[T], str | None] | None = None,
    ) -> T:
        """
        Run *work* on a background thread while animating a TTY spinner, returning its result.

        *done_message* is the line the spinner settles on: a literal string, a callable
        handed whatever *work* returned, or None (the default) for no final line at all.
        An exception from *work* propagates on both the TTY and the non-TTY path.
        """
        msg_fn = (lambda: message) if isinstance(message, str) else message

        if not sys.stderr.isatty():
            print(f"{self._label}: {msg_fn()}", file=sys.stderr)
            return work()

        frames, render_interval = self._choose_spinner()

        start = time.monotonic()
        # The future is the typed carrier for the result and for anything *work* raises,
        # neither of which a bare thread can hand back.
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(work)
            n = 0
            while not future.done():
                elapsed = int(time.monotonic() - start)
                print(
                    self._frame(frames, n, f"{msg_fn()} ({elapsed}s)"),
                    end="",
                    file=sys.stderr,
                )
                n += 1
                wait([future], timeout=render_interval)

        error = future.exception()
        if error is not None:
            # Close the spinner line first: the traceback must not land mid-frame.
            print(file=sys.stderr)
            raise error

        result = future.result()
        elapsed = int(time.monotonic() - start)
        final = self._done_line(done_message, result)
        if final is None:
            print(file=sys.stderr)
        else:
            print(self._final(f"{final} ({elapsed}s)"), file=sys.stderr)
        return result

    def poll_until(
        self,
        *,
        poll: Callable[[], tuple[PollResult, str]],
        message: str,
        done_message: str,
        timeout: float,
        poll_interval: float = 0.5,
    ) -> bool:
        deadline = time.monotonic() + timeout
        if not sys.stderr.isatty():
            return self._poll_quiet(poll, deadline, poll_interval)

        # *status* stays a closure variable because *message* reads it while the spinner
        # runs; only the verdict can travel back as a return value.
        status = ""

        def work() -> bool:
            nonlocal status
            while time.monotonic() < deadline:
                verdict, poll_status = poll()
                status = poll_status
                if verdict is PollResult.SUCCESS:
                    return True
                if verdict is PollResult.FAILURE:
                    return False
                time.sleep(poll_interval)
            return False

        return self.spin_while(
            message=lambda: f"{message} [{status or '?'}]",
            done_message=lambda ok: done_message if ok else None,
            work=work,
        )

    def _poll_quiet(
        self,
        poll: Callable[[], tuple[PollResult, str]],
        deadline: float,
        poll_interval: float,
    ) -> bool:
        """Non-TTY: poll each tick, printing the status only when it changes."""
        last_status = ""
        while time.monotonic() < deadline:
            verdict, status = poll()
            if status and status != last_status:
                print(f"{self._label}: {status}", file=sys.stderr)
                last_status = status
            if verdict is PollResult.SUCCESS:
                return True
            if verdict is PollResult.FAILURE:
                return False
            time.sleep(poll_interval)
        return False
