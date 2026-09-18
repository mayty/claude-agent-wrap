# This file has been edited with the assistance of an AI tool.
"""Daily usage tracking for the logs viewer background thread."""

import contextlib
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from agent_wrap.constants import DAY_START_HOURS, GLOBAL_CONFIG_DIR
from agent_wrap.domain.logs.constants import USAGE_JSON_RELPATH
from agent_wrap.lib.atomic import atomic_write_json
from agent_wrap.lib.daytime import get_day

if TYPE_CHECKING:
    from agent_wrap.domain.stats.service import StatsService


class UsageTracker:
    """
    Maintains ``usage.json`` — today's LLM usage, for the statusline.

    One aggregate over the request index per flush, and nothing else. There is no
    per-file state here any more: no bucket per file, no ``(mtime_ns, size)``
    fingerprint, no rescan of a session's whole ``messages.jsonl`` on every append.
    That scheme was quadratic in the worst place possible — a session's log file is
    re-read in full each time a request is appended to it, so a 68 MB session cost
    ~2.3 GB of parsing across its life to keep a 130-byte file up to date. The index
    makes the cost proportional to the *day*: a few dozen usage cells, summed in SQL.

    What is left is the day boundary, which is genuinely stateful — ``usage.json`` says
    "today", so the tracker has to notice when today changes.

    All public methods are called exclusively from the watcher's single consumer
    thread, via ``LogsCache``, so no internal locking is needed.
    """

    def __init__(self, stats: StatsService) -> None:
        self._stats = stats
        self._output_path = GLOBAL_CONFIG_DIR / USAGE_JSON_RELPATH

        # Today's ISO day key (e.g. "2026-07-16") from DAY_START_HOURS.
        self._today_key = self._current_day_key()

        # Last payload written to usage.json; None until this process writes one, so
        # the first flush always rewrites — that is what clears a payload left behind
        # by a previous run on an earlier day.
        self._last_output: dict[str, int | str] | None = None

    def detect_rollover(self) -> bool:
        """Return True when the calendar day (per :data:`DAY_START_HOURS`) has changed."""
        return self._current_day_key() != self._today_key

    def reset(self) -> None:
        """Adopt the current day as today's, so the next flush reports that day."""
        self._today_key = self._current_day_key()

    def flush(self) -> None:
        """
        Aggregate today's usage from the index and write ``usage.json``.

        The payload is atomically rewritten whenever it differs from the one this
        process last wrote, or the file is missing; otherwise the file is only
        ``touch``-ed (mtime updated) so consumers can still see it is live. Because
        the first flush of a process always writes, a payload left behind by an
        earlier run — for a day that has since rolled over — is replaced rather than
        touched, which would have kept the statusline reporting the previous day's
        totals under "Today".

        Detects day rollover as a safety net (the caller is expected to handle
        rollover explicitly via :meth:`detect_rollover`, but if a pass straddles
        midnight this reset keeps the output from carrying stale data).
        """
        if self.detect_rollover():
            self.reset()

        total = self._stats.day_totals(self._today_key)
        cost_str = f"${total.cost:.2f}" if not total.cost_unknown else "?"

        output: dict[str, int | str] = {
            "in": total.in_,
            "out": total.out,
            "cache": total.cr,
            "cache_creation": total.cw,
            "cost": cost_str,
            "requests": total.msgs,
        }

        if output != self._last_output or not self._output_path.exists():
            atomic_write_json(self._output_path, output)
            self._last_output = output
        else:
            with contextlib.suppress(OSError):
                self._output_path.touch()

    @staticmethod
    def _current_day_key() -> str:
        return get_day(datetime.now(UTC), DAY_START_HOURS).isoformat()
