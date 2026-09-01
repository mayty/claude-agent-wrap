# This file has been created with the assistance of an AI tool.
"""Data types for the display service."""

from typing import TYPE_CHECKING, Literal, NamedTuple

if TYPE_CHECKING:
    from agent_wrap.domain.display.constants import Ansi


class RowItem(NamedTuple):
    """A table content row: cells, optional style, and tree-prefix length."""

    cells: list[str]
    style: Ansi
    prefix_len: int


# ``"__div__"`` marks a horizontal divider in the body list.
RowItemOrDivider = RowItem | Literal["__div__"]
