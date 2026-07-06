# This file has been created with the assistance of an AI tool.
"""Data types for the display service."""

from __future__ import annotations

from enum import Enum
from typing import Literal, NamedTuple


class Ansi(str, Enum):
    """Terminal control sequences."""

    # Empty sentinel for "no styling" — falsy, so `if style:` guards skip it.
    NONE = ""
    RESET = "\033[0m"
    BOLD_GREEN = "\033[1;32m"
    BOLD_YELLOW = "\033[1;33m"
    BOLD_RED = "\033[1;31m"
    CYAN = "\033[36m"
    DIM = "\033[90m"

    # Cursor/line control (not SGR — don't append 'm')
    CR = "\r"
    ERASE_LINE = "\033[2K"

    def __str__(self) -> str:
        return self.value


class RowItem(NamedTuple):
    """A table content row: cells, optional style, and tree-prefix length."""

    cells: list[str]
    style: Ansi
    prefix_len: int


# ``"__div__"`` marks a horizontal divider in the body list.
RowItemOrDivider = RowItem | Literal["__div__"]
