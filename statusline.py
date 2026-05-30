#!/usr/bin/env python3
# This file has been created with the assistance of an AI tool.
"""Claude Code statusline.

Layout (two rows, right segment flush to the terminal edge):

    {model} ({effort+thinking})            {session_cost}
    {used%} context                        {update_available}

Colors on context %: green <10, yellow 10-19, red >=20 or >200k tokens.
"""
from __future__ import annotations

import fcntl
import json
import os
import re
import shutil
import struct
import subprocess
import sys
import termios
import time
from pathlib import Path

CACHE = Path.home() / ".cache" / "claude-latest-version"
REFRESH_AFTER_SECONDS = 6 * 3600
NPM_PACKAGE = "@anthropic-ai/claude-code"
# Claude renders the statusline inside a padded slot narrower than the raw
# terminal — if we right-align to the true column count, the left side
# overflows and gets truncated with an ellipsis. The slot width is not
# exposed in the input JSON, so reserve a conservative margin.
WIDTH_MARGIN = 6

GREEN = "\033[32m"
YELLOW = "\033[33m"
RED = "\033[31m"
DIM = "\033[2m"
RESET = "\033[0m"

_ANSI_RE = re.compile(r"\033\[[0-9;]*m")


def visible_len(s: str) -> int:
    return len(_ANSI_RE.sub("", s))


def terminal_cols(default: int = 120) -> int:
    # Claude Code does not attach a TTY or export COLUMNS to the statusline
    # process, but /dev/tty refers to the controlling terminal and usually
    # works. Fall back to shutil (which honours COLUMNS), then to a default.
    try:
        with open("/dev/tty") as tty:
            packed = fcntl.ioctl(tty.fileno(), termios.TIOCGWINSZ, b"\0" * 8)
            _, cols, _, _ = struct.unpack("hhhh", packed)
            if cols > 0:
                return cols
    except Exception:
        pass
    cols = shutil.get_terminal_size(fallback=(default, 24)).columns
    return cols if cols > 0 else default


def right_align(left: str, right: str, width: int) -> str:
    if not right:
        return left
    gap = width - visible_len(left) - visible_len(right)
    if gap < 1:
        gap = 1
    return f"{left}{' ' * gap}{right}"


def latest_version(current: str) -> str | None:
    """Return the latest published version if newer than ``current``.

    Refreshes a cache file in the background so the statusline never blocks on
    the network — the value shown reflects the previous refresh.
    """
    try:
        CACHE.parent.mkdir(parents=True, exist_ok=True)
        stale = (
            not CACHE.exists()
            or (time.time() - CACHE.stat().st_mtime) > REFRESH_AFTER_SECONDS
        )
        if stale:
            subprocess.Popen(
                ["sh", "-c", f"npm view {NPM_PACKAGE} version > {CACHE} 2>/dev/null"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
        if CACHE.exists():
            latest = CACHE.read_text().strip()
            if latest and latest != current:
                return latest
    except Exception:
        pass
    return None


def context_segment(data: dict) -> str:
    cw = data.get("context_window") or {}
    used = cw.get("used_percentage")
    exceeds_200k = bool(data.get("exceeds_200k_tokens"))
    if used is None:
        return f"{DIM}context —{RESET}"
    if used >= 20 or exceeds_200k:
        color = RED
    elif used >= 10:
        color = YELLOW
    else:
        color = GREEN
    return f"{color}{used:.0f}% context{RESET}"


def model_segment(data: dict) -> str:
    model = (data.get("model") or {}).get("display_name") or "?"
    effort = (data.get("effort") or {}).get("level")
    thinking = bool((data.get("thinking") or {}).get("enabled"))
    mods: list[str] = []
    if effort:
        mods.append(effort)
    if thinking:
        mods.append("thinking")
    suffix = f" ({'+'.join(mods)})" if mods else ""
    return f"{model}{suffix}"


def cost_segment(data: dict) -> str:
    cost = (data.get("cost") or {}).get("total_cost_usd") or 0.0
    return f"${cost:.2f}"


def update_segment(data: dict) -> str:
    version = data.get("version") or ""
    if not version:
        return ""
    newer = latest_version(version)
    return f"{YELLOW}↑ {newer} available{RESET}" if newer else ""


def main() -> None:
    try:
        data = json.load(sys.stdin)
    except Exception:
        return

    cols = max(20, terminal_cols() - WIDTH_MARGIN)
    line1 = right_align(model_segment(data), cost_segment(data), cols)
    line2 = right_align(context_segment(data), update_segment(data), cols)
    if os.environ.get("STATUSLINE_DEBUG"):
        sys.stderr.write(f"[statusline] cols={cols}\n")
    sys.stdout.write(f"{line1}\n{line2}")


if __name__ == "__main__":
    main()
