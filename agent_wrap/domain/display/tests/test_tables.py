# This file has been created with the assistance of an AI tool.
"""
Domain-layer tests for table layout: terminal width, column shrinking, and wrapping.

Every assertion goes through the public ``DisplayService`` surface. Stdout is captured
rather than a TTY here, so ``COLUMNS`` is what states a width -- which is exactly the lever
these tables give a script, and why no test needs to pretend to be a terminal.
"""

import sys
from typing import TYPE_CHECKING

import pytest

from agent_wrap.domain.display.constants import DEFAULT_TERM_WIDTH, Ansi
from agent_wrap.domain.display.models import RowItem, TableSpec
from agent_wrap.domain.display.service import DisplayService

if TYPE_CHECKING:
    from agent_wrap.domain.display.models import RowItemOrDivider

HEADERS = ("PROJECT", "COUNT")
ALIGNS = ("<", ">")


@pytest.fixture
def display() -> DisplayService:
    """Return a real DisplayService — table layout is pure computation over its args."""
    return DisplayService()


@pytest.fixture(autouse=True)
def _no_inherited_width(monkeypatch: pytest.MonkeyPatch) -> None:
    """Drop any COLUMNS the test runner inherited, so each test states its own width."""
    monkeypatch.delenv("COLUMNS", raising=False)


def _row(label: str, prefix_len: int) -> RowItem:
    return RowItem(cells=[label, "1"], style=Ansi.NONE, prefix_len=prefix_len)


def _tree_body() -> list[RowItemOrDivider]:
    """Return a body shaped like a project tree, with one wide folded node in the middle."""
    return [
        RowItem(cells=["/", "4"], style=Ansi.NONE, prefix_len=0),
        _row("├home/me/work/wargaming/", 1),
        _row(" ├wotp", 2),
        _row(" └wotp-be", 2),
        _row("└srv/deploy", 1),
    ]


def _table(elide: tuple[int, ...] = ()) -> TableSpec:
    return TableSpec(headers=HEADERS, aligns=ALIGNS, leading=1, elide=elide)


def _render(
    display: DisplayService, body: list[RowItemOrDivider], elide: tuple[int, ...] = ()
) -> list[str]:
    # COUNT's width is stated rather than measured, so these assertions stay pinned to the
    # header floor they were written against.
    return display.render_table("T:", _table(elide), body, [5])


def _project_cells(lines: list[str]) -> list[str]:
    """Return the PROJECT cell of every line, borders excluded and padding stripped."""
    # Split from the right: tree glyphs reuse the column separator's own glyph.
    return [line[1:].rsplit("│", 2)[0].strip() for line in lines[4:-1]]


def test_terminal_width_is_unlimited_when_stdout_is_not_a_terminal(
    display: DisplayService,
) -> None:
    """Piped output must not depend on whichever terminal happened to launch it."""
    assert display.terminal_width() is None


