# This file has been edited with the assistance of an AI tool.
"""Styles and layout constants for the display service."""

from enum import StrEnum
from typing import TYPE_CHECKING

from rich import box

if TYPE_CHECKING:
    from rich.console import JustifyMethod


class Style(StrEnum):
    """
    The palette, as rich style names rather than escape sequences.

    rich resolves these against the console it is printing to, which is what makes one
    name mean "colour on a terminal" and "nothing at all" down a pipe or under NO_COLOR.

    ``DIM`` is ``bright_black`` rather than rich's ``dim``: the two render differently
    (an SGR 2 face versus a colour), and this is the one the tables were built around.
    """

    # Empty sentinel for "no styling" — falsy, so `if style:` guards skip it.
    NONE = ""
    BOLD_GREEN = "bold green"
    BOLD_YELLOW = "bold yellow"
    BOLD_RED = "bold red"
    CYAN = "cyan"
    MAGENTA = "magenta"
    DIM = "bright_black"


# Severity tags. Owned by DisplayService.error/warning rather than by each
# caller, so they cannot drift. Bracketed because colour is the only other severity
# marker and the console drops it off a TTY — piped and captured output keeps
# the tag. Continuation lines are indented by len(prefix), so widths may differ.
ERROR_PREFIX = "[ERROR] "
WARNING_PREFIX = "[WARNING] "

# Formatting
THOUSAND = 1_000

#: Decimal unit suffixes for an abbreviated count, smallest first. Runs to exa because a
#: table of unabbreviated digits is what the abbreviation exists to prevent, and a list
#: that stops short silently produces one ("1024.0G") at the first figure past its end.
COUNT_UNITS = ("K", "M", "G", "T", "P", "E")

# Duration abbreviation steps.
SECONDS_PER_MINUTE = 60
SECONDS_PER_HOUR = 60 * SECONDS_PER_MINUTE
SECONDS_PER_DAY = 24 * SECONDS_PER_HOUR

# Table fitting.

#: Read before the terminal is asked, and honored even when stdout is not a TTY — the one
#: lever a script (or a test) has to state a width the terminal cannot be asked for.
TERM_WIDTH_ENV = "COLUMNS"

#: Assumed width when stdout is a TTY that will not report one. Wide rather than the
#: conventional 80, because these tables have up to nine columns and a too-narrow guess
#: would chop and wrap output that had no need of it.
DEFAULT_TERM_WIDTH = 120

#: The box-drawing set the tables are built from: ``┌┬┐├┼┤└┴┘│─``.
TABLE_BOX = box.SQUARE

#: Cell padding, one space either side, and the per-column overhead that implies. Named
#: because the width arithmetic has to agree with what rich then draws.
TABLE_CELL_PADDING = (0, 1)
TABLE_CELL_OVERHEAD = 2

#: ``TableSpec.aligns`` states each column in ``str.format``'s spelling, which is what a
#: caller reads as "left" and "right"; rich wants the words.
ALIGN_TO_JUSTIFY: dict[str, JustifyMethod] = {"<": "left", ">": "right", "^": "center"}

# Spinner cadence.

#: The catalogue entry a spinner is constructed from before its frames are replaced.
#: Any name would do -- rich needs one to exist, and none of it survives the swap.
DEFAULT_RICH_SPINNER = "dots"

#: How long one full pass through a frame set takes, for a set that names no duration.
SPINNER_CYCLE_SEC = 1.0
MILLISECONDS_PER_SECOND = 1000

#: How often Live redraws. Above the fastest set's frame rate (``pulsing``, 8 frames in
#: half a second), so no set is ever drawn slower than it asked for.
SPINNER_REFRESH_PER_SEC = 20

#: How often the spinner's *message* is recomposed, which is a separate question from how
#: often it is drawn: the message carries a whole-second elapsed count and a status word
#: that only changes when the thing being waited on does.
MESSAGE_UPDATE_INTERVAL_SEC = 0.1

# Spinner glyph sets
SPINNERS: dict[str, tuple[tuple[str, ...], float | None]] = {
    "default": (("⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"), None),
    "braille": (
        ("⡠⠂", "⡤⠀", "⡆⠀", "⠇⠀", "⠓⠀", "⠑⠄", "⠐⢄", "⠀⢤", "⠀⢰", "⠀⠸", "⠀⠚", "⠠⠊"),
        None,
    ),
    "blinking": (("( ●_●)", "( -_-)", "(●_● )", "(-_- )"), None),
    "pulsing": (("  ", "░░", "▒▒", "▓▓", "██", "▓▓", "▒▒", "░░"), 0.5),
    "pump": (
        ("▁█", "▂▇", "▃▆", "▄▅", "▅▄", "▆▃", "▇▂", "█▁", "▇▂", "▆▃", "▅▄", "▄▅", "▃▆", "▂▇"),
        None,
    ),
    "aliens": (
        (
            "🐄      🛸  ",
            " 🐄     🛸  ",
            "  🐄    🛸  ",
            "   🐄   🛸  ",
            "    🐄  🛸  ",
            "     🐄 🛸  ",
            "      🐄🛸  ",
            "        🛸  ",
            "        🛸💨",
            "       🛸   ",
            "      🛸    ",
            "     🛸     ",
            "    🛸      ",
            "   🛸       ",
            "  🛸        ",
            " 🛸         ",
            "🛸          ",
            "            ",
            "            ",
        ),
        2.0,
    ),
}
