# This file has been edited with the assistance of an AI tool.
"""Terminal control sequences (ANSI SGR + cursor codes)."""

from enum import StrEnum


class Ansi(StrEnum):
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


# Severity tags. Owned by DisplayService.error/warning rather than by each
# caller, so they cannot drift. Bracketed because colour is the only other severity
# marker and _TextStyler.color strips it off a TTY — piped and captured output keeps
# the tag. Continuation lines are indented by len(prefix), so widths may differ.
ERROR_PREFIX = "[ERROR] "
WARNING_PREFIX = "[WARNING] "

# Formatting
THOUSAND = 1_000

# Byte-size abbreviation step. Binary, unlike THOUSAND's decimal step, because
# byte counts come from the filesystem.
KIBIBYTE = 1024

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
