# This file has been created with the assistance of an AI tool.
"""Data types for the display service."""

from typing import TYPE_CHECKING, Literal, NamedTuple

if TYPE_CHECKING:
    from agent_wrap.domain.display.constants import Style


class RowItem(NamedTuple):
    """A table content row: cells, optional style, and tree-prefix length."""

    cells: list[str]
    style: Style
    prefix_len: int


# ``"__div__"`` marks a horizontal divider in the body list.
RowItemOrDivider = RowItem | Literal["__div__"]


class TableSpec(NamedTuple):
    """
    A table's fixed shape: everything about it that does not vary with its body.

    Travelling as one value is what keeps two invariants the caller used to hold by hand.
    ``leading`` plus the shared column count has to cover every header, so the count is
    derived from this pair rather than passed alongside it; and `table_overflow` has to be
    told the same ``elide`` columns `render_table` will cut, which is now the same field
    read twice instead of two arguments that can drift.
    """

    headers: tuple[str, ...]
    aligns: tuple[str, ...]
    #: Columns measured per-table. The rest are sized across every table in a group, so
    #: their figures line up vertically when two tables are stacked.
    leading: int
    #: Columns that may be cut short, with an ellipsis, when the table will not fit.
    #: Only prose belongs here -- half a date or a token count reads as a wrong figure.
    elide: tuple[int, ...] = ()

    @property
    def n_shared(self) -> int:
        return len(self.headers) - self.leading
