# This file has been created with the assistance of an AI tool.
"""Spinner collaborator for DisplayService."""

import random
import sys
import time
from concurrent.futures import ThreadPoolExecutor, wait
from typing import TYPE_CHECKING

from rich.live import Live
from rich.spinner import Spinner as RichSpinner
from rich.text import Text

from agent_wrap.constants import PollResult
from agent_wrap.domain.display.console import err
from agent_wrap.domain.display.constants import (
    DEFAULT_RICH_SPINNER,
    MESSAGE_UPDATE_INTERVAL_SEC,
    MILLISECONDS_PER_SECOND,
    SPINNER_CYCLE_SEC,
    SPINNER_REFRESH_PER_SEC,
    SPINNERS,
)

if TYPE_CHECKING:
    from collections.abc import Callable


class Spinner:
    def __init__(self, label: str) -> None:
        self._label = label

    def _final(self, message: str) -> str:
        return f"{self._label}: {message}"

    def _choose_spinner(self, text: str) -> RichSpinner:
        """
        Pick one of the project's frame sets, as a rich spinner showing *text*.

        rich ships a catalogue of its own but not these sets, so the frames and their
        cadence are assigned over whichever entry was used to construct it. A set that
        declares no duration gets the default one.
        """
        frames, duration = random.choice(list(SPINNERS.values()))  # noqa: S311
        spinner = RichSpinner(DEFAULT_RICH_SPINNER, Text(text))
        spinner.frames = list(frames)
        spinner.interval = (duration or SPINNER_CYCLE_SEC) / len(frames) * MILLISECONDS_PER_SECOND
        return spinner

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

        start = time.monotonic()
        spinner = self._choose_spinner(f"{self._label}: {msg_fn()} (0s)")

        # The future is the typed carrier for the result and for anything *work* raises,
        # neither of which a bare thread can hand back. Live drives the frames on its own
        # refresh thread, so this loop only restates the message -- and `transient` takes
        # the whole spinner line back off the terminal when the block ends, which is what
        # lets the final line, or a traceback, start on a clean one.
        with (
            Live(
                spinner, console=err(), refresh_per_second=SPINNER_REFRESH_PER_SEC, transient=True
            ),
            ThreadPoolExecutor(max_workers=1) as pool,
        ):
            future = pool.submit(work)
            while not future.done():
                elapsed = int(time.monotonic() - start)
                spinner.update(text=Text(f"{self._label}: {msg_fn()} ({elapsed}s)"))
                wait([future], timeout=MESSAGE_UPDATE_INTERVAL_SEC)

        error = future.exception()
        if error is not None:
            raise error

        result = future.result()
        elapsed = int(time.monotonic() - start)
        final = self._done_line(done_message, result)
        if final is not None:
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
