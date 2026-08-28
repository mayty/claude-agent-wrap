# This file has been edited with the assistance of an AI tool.
"""Centralized display output — domain service."""

from __future__ import annotations

import sys
from getpass import getpass
from typing import TYPE_CHECKING, TextIO

from agent_wrap.domain.display.constants import (
    ERROR_PREFIX,
    KIBIBYTE,
    SECONDS_PER_DAY,
    SECONDS_PER_HOUR,
    SECONDS_PER_MINUTE,
    THOUSAND,
    WARNING_PREFIX,
    Ansi,
)
from agent_wrap.domain.display.spinner import Spinner

if TYPE_CHECKING:
    from collections.abc import Callable
    from datetime import datetime

    from agent_wrap.constants import PollResult
    from agent_wrap.domain.display.models import RowItemOrDivider


class _TextStyler:
    """ANSI text styling helpers."""

    @staticmethod
    def color(s: str, code: Ansi, *, stream: TextIO = sys.stdout) -> str:
        """Wrap *s* in *code* / RESET when *stream* is a TTY, else return unchanged."""
        if not stream.isatty():
            return s
        return f"{code}{s}{Ansi.RESET}"

    @staticmethod
    def prefixed(message: str, prefix: str) -> str:
        """
        Tag the first line of *message* with *prefix*, aligning the rest under it.

        Indenting continuation lines by the prefix width is what lets a caller pass one
        multi-line message instead of several calls: the block reads as a single
        diagnostic, and nothing else can interleave between its lines.
        """
        head, _, rest = message.partition("\n")
        if not rest:
            return prefix + head
        pad = " " * len(prefix)
        return prefix + head + "\n" + "\n".join(pad + line for line in rest.split("\n"))


class _TableRenderer:
    """Table layout computation and rendering helpers."""

    @staticmethod
    def widths_for(
        headers: list[str],
        body: list[RowItemOrDivider],
        leading: int,
        shared_widths: list[int],
    ) -> list[int]:
        """Compute column widths for a table."""
        leading_widths = [len(headers[j]) for j in range(leading)]
        for item in body:
            if isinstance(item, str):  # divider sentinel
                continue
            cells = item.cells
            for j in range(leading):
                leading_widths[j] = max(leading_widths[j], len(cells[j]))
        return leading_widths + shared_widths

    @staticmethod
    def render_row(
        cells: list[str],
        aligns: list[str],
        widths: list[int],
        style: Ansi = Ansi.NONE,
        prefix_len: int = 0,
    ) -> str:
        """Render a single table row with alignment and optional styling."""
        parts = [f" {cell:{aligns[i]}{widths[i]}} " for i, cell in enumerate(cells)]
        sep = _TextStyler.color("│", Ansi.DIM)
        if style:
            if prefix_len:
                # Keep tree glyphs at the default colour; style only content after prefix.
                first = parts[0]
                head = first[: 1 + prefix_len]
                tail = first[1 + prefix_len :]
                parts[0] = head + _TextStyler.color(tail, style)
                parts[1:] = [_TextStyler.color(p, style) for p in parts[1:]]
            else:
                parts = [_TextStyler.color(p, style) for p in parts]
        return sep + sep.join(parts) + sep

    @staticmethod
    def make_border(widths: list[int], left: str, mid: str, right: str) -> str:
        """Render a horizontal border line."""
        parts = ["─" * (w + 2) for w in widths]
        return _TextStyler.color(left + mid.join(parts) + right, Ansi.DIM)


