# This file has been created with the assistance of an AI tool.
"""Spinner collaborator for DisplayService."""

from __future__ import annotations

import random
import sys
import threading
import time
from typing import TYPE_CHECKING

from agent_wrap.constants import PollResult
from agent_wrap.domain.display.constants import SPINNERS, Ansi

if TYPE_CHECKING:
    from collections.abc import Callable


class Spinner:
    """Animated, label-prefixed stderr spinner."""

    def __init__(self, label: str) -> None:
        self._label = label

    # -- internal helpers --

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

    # -- public --

    def spin_while(
        self,
        *,
        message: str | Callable[[], str],
        done_message: str | Callable[[], str | None],
        work: Callable[[], object],
    ) -> None:
        """Run *work* on a background thread while animating a TTY spinner."""
        msg_fn = (lambda: message) if isinstance(message, str) else message
        done_fn = (lambda: done_message) if isinstance(done_message, str) else done_message

        if not sys.stderr.isatty():
            print(f"{self._label}: {msg_fn()}", file=sys.stderr)
            work()
            return

        frames, render_interval = self._choose_spinner()

        start = time.monotonic()
        thread = threading.Thread(target=work)
        thread.start()
        n = 0
        while thread.is_alive():
            elapsed = int(time.monotonic() - start)
            print(
                self._frame(frames, n, f"{msg_fn()} ({elapsed}s)"),
                end="",
                file=sys.stderr,
            )
            n += 1
            thread.join(timeout=render_interval)
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
        """Poll until *poll* reports SUCCESS/FAILURE or *timeout* seconds elapse."""
        deadline = time.monotonic() + timeout
        if not sys.stderr.isatty():
            return self._poll_quiet(poll, deadline, poll_interval)

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
                print(f"{self._label}: {status}", file=sys.stderr)
                last_status = status
            if verdict is PollResult.SUCCESS:
                return True
            if verdict is PollResult.FAILURE:
                return False
            time.sleep(poll_interval)
        return False
