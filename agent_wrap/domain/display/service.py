# This file has been edited with the assistance of an AI tool.
"""Centralized display output — domain service."""

import os
import shutil
import sys
from getpass import getpass
from typing import TYPE_CHECKING, TextIO

from agent_wrap.domain.display.constants import (
    DEFAULT_TERM_WIDTH,
    ERROR_PREFIX,
    KIBIBYTE,
    SECONDS_PER_DAY,
    SECONDS_PER_HOUR,
    SECONDS_PER_MINUTE,
    TERM_WIDTH_ENV,
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
    def table_width(widths: list[int]) -> int:
        """Return a table's rendered width: each cell padded a space either side, plus borders."""
        return sum(w + 2 for w in widths) + len(widths) + 1

    @staticmethod
    def elide_cell(text: str, width: int) -> str:
        """
        Cut *text* to *width*, marking that it was cut.

        The last resort, reached only once chopping the tree has run out: losing the tail of
        a sentence beats a row that runs past the border, and beats a row broken over three
        lines. Nothing numeric is ever passed here -- `render_table` elides only the columns
        its caller nominates, and half a figure is worse than no table.
        """
        if len(text) <= width:
            return text
        return text[: width - 1].rstrip() + "…"

    @staticmethod
    def squeeze(
        widths: list[int], elide: tuple[int, ...], headers: list[str], limit: int
    ) -> list[int]:
        """
        Take a table's overflow out of its *elide* columns, widest first, down to their headers.

        One character at a time from whichever elidable column is currently widest, so two
        wide columns give up roughly equal shares instead of the first absorbing everything.
        A column stops at its own header, which has to stay readable; past that the table
        simply stays wider than the terminal, since the columns that are left are the ones
        the caller said must not be cut.
        """
        out = list(widths)
        overflow = _TableRenderer.table_width(out) - limit
        while overflow > 0:
            shrinkable = [col for col in elide if out[col] > len(headers[col])]
            if not shrinkable:
                break
            # Keyed on the list itself: `shrinkable` holds indices, not widths.
            out[max(shrinkable, key=out.__getitem__)] -= 1
            overflow -= 1
        return out

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

    def alert(self, message: str, *, end: str = "\n", flush: bool = False) -> None:
        """
        Print *message* to stderr, tagged ``[WARNING]``, with red styling (TTY only).

        The loud warning: a caution the reader is expected to act on rather than note in
        passing, which is why it takes `error`'s colour and `warning`'s tag. A plain
        `error` would be wrong for one — it precedes a prompt the reader may well answer
        yes to, so nothing has failed yet — and `warning`'s yellow is too quiet for it.
        """
        print(
            _TextStyler.color(
                _TextStyler.prefixed(message, WARNING_PREFIX), Ansi.BOLD_RED, stream=sys.stderr
            ),
            end=end,
            flush=flush,
            file=sys.stderr,
        )

    def success(self, message: str, *, end: str = "\n", flush: bool = False) -> None:
        """Print *message* to stdout with green styling (TTY only)."""
        print(_TextStyler.color(message, Ansi.BOLD_GREEN), end=end, flush=flush)

    def banner(self, text: str) -> None:
        """
        Print a banner line to stdout, marked ``>`` and styled purple (TTY only).

        The marker sits inside the colour span but is plain text, so a redirect or a pipe
        keeps it once colour is stripped — the same division of labour as `error`'s tag.
        """
        self.info(_TextStyler.color(f"> {text}", Ansi.MAGENTA, stream=sys.stdout))

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
        elide: tuple[int, ...] = (),
    ) -> list[str]:
        """
        Render a complete table with Unicode box-drawing borders. Returns lines.

        *elide* names the columns that may be cut short, with an ellipsis, when the table
        would not fit the terminal. It is the last resort: a caller with a shrinkable path
        tree should chop that first (see `table_overflow`), because a chopped tree loses
        nothing and a cut cell does. Only prose belongs here -- a truncated date or token
        count reads as a wrong figure rather than a shortened one, so a caller with nothing
        safe to cut nominates nothing and lets the table overflow instead.

        With no *elide* column, or no terminal width to respect, every column is sized to
        its content exactly as before.
        """
        widths = _TableRenderer.widths_for(headers, body, leading, shared_widths)
        limit = self.terminal_width()
        if elide and limit is not None:
            widths = _TableRenderer.squeeze(widths, elide, headers, limit)
        out: list[str] = [_TextStyler.color(title, Ansi.DIM)]
        out.append(_TableRenderer.make_border(widths, "┌", "┬", "┐"))
        out.append(_TableRenderer.render_row(headers, aligns, widths, Ansi.DIM))
        out.append(_TableRenderer.make_border(widths, "├", "┼", "┤"))
        for item in body:
            if isinstance(item, str):  # divider sentinel
                out.append(_TableRenderer.make_border(widths, "├", "┼", "┤"))
            else:
                # Only a nominated column is ever narrower than its content, so only those
                # can need cutting -- and `render_row`'s padding does not truncate, so the
                # cut has to happen before it.
                cells = [
                    _TableRenderer.elide_cell(cell, widths[i]) if i in elide else cell
                    for i, cell in enumerate(item.cells)
                ]
                out.append(
                    _TableRenderer.render_row(cells, aligns, widths, item.style, item.prefix_len)
                )
        out.append(_TableRenderer.make_border(widths, "└", "┴", "┘"))
        return out

    def terminal_width(self) -> int | None:
        """
        Columns available for output, or ``None`` when there is no limit worth respecting.

        `TERM_WIDTH_ENV` wins outright, which is the one lever a script or a test has to
        state a width nothing can be asked for. Failing that, a non-TTY stdout has no width
        at all: piped and captured output must not depend on whichever terminal happened to
        launch it, the same reasoning `_TextStyler.color` uses to drop styling there.
        """
        override = os.environ.get(TERM_WIDTH_ENV, "").strip()
        if override.isdigit():
            return int(override) or None
        if not sys.stdout.isatty():
            return None
        return shutil.get_terminal_size(fallback=(DEFAULT_TERM_WIDTH, 24)).columns

    def table_overflow(
        self,
        headers: list[str],
        body: list[RowItemOrDivider],
        leading: int,
        shared_widths: list[int],
        elide: tuple[int, ...] = (),
    ) -> int:
        """
        Characters this table overruns the terminal by that eliding cannot absorb.

        The signal a caller's chop loop runs on, so *elide* must name the same columns the
        matching `render_table` call does. Those columns are measured at their floor rather
        than their content, which makes this "how far over budget are the columns you refuse
        to cut" -- and that is the only question chopping the tree can answer. Reporting the
        raw overflow instead would chop a tree that was never the problem: a 121-character
        reason cannot fit any normal console, so the loop would run to exhaustion every time
        and spend a five-row ladder per project to win the reason a few more characters.

        With no *elide* column the two readings coincide, which is what a caller with
        nothing safe to cut wants: for it, any overflow is the tree's to absorb or to live
        with.

        0 when there is no width to respect, so a fit loop simply never runs off a non-TTY
        and that output stays what it always was.
        """
        limit = self.terminal_width()
        if limit is None:
            return 0
        widths = _TableRenderer.widths_for(headers, body, leading, shared_widths)
        floored = [len(headers[i]) if i in elide else w for i, w in enumerate(widths)]
        return max(0, _TableRenderer.table_width(floored) - limit)

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
        except EOFError, KeyboardInterrupt:
            return False
        return ans.strip().lower() == "y"

    def prompt_secret(self, description: str) -> str:
        """Prompt the user for a secret value, echoing input hidden."""
        try:
            return getpass(f"Enter {description}: ")
        except EOFError as exc:
            self.error("secret input interrupted")
            raise SystemExit(1) from exc
