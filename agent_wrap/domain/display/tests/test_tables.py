# This file has been created with the assistance of an AI tool.
"""
Domain-layer tests for table layout: terminal width, column shrinking, and wrapping.

Every assertion goes through the public ``DisplayService`` surface. Stdout is captured
rather than a TTY here, so ``COLUMNS`` is what states a width -- which is exactly the lever
these tables give a script, and why no test needs to pretend to be a terminal.
"""

from typing import TYPE_CHECKING, Literal

import pytest
from rich.cells import cell_len

from agent_wrap.domain.display.constants import DEFAULT_TERM_WIDTH, Style
from agent_wrap.domain.display.models import RowItem, TableSpec

if TYPE_CHECKING:
    from collections.abc import Callable

    from rich.console import RenderableType

    from agent_wrap.domain.display.models import RowItemOrDivider
    from agent_wrap.domain.display.service import DisplayService

#: The SGR sequences a dim title arrives as, spelled out for the same reason test_output.py
#: spells its own out: a test that recomputed them from `Style` could not tell a renamed
#: style from a wrong one.
DIM = "\033[90m"
RESET = "\033[0m"

HEADERS = ("PROJECT", "COUNT")
ALIGNS: tuple[Literal["<", ">", "^"], ...] = ("<", ">")


@pytest.fixture
def shown(
    non_tty_display: DisplayService, capsys: pytest.CaptureFixture[str]
) -> Callable[[RenderableType], list[str]]:
    """
    Return the lines `show` puts on stdout for a renderable.

    Assertions are made against what a terminal actually receives, rather than against a
    console the test built for itself: a second console would have to restate `show`'s
    size and flags, and would then stop answering for them.
    """

    def _shown(renderable: RenderableType) -> list[str]:
        non_tty_display.show(renderable)
        return capsys.readouterr().out.splitlines()

    return _shown


@pytest.fixture(autouse=True)
def _no_inherited_width(monkeypatch: pytest.MonkeyPatch) -> None:
    """Drop any COLUMNS the test runner inherited, so each test states its own width."""
    monkeypatch.delenv("COLUMNS", raising=False)


def _row(label: str, prefix_len: int) -> RowItem:
    return RowItem(cells=[label, "1"], style=Style.NONE, prefix_len=prefix_len)


def _tree_body() -> list[RowItemOrDivider]:
    """Return a body shaped like a project tree, with one wide folded node in the middle."""
    return [
        RowItem(cells=["/", "4"], style=Style.NONE, prefix_len=0),
        _row("├home/me/work/wargaming/", 1),
        _row(" ├wotp", 2),
        _row(" └wotp-be", 2),
        _row("└srv/deploy", 1),
    ]


def _table(elide: tuple[int, ...] = ()) -> TableSpec:
    return TableSpec(headers=HEADERS, aligns=ALIGNS, leading=1, elide=elide)


@pytest.fixture
def rendered(
    non_tty_display: DisplayService, shown: Callable[[RenderableType], list[str]]
) -> Callable[..., list[str]]:
    def _rendered(body: list[RowItemOrDivider], elide: tuple[int, ...] = ()) -> list[str]:
        # COUNT's width is stated rather than measured, so these assertions stay pinned to
        # the header floor they were written against.
        return shown(non_tty_display.render_table("T:", _table(elide), body, [5]))

    return _rendered


def _project_cells(lines: list[str]) -> list[str]:
    """Return the PROJECT cell of every line, borders excluded and padding stripped."""
    # Split from the right: tree glyphs reuse the column separator's own glyph.
    return [line[1:].rsplit("│", 2)[0].strip() for line in lines[4:-1]]


def test_terminal_width_is_unlimited_when_stdout_is_not_a_terminal(
    non_tty_display: DisplayService,
) -> None:
    """Piped output must not depend on whichever terminal happened to launch it."""
    assert non_tty_display.terminal_width() is None


