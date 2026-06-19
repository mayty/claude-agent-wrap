# This file has been created with the assistance of an AI tool.
"""A label-prefixed, animated stderr spinner for long-running operations."""

from __future__ import annotations

import sys
import threading
import time
from enum import Enum, auto
from typing import TYPE_CHECKING

from agent_wrap.lib.console import Ansi

if TYPE_CHECKING:
    from collections.abc import Callable


class PollResult(Enum):
    """Verdict a ``poll`` callback returns each tick to ``Spinner.poll_until``."""

    PENDING = auto()
    SUCCESS = auto()
    FAILURE = auto()


class Spinner:
    """Animated, label-prefixed stderr spinner for long-running operations."""

    FRAMES = ("|", "/", "-", "\\")

    def __init__(self, label: str, *, fps: float = 2.0) -> None:
        self.label = label
        self._interval = 1.0 / fps

    def _frame(self, n: int, message: str) -> str:
        """In-place redraw of one spinner frame (caller prints with end='')."""
        glyph = self.FRAMES[n % len(self.FRAMES)]
        return f"{Ansi.CR}{Ansi.ERASE_LINE}{self.label}: {glyph} {message}"

    def _final(self, message: str) -> str:
        """Clear the line and render the final text (caller prints normally)."""
        return f"{Ansi.CR}{Ansi.ERASE_LINE}{self.label}: {message}"

    def spin_while(self, *, message: str, done_message: str, work: Callable[[], object]) -> None:
        """
        Run *work* on a background thread while animating a TTY spinner.

        TTY: animates "<label>: <frame> {message} (Ns)" at the spinner's
        configured cadence, then clears the line and prints
        "<label>: {done_message} (Ns)". Non-TTY: prints a single plain
        "<label>: {message}" line and nothing else. *work* always runs to
        completion either way; the worker thread is joined before return.
        """
        if not sys.stderr.isatty():
            print(f"{self.label}: {message}", file=sys.stderr)
            work()
            return
        start = time.monotonic()
        thread = threading.Thread(target=work)
        thread.start()
        n = 0
        while thread.is_alive():
            elapsed = int(time.monotonic() - start)
            print(self._frame(n, f"{message} ({elapsed}s)"), end="", file=sys.stderr)
            n += 1
            thread.join(timeout=self._interval)
        elapsed = int(time.monotonic() - start)
        print(self._final(f"{done_message} ({elapsed}s)"), file=sys.stderr)

    def poll_until(
        self,
        *,
        poll: Callable[[], tuple[PollResult, str]],
        message: str,
        done_message: str,
        timeout: float,
    ) -> bool:
        """
        Poll until *poll* reports SUCCESS/FAILURE or *timeout* seconds elapse.

        *poll* returns (verdict, status_text) each tick. TTY: animates
        "<label>: <frame> {message} [{status}] (Ns)" at the spinner's configured
        cadence; on SUCCESS clears the line and prints "<label>: {done_message}
        (Ns)" and returns True; on FAILURE / timeout prints a newline and returns
        False. Non-TTY: prints "<label>: {status}" only when the status changes,
        and nothing on finalize.
        """
        is_tty = sys.stderr.isatty()
        start = time.monotonic()
        deadline = start + timeout
        frame = 0
        last_status = ""
        while time.monotonic() < deadline:
            verdict, status = poll()
            elapsed = int(time.monotonic() - start)
            if is_tty:
                print(
                    self._frame(frame, f"{message} [{status or '?'}] ({elapsed}s)"),
                    end="",
                    file=sys.stderr,
                )
                frame += 1
            elif status and status != last_status:
                print(f"{self.label}: {status}", file=sys.stderr)
                last_status = status
            if verdict is PollResult.SUCCESS:
                if is_tty:
                    print(self._final(f"{done_message} ({elapsed}s)"), file=sys.stderr)
                return True
            if verdict is PollResult.FAILURE:
                if is_tty:
                    print(file=sys.stderr)
                return False
            time.sleep(self._interval)
        if is_tty:
            print(file=sys.stderr)
        return False
