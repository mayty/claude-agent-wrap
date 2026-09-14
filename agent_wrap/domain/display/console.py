# This file has been created with the assistance of an AI tool.
"""
The rich consoles the display layer writes through.

Built per call rather than once at import. ``Console`` resolves ``color_system="auto"``
in its constructor, so a console built while ``sys.stdout`` is something else -- a test's
capture buffer, a pipe at import time -- would carry that verdict for the life of the
process and print unstyled to a terminal. Construction is only attribute setup and a few
environment reads, and nothing here is on a hot path.

Neither is handed a ``file``: rich falls back to ``sys.stdout`` / ``sys.stderr`` at write
time, so replacing either stream is enough to redirect the output. Colour then follows the
stream that is actually in place, and rich drops it under ``NO_COLOR`` and on a dumb
terminal without anything here asking.

``markup``, ``highlight`` and ``emoji`` are all off: every string printed through these is
somebody's message or a path, and rich must not read ``[ERROR]`` as a style tag, recolour a
number inside one, or rewrite ``:sunny:`` into a picture.
"""

from rich.console import Console


def out() -> Console:
    return Console(soft_wrap=True, highlight=False, markup=False, emoji=False)


def err() -> Console:
    return Console(stderr=True, soft_wrap=True, highlight=False, markup=False, emoji=False)
