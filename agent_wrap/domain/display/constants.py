# This file has been created with the assistance of an AI tool.
"""Terminal control sequences (ANSI SGR + cursor codes)."""

# Formatting
THOUSAND = 1_000

# Byte-size abbreviation step. Binary, unlike THOUSAND's decimal step, because
# byte counts come from the filesystem.
KIBIBYTE = 1024

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
