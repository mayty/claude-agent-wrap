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
from agent_wrap.domain.display.models import RowItem
from agent_wrap.domain.display.service import DisplayService

if TYPE_CHECKING:
    from agent_wrap.domain.display.models import RowItemOrDivider

HEADERS = ["PROJECT", "COUNT"]
ALIGNS = ["<", ">"]


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


def _render(display: DisplayService, body: list[RowItemOrDivider], **kwargs: object) -> list[str]:
    return display.render_table("T:", HEADERS, ALIGNS, body, 1, [5], **kwargs)  # type: ignore[arg-type]


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
    assert display.table_overflow(HEADERS, _tree_body(), 1, [5]) == 0


def test_table_overflow_reports_the_excess(
    display: DisplayService, monkeypatch: pytest.MonkeyPatch
) -> None:
    """PROJECT measures 24, COUNT 5: 26 + 7 columns of cells plus 3 borders."""
    monkeypatch.setenv("COLUMNS", "30")
    assert display.table_overflow(HEADERS, _tree_body(), 1, [5]) == 6


def test_table_overflow_is_zero_when_the_table_fits(
    display: DisplayService, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("COLUMNS", "200")
    assert display.table_overflow(HEADERS, _tree_body(), 1, [5]) == 0


def test_table_overflow_discounts_what_an_elidable_column_could_give_up(
    display: DisplayService, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    Told REASON may be cut, the overflow the tree has to answer for drops to nothing.

    This is what stops a chop loop flattening a tree that was never the problem: 18 columns
    over, every one of which the prose column can surrender on its own.
    """
    monkeypatch.setenv("COLUMNS", "40")
    headers = ["PROJECT", "REASON"]
    body: list[RowItemOrDivider] = [
        RowItem(
            cells=["├home/me/work/wargaming/", "a fairly long reason string"],
            style=Ansi.NONE,
            prefix_len=1,
        )
    ]
    assert display.table_overflow(headers, body, 1, [27]) == 18
    assert display.table_overflow(headers, body, 1, [27], elide=(1,)) == 0


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
    lines = display.render_table(
        "T:", ["PROJECT", "IMAGE"], ["<", "<"], body, 1, [25], elide=(0, 1)
    )
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
