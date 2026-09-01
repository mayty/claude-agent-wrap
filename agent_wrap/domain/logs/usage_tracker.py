# This file has been created with the assistance of an AI tool.
"""Daily usage tracking for the logs viewer background thread."""

import contextlib
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from agent_wrap.constants import DAY_START_HOURS, GLOBAL_CONFIG_DIR
from agent_wrap.domain.logs.constants import USAGE_JSON_RELPATH
from agent_wrap.lib.atomic import atomic_write_json
from agent_wrap.lib.daytime import get_day

if TYPE_CHECKING:
    from pathlib import Path

    from agent_wrap.domain.pricing.models import Bucket
    from agent_wrap.domain.pricing.service import PricingService
    from agent_wrap.domain.stats.service import StatsService


class UsageTracker:
    """
    Tracks today's LLM usage by incrementally re-scanning changed messages.jsonl files.

    Maintains a per-file bucket contribution and ``(mtime_ns, size)`` fingerprint so
    that ``update_file`` skips files whose metadata hasn't changed — regardless of
    which code path calls it.

    * A changed file is re-scanned and its old contribution is replaced.
    * A deleted file has its contribution removed.
    * Day rollover (per :data:`DAY_START_HOURS`) resets all state.

    All public methods are called exclusively from the ``LogsCache`` poll thread,
    so no internal locking is needed.
    """

    def __init__(self, pricing: PricingService, stats: StatsService) -> None:
        self._pricing = pricing
        self._stats = stats
        self._output_path = GLOBAL_CONFIG_DIR / USAGE_JSON_RELPATH

        # Today's ISO day key (e.g. "2026-07-16") from DAY_START_HOURS.
        self._today_key = self._current_day_key()

        # Per-file bucket contributions and stat fingerprints for today only.
        self._file_buckets: dict[Path, Bucket] = {}
        self._fingerprints: dict[Path, tuple[int, int]] = {}

        # Last payload written to usage.json; None until this process writes one, so
        # the first flush always rewrites — that is what clears a payload left behind
        # by a previous run on an earlier day.
        self._last_output: dict[str, int | str] | None = None

    # ------------------------------------------------------------------
    # Public API (called from LogsCache poll thread)
    # ------------------------------------------------------------------

    def detect_rollover(self) -> bool:
        """Return True when the calendar day (per :data:`DAY_START_HOURS`) has changed."""
        return self._current_day_key() != self._today_key

    def reset(self) -> None:
        """Clear all tracked state (called on day rollover or full rebuild)."""
        self._today_key = self._current_day_key()
        self._file_buckets.clear()
        self._fingerprints.clear()

    def update_file(self, file_path: Path, stat_info: tuple[int, int]) -> None:
        """
        Re-scan *file_path* for today's records if its stat fingerprint changed.

        The file is left alone when its fingerprint is unchanged, or when its mtime
        predates today's day boundary (it then cannot hold today's records).

        The provider name is extracted from the path structure::

            <logs_dir>/<provider_name>/<session_id>/messages.jsonl
        """
        if self._fingerprints.get(file_path) == stat_info:
            return

        # If the file's mtime predates today's day boundary, it can't contain
        # today's records — store the fingerprint and skip I/O entirely.
        mtime_ns = stat_info[0]
        mtime_dt = datetime.fromtimestamp(mtime_ns / 1_000_000_000, tz=UTC)
        if get_day(mtime_dt, DAY_START_HOURS).isoformat() < self._today_key:
            self._fingerprints[file_path] = stat_info
            return

        self._fingerprints[file_path] = stat_info
        provider = file_path.parent.parent.name
        bucket = self._stats.scan_day_file(provider, file_path, self._today_key)
        self._file_buckets[file_path] = bucket

    def remove_file(self, file_path: Path) -> None:
        """Remove a deleted file's tracked contribution and fingerprint."""
        self._file_buckets.pop(file_path, None)
        self._fingerprints.pop(file_path, None)

    def flush(self) -> None:
        """
        Aggregate all tracked file contributions and write ``usage.json``.

        The payload is atomically rewritten whenever it differs from the one this
        process last wrote, or the file is missing; otherwise the file is only
        ``touch``-ed (mtime updated) so consumers can still see it is live. Because
        the first flush of a process always writes, a payload left behind by an
        earlier run — for a day that has since rolled over — is replaced rather than
        touched, which would have kept the statusline reporting the previous day's
        totals under "Today".

        Detects day rollover as a safety net (the caller is expected to handle
        rollover explicitly via :meth:`detect_rollover`, but if a tick straddles
        midnight this reset keeps the output from carrying stale data).
        """
        today = self._current_day_key()
        if today != self._today_key:
            self._today_key = today
            self._file_buckets.clear()
            self._fingerprints.clear()

        total = self._pricing.new_bucket()
        for bucket in self._file_buckets.values():
            total.merge(bucket)

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

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _current_day_key() -> str:
        return get_day(datetime.now(UTC), DAY_START_HOURS).isoformat()
