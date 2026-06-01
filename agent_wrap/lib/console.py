# This file has been created with the assistance of an AI tool.

"""Terminal control sequences (ANSI SGR + cursor codes)."""

from enum import Enum


class Ansi(str, Enum):
    """Terminal control sequences."""

    RESET = "\033[0m"
    BOLD_GREEN = "\033[1;32m"
    BOLD_YELLOW = "\033[1;33m"
    DIM = "\033[90m"

    # Cursor/line control (not SGR — don't append 'm')
    CR = "\r"
    ERASE_LINE = "\033[2K"

    def __str__(self) -> str:
        return self.value
