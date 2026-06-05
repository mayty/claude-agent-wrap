# This file has been created with the assistance of an AI tool.
"""Bordered-table rendering helpers shared by the usage-stats subcommands."""

from __future__ import annotations

from agent_wrap.lib.console import Ansi
from agent_wrap.lib.format import color


def widths_for(
    headers: list[str], body: list, leading: int, shared_widths: list[int], div: str
) -> list[int]:
    """Compute column widths for a table."""
    leading_widths = [len(headers[j]) for j in range(leading)]
    for item in body:
        if item == div:
            continue
        cells, _, _ = item
        for j in range(leading):
            leading_widths[j] = max(leading_widths[j], len(cells[j]))
    return leading_widths + shared_widths


def render_row(
    cells: list[str],
    aligns: list[str],
    widths: list[int],
    style: Ansi = Ansi.NONE,
    prefix_len: int = 0,
) -> str:
    """Render a single table row with alignment and optional styling."""
    parts = [f" {cell:{aligns[i]}{widths[i]}} " for i, cell in enumerate(cells)]
    if style:
        if prefix_len:
            # Keep tree glyphs (`├`, `└`, `│`) at the row's default color;
            # only style the content after the prefix.
            first = parts[0]
            # the cell starts after the leading space
            head = first[: 1 + prefix_len]
            tail = first[1 + prefix_len :]
            parts[0] = head + color(tail, style)
            parts[1:] = [color(p, style) for p in parts[1:]]
        else:
            parts = [color(p, style) for p in parts]
    sep = color("│", Ansi.DIM)
    return sep + sep.join(parts) + sep


def make_border(widths: list[int], left: str, mid: str, right: str) -> str:
    """Render a horizontal border line."""
    parts = ["─" * (w + 2) for w in widths]
    return color(left + mid.join(parts) + right, Ansi.DIM)


def render_table(  # noqa: PLR0913
    title: str,
    headers: list[str],
    aligns: list[str],
    body: list,
    leading: int,
    shared_widths: list[int],
    div: str,
) -> list[str]:
    """Render a complete table with borders."""
    widths = widths_for(headers, body, leading, shared_widths, div)
    out = [color(title, Ansi.DIM)]
    out.append(make_border(widths, "┌", "┬", "┐"))
    out.append(render_row(headers, aligns, widths, Ansi.DIM))
    out.append(make_border(widths, "├", "┼", "┤"))
    for item in body:
        if item == div:
            out.append(make_border(widths, "├", "┼", "┤"))
        else:
            cells, style, prefix_len = item
            out.append(render_row(cells, aligns, widths, style, prefix_len))
    out.append(make_border(widths, "└", "┴", "┘"))
    return out


def compute_shared_widths(
    tables: list[tuple[list[str], list, int]],
    n_shared: int,
    div: str,
) -> list[int]:
    """Compute shared column widths across multiple tables."""
    shared_widths = [0] * n_shared
    for headers, body, leading in tables:
        for j in range(n_shared):
            shared_widths[j] = max(shared_widths[j], len(headers[leading + j]))
        for item in body:
            if item == div:
                continue
            cells, _, _ = item
            for j in range(n_shared):
                shared_widths[j] = max(shared_widths[j], len(cells[leading + j]))
    return shared_widths
