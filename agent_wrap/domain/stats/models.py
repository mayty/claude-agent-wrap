# This file has been created with the assistance of an AI tool.
"""Data models for the stats domain."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, NamedTuple, TypedDict

if TYPE_CHECKING:
    import re
    from collections.abc import Callable
    from datetime import datetime
    from pathlib import Path

    from agent_wrap.domain.pricing.models import Bucket, TokenUsage


@dataclass
class UsageArgs:
    """The resolved selection window and filters for one ``agent stats`` invocation."""

    from_iso: str | None = None
    until_iso: str | None = None
    verbose: bool = False
    pattern: re.Pattern[str] | None = None


class Group:
    """Per-transient-project accumulator across one or more physical paths."""

    __slots__ = ("exists", "last_ts", "name", "root", "sessions", "total", "transient")

    def __init__(
        self,
        root: Path,
        name: str,
        *,
        transient: bool,
        new_bucket: Callable[[], Bucket],
    ) -> None:
        self.root = root
        self.name = name
        self.transient = transient
        self.total = new_bucket()
        self.sessions = 0
        self.last_ts: datetime | None = None
        self.exists = False


# A unit of parallel work. *dir_index* tags which logs dir the file belongs to
# so the parent can fold results per dir (a project's sessions vs. an orphaned
# dir's) without the worker knowing.
class WorkUnit(NamedTuple):
    dir_index: int
    provider_name: str
    messages_file: Path


# A scan cache maps each logs dir to its folded
# (sessions, last_ts, by_day, by_source) result, so the
# aggregation pass can look results up instead of re-scanning.
class DirResult(NamedTuple):
    sessions: int
    last_ts: datetime | None
    by_day: dict[str, dict[str, Bucket]]
    by_source: dict[str, dict[str, Bucket]]


ScanCache = dict["Path", DirResult]


# A raw record returned by scan workers. Workers produce these without any
# pricing-domain knowledge; the master normalizes model names and folds into
# Buckets.
#
# *ts* is the record's raw UTC timestamp, kept alongside *day_key* because the
# latter has already had ``DAY_START_HOURS`` applied — the usage archive needs
# the un-offset instant so ``agent stats`` can re-bucket it at read time with
# whatever ``AGENT_DAY_START_UTC`` is in force then.
class RawRecord(NamedTuple):
    day_key: str
    display_model: str
    usage: TokenUsage
    source: str
    unrecorded: bool
    ts: datetime | None


# A raw file result from a pool worker.
class RawFileResult(NamedTuple):
    had_record: bool
    last_ts: datetime | None
    records: list[RawRecord]


# Return type for accumulate_record: extracted fields from one log record.
class AccumulatedRecord(NamedTuple):
    accumulated: bool
    ts: datetime | None
    day_key: str | None
    display_model: str | None
    usage: TokenUsage | None
    source: str
    unrecorded: bool


# Return type for scan_project: a single project's scan result plus existence flag.
class ScanProjectResult(NamedTuple):
    sessions: int
    last_ts: datetime | None
    by_day: dict[str, dict[str, Bucket]]
    by_source: dict[str, dict[str, Bucket]]
    exists: bool


class _ProjectRowBase(TypedDict):
    """Fields common to project rows and model display rows."""

    path: Path
    exists: bool
    sessions: int
    last_ts: datetime | None
    total: Bucket
    cost: float | None


class ProjectRow(_ProjectRowBase, total=False):
    """
    A project row for rendering the project tree.

    ``name`` and ``transient`` are optional — model display rows (produced by
    ``_model_display_rows``) omit them, and the tree renderer only accesses
    ``name`` when ``transient`` is truthy.
    """

    name: str
    transient: bool


class OrphanedResult(TypedDict):
    """Aggregated result for orphaned sessions."""

    sessions: int
    last_ts: datetime | None
    total: Bucket


class ArchiveLeaf(TypedDict):
    """
    One archived ``(date, hour, model, source)`` cell's token counts.

    Field names are descriptive rather than mirroring ``Bucket``'s internal
    ``in_``/``cw_5m`` slots — this is a persisted on-disk format, not an
    in-memory struct. Cost is deliberately absent: pricing is applied fresh on
    every read so archived spend tracks later pricing-table changes.
    """

    msgs: int
    input_tokens: int
    output_tokens: int
    cache_write_5m: int
    cache_write_1h: int
    cache_read: int
    unrecorded: int


# The usage archive: date -> hour -> "provider/model" -> source -> leaf.
#
# Dates are raw UTC calendar days (``YYYY-MM-DD``) and hours are zero-padded UTC
# hours (``"00"``-``"23"``), NOT stats-bucketed days — re-bucketing via
# ``get_day``/``DAY_START_HOURS`` happens at read time. Records with no
# timestamp use ``"?"`` for both, matching ``day_in_range``'s synthetic key.
# Hours are a dict rather than a list because most hours in a day are empty.
ArchiveDoc = dict[str, dict[str, dict[str, dict[str, ArchiveLeaf]]]]


# Archived usage materialized into priceable buckets, before it is merged into
# the shared stats totals. *last_ts* is the newest in-window archived hour.
class ArchivedBuckets(NamedTuple):
    by_day: dict[str, dict[str, Bucket]]
    by_source: dict[str, dict[str, Bucket]]
    last_ts: datetime | None


# Return type for archive_and_delete_orphaned.
#
# *removed*/*freed_bytes* count only dirs actually deleted, so they may fall
# short of the pre-confirmation estimate when a ``rmtree`` failed.
# *finalized* is False when promoting the staging file over the real archive
# failed, meaning the caller must tell the user to move it by hand.
class CleanupResult(NamedTuple):
    removed: int
    freed_bytes: int
    archive_path: Path
    staging_path: Path
    finalized: bool


# Return type for aggregate_projects: the four render inputs rolled up across all projects.
class AggregateResult(NamedTuple):
    rows: list[ProjectRow]
    totals_by_model: dict[str, Bucket]
    totals_by_day_by_model: dict[str, dict[str, Bucket]]
    totals_by_source: dict[str, dict[str, Bucket]]


# Return type for resolve_group: a project path resolved to its transient group.
class GroupResult(NamedTuple):
    group_root: Path
    display_name: str
    is_transient: bool


class CleanupScope(NamedTuple):
    """What a cleanup would remove, surveyed before anything is deleted."""

    orphaned_dirs: list[Path]
    stale_paths: list[Path]
    freed_estimate: int

    @property
    def is_empty(self) -> bool:
        """Whether there is nothing to clean up."""
        return not self.orphaned_dirs and not self.stale_paths


class CleanupOutcome(NamedTuple):
    """
    What a cleanup actually did.

    *removed_paths* is empty when the archive did not finalize — the registry is
    deliberately left alone in that case (see ``StatsService.run_cleanup``).
    """

    result: CleanupResult
    removed_paths: list[Path]


class StatsReport(NamedTuple):
    """
    Everything ``agent stats`` renders for one selection window.

    ``rows`` holds only projects that contributed sessions; ``orphaned`` is the merged
    live + archived ``<orphaned>`` row, or None when the pattern suppresses it. The
    totals already include the orphaned spend, so the tables agree with each other.
    ``unrecorded`` counts successful requests whose usage was never logged, which the
    caller footnotes because those requests contribute $0 to the costs above.
    """

    rows: list[ProjectRow]
    totals_by_model: dict[str, Bucket]
    totals_by_day_by_model: dict[str, dict[str, Bucket]]
    totals_by_source: dict[str, dict[str, Bucket]]
    orphaned: OrphanedResult | None
    unrecorded: int


@dataclass(frozen=True)
class WindowError:
    """
    A rejected ``--from``/``--until``/``--days`` combination, with the reason to print.

    Deliberately not a NamedTuple: it shares a return union with the resolved
    ``(from_iso, until_iso)`` pair, and a 1-tuple there would be structurally
    unpackable — the type checker could not tell the failure from the success.
    """

    message: str