class DisplayService:
    """Centralized terminal output: printing, formatting, tables, spinners, and prompts."""

    # ------------------------------------------------------------------
    # Basic output
    # ------------------------------------------------------------------

    def info(self, message: str, *, end: str = "\n", flush: bool = False) -> None:
        """Print *message* to stdout."""
        print(message, end=end, flush=flush)

    def error(self, message: str, *, end: str = "\n", flush: bool = False) -> None:
        """Print *message* to stderr, tagged ``[ERROR]``, with red styling (TTY only)."""
        print(
            _TextStyler.color(
                _TextStyler.prefixed(message, ERROR_PREFIX), Ansi.BOLD_RED, stream=sys.stderr
            ),
            end=end,
            flush=flush,
            file=sys.stderr,
        )

    def warning(self, message: str, *, end: str = "\n", flush: bool = False) -> None:
        """Print *message* to stderr, tagged ``[WARNING]``, with yellow styling (TTY only)."""
        print(
            _TextStyler.color(
                _TextStyler.prefixed(message, WARNING_PREFIX), Ansi.BOLD_YELLOW, stream=sys.stderr
            ),
            end=end,
            flush=flush,
            file=sys.stderr,
        )

    def success(self, message: str, *, end: str = "\n", flush: bool = False) -> None:
        """Print *message* to stdout with green styling (TTY only)."""
        print(_TextStyler.color(message, Ansi.BOLD_GREEN), end=end, flush=flush)

    def banner(self, text: str) -> None:
        """Print a banner line to stdout."""
        self.info(f"--- {text} ---")

    def newline(self) -> None:
        """Print a blank line to stdout."""
        self.info("")

    # ------------------------------------------------------------------
    # Formatting helpers
    # ------------------------------------------------------------------

    def format_count(self, n: int) -> str:
        """Abbreviate large integers: ``1000`` → ``"1.0K"``, ``1_500_000`` → ``"1.50M"``."""
        units = "K", "M", "G"

        if n < THOUSAND:
            return str(n)

        value = float(n)
        for unit in units:
            value /= THOUSAND
            if value < THOUSAND:
                return f"{value:.1f}{unit}"

        return f"{value:.1f}{units[-1]}"

    def format_bytes(self, n: int) -> str:
        """
        Abbreviate a byte count with binary units: ``2048`` → ``"2.0KB"``.

        The binary-stepped sibling of :meth:`format_count`. Sub-kilobyte values
        render exactly (``0`` → ``"0B"``), which is a legitimate result when the
        directories being measured are empty rather than an error.
        """
        units = "KB", "MB", "GB"

        if n < KIBIBYTE:
            return f"{n}B"

        value = float(n)
        for unit in units:
            value /= KIBIBYTE
            if value < KIBIBYTE:
                return f"{value:.1f}{unit}"

        return f"{value:.1f}{units[-1]}"

    def format_duration(self, seconds: float | None) -> str:
        """
        Abbreviate an elapsed duration coarsely: ``11520`` → ``"3h 12m"``, ``None`` → ``"—"``.

        Two units at most, largest first, and never sub-second precision — this reads
        uptimes, where "3h 12m" is the useful answer and "3h 12m 07s" is noise. The
        ``"—"`` for None matches :meth:`format_timestamp`, so a container that never
        started renders the same as a missing timestamp.
        """
        if seconds is None or seconds < 0:
            return "—"
        secs = int(seconds)
        if secs < SECONDS_PER_MINUTE:
            return f"{secs}s"
        if secs < SECONDS_PER_HOUR:
            return f"{secs // SECONDS_PER_MINUTE}m"
        if secs < SECONDS_PER_DAY:
            hours, rem = divmod(secs, SECONDS_PER_HOUR)
            return f"{hours}h {rem // SECONDS_PER_MINUTE}m"
        days, rem = divmod(secs, SECONDS_PER_DAY)
        return f"{days}d {rem // SECONDS_PER_HOUR}h"

    def format_timestamp(self, dt: datetime | None) -> str:
        """Format a datetime as ``YYYY-MM-DD``, or ``"—"`` when *dt* is None."""
        if dt is None:
            return "—"
        return dt.astimezone().strftime("%Y-%m-%d")

    def format_cost(self, c: float | None) -> str:
        """Format a cost value as ``$X.XX``, or ``"?"`` when unknown (None)."""
        if c is None:
            return "?"
        return f"${c:.2f}"

    def format_cost_with_unknown(self, c: float | None, *, unknown: bool) -> str:
        """Format cost with an unknown flag, collapsing ``$0.00+?`` to just ``"?"``."""
        if c is None or (c == 0.0 and unknown):
            return "?"
        if unknown:
            return f"${c:.2f}+?"
        return f"${c:.2f}"

    # ------------------------------------------------------------------
    # Table rendering
    # ------------------------------------------------------------------

    def render_table(  # noqa: PLR0913, PLR0917
        self,
        title: str,
        headers: list[str],
        aligns: list[str],
        body: list[RowItemOrDivider],
        leading: int,
        shared_widths: list[int],
    ) -> list[str]:
        """Render a complete table with Unicode box-drawing borders. Returns lines."""
        widths = _TableRenderer.widths_for(headers, body, leading, shared_widths)
        out: list[str] = [_TextStyler.color(title, Ansi.DIM)]
        out.append(_TableRenderer.make_border(widths, "┌", "┬", "┐"))
        out.append(_TableRenderer.render_row(headers, aligns, widths, Ansi.DIM))
        out.append(_TableRenderer.make_border(widths, "├", "┼", "┤"))
        for item in body:
            if isinstance(item, str):  # divider sentinel
                out.append(_TableRenderer.make_border(widths, "├", "┼", "┤"))
            else:
                out.append(
                    _TableRenderer.render_row(
                        item.cells, aligns, widths, item.style, item.prefix_len
                    )
                )
        out.append(_TableRenderer.make_border(widths, "└", "┴", "┘"))
        return out

    def compute_shared_widths(
        self,
        tables: list[tuple[list[str], list[RowItemOrDivider], int]],
        n_shared: int,
    ) -> list[int]:
        """Compute shared column widths across multiple tables."""
        shared_widths = [0] * n_shared
        for headers, body, leading in tables:
            for j in range(n_shared):
                shared_widths[j] = max(shared_widths[j], len(headers[leading + j]))
            for item in body:
                if isinstance(item, str):
                    continue
                for j in range(n_shared):
                    shared_widths[j] = max(shared_widths[j], len(item.cells[leading + j]))
        return shared_widths

    # ------------------------------------------------------------------
    # Spinner (public API delegates to Spinner collaborator)
    # ------------------------------------------------------------------

    def spin_while(
        self,
        *,
        label: str,
        message: str | Callable[[], str],
        done_message: str | Callable[[], str | None],
        work: Callable[[], object],
    ) -> None:
        """Animate a spinner while running *work* on a background thread."""
        Spinner(label).spin_while(message=message, done_message=done_message, work=work)

    def poll_until(  # noqa: PLR0913
        self,
        *,
        label: str,
        poll: Callable[[], tuple[PollResult, str]],
        message: str,
        done_message: str,
        timeout: float,
        poll_interval: float = 0.5,
    ) -> bool:
        """Animate a spinner, polling *poll* until success, failure, or *timeout*."""
        return Spinner(label).poll_until(
            poll=poll,
            message=message,
            done_message=done_message,
            timeout=timeout,
            poll_interval=poll_interval,
        )

    # ------------------------------------------------------------------
    # Interactive prompts
    # ------------------------------------------------------------------

    def prompt_confirm(self, prompt: str) -> bool:
        """Prompt the user for y/N confirmation. Returns True for ``y`` or ``Y``."""
        self.info(prompt, end=" ")
        try:
            ans = input()
        except (EOFError, KeyboardInterrupt):
            return False
        return ans.strip().lower() == "y"

    def prompt_secret(self, description: str) -> str:
        """Prompt the user for a secret value, echoing input hidden."""
        try:
            return getpass(f"Enter {description}: ")
        except EOFError as exc:
            self.error("secret input interrupted")
            raise SystemExit(1) from exc
