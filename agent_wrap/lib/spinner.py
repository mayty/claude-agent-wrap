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

    FRAMES = ("⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏")

    def __init__(self, label: str, *, fps: float = 12.0) -> None:
        self.label = label
        self._render_interval = 1.0 / fps

    def _frame(self, n: int, message: str) -> str:
        """In-place redraw of one spinner frame (caller prints with end='')."""
        glyph = self.FRAMES[n % len(self.FRAMES)]
        return f"{Ansi.CR}{Ansi.ERASE_LINE}{self.label}: {glyph} {message}"

    def _final(self, message: str) -> str:
        """Clear the line and render the final text (caller prints normally)."""
        return f"{Ansi.CR}{Ansi.ERASE_LINE}{self.label}: {message}"

    def spin_while(
        self,
        *,
        message: str | Callable[[], str],
        done_message: str | Callable[[], str | None],
        work: Callable[[], object],
    ) -> None:
        """
        Run *work* on a background thread while animating a TTY spinner.

        *message* may be a callable, re-evaluated each frame, so the line can
        reflect state *work* mutates. *done_message* is evaluated once after the
        thread ends; ``None`` means "no finalize text, just end the line".

        TTY: animates "<label>: <frame> {message} (Ns)" at the spinner's
        configured cadence, then clears the line and prints
        "<label>: {done_message} (Ns)" (or a bare newline when it is ``None``).
        Non-TTY: prints a single plain "<label>: {message}" line and nothing else.
        *work* always runs to completion either way; the worker thread is joined
        before return.
        """
        msg_fn = (lambda: message) if isinstance(message, str) else message
        done_fn = (lambda: done_message) if isinstance(done_message, str) else done_message

        if not sys.stderr.isatty():
            print(f"{self.label}: {msg_fn()}", file=sys.stderr)
            work()
            return
        start = time.monotonic()
        thread = threading.Thread(target=work)
        thread.start()
        n = 0
        while thread.is_alive():
            elapsed = int(time.monotonic() - start)
            print(self._frame(n, f"{msg_fn()} ({elapsed}s)"), end="", file=sys.stderr)
            n += 1
            thread.join(timeout=self._render_interval)
        elapsed = int(time.monotonic() - start)
        final = done_fn()
        if final is None:
            print(file=sys.stderr)
        else:
            print(self._final(f"{final} ({elapsed}s)"), file=sys.stderr)

    def poll_until(
        self,
        *,
        poll: Callable[[], tuple[PollResult, str]],
        message: str,
        done_message: str,
        timeout: float,
        poll_interval: float = 0.5,
    ) -> bool:
        """
        Poll until *poll* reports SUCCESS/FAILURE or *timeout* seconds elapse.

        *poll* returns (verdict, status_text). On a TTY the animation runs at the
        spinner's render cadence while *poll* is called on its own thread every
        *poll_interval* seconds, so the underlying check (e.g. a docker inspect) is
        not run once per frame: animates "<label>: <frame> {message} [{status}]
        (Ns)"; on SUCCESS clears the line and prints "<label>: {done_message} (Ns)"
        and returns True; on FAILURE / timeout prints a newline and returns False.
        Non-TTY: no animation — polls every *poll_interval*, printing
        "<label>: {status}" only when the status changes, and nothing on finalize.
        """
        deadline = time.monotonic() + timeout
        if not sys.stderr.isatty():
            return self._poll_quiet(poll, deadline, poll_interval)

        # Poll on a worker thread (paced by poll_interval) while spin_while
        # animates at the render cadence, reading the latest status it publishes.
        state = {"status": "", "ok": False}

        def work() -> None:
            while time.monotonic() < deadline:
                verdict, status = poll()
                state["status"] = status
                if verdict is PollResult.SUCCESS:
                    state["ok"] = True
                    return
                if verdict is PollResult.FAILURE:
                    return
                time.sleep(poll_interval)

        self.spin_while(
            message=lambda: f"{message} [{state['status'] or '?'}]",
            done_message=lambda: done_message if state["ok"] else None,
            work=work,
        )
        return bool(state["ok"])

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
                print(f"{self.label}: {status}", file=sys.stderr)
                last_status = status
            if verdict is PollResult.SUCCESS:
                return True
            if verdict is PollResult.FAILURE:
                return False
            time.sleep(poll_interval)
        return False
