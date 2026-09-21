# This file has been created with the assistance of an AI tool.
"""Repetition detection and redacted display for user-entered text."""

#: Characters kept from each end of a masked value.
_MASK_KEEP = 2

#: Shortest core that may show any of its own characters. Below it the mask alone is shown,
#: so a short value never gives away most of itself.
_MASK_MIN_LENGTH = 8

#: Longest run of trailing "=" base64 can produce. A longer run is data, not padding.
_MAX_BASE64_PADDING = 2


def repetition_count(text: str) -> int:
    """
    Return N such that *text* is N copies of its shortest repeating unit; 1 when it is not.

    ``(text + text).find(text, 1)`` is the shortest period of *text*, and that period always
    divides ``len(text)``, so the division is exact and ``text[: len(text) // N] * N == text``
    holds for every input. The empty string has no period to find and is reported as 1.
    """
    if not text:
        return 1
    period = (text + text).find(text, 1)
    return len(text) // period


def mask_value(text: str) -> str:
    """
    Return a redacted form of *text* — ``"ab***yz"``, with base64 padding kept (``"ab***yz=="``).

    A value whose core is too short to redact meaningfully, and one whose trailing ``"="`` run
    is too long to be padding, are both reduced to ``"***"`` rather than partly shown.
    """
    core = text.rstrip("=")
    padding = text[len(core) :]
    if len(core) < _MASK_MIN_LENGTH or len(padding) > _MAX_BASE64_PADDING:
        return "***"
    return f"{core[:_MASK_KEEP]}***{core[-_MASK_KEEP:]}{padding}"