def test_terminal_width_reads_the_columns_override(
    non_tty_display: DisplayService, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("COLUMNS", "72")
    assert non_tty_display.terminal_width() == 72


def test_terminal_width_ignores_a_non_numeric_columns(
    non_tty_display: DisplayService, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("COLUMNS", "wide")
    assert non_tty_display.terminal_width() is None


def test_terminal_width_treats_a_zero_columns_as_unlimited(
    non_tty_display: DisplayService, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A width of zero is no width at all, not a table squeezed to nothing."""
    monkeypatch.setenv("COLUMNS", "0")
    assert non_tty_display.terminal_width() is None


def test_terminal_width_falls_back_when_a_terminal_will_not_say(
    tty_display: DisplayService, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "shutil.get_terminal_size",
        lambda fallback=(80, 24): type("S", (), {"columns": fallback[0]}),
    )
    assert tty_display.terminal_width() == DEFAULT_TERM_WIDTH


def test_table_overflow_is_zero_when_there_is_no_width_to_respect(
    non_tty_display: DisplayService,
) -> None:
    assert non_tty_display.table_overflow(_table(), _tree_body(), [5]) == 0


def test_table_overflow_reports_the_excess(
    non_tty_display: DisplayService, monkeypatch: pytest.MonkeyPatch
) -> None:
    """PROJECT measures 24, COUNT 5: 26 + 7 columns of cells plus 3 borders."""
    monkeypatch.setenv("COLUMNS", "30")
    assert non_tty_display.table_overflow(_table(), _tree_body(), [5]) == 6


def test_table_overflow_is_zero_when_the_table_fits(
    non_tty_display: DisplayService, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("COLUMNS", "200")
    assert non_tty_display.table_overflow(_table(), _tree_body(), [5]) == 0


def test_table_overflow_discounts_what_an_elidable_column_could_give_up(
    non_tty_display: DisplayService, monkeypatch: pytest.MonkeyPatch
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
            style=Style.NONE,
            prefix_len=1,
        )
    ]
    assert non_tty_display.table_overflow(spec, body, [27]) == 18
    assert non_tty_display.table_overflow(spec._replace(elide=(1,)), body, [27]) == 0


def test_render_table_leaves_a_table_with_no_elidable_column_alone(
    rendered: Callable[..., list[str]], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Nothing was nominated as safe to cut, so the table overflows rather than lie."""
    monkeypatch.setenv("COLUMNS", "30")
    lines = rendered(_tree_body())
    assert max(len(line) for line in lines) == 36
    assert not any("…" in line for line in lines)


def test_render_table_ignores_an_elidable_column_when_the_table_already_fits(
    rendered: Callable[..., list[str]], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("COLUMNS", "200")
    assert rendered(_tree_body(), elide=(0,)) == rendered(_tree_body())


def test_render_table_cuts_an_elidable_column_to_the_terminal(
    rendered: Callable[..., list[str]], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("COLUMNS", "30")
    lines = rendered(_tree_body(), elide=(0,))
    assert {len(line) for line in lines[1:]} == {30}
    assert _project_cells(lines)[1] == "├home/me/work/war…"


def test_render_table_never_cuts_a_column_that_was_not_nominated(
    rendered: Callable[..., list[str]], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The whole point of the parameter: a tree column keeps every character it had."""
    monkeypatch.setenv("COLUMNS", "30")
    lines = rendered(_tree_body(), elide=(1,))
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
    non_tty_display: DisplayService,
    shown: Callable[[RenderableType], list[str]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One character at a time off whichever is widest, so neither absorbs it all."""
    monkeypatch.setenv("COLUMNS", "40")
    body: list[RowItemOrDivider] = [
        RowItem(
            cells=["a-fairly-long-project", "an-even-longer-image-name"],
            style=Style.NONE,
            prefix_len=0,
        )
    ]
    spec = TableSpec(headers=("PROJECT", "IMAGE"), aligns=("<", "<"), leading=1, elide=(0, 1))
    lines = shown(non_tty_display.render_table("T:", spec, body, [25]))
    assert {len(line) for line in lines[1:]} == {40}
    # 16 and 17 columns: neither gave up more than a character more than the other.
    assert lines[4] == "│ a-fairly-long-p… │ an-even-longer-i… │"


def test_render_table_stops_cutting_at_the_header_width(
    rendered: Callable[..., list[str]], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A column narrower than its own heading would leave the table unreadable."""
    monkeypatch.setenv("COLUMNS", "10")
    lines = rendered(_tree_body(), elide=(0, 1))
    # PROJECT floors at 7 and COUNT at 5, so 19 is as narrow as this table goes.
    assert {len(line) for line in lines[1:]} == {19}
    assert _project_cells(lines)[1] == "├home/…"


def test_render_table_keeps_a_divider_spanning_the_cut_widths(
    rendered: Callable[..., list[str]], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A divider is drawn from the same widths as the rows, narrowing with them."""
    monkeypatch.setenv("COLUMNS", "30")
    body: list[RowItemOrDivider] = [*_tree_body(), "__div__", _row("└other", 1)]
    lines = rendered(body, elide=(0,))
    assert {len(line) for line in lines[1:]} == {30}
    assert lines[-3].startswith("├")


def test_render_table_measures_its_own_shared_widths_when_not_given_any(
    non_tty_display: DisplayService, shown: Callable[[RenderableType], list[str]]
) -> None:
    """A single table needs no width list: its shared columns come from its own body."""
    body: list[RowItemOrDivider] = [_row("/srv", 0)]
    assert shown(non_tty_display.render_table("T:", _table(), body)) == shown(
        non_tty_display.render_table(
            "T:", _table(), body, non_tty_display.compute_shared_widths([(_table(), body)])
        )
    )


def test_compute_shared_widths_takes_its_count_from_the_first_spec(
    non_tty_display: DisplayService,
) -> None:
    """The arithmetic callers used to pass: leading plus the count covers every header."""
    body: list[RowItemOrDivider] = [_row("/srv", 0)]
    assert non_tty_display.compute_shared_widths([(_table(), body)]) == [len("COUNT")]


def test_compute_shared_widths_sizes_a_column_across_every_table_in_the_group(
    non_tty_display: DisplayService,
) -> None:
    """Stacked tables share one width so their figures line up vertically."""
    wide = TableSpec(headers=("MODEL", "COUNT"), aligns=("<", ">"), leading=1)
    narrow: list[RowItemOrDivider] = [_row("/srv", 0)]
    broad: list[RowItemOrDivider] = [
        RowItem(cells=["m", "1234567"], style=Style.NONE, prefix_len=0)
    ]
    assert non_tty_display.compute_shared_widths([(_table(), narrow), (wide, broad)]) == [7]


def test_fit_table_stops_once_the_table_fits(
    non_tty_display: DisplayService, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("COLUMNS", "200")
    calls: list[int] = []

    def shrink() -> bool:
        calls.append(1)
        return True

    body, shared = non_tty_display.fit_table(_table(), _tree_body, shrink=shrink)
    assert calls == []
    assert shared == [len("COUNT")]
    assert len(body) == len(_tree_body())


def test_fit_table_gives_up_when_shrinking_reports_nothing_left(
    non_tty_display: DisplayService, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The table then simply overflows, which beats cutting a column nothing nominated."""
    monkeypatch.setenv("COLUMNS", "10")
    _, shared = non_tty_display.fit_table(_table(), _tree_body, shrink=lambda: False)
    assert non_tty_display.table_overflow(_table(), _tree_body(), shared) > 0


def test_fit_table_rebuilds_the_body_until_shrinking_makes_it_fit(
    non_tty_display: DisplayService, monkeypatch: pytest.MonkeyPatch
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

    body, _shared = non_tty_display.fit_table(_table(), build_body, shrink=shrink)
    assert chops == 2
    assert body == [_row("x" * 11, 0)]


def test_fit_table_counts_a_companion_table_in_the_shared_widths(
    non_tty_display: DisplayService,
) -> None:
    """*others* do not shrink, but their content still sets the widths the group shares."""
    wide = TableSpec(headers=("MODEL", "COUNT"), aligns=("<", ">"), leading=1)
    broad: list[RowItemOrDivider] = [
        RowItem(cells=["m", "1234567"], style=Style.NONE, prefix_len=0)
    ]
    _body, shared = non_tty_display.fit_table(
        _table(), _tree_body, shrink=lambda: False, others=[(wide, broad)]
    )
    assert shared == [7]


@pytest.mark.parametrize(
    ("label", "display_width"),
    [
        ("日本語のプロジェクト", 20),
        ("emoji🎉here", 11),
        ("plain-ascii", 11),
    ],
)
def test_render_table_measures_a_cell_by_display_width(
    non_tty_display: DisplayService,
    shown: Callable[[RenderableType], list[str]],
    label: str,
    display_width: int,
) -> None:
    """
    Columns are sized in terminal cells, not codepoints.

    A CJK segment or an emoji occupies two cells per codepoint, so measuring with ``len``
    under-sizes the column and every border below it slides left.
    """
    lines = shown(non_tty_display.render_table("T:", _table(), [_row(label, 0)]))
    borders_and_rows = lines[1:]
    assert len({cell_len(line) for line in borders_and_rows}) == 1
    assert cell_len(borders_and_rows[0]) == display_width + len("COUNT") + 7


def test_render_table_is_not_cropped_by_a_dumb_terminal(
    non_tty_display: DisplayService,
    shown: Callable[[RenderableType], list[str]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    A table is sized here, and `show`'s console must not re-negotiate it.

    rich answers 80x25 for a dumb terminal unless an explicit height accompanies the
    width, which silently cropped every column past that.
    """
    monkeypatch.setenv("TERM", "dumb")
    monkeypatch.setenv("FORCE_COLOR", "1")
    wide = _row("x" * 100, 0)

    lines = shown(non_tty_display.render_table("T:", _table(), [wide]))

    assert "x" * 100 in lines[-2]


def test_a_title_is_one_dim_span_on_a_tty(
    tty_display: DisplayService, capsys: pytest.CaptureFixture[str]
) -> None:
    """
    A title is dim from end to end, digits included.

    rich recolours numbers and brackets in a plain string unless the console says
    otherwise. Spelled as an exact match rather than a substring, because the failure this
    guards against is extra escape codes in the middle of the line, not missing ones.
    """
    tty_display.show(tty_display.render_table("Sidecars (3):", _table(), [_row("/srv", 0)]))
    assert capsys.readouterr().out.splitlines()[0] == f"{DIM}Sidecars (3):{RESET}"


def test_a_title_keeps_square_brackets(
    non_tty_display: DisplayService, shown: Callable[[RenderableType], list[str]]
) -> None:
    """Bracketed text is a title, never rich markup: a title naming one would vanish."""
    lines = shown(non_tty_display.render_table("Stale [images]:", _table(), [_row("/srv", 0)]))
    assert lines[0] == "Stale [images]:"


def test_a_title_survives_a_zero_columns(
    rendered: Callable[..., list[str]], monkeypatch: pytest.MonkeyPatch
) -> None:
    """`show` states its own size, so a console rich would have built zero-wide cannot eat it."""
    monkeypatch.setenv("COLUMNS", "0")
    assert rendered([_row("/srv", 0)])[0] == "T:"