def test_terminal_width_reads_the_columns_override(
    display: DisplayService, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("COLUMNS", "72")
    assert display.terminal_width() == 72


def test_terminal_width_ignores_a_non_numeric_columns(
    display: DisplayService, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("COLUMNS", "wide")
    assert display.terminal_width() is None


def test_terminal_width_treats_a_zero_columns_as_unlimited(
    display: DisplayService, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A width of zero is no width at all, not a table squeezed to nothing."""
    monkeypatch.setenv("COLUMNS", "0")
    assert display.terminal_width() is None


def test_terminal_width_falls_back_when_a_terminal_will_not_say(
    display: DisplayService, monkeypatch: pytest.MonkeyPatch
) -> None:
    class _Tty:
        @staticmethod
        def isatty() -> bool:
            return True

    monkeypatch.setattr(sys, "stdout", _Tty())
    monkeypatch.setattr(
        "shutil.get_terminal_size",
        lambda fallback=(80, 24): type("S", (), {"columns": fallback[0]}),
    )
    assert display.terminal_width() == DEFAULT_TERM_WIDTH


def test_table_overflow_is_zero_when_there_is_no_width_to_respect(
    display: DisplayService,
) -> None:
    assert display.table_overflow(_table(), _tree_body(), [5]) == 0


def test_table_overflow_reports_the_excess(
    display: DisplayService, monkeypatch: pytest.MonkeyPatch
) -> None:
    """PROJECT measures 24, COUNT 5: 26 + 7 columns of cells plus 3 borders."""
    monkeypatch.setenv("COLUMNS", "30")
    assert display.table_overflow(_table(), _tree_body(), [5]) == 6


def test_table_overflow_is_zero_when_the_table_fits(
    display: DisplayService, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("COLUMNS", "200")
    assert display.table_overflow(_table(), _tree_body(), [5]) == 0


def test_table_overflow_discounts_what_an_elidable_column_could_give_up(
    display: DisplayService, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    Told REASON may be cut, the overflow the tree has to answer for drops to nothing.

    This is what stops a chop loop flattening a tree that was never the problem: 18 columns
    over, every one of which the prose column can surrender on its own.
    """
    monkeypatch.setenv("COLUMNS", "40")
    spec = TableSpec(headers=("PROJECT", "REASON"), aligns=("<", "<"), leading=1)
    body: list[RowItemOrDivider] = [
        RowItem(
            cells=["├home/me/work/wargaming/", "a fairly long reason string"],
            style=Ansi.NONE,
            prefix_len=1,
        )
    ]
    assert display.table_overflow(spec, body, [27]) == 18
    assert display.table_overflow(spec._replace(elide=(1,)), body, [27]) == 0


def test_render_table_leaves_a_table_with_no_elidable_column_alone(
    display: DisplayService, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Nothing was nominated as safe to cut, so the table overflows rather than lie."""
    monkeypatch.setenv("COLUMNS", "30")
    lines = _render(display, _tree_body())
    assert max(len(line) for line in lines) == 36
    assert not any("…" in line for line in lines)


def test_render_table_ignores_an_elidable_column_when_the_table_already_fits(
    display: DisplayService, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("COLUMNS", "200")
    assert _render(display, _tree_body(), elide=(0,)) == _render(display, _tree_body())


def test_render_table_cuts_an_elidable_column_to_the_terminal(
    display: DisplayService, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("COLUMNS", "30")
    lines = _render(display, _tree_body(), elide=(0,))
    assert {len(line) for line in lines[1:]} == {30}
    assert _project_cells(lines)[1] == "├home/me/work/war…"


def test_render_table_never_cuts_a_column_that_was_not_nominated(
    display: DisplayService, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The whole point of the parameter: a tree column keeps every character it had."""
    monkeypatch.setenv("COLUMNS", "30")
    lines = _render(display, _tree_body(), elide=(1,))
    # Wider than the console, and deliberately so: COUNT holds a figure and PROJECT a path.
    assert {len(line) for line in lines[1:]} == {36}
    assert _project_cells(lines) == [
        "/",
        "├home/me/work/wargaming/",
        "├wotp",
        "└wotp-be",
        "└srv/deploy",
    ]


def test_render_table_spreads_the_cut_across_several_elidable_columns(
    display: DisplayService, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One character at a time off whichever is widest, so neither absorbs it all."""
    monkeypatch.setenv("COLUMNS", "40")
    body: list[RowItemOrDivider] = [
        RowItem(
            cells=["a-fairly-long-project", "an-even-longer-image-name"],
            style=Ansi.NONE,
            prefix_len=0,
        )
    ]
    spec = TableSpec(headers=("PROJECT", "IMAGE"), aligns=("<", "<"), leading=1, elide=(0, 1))
    lines = display.render_table("T:", spec, body, [25])
    assert {len(line) for line in lines[1:]} == {40}
    # 16 and 17 columns: neither gave up more than a character more than the other.
    assert lines[4] == "│ a-fairly-long-p… │ an-even-longer-i… │"


def test_render_table_stops_cutting_at_the_header_width(
    display: DisplayService, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A column narrower than its own heading would leave the table unreadable."""
    monkeypatch.setenv("COLUMNS", "10")
    lines = _render(display, _tree_body(), elide=(0, 1))
    # PROJECT floors at 7 and COUNT at 5, so 19 is as narrow as this table goes.
    assert {len(line) for line in lines[1:]} == {19}
    assert _project_cells(lines)[1] == "├home/…"


def test_render_table_keeps_a_divider_spanning_the_cut_widths(
    display: DisplayService, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A divider is drawn from the same widths as the rows, narrowing with them."""
    monkeypatch.setenv("COLUMNS", "30")
    body: list[RowItemOrDivider] = [*_tree_body(), "__div__", _row("└other", 1)]
    lines = _render(display, body, elide=(0,))
    assert {len(line) for line in lines[1:]} == {30}
    assert lines[-3].startswith("├")


def test_render_table_measures_its_own_shared_widths_when_not_given_any(
    display: DisplayService,
) -> None:
    """A single table needs no width list: its shared columns come from its own body."""
    body: list[RowItemOrDivider] = [_row("/srv", 0)]
    assert display.render_table("T:", _table(), body) == display.render_table(
        "T:", _table(), body, display.compute_shared_widths([(_table(), body)])
    )


def test_compute_shared_widths_takes_its_count_from_the_first_spec(
    display: DisplayService,
) -> None:
    """The arithmetic callers used to pass: leading plus the count covers every header."""
    body: list[RowItemOrDivider] = [_row("/srv", 0)]
    assert display.compute_shared_widths([(_table(), body)]) == [len("COUNT")]


def test_compute_shared_widths_sizes_a_column_across_every_table_in_the_group(
    display: DisplayService,
) -> None:
    """Stacked tables share one width so their figures line up vertically."""
    wide = TableSpec(headers=("MODEL", "COUNT"), aligns=("<", ">"), leading=1)
    narrow: list[RowItemOrDivider] = [_row("/srv", 0)]
    broad: list[RowItemOrDivider] = [RowItem(cells=["m", "1234567"], style=Ansi.NONE, prefix_len=0)]
    assert display.compute_shared_widths([(_table(), narrow), (wide, broad)]) == [7]


def test_fit_table_stops_once_the_table_fits(
    display: DisplayService, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("COLUMNS", "200")
    calls: list[int] = []

    def shrink() -> bool:
        calls.append(1)
        return True

    body, shared = display.fit_table(_table(), _tree_body, shrink=shrink)
    assert calls == []
    assert shared == [len("COUNT")]
    assert len(body) == len(_tree_body())


def test_fit_table_gives_up_when_shrinking_reports_nothing_left(
    display: DisplayService, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The table then simply overflows, which beats cutting a column nothing nominated."""
    monkeypatch.setenv("COLUMNS", "10")
    _, shared = display.fit_table(_table(), _tree_body, shrink=lambda: False)
    assert display.table_overflow(_table(), _tree_body(), shared) > 0


def test_fit_table_rebuilds_the_body_until_shrinking_makes_it_fit(
    display: DisplayService, monkeypatch: pytest.MonkeyPatch
) -> None:
    """*build_body* is a callable because a chopped tree's labels are only known once built."""
    monkeypatch.setenv("COLUMNS", "30")
    # Three chops' worth of labels: the first two still overflow a 30-column console.
    widths = [24, 24, 11]
    chops = 0

    def build_body() -> list[RowItemOrDivider]:
        return [_row("x" * widths[chops], 0)]

    def shrink() -> bool:
        nonlocal chops
        chops += 1
        return chops < len(widths)

    body, _shared = display.fit_table(_table(), build_body, shrink=shrink)
    assert chops == 2
    assert body == [_row("x" * 11, 0)]


def test_fit_table_counts_a_companion_table_in_the_shared_widths(
    display: DisplayService,
) -> None:
    """*others* do not shrink, but their content still sets the widths the group shares."""
    wide = TableSpec(headers=("MODEL", "COUNT"), aligns=("<", ">"), leading=1)
    broad: list[RowItemOrDivider] = [RowItem(cells=["m", "1234567"], style=Ansi.NONE, prefix_len=0)]
    _body, shared = display.fit_table(
        _table(), _tree_body, shrink=lambda: False, others=[(wide, broad)]
    )
    assert shared == [7]
